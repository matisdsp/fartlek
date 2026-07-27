"""First-run detection and the login-terminal bridge (DESIGN §2.4).

Two callers need the same answer to "is this machine set up?": the server at
import time, to shape the MCP `instructions` string, and `garmin_setup` on
every call. The probe is therefore **filesystem-only** — no network, no Garmin
call, no SQLite open — because it runs on every server start and a slow or
failing probe would break the handshake.

Credentials never appear here. The login is spawned into a terminal the human
types into; this module builds the command and opens the window, and never
sees an email, a password or an MFA code.
"""
from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from fartlek.paths import default_tokenstore, fartlek_home

TOKEN_FILENAME = "garmin_tokens.json"

#: Prefixed to the routing instructions when no credentials are on disk. Kept
#: short: it is paid by every conversation until the user authenticates.
SETUP_BANNER = (
    "SETUP REQUIRED — no Garmin credentials on this machine yet, so every "
    "coaching tool below will fail. Call garmin_setup() first and relay what it "
    "returns. Do not guess the user's data, and do not invent setup steps or "
    "credential lifetimes: garmin_setup is the only source of truth about this "
    "server's own state.\n\n"
)


@dataclass(frozen=True)
class SetupState:
    """What is on disk. Everything here is cheap to determine."""

    token_file: Path
    has_tokens: bool
    home: Path
    accounts: tuple[str, ...]
    store_bytes: int

    @property
    def configured(self) -> bool:
        """Credentials present. The store fills itself on the first tool call
        (cold start, ToolContext._init), so it is not part of the gate."""
        return self.has_tokens

    @property
    def has_store(self) -> bool:
        return bool(self.accounts)


def probe() -> SetupState:
    home = fartlek_home()
    token_file = default_tokenstore() / TOKEN_FILENAME
    accounts: list[str] = []
    store_bytes = 0
    try:
        for child in sorted(home.iterdir()):
            store = child / "store.db"
            if child.is_dir() and store.is_file():
                accounts.append(child.name)
                store_bytes += store.stat().st_size
    except OSError:
        pass  # no home yet, or unreadable — indistinguishable from "not set up"
    return SetupState(
        token_file=token_file,
        has_tokens=token_file.is_file(),
        home=home,
        accounts=tuple(accounts),
        store_bytes=store_bytes,
    )


def instructions(routing: str, state: SetupState | None = None) -> str:
    """Routing instructions, prefixed with the setup banner when unconfigured.

    Computed at server start, so it is a snapshot: a session that authenticates
    mid-conversation keeps the stale banner until the server restarts. That is
    why garmin_setup re-probes live and is the authority (DESIGN §2.4).
    """
    state = state or probe()
    return routing if state.configured else SETUP_BANNER + routing


# --- the login bridge -------------------------------------------------------


def login_argv(replace: bool = False) -> list[str]:
    """`fartlek auth`, run through *this* interpreter so a uvx-ephemeral or
    venv install launches its own code rather than whatever is on PATH.

    `replace=True` skips the "existing tokens found, replace?" prompt — used
    when re-authenticating a dead session, where the answer is never no.
    """
    argv = [sys.executable, "-m", "fartlek.cli", "auth"]
    return [*argv, "--replace"] if replace else argv


def login_command(replace: bool = False) -> str:
    """The shell one-liner to run in a terminal.

    A new terminal window does not inherit the server's environment, so the
    home overrides are re-exported explicitly: authenticating into the wrong
    FARTLEK_HOME is worse than failing to authenticate at all.
    """
    parts = [
        f"export {var}={shlex.quote(value)}"
        for var in ("FARTLEK_HOME", "GARMINTOKENS")
        if (value := os.environ.get(var))
    ]
    parts.append(shlex.join(login_argv(replace)))
    return " && ".join(parts)


def open_login_terminal(command: str | None = None) -> str | None:
    """Open a terminal running the login. Returns None on success, else why not.

    Never raises: a failure to spawn degrades to the copy-pasteable command,
    which is a supported outcome (DESIGN §2.4).
    """
    command = command or login_command()
    system = platform.system()
    try:
        if system == "Darwin":
            script = f"tell application \"Terminal\"\nactivate\ndo script {json.dumps(command)}\nend tell"
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                timeout=20,
            )
            return None
        if system == "Linux":
            held = f"{command}; echo; echo '[press Enter to close]'; read _"
            for term, args in (
                ("x-terminal-emulator", ["-e"]),
                ("gnome-terminal", ["--"]),
                ("konsole", ["-e"]),
                ("xfce4-terminal", ["-e"]),
                ("xterm", ["-e"]),
            ):
                if shutil.which(term):
                    subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                        [term, *args, "bash", "-lc", held],
                        start_new_session=True,
                    )
                    return None
            return "no terminal emulator found (tried x-terminal-emulator, gnome-terminal, konsole, xfce4-terminal, xterm)"
        if system == "Windows":
            subprocess.Popen(  # noqa: S603
                ["cmd", "/c", "start", "cmd", "/k", command],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            return None
    except Exception as exc:  # noqa: BLE001 — any spawn failure degrades, never propagates
        return f"could not open a terminal ({type(exc).__name__})"
    return f"no terminal support on this platform ({system or 'unknown'})"
