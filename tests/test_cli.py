"""CLI prompt behaviour — no traceback when stdin is not a terminal.

The agent/CI/pipe case must produce a usable message and exit(1), never an
EOFError traceback, and the --replace / --yes flags must skip the questions
that can be skipped.
"""
from __future__ import annotations

import argparse
import logging

import pytest

from fartlek import cli


def _no_stdin(monkeypatch):
    def boom(*_a, **_k):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(cli.getpass, "getpass", boom)


def test_ask_reports_missing_terminal(monkeypatch, capsys):
    _no_stdin(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli._ask("Question? ")
    assert exc.value.code == 1
    assert "not a terminal" in capsys.readouterr().err


def test_ask_reports_interrupt(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(SystemExit) as exc:
        cli._ask("Question? ")
    assert exc.value.code == 1
    assert "Aborted" in capsys.readouterr().err


def test_auth_without_terminal_does_not_traceback(monkeypatch, tmp_path, capsys):
    token_file = tmp_path / "tokens" / "garmin_tokens.json"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "default_tokenstore", lambda: token_file.parent)
    _no_stdin(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_auth(argparse.Namespace(replace=False))

    assert exc.value.code == 1
    assert "not a terminal" in capsys.readouterr().err
    assert token_file.exists(), "a failed prompt must not delete existing tokens"


def test_auth_replace_skips_confirmation_then_asks_credentials(monkeypatch, tmp_path, capsys):
    """--replace answers the 'replace existing tokens?' question, so the run
    reaches the credential prompt (which still needs a real terminal)."""
    token_file = tmp_path / "tokens" / "garmin_tokens.json"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "default_tokenstore", lambda: token_file.parent)
    _no_stdin(monkeypatch)

    with pytest.raises(SystemExit):
        cli.cmd_auth(argparse.Namespace(replace=True))

    assert "--replace" in capsys.readouterr().out
    assert not token_file.exists(), "--replace removes the stale token file"


def test_reset_yes_skips_confirmation(monkeypatch, tmp_path, capsys):
    home = tmp_path / "fartlek"
    (home / "account").mkdir(parents=True)
    monkeypatch.setattr(cli, "fartlek_home", lambda: home)
    _no_stdin(monkeypatch)

    assert cli.cmd_reset(argparse.Namespace(yes=True)) == 0
    assert not home.exists()
    assert "removed" in capsys.readouterr().out


def test_reset_without_terminal_keeps_data(monkeypatch, tmp_path, capsys):
    home = tmp_path / "fartlek"
    (home / "account").mkdir(parents=True)
    monkeypatch.setattr(cli, "fartlek_home", lambda: home)
    _no_stdin(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_reset(argparse.Namespace(yes=False))

    assert exc.value.code == 1
    assert home.exists(), "a failed prompt must never wipe the store"
    assert "not a terminal" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv, attr",
    [(["auth", "--replace"], "replace"), (["reset", "--yes"], "yes"), (["reset", "-y"], "yes")],
)
def test_flags_are_wired(monkeypatch, argv, attr):
    monkeypatch.setattr("sys.argv", ["fartlek", *argv])
    parser_args = {}

    def capture(args):
        parser_args.update(vars(args))
        return 0

    monkeypatch.setattr(cli, "cmd_auth", capture)
    monkeypatch.setattr(cli, "cmd_reset", capture)
    with pytest.raises(SystemExit):
        cli.main()
    assert parser_args[attr] is True


# --- login output curation (first-run UX) -----------------------------------


def test_login_progress_translates_strategy_warnings(capsys):
    """A first-time user must not read `mobile+cffi returned 429` and conclude
    the login crashed while it is still trying other methods."""
    logger = logging.getLogger("garminconnect.client")
    with cli._curated_login_output():
        logger.warning("%s returned 429: %s", "mobile+cffi", "IP rate limited by Garmin")
        logger.warning("%s failed: %s", "widget+cffi", "boom")
    captured = capsys.readouterr()
    assert captured.err == "", "progress is not an error; it must share stdout with the prompts"
    out = captured.out
    assert "mobile+cffi" not in out and "429" not in out
    assert "sign-in method 1 rate-limited by Garmin — trying another" in out
    assert "sign-in method 2 refused — trying another" in out


def test_curated_login_output_restores_the_logger():
    logger = logging.getLogger("garminconnect")
    before = (logger.propagate, logger.level, len(logger.handlers))
    with cli._curated_login_output():
        assert logger.propagate is False
    assert (logger.propagate, logger.level, len(logger.handlers)) == before


def test_login_progress_ignores_unrelated_records(capsys):
    with cli._curated_login_output():
        logging.getLogger("garminconnect.client").warning("some unrelated notice")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


# --- `fartlek sync --since` (backfill floor date) ----------------------------


def test_span_days_covers_the_floor_date_inclusively():
    from datetime import date as _date

    # 2026-08-18 back to 2026-08-18 is one day, not zero: every engine backfill
    # counts a depth, and the shortest-window convention must still reach it.
    assert cli._span_days("2026-08-18", today=_date(2026, 8, 18)) == 1
    assert cli._span_days("2026-08-11", today=_date(2026, 8, 18)) == 8
    assert cli._span_days("2025-11-14", today=_date(2026, 8, 18)) == 278
    assert cli._span_days(None) is None


def test_span_days_rejects_a_bad_or_future_date():
    from datetime import date as _date

    with pytest.raises(SystemExit):
        cli._span_days("14/11/2025")
    with pytest.raises(SystemExit):
        cli._span_days("2026-08-19", today=_date(2026, 8, 18))


class _RecordingEngine:
    def __init__(self, *_a, **_k):
        self.calls: list[tuple] = []

    def _record(self, name, **kw):
        self.calls.append((name, kw))
        return {
            "calls": 0, "activities": 0, "laps": 0, "remaining": 0,
            "linked": 0, "filled": 0, "nights": 0,
        }

    def tier0(self):
        return self._record("tier0")

    def tier1(self, history_days=None):
        return self._record("tier1", history_days=history_days)

    def backfill_splits(self, days=None, **_k):
        return self._record("splits", days=days)

    def backfill_gear(self, days=None, **_k):
        return self._record("gear", days=days)

    def tier2(self, backfill_days=0):
        return self._record("tier2", backfill_days=backfill_days)

    def backfill_hrv(self, days=None, **_k):
        return self._record("hrv", days=days)

    def backfill_daily_summary(self, days=None, **_k):
        return self._record("daily_summary", days=days)

    def _today(self):
        return "2026-08-18"


class _FakeStore:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get_pmc(self, **_k):
        return []


def _patch_sync(monkeypatch, engines):
    """Wire cmd_sync to a fake Garmin client, store and engine."""
    import fartlek.health.adapters.garmin_connect as gc
    import fartlek.store as store_mod
    import fartlek.sync.engine as engine_mod

    class _Client:
        display_name = "tester"

    class _Adapter:
        def __init__(self, *_a, **_k):
            pass

        def connect_sync(self):
            return _Client()

        def fetch_sync(self, *_a, **_k):
            return None

    def _make_engine(*a, **k):
        eng = _RecordingEngine(*a, **k)
        engines.append(eng)
        return eng

    monkeypatch.setattr(gc, "GarminConnectAdapter", _Adapter)
    monkeypatch.setattr(store_mod, "Store", _FakeStore)
    monkeypatch.setattr(engine_mod, "SyncEngine", _make_engine)


def test_sync_since_deepens_every_backfill_step(monkeypatch, tmp_path):
    """--since is a floor for the WHOLE pass, not just the nightly tier: the
    depth-only defaults (splits at 120d, tier2 skipped without --nights) are
    exactly what left a widened history with no laps and no old nights."""
    monkeypatch.setenv("FARTLEK_HOME", str(tmp_path))
    engines: list[_RecordingEngine] = []
    _patch_sync(monkeypatch, engines)
    monkeypatch.setattr(cli, "_span_days", lambda since, today=None: 278 if since else None)

    assert cli.cmd_sync(argparse.Namespace(nights=0, since="2025-11-14")) == 0
    got = dict(engines[0].calls)
    assert got["tier1"]["history_days"] == 278
    assert got["splits"]["days"] == 278
    assert got["gear"]["days"] == 278
    assert got["tier2"]["backfill_days"] == 278
    assert got["hrv"]["days"] == 278
    assert got["daily_summary"]["days"] == 278


def test_sync_without_since_keeps_the_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("FARTLEK_HOME", str(tmp_path))
    engines: list[_RecordingEngine] = []
    _patch_sync(monkeypatch, engines)

    assert cli.cmd_sync(argparse.Namespace(nights=0, since=None)) == 0
    names = [name for name, _ in engines[0].calls]
    got = dict(engines[0].calls)
    assert "tier2" not in names and "hrv" not in names
    assert got["tier1"]["history_days"] is None
    assert got["gear"]["days"] is None
    from fartlek.sync.engine import SPLITS_HISTORY_DAYS

    assert got["splits"]["days"] == SPLITS_HISTORY_DAYS


def test_sync_shallow_since_never_shrinks_the_splits_window(monkeypatch, tmp_path):
    monkeypatch.setenv("FARTLEK_HOME", str(tmp_path))
    engines: list[_RecordingEngine] = []
    _patch_sync(monkeypatch, engines)
    monkeypatch.setattr(cli, "_span_days", lambda since, today=None: 10 if since else None)

    assert cli.cmd_sync(argparse.Namespace(nights=0, since="2026-08-09")) == 0
    from fartlek.sync.engine import SPLITS_HISTORY_DAYS

    assert dict(engines[0].calls)["splits"]["days"] == SPLITS_HISTORY_DAYS
