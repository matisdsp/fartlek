"""Fartlek MCP server — the Phase-1 synthesis tool surface (DESIGN §2).

8 question-shaped tools over the local store; raw Garmin JSON never reaches
the model. stdout is reserved for JSON-RPC; log to stderr only.

Environment: GARMINTOKENS (default ~/.fartlek/tokens, from `fartlek auth`),
FARTLEK_HOME (default ~/.fartlek).
"""
from __future__ import annotations

import logging
import os
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from fartlek.health.exceptions import GarminAuthError
from fartlek.mcp_server.context import ToolContext
from fartlek.mcp_server.tools import (
    activities as t_activities,
)
from fartlek.mcp_server.tools import (
    activity as t_activity,
)
from fartlek.mcp_server.tools import (
    athlete as t_athlete,
)
from fartlek.mcp_server.tools import (
    brief as t_brief,
)
from fartlek.mcp_server.tools import (
    fitness as t_fitness,
)
from fartlek.mcp_server.tools import (
    load as t_load,
)
from fartlek.mcp_server.tools import (
    log_tool as t_log,
)
from fartlek.mcp_server.tools import (
    raw as t_raw,
)
from fartlek.mcp_server.tools import (
    recovery as t_recovery,
)
from fartlek.mcp_server.tools import (
    reference as t_reference,
)
from fartlek.mcp_server.tools import (
    set_profile as t_set_profile,
)
from fartlek.mcp_server.tools import (
    sync_tool as t_sync,
)
from fartlek.mcp_server.tools import (
    week as t_week,
)
from fartlek.mcp_server.tools import (
    whats_changed as t_whats_changed,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("fartlek-mcp")

AUTH_ERROR = "Garmin session expired — the user must re-run `fartlek auth`. Retrying will not help."

INSTRUCTIONS = (
    "Garmin coaching server. Routing: questions about **today** — readiness, whether to "
    "train, current state — start with `garmin_brief` (zero arguments). Browse or find "
    "sessions → `garmin_activities`. One session in depth → `garmin_activity` (by id, date, "
    "or latest-of-sport). Athlete context (zones, PRs, goal, data coverage) → "
    "`garmin_athlete`. Log athlete-reported RPE, illness, injuries with `garmin_log`; goals "
    "and phases with `garmin_set_profile`. Never start with `garmin_raw`. All numbers are "
    "pre-computed against this athlete's personal baselines: do not re-derive statistics or "
    "aggregates — but athlete-reported state (illness, pain, exhaustion) always outranks a "
    "sensor-based GREEN; if the user reports feeling unwell, advise caution regardless of "
    "the verdict, and log it. Dates include weekdays; trust them."
)

mcp = FastMCP("fartlek", instructions=INSTRUCTIONS)
_ctx = ToolContext()

READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
LOCAL_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}


async def _guard(coro):
    try:
        return await coro
    except GarminAuthError:
        return AUTH_ERROR


@mcp.tool(
    annotations=READ,
    description=(
        "Call FIRST for anything about TODAY: readiness, whether to train, current state. "
        "Zero arguments. Returns a fused go/modify/rest verdict against personal baselines, "
        "active alerts, yesterday's session with its activity_id, and today's planned "
        "workout. One session → garmin_activity; browsing → garmin_activities."
    ),
)
async def garmin_brief(
    date: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
) -> str:
    return await _guard(t_brief.run(_ctx, date=date))


@mcp.tool(
    annotations=READ,
    description=(
        "Browse the log and get activity IDs. One row per session, each carrying the "
        "activity_id garmin_activity accepts. Filter by date range and sport; truncation is "
        "disclosed with narrowing advice."
    ),
)
async def garmin_activities(
    start_date: Annotated[str | None, Field(description="YYYY-MM-DD, default today−13d")] = None,
    end_date: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
    sport: Literal["running", "cycling", "swimming", "strength", "other"] | None = None,
    limit: Annotated[int, Field(ge=1, le=30)] = 25,
) -> str:
    return await _guard(
        t_activities.run(_ctx, start_date=start_date, end_date=end_date, sport=sport, limit=limit)
    )


@mcp.tool(
    annotations=READ,
    description=(
        "ONE session in depth: execution vs structure, rep-by-rep fade, decoupling, "
        "comparison to the closest past session, planned-vs-executed. Select by "
        "activity_id, by date, or omit both for the latest — add sport for the latest of "
        "that sport. 'splits' adds the lap table; 'full' adds an HR/pace curve."
    ),
)
async def garmin_activity(
    activity_id: int | None = None,
    date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    sport: Literal["running", "cycling", "swimming", "strength", "other"] | None = None,
    detail: Literal["standard", "splits", "full"] = "standard",
) -> str:
    return await _guard(
        t_activity.run(_ctx, activity_id=activity_id, date=date, sport=sport, detail=detail)
    )


@mcp.tool(
    annotations=READ,
    description=(
        "Reference card: zones, thresholds, PRs, goal and phase, baselines, injury notes, "
        "device data coverage. Call once when athlete context is unknown; it changes "
        "rarely. To change it, garmin_set_profile."
    ),
)
async def garmin_athlete() -> str:
    return await _guard(t_athlete.run(_ctx))


@mcp.tool(
    annotations=LOCAL_WRITE,
    description=(
        "Athlete context the watch cannot know: goal race (date; a distance, or a "
        "fixed-time event like 24h with a target distance), phase, weekly availability, "
        "intensity preference, LT1 override. Local only; only provided fields change. "
        "Injuries and illness go to garmin_log."
    ),
)
async def garmin_set_profile(
    goal_race_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    goal_distance: Literal[
        "5k", "10k", "half", "marathon", "custom", "6h", "12h", "24h"
    ] | None = None,
    goal_custom_km: Annotated[
        float | None, Field(description="with goal_distance='custom'")
    ] = None,
    goal_target_km: Annotated[
        float | None, Field(description="for fixed-time events (6h/12h/24h)")
    ] = None,
    goal_time: Annotated[str | None, Field(description="H:MM:SS, distance races only")] = None,
    phase: Literal["base", "build", "peak", "taper", "recovery", "none"] | None = None,
    phase_week: int | None = None,
    phase_total_weeks: int | None = None,
    availability_days: Annotated[int | None, Field(ge=1, le=7)] = None,
    tid_target: Literal["polarized", "pyramidal", "auto"] | None = None,
    lt1_hr_override: int | None = None,
) -> str:
    return await _guard(
        t_set_profile.run(
            _ctx,
            goal_race_date=goal_race_date,
            goal_distance=goal_distance,
            goal_custom_km=goal_custom_km,
            goal_target_km=goal_target_km,
            goal_time=goal_time,
            phase=phase,
            phase_week=phase_week,
            phase_total_weeks=phase_total_weeks,
            availability_days=availability_days,
            tid_target=tid_target,
            lt1_hr_override=lt1_hr_override,
        )
    )


@mcp.tool(
    annotations=LOCAL_WRITE,
    description=(
        "Subjective data the watch cannot capture: session RPE (1-10), Hooper wellness "
        "(fatigue, soreness, stress, mood, sleep quality, each 1-7), and notes — especially "
        "illness or injury (set flag; resolve when healed). Feeds sRPE load and caps the "
        "readiness verdict. Ask for RPE after discussing a session if missing."
    ),
)
async def garmin_log(
    date: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
    rpe: Annotated[int | None, Field(ge=1, le=10)] = None,
    fatigue: Annotated[int | None, Field(ge=1, le=7)] = None,
    soreness: Annotated[int | None, Field(ge=1, le=7)] = None,
    stress: Annotated[int | None, Field(ge=1, le=7)] = None,
    mood: Annotated[int | None, Field(ge=1, le=7)] = None,
    sleep_quality: Annotated[int | None, Field(ge=1, le=7)] = None,
    note: str | None = None,
    flag: Literal["illness", "injury"] | None = None,
    resolve_flag: bool = False,
    activity_id: int | None = None,
) -> str:
    return await _guard(
        t_log.run(
            _ctx,
            date=date,
            rpe=rpe,
            fatigue=fatigue,
            soreness=soreness,
            stress=stress,
            mood=mood,
            sleep_quality=sleep_quality,
            note=note,
            flag=flag,
            resolve_flag=resolve_flag,
            activity_id=activity_id,
        )
    )


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False},
    description=(
        "Force a refresh and report freshness, or start a resumable historical backfill "
        "(backfill_days > 0, deepens sleep/HRV history). Use only if data looks stale — "
        "every other tool auto-refreshes."
    ),
)
async def garmin_sync(
    backfill_days: Annotated[int, Field(ge=0, le=365)] = 0,
) -> str:
    return await _guard(t_sync.run(_ctx, backfill_days=backfill_days))


@mcp.tool(
    annotations=READ,
    description=(
        "Is training working: VO2max and efficiency trends, HR at a fixed pace, long-run "
        "durability, a race projection against the stored goal, and form projected to race "
        "day with taper guidance. Call for 'am I getting fitter', race planning, taper "
        "timing, goal feasibility. Set the goal with garmin_set_profile."
    ),
)
async def garmin_fitness(
    weeks: Annotated[int, Field(ge=4, le=52, description="window length, default 12")] = 12,
    anchor_date: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
) -> str:
    return await _guard(t_fitness.run(_ctx, weeks=weeks, anchor_date=anchor_date))


@mcp.tool(
    annotations=READ,
    description=(
        "Sleep, HRV, resting HR and load structure vs personal baselines, plus the "
        "multi-marker overtraining audit. Call for tiredness, sleep, 'am I overtraining "
        "or getting sick', or when another tool flags recovery. OWNS overtraining "
        "questions. Single-day go/no-go is garmin_brief."
    ),
)
async def garmin_recovery(
    days: Annotated[int, Field(ge=7, le=90, description="window length, default 28")] = 28,
    anchor_date: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
) -> str:
    return await _guard(t_recovery.run(_ctx, days=days, anchor_date=anchor_date))


@mcp.tool(
    annotations=READ,
    description=(
        "Multi-week dose: fitness/fatigue/form (CTL/ATL/TSB), ramp rate, ACWR, "
        "monotony/strain, and intensity drift vs this athlete's own norm. Call for 'am I "
        "training too much', ramp/taper dosing, periodization. Not single-day readiness "
        "(garmin_brief); overtraining physiology is garmin_recovery."
    ),
)
async def garmin_load(
    weeks: Annotated[int, Field(ge=2, le=52, description="window length, default 8")] = 8,
    anchor_date: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
) -> str:
    return await _guard(t_load.run(_ctx, weeks=weeks, anchor_date=anchor_date))


@mcp.tool(
    annotations=READ,
    description=(
        "One week in session-level detail: load vs recent weeks, intensity distribution, a "
        "per-day session table with activity_ids, recovery summary, and plan compliance "
        "where a plan exists. Call for 'how was my week' or a specific week. Multi-week "
        "trajectory is garmin_load."
    ),
)
async def garmin_week(
    anchor_date: Annotated[str | None, Field(description="YYYY-MM-DD, its Mon-Sun week")] = None,
) -> str:
    return await _guard(t_week.run(_ctx, anchor_date=anchor_date))


@mcp.tool(
    annotations=READ,
    description=(
        "Call for 'anything I should know?', 'what's new?', 'catch me up', or after days "
        "away. Scans every tracked metric and returns ONLY statistically significant "
        "changes, ranked safety-first; says 'nothing notable' when nothing tripped. "
        "Today's readiness is garmin_brief."
    ),
)
async def garmin_whats_changed(
    since_days: Annotated[int, Field(ge=1, le=60, description="default 7")] = 7,
) -> str:
    return await _guard(t_whats_changed.run(_ctx, since_days=since_days))


@mcp.tool(
    annotations=READ,
    description=(
        "How a number was computed and whether to trust it: formula, inputs, whether each "
        "threshold is a population default or personally derived, and the caveats. No "
        "arguments for the index; metric='acwr' for one in depth."
    ),
)
async def garmin_reference(
    topic: str = "metrics_glossary",
    metric: Annotated[str | None, Field(description="one metric name")] = None,
) -> str:
    return await _guard(t_reference.run(_ctx, topic=topic, metric=metric))


@mcp.tool(
    annotations=READ,
    description=(
        "Bounded escape hatch to one named Garmin source, compacted and hard-capped. Use "
        "ONLY when a synthesis tool cannot answer and the user explicitly asks for raw "
        "values. Never a starting point."
    ),
)
async def garmin_raw(
    source: Literal[
        "daily_summary",
        "sleep_detail",
        "hrv_detail",
        "stress_detail",
        "body_battery",
        "activity_summary",
        "activity_splits",
        "activity_zones",
        "training_status",
        "race_predictions",
        "weather",
    ],
    date: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
    activity_id: Annotated[
        int | None, Field(description="required for activity_* sources and weather")
    ] = None,
    series: Literal["hypnogram", "hr", "movement", "spo2", "respiration", "stress"]
    | None = None,
    max_points: Annotated[int, Field(ge=1, le=200)] = 50,
) -> str:
    return await _guard(
        t_raw.run(
            _ctx,
            source=source,
            date=date,
            activity_id=activity_id,
            series=series,
            max_points=max_points,
        )
    )


def main() -> None:
    log.info("Starting Fartlek MCP server")
    mcp.run()


if __name__ == "__main__":
    main()
