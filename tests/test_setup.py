"""garmin_setup + first-run detection — hermetic (DESIGN §2.4).

No network, no terminal is ever really spawned: the platform bridge is
monkeypatched so the tests assert *what would be launched* and, just as
importantly, that nothing is launched when it should not be.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fartlek.health.exceptions import GarminAuthError
from fartlek.mcp_server import setup_state
from fartlek.mcp_server.tools import setup_tool

ROUTING = "Garmin coaching server. Routing: …"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty FARTLEK_HOME — a machine that has never been set up."""
    monkeypatch.setenv("FARTLEK_HOME", str(tmp_path))
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    return tmp_path


def _write_tokens(home: Path) -> Path:
    token_file = home / "tokens" / setup_state.TOKEN_FILENAME
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps({"di_token": "x"}))
    return token_file


def _write_store(home: Path, account: str = "acct-1", size: int = 2048) -> None:
    (home / account).mkdir(parents=True, exist_ok=True)
    (home / account / "store.db").write_bytes(b"\0" * size)


class FakeContext:
    def __init__(self, store=None, banner: str | None = None, auth_fails: bool = False):
        self.store = store
        self._banner = banner
        self._auth_fails = auth_fails
        self.ready_calls = 0

    async def ensure_ready(self) -> None:
        self.ready_calls += 1
        if self._auth_fails:
            raise GarminAuthError("dead session")

    def banner(self) -> str | None:
        return self._banner


class FakeStore:
    def __init__(self, profile: dict[str, str] | None = None):
        self._profile = profile or {}

    def get_profile(self) -> dict[str, str]:
        return dict(self._profile)


# --- probe ------------------------------------------------------------------


def test_probe_empty_home_is_not_configured(home):
    state = setup_state.probe()
    assert not state.configured
    assert not state.has_tokens
    assert state.accounts == ()
    assert state.store_bytes == 0


def test_probe_missing_home_does_not_raise(tmp_path, monkeypatch):
    """A brand-new machine has no ~/.fartlek at all — the probe runs on every
    server start, so it must never be the thing that breaks the handshake."""
    monkeypatch.setenv("FARTLEK_HOME", str(tmp_path / "nope"))
    assert setup_state.probe().configured is False


def test_probe_finds_tokens_and_stores(home):
    _write_tokens(home)
    _write_store(home, "acct-1", 1024)
    _write_store(home, "acct-2", 3072)
    state = setup_state.probe()
    assert state.configured and state.has_store
    assert state.accounts == ("acct-1", "acct-2")
    assert state.store_bytes == 4096


def test_probe_honours_garmintokens_override(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "other-tokens"
    elsewhere.mkdir()
    monkeypatch.setenv("GARMINTOKENS", str(elsewhere))
    assert not setup_state.probe().has_tokens
    (elsewhere / setup_state.TOKEN_FILENAME).write_text("{}")
    assert setup_state.probe().has_tokens


def test_tokens_alone_configure_the_server(home):
    """The store is not part of the gate: it fills itself on the first tool
    call, so credentials are the only thing the user must supply."""
    _write_tokens(home)
    state = setup_state.probe()
    assert state.configured and not state.has_store


# --- instructions -----------------------------------------------------------


def test_instructions_prefix_setup_banner_when_unconfigured(home):
    text = setup_state.instructions(ROUTING)
    assert text.startswith(setup_state.SETUP_BANNER)
    assert "garmin_setup()" in text
    assert text.endswith(ROUTING)


def test_instructions_are_bare_routing_when_configured(home):
    _write_tokens(home)
    assert setup_state.instructions(ROUTING) == ROUTING


def test_instructions_accept_an_explicit_state(home):
    """Callers can pass state so behaviour never depends on the test machine."""
    configured = setup_state.SetupState(
        token_file=Path("/x"), has_tokens=True, home=Path("/x"), accounts=(), store_bytes=0
    )
    assert setup_state.instructions(ROUTING, configured) == ROUTING


# --- the login bridge -------------------------------------------------------


def test_login_argv_runs_this_interpreter(home):
    import sys

    argv = setup_state.login_argv()
    assert argv[0] == sys.executable
    assert argv[1:] == ["-m", "fartlek.cli", "auth"]
    assert setup_state.login_argv(replace=True)[-1] == "--replace"


def test_login_command_reexports_the_home_override(home):
    """A new terminal does not inherit the server's env; authenticating into
    the wrong home is worse than failing to authenticate."""
    command = setup_state.login_command()
    assert f"export FARTLEK_HOME={str(home)!r}".replace("'", "'") in command or str(home) in command
    assert "fartlek.cli" in command and command.index("export FARTLEK_HOME") < command.index("fartlek.cli")


def test_login_command_omits_unset_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("FARTLEK_HOME", raising=False)
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    assert "export" not in setup_state.login_command()


def test_open_login_terminal_reports_unsupported_platform(monkeypatch):
    monkeypatch.setattr(setup_state.platform, "system", lambda: "Plan9")
    assert "Plan9" in (setup_state.open_login_terminal("echo hi") or "")


def test_open_login_terminal_never_raises(monkeypatch):
    monkeypatch.setattr(setup_state.platform, "system", lambda: "Darwin")

    def boom(*a, **k):
        raise OSError("no osascript")

    monkeypatch.setattr(setup_state.subprocess, "run", boom)
    failure = setup_state.open_login_terminal("echo hi")
    assert failure and "OSError" in failure


def test_open_login_terminal_darwin_passes_the_command_to_osascript(monkeypatch):
    monkeypatch.setattr(setup_state.platform, "system", lambda: "Darwin")
    seen: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return None

    monkeypatch.setattr(setup_state.subprocess, "run", fake_run)
    assert setup_state.open_login_terminal("fartlek auth") is None
    assert seen["argv"][0] == "osascript"
    assert "fartlek auth" in seen["argv"][-1]


# --- the tool ---------------------------------------------------------------


async def test_unconfigured_reports_without_side_effect(home, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("must not spawn a terminal without launch_login=True")

    monkeypatch.setattr(setup_state, "open_login_terminal", forbidden)
    out = await setup_tool.run(FakeContext())
    assert "Not configured" in out
    assert "garmin_setup(launch_login=True)" in out


async def test_unconfigured_never_asks_for_credentials(home):
    out = await setup_tool.run(FakeContext())
    lowered = out.lower()
    assert "do not ask for them here" in lowered
    # The tool must never invite the model to collect secrets in conversation.
    assert "provide your password" not in lowered
    assert "enter your password" not in lowered


async def test_launch_login_spawns_and_does_not_claim_success(home, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        setup_state, "open_login_terminal", lambda cmd=None: calls.append(cmd) or None
    )
    out = await setup_tool.run(FakeContext(), launch_login=True)
    assert len(calls) == 1 and "fartlek.cli" in calls[0]
    assert "terminal window is now open" in out.lower()
    # It cannot observe the login, so it must not report it as done.
    assert "does not wait" in out
    assert "Next: garmin_setup()" in out


async def test_launch_login_degrades_to_a_command(home, monkeypatch):
    monkeypatch.setattr(setup_state, "open_login_terminal", lambda cmd=None: "no terminal here")
    out = await setup_tool.run(FakeContext(), launch_login=True)
    assert "no terminal here" in out
    assert "fartlek.cli" in out  # the copy-pasteable fallback


async def test_dead_session_says_reauth_not_setup(home):
    _write_tokens(home)
    out = await setup_tool.run(FakeContext(auth_fails=True))
    assert "session is dead" in out
    assert "--replace" in out


async def test_ready_reports_store_and_profile_gaps(home):
    _write_tokens(home)
    _write_store(home, "acct-1", 4096)
    ctx = FakeContext(store=FakeStore({"goal_race_date": "2026-09-05"}))
    out = await setup_tool.run(ctx)
    assert "Ready." in out
    assert "1 account" in out
    assert "goal race date" not in out  # already set
    assert "the event" in out and "training phase" in out
    assert "garmin_set_profile" in out


async def test_ready_with_full_profile_is_terminal(home):
    _write_tokens(home)
    _write_store(home)
    profile = {
        "goal_race_date": "2026-09-05",
        "goal_distance": "24h",
        "phase": "build",
        "availability_days": "5",
    }
    out = await setup_tool.run(FakeContext(store=FakeStore(profile)))
    assert "Setup is complete" in out
    assert "Next: garmin_brief()" in out


async def test_cold_store_is_disclosed_as_provisional(home):
    _write_tokens(home)
    out = await setup_tool.run(FakeContext(store=FakeStore()))
    assert "provisional" in out


async def test_banner_is_prefixed_when_an_alert_is_active(home):
    _write_tokens(home)
    _write_store(home)
    ctx = FakeContext(store=FakeStore(), banner="⚠ ALERT")
    assert (await setup_tool.run(ctx)).startswith("⚠ ALERT")


async def test_output_stays_under_cap(home):
    _write_tokens(home)
    _write_store(home)
    from fartlek.render.renderer import estimate_tokens

    for ctx in (
        FakeContext(store=FakeStore()),
        FakeContext(store=FakeStore(), auth_fails=True),
        FakeContext(store=FakeStore({"goal_race_date": "2026-09-05"})),
    ):
        assert estimate_tokens(await setup_tool.run(ctx)) <= setup_tool.CAP_TOKENS
