"""CLI prompt behaviour — no traceback when stdin is not a terminal.

The agent/CI/pipe case must produce a usable message and exit(1), never an
EOFError traceback, and the --replace / --yes flags must skip the questions
that can be skipped.
"""
from __future__ import annotations

import argparse

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
