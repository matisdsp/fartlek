"""MCP prompt bodies (DESIGN §4.6) — progressive enhancement.

Each prompt is a conversation starter that pairs **data with methodology**: it
directs the model to the right tool(s) and frames the read in a coaching-doctrine
review order (Seiler's intensity distribution, Friel's form/periodization), with
the project's honesty rules restated inline (the athlete outranks the sensors;
do not re-derive a pre-computed number; never invent a cause).

No correctness depends on these — a tools-only client gets the full load from
tool descriptions and breadcrumbs. They live here as pure text builders so they
are unit-testable; `server.py` registers thin `@mcp.prompt()` wrappers.

Every `garmin_*` name mentioned below is a shipped tool (a phantom name would be
a routing trap), and the test suite enforces that.
"""
from __future__ import annotations

_NO_REDERIVE = ("Every number is pre-computed against my own baselines — read and "
                "translate them, don't recompute.")


def morning_briefing() -> str:
    return (
        "Give me my morning briefing.\n\n"
        "1. Call `garmin_brief` (zero arguments) for today's fused readiness.\n"
        "2. Read it in this order: my logged/subjective state first — illness, pain "
        "or exhaustion outranks any GREEN sensor verdict; then the verdict and which "
        "markers drove it; then today's planned workout against that verdict.\n"
        "3. If anything is flagged, follow the breadcrumb into `garmin_recovery` for "
        "the physiology before deciding.\n\n"
        "End with one line — go / modify / rest — then the why. " + _NO_REDERIVE
    )


def weekly_review() -> str:
    return (
        "Review my past training week.\n\n"
        "1. Call `garmin_week`.\n"
        "2. Interpret in review order: total load vs recent weeks and the ramp; "
        "intensity distribution vs my own norm (polarized, or drifting into the grey "
        "zone?); recovery across the week; then session-execution highlights.\n"
        "3. Call `garmin_load` if you need the multi-week trajectory behind it.\n\n"
        "Finish with one thing to keep and one thing to change next week. " + _NO_REDERIVE
    )


def post_activity_debrief(activity_id: str) -> str:
    return (
        f"Debrief this session.\n\n"
        f"1. Call `garmin_activity(activity_id={activity_id})`.\n"
        "2. Cover execution vs structure (did the reps hold, any fade?), "
        "decoupling / durability, and how it compares to the closest past session.\n"
        "3. If RPE is missing from the watch and my log, ask how it felt and offer to "
        "record it with `garmin_log`.\n\n"
        "Stay with what the data shows — state co-occurrence, never an invented cause."
    )


def race_readiness() -> str:
    return (
        "Assess my readiness for the goal race.\n\n"
        "1. Call `garmin_fitness`.\n"
        "2. Cover: is fitness trending the right way (VO2max, efficiency, durability)? "
        "the race projection vs my target — state the range and its assumptions, never "
        "a false point estimate; and the taper window / form to race day.\n"
        "3. If no goal is on file, tell me to set one with `garmin_set_profile`.\n\n"
        "Call out the single most sensitive lever in the projection. " + _NO_REDERIVE
    )


def plan_next_week() -> str:
    return (
        "Help me plan next week.\n\n"
        "1. Call `garmin_load` (dose, ramp, ACWR, monotony/strain vs my thresholds) "
        "and `garmin_week` (last week's shape).\n"
        "2. Propose next week's volume and intensity distribution that respects a sane "
        "ramp, fits my phase and goal, and keeps me clear of my own overreach levels.\n\n"
        "Give the load target and the key sessions; flag anything that would spike ACWR "
        "or monotony. " + _NO_REDERIVE
    )


def injury_risk_check() -> str:
    return (
        "Check my injury / overtraining risk.\n\n"
        "1. Call `garmin_recovery` (the multi-marker overtraining audit) and "
        "`garmin_load` (ramp, ACWR, monotony/strain).\n"
        "2. A single marker is never an alarm — look for two or more marker groups "
        "converging, and weigh my logged state above the sensors. Surface any "
        "unresolved injury on file.\n\n"
        "If nothing converges, say so plainly rather than manufacturing concern."
    )


def setup_athlete() -> str:
    return (
        "Set up my athlete profile. Ask me one question at a time, then persist each "
        "answer as you go:\n\n"
        "1. Goal race — the date, and either a distance (5k/10k/half/marathon/custom) "
        "or a fixed-time event (e.g. 24h) with a target distance.\n"
        "2. Current training phase and which week of it I'm in.\n"
        "3. Weekly availability (days I can train).\n"
        "4. Intensity preference / target distribution.\n"
        "5. Any known injuries or ongoing niggles.\n\n"
        "Persist 1–4 with `garmin_set_profile`; record injuries/illness with "
        "`garmin_log`. When done, confirm the result with `garmin_athlete`."
    )


# name → (builder, one-line description) for registration and testing
PROMPTS = {
    "morning_briefing": (morning_briefing, "Today's readiness, read in coaching order"),
    "weekly_review": (weekly_review, "Review the past training week"),
    "post_activity_debrief": (post_activity_debrief, "Debrief one session by activity_id"),
    "race_readiness": (race_readiness, "Goal-race fitness, projection and taper"),
    "plan_next_week": (plan_next_week, "Plan next week within load guardrails"),
    "injury_risk_check": (injury_risk_check, "Multi-marker overtraining / injury check"),
    "setup_athlete": (setup_athlete, "Guided athlete-profile setup"),
}
