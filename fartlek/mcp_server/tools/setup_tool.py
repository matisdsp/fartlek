"""garmin_setup — first-run state and the login bridge (DESIGN §2.4, cap 400).

Answers "is this machine set up, and what is missing?" and performs the one
step the model cannot: opening a terminal on `fartlek auth`. It never accepts
and never asks for an email, a password or an MFA code — those are typed by
the human into that terminal, so they never enter the conversation.

Everything else that setup needs is already automatable: the store fills
itself on the first tool call (cold start), and the athlete profile is filled
conversationally with garmin_set_profile. This tool's job is to say so.
"""
from __future__ import annotations

from fartlek.health.exceptions import GarminAuthError
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


def _not_configured(state, launch_login: bool) -> str:
    command = setup_state.login_command()
    head = f"# Fartlek setup — step 1 of 2\n\n**Not configured.** No Garmin credentials at `{state.token_file}`."

    if not launch_login:
        return (
            f"{head}\n\n"
            "Garmin needs an email, a password and an MFA code. They are typed by the user "
            "in a terminal, never in this conversation — do not ask for them here.\n\n"
            "Call `garmin_setup(launch_login=True)` to open that terminal for the user, or "
            f"offer this command to run manually:\n\n    {command}\n\n"
            "Next: garmin_setup(launch_login=True)"
        )

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
    except GarminAuthError:
        command = setup_state.login_command(replace=True)
        body = (
            "# Fartlek setup\n\n"
            f"**Credentials found but the Garmin session is dead** (`{state.token_file}`). "
            "Re-authenticating is the only fix — retrying tools will not help.\n\n"
            f"Ask the user to run:\n\n    {command}\n\n"
            "Then call garmin_setup() again.\n\n"
            "Next: garmin_setup()"
        )
        return _finish(None, body)

    banner = ctx.banner()
    lines = ["# Fartlek setup\n", "**Ready.** Garmin session live; the coaching tools will answer."]

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
