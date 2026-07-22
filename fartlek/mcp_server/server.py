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
    log_tool as t_log,
)
from fartlek.mcp_server.tools import (
    raw as t_raw,
)
from fartlek.mcp_server.tools import (
    recovery as t_recovery,
)
from fartlek.mcp_server.tools import (
    set_profile as t_set_profile,
)
from fartlek.mcp_server.tools import (
    sync_tool as t_sync,
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
        "Call this FIRST for any question about how the athlete is doing today: readiness, "
        "whether to train, how recovery looks right now, or anything time-ambiguous about "
        "current state. Zero arguments needed. Returns a fused go/modify/rest verdict with "
        "every recovery signal compared to this athlete's personal baseline, active alerts, "
        "yesterday's session (with its activity_id), and today's scheduled workout. For "
        "per-session analysis use garmin_activity; for browsing history use garmin_activities."
    ),
)
async def garmin_brief(
    date: Annotated[str | None, Field(description="YYYY-MM-DD, default today (retrospective briefs)")] = None,
) -> str:
    return await _guard(t_brief.run(_ctx, date=date))


@mcp.tool(
    annotations=READ,
    description=(
        "Browse the training log and get activity IDs for drill-down. One compact row per "
        "session — every row carries the activity_id that garmin_activity accepts. Filter by "
        "date range and sport. All rows in the window are listed (up to limit); truncation "
        "is disclosed with narrowing advice."
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
        "Deep analysis of ONE session: execution vs. structure, rep-by-rep fade for "
        "intervals, decoupling for steady runs, comparison to the most similar past "
        "session, planned-vs-executed where a planned workout exists. Select by activity_id "
        "(from garmin_activities/garmin_brief), OR by date, OR omit both for the latest "
        "activity — add sport to get the latest of that sport ('analyze my last run' = "
        "garmin_activity(sport='running')). detail='standard' is enough for coaching; "
        "'splits' adds the full lap table; 'full' adds a downsampled HR/pace curve."
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
        "Athlete reference card: zones, thresholds, PRs, goal race and phase (from "
        "garmin_set_profile), personal baselines, injury notes, device data coverage. Call "
        "once when athlete context is unknown; contents change rarely. To change goal/phase/"
        "overrides, use garmin_set_profile."
    ),
)
async def garmin_athlete() -> str:
    return await _guard(t_athlete.run(_ctx))


@mcp.tool(
    annotations=LOCAL_WRITE,
    description=(
        "Set or update athlete context the watch can't know: goal race (date, distance or "
        "fixed-time event such as 24h with a target distance, target time), training phase, weekly availability, intensity-distribution "
        "preference, LT1 override. Stored locally only; grounds the plan/goal context used "
        "by other tools. Only provided fields change. Call when the user states or changes "
        "a goal, phase, or constraint. Injuries and illness belong to garmin_log, not here."
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
        float | None,
        Field(description="target distance for a fixed-time event (6h/12h/24h)"),
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
        "Record subjective data the watch cannot capture: session RPE (1-10) and "
        "Hooper-style wellness — fatigue, soreness, stress, mood, sleep quality (each 1-7) "
        "— plus notes, especially illness or injury (set flag; resolve when healed). Stored "
        "locally; feeds sRPE load, the readiness verdict (an illness note caps today's "
        "verdict), and future analyses. Ask the athlete for RPE after discussing a session "
        "if it is missing."
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
        "Force a data refresh from Garmin and report freshness, or start a historical "
        "backfill (backfill_days > 0; resumable). Backfill deepens the sleep/HRV history "
        "window. Use only if the user says data looks stale — all other tools auto-refresh "
        "when stale."
    ),
)
async def garmin_sync(
    backfill_days: Annotated[int, Field(ge=0, le=365)] = 0,
) -> str:
    return await _guard(t_sync.run(_ctx, backfill_days=backfill_days))


@mcp.tool(
    annotations=READ,
    description=(
        "Fitness outcomes and race feasibility: VO2max and efficiency trends, heart rate at "
        "a fixed pace, long-run durability, a race projection against the athlete's stored "
        "goal, and a form projection to race day with taper guidance. Call for 'am I getting "
        "fitter', 'is training working', race planning, taper timing, or goal-feasibility "
        "questions. The goal race comes from the athlete profile — set it with "
        "garmin_set_profile."
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
        "Recovery physiology over time: sleep, HRV, resting HR and load structure compared "
        "to this athlete's personal baselines, plus the multi-marker overtraining audit. "
        "Call when the user asks why they feel tired, how they are sleeping, whether they "
        "are overtraining or getting sick, or when another tool flags a recovery signal. "
        "This tool OWNS overtraining questions. Not for a single day's go/no-go — that is "
        "garmin_brief."
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
        "Bounded escape hatch to a named Garmin data source, compacted (nulls/boilerplate "
        "stripped, series downsampled) and hard-capped. Use ONLY when a synthesis tool "
        "cannot answer and the user explicitly asks for underlying values. Never a starting "
        "point."
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
