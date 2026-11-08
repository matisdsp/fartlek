"""garmin_setup — first-run state and the login bridge (cap 400).

Answers "is this machine set up, and what is missing?". Setup starts with the
Garmin account export (`fartlek import <zip>`, no login). The direct
connection is an option: it needs the extra `connect`, the README warning, and
one step the model cannot do, opening a terminal on `fartlek auth`. This tool
never accepts and never asks for an email, a password or an MFA code; they are
typed by the human into that terminal, so they never enter the conversation.

Everything else that setup needs is already automatable: the store fills
itself on the first tool call, and the athlete profile is filled
conversationally with garmin_set_profile. This tool's job is to say so.
"""
from __future__ import annotations

from fartlek.health.exceptions import GarminAuthError, GarminConnectUnavailable
from fartlek.mcp_server import setup_state
from fartlek.render.renderer import estimate_tokens

CAP_TOKENS = 400

# Asked in this order: a goal race is what unlocks the projection and taper
# machinery, the rest only sharpens it.
_PROFILE_PROMPTS = (
    ("goal_race_date", "the goal race date"),
    ("goal_distance", "the event (5k/10k/half/marathon, or 6h/12h/24h)"),
    ("phase", "the training phase"),
    ("availability_days", "training days per week"),
)


def _finish(banner: str | None, body: str, cap: int = CAP_TOKENS) -> str:
    text = f"{banner}\n\n{body}" if banner else body
    if estimate_tokens(text) > cap:
        text = text[: int(cap * 3.2) - 2].rstrip() + " …"
    return text


def _mb(size: int) -> str:
    return f"{size / 1024:.0f} KB" if size < 1_048_576 else f"{size / 1_048_576:.1f} MB"


_README_WARNING = (
    "Garmin's terms of use do not allow this kind of access, and Garmin can suspend accounts "
    "for it: the user should know that before choosing it."
)


def _connect_missing() -> str:
    return (
        "# Fartlek setup\n\n"
        "**The direct connection is not installed.** It needs the optional dependency "
        "`garminconnect` (extra `connect`): in a source checkout, `uv sync --extra connect`. "
        f"{_README_WARNING}\n\n"
        "No login is needed to use an account export: `fartlek import <export.zip>`.\n\n"
        "Next: garmin_setup()"
    )


def _not_configured(state, launch_login: bool) -> str:
    head = "# Fartlek setup\n\n**Not configured.** No Garmin data on this machine yet."

    if not launch_login:
        return (
            f"{head}\n\n"
            "The user requests their account export in Garmin (Data Management > Export Your "
            "Data; Garmin emails a link, which can take days), then runs "
            "`fartlek import <export.zip>` in a terminal and calls garmin_setup() again.\n\n"
            "Next: garmin_setup()"
        )

    if not setup_state.connect_installed():
        return _connect_missing()

    command = setup_state.login_command()
    head = f"{head} Direct connection chosen: {_README_WARNING}"
    failure = setup_state.open_login_terminal(command)
    if failure:
        return (
            f"{head}\n\n"
            f"Could not open a terminal automatically ({failure}). Ask the user to run this "
            f"in their own terminal:\n\n    {command}\n\n"
            "Then call garmin_setup() again.\n\n"
            "Next: garmin_setup()"
        )
    return (
        f"{head}\n\n"
        "**A terminal window is now open** and asking for the Garmin email, password and "
        "MFA code. Tell the user to complete it there — what they type stays on their "
        "machine and never passes through this conversation.\n\n"
        "This tool does not wait for the login. When the user says it is done, call "
        "garmin_setup() again to confirm and continue.\n\n"
        "Next: garmin_setup()"
    )


def _profile_gaps(profile: dict[str, str]) -> list[str]:
    return [label for key, label in _PROFILE_PROMPTS if not profile.get(key)]


async def run(ctx, launch_login: bool = False) -> str:
    state = setup_state.probe()

    if not state.configured:
        return _finish(None, _not_configured(state, launch_login))

    try:
        await ctx.ensure_ready()
    except GarminConnectUnavailable:
        return _finish(None, _connect_missing())
    except GarminAuthError:
        if not state.has_tokens:
            return _finish(None, (
                "# Fartlek setup\n\n"
                "**An export was imported before, but it cannot be opened now:** its file is "
                "gone and the store it filled is missing. Import an export again with "
                "`fartlek import <export.zip>`.\n\nNext: garmin_setup()"
            ))
        command = setup_state.login_command(replace=True)
        body = (
            "# Fartlek setup\n\n"
            f"**Credentials found but the Garmin session is dead** (`{state.token_file}`). "
            "Re-authenticating is the only fix for the direct connection — retrying tools "
            "will not help.\n\n"
            f"Ask the user to run:\n\n    {command}\n\n"
            "Then call garmin_setup() again. Without a login, an account export also works: "
            "`fartlek import <export.zip>`.\n\n"
            "Next: garmin_setup()"
        )
        return _finish(None, body)

    banner = ctx.banner()
    kind = getattr(ctx, "source_kind", "connect")
    if kind == "export":
        ready = (
            "**Ready.** Reading the Garmin export imported earlier (no login). It does not "
            "refresh: for newer data, request a new export and run `fartlek import <export.zip>`."
        )
    elif kind == "store":
        ready = (
            "**Ready, from the store.** The export file is no longer on disk, so the store from "
            "the earlier import is served as is. Import a new export to update it."
        )
    else:
        ready = "**Ready.** Garmin session live; the coaching tools will answer."
    lines = ["# Fartlek setup\n", ready]

    if state.has_store:
        n = len(state.accounts)
        lines.append(
            f"- Local store: {n} account{'' if n == 1 else 's'}, {_mb(state.store_bytes)} on disk."
        )
    else:
        lines.append(
            "- Local store: empty — the first coaching call backfills it inline, "
            "so that call is slower and its verdict is labelled provisional."
        )

    gaps = _profile_gaps(ctx.store.get_profile())
    if gaps:
        lines.append(
            "\n**Missing athlete context** (no goal race means no projection, no taper "
            f"guidance): {', '.join(gaps)}. Ask the user for these in conversation and "
            "record them with garmin_set_profile — never assume them."
        )
        lines.append("\nNext: garmin_set_profile(goal_race_date='YYYY-MM-DD') · garmin_brief()")
    else:
        lines.append("\nSetup is complete — nothing left to configure.")
        lines.append("\nNext: garmin_brief()")

    return _finish(banner, "\n".join(lines))
