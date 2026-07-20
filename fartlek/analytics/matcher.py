"""Planned-vs-executed matching (DESIGN.md §3.2 #15).

match_plan(plan_entries, activities) pairs plan_calendar rows with activity
rows in two passes, each greedy by |duration delta| ascending across all
candidate pairs (stable order, each side used at most once):

  1. 'garmin_link' — activity extra_json carries a workoutId equal to the
     plan entry's garmin_workout_id (string-compared). The explicit link is
     definitive: no date/sport/duration gate.
  2. 'heuristic' — on the remainder: same date + same sport family +
     duration within ±25% (inclusive) of planned duration.

No match → matched_activity_id None (renders as 'missed' once date is past).
Pure function; persistence via Store.set_plan_match by the caller.
"""
from __future__ import annotations

import json
from typing import Any

_FAMILY_BY_KEY = {
    # running
    "running": "running",
    "treadmill_running": "running",
    "trail_running": "running",
    "track_running": "running",
    "indoor_running": "running",
    "virtual_run": "running",
    # cycling (plus any cycling_* key, handled by prefix below)
    "cycling": "cycling",
    "road_biking": "cycling",
    "mountain_biking": "cycling",
    "gravel_cycling": "cycling",
    "indoor_cycling": "cycling",
    "virtual_ride": "cycling",
    # swimming
    "lap_swimming": "swimming",
    "open_water_swimming": "swimming",
    "swimming": "swimming",
    # strength
    "strength_training": "strength",
    "indoor_cardio": "strength",
    "hiit": "strength",
    # walking
    "walking": "walking",
    "casual_walking": "walking",
    "speed_walking": "walking",
    # hiking
    "hiking": "hiking",
}


def sport_family(type_key: str) -> str:
    """Collapse Garmin typeKeys into families: running, cycling, swimming,
    strength, walking, hiking, other. `cycling_*` keys → cycling; unknown → 'other'."""
    if not type_key:
        return "other"
    key = type_key.strip().lower()
    family = _FAMILY_BY_KEY.get(key)
    if family is not None:
        return family
    if key.startswith("cycling_"):
        return "cycling"
    return "other"


def _planned_duration_s(entry: dict[str, Any]) -> float | None:
    """Planned duration: entry['duration_s'] if present, else from planned_json."""
    value = entry.get("duration_s")
    if value is None:
        planned = entry.get("planned_json")
        if isinstance(planned, str):
            try:
                planned = json.loads(planned)
            except (ValueError, TypeError):
                planned = None
        if isinstance(planned, dict):
            value = planned.get("duration_s")
            if value is None:
                value = planned.get("durationSeconds")
            if value is None:
                value = planned.get("duration")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _activity_workout_id(activity: dict[str, Any]) -> str | None:
    extra = activity.get("extra_json")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (ValueError, TypeError):
            return None
    if not isinstance(extra, dict):
        return None
    wid = extra.get("workoutId")
    if wid is None:
        wid = extra.get("workout_id")
    return str(wid) if wid is not None else None


def _greedy_assign(
    candidates: list[tuple[float, int, int]],
    plan_used: set[int],
    act_used: set[int],
    assignment: dict[int, int],
) -> None:
    """Take (delta, plan_idx, act_idx) pairs by ascending delta (stable) with
    each side used at most once."""
    for _delta, pi, ai in sorted(candidates, key=lambda c: c[0]):
        if pi in plan_used or ai in act_used:
            continue
        plan_used.add(pi)
        act_used.add(ai)
        assignment[pi] = ai


def match_plan(
    plan_entries: list[dict[str, Any]],
    activities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Returns [{plan_id, matched_activity_id, match_method}] for every plan
    entry, in input order. plan_id is entry['id'] (or entry['plan_id'])."""
    plans = list(plan_entries)
    acts = list(activities)
    plan_used: set[int] = set()
    act_used: set[int] = set()
    assignment: dict[int, int] = {}
    method: dict[int, str] = {}

    # Pass 1: explicit Garmin workout-activity link.
    act_wids = [_activity_workout_id(a) for a in acts]
    link_candidates: list[tuple[float, int, int]] = []
    for pi, plan in enumerate(plans):
        wid = plan.get("garmin_workout_id")
        if wid is None:
            continue
        wid = str(wid)
        planned = _planned_duration_s(plan)
        for ai, act in enumerate(acts):
            if act_wids[ai] != wid:
                continue
            dur = act.get("duration_s")
            delta = abs(float(dur) - planned) if dur is not None and planned is not None else float("inf")
            link_candidates.append((delta, pi, ai))
    _greedy_assign(link_candidates, plan_used, act_used, assignment)
    for pi in assignment:
        method[pi] = "garmin_link"

    # Pass 2: heuristic on the remainder.
    heur_candidates: list[tuple[float, int, int]] = []
    for pi, plan in enumerate(plans):
        if pi in plan_used:
            continue
        planned = _planned_duration_s(plan)
        plan_sport = plan.get("sport")
        if planned is None or planned <= 0 or not plan_sport:
            continue
        family = sport_family(plan_sport)
        for ai, act in enumerate(acts):
            if ai in act_used:
                continue
            if act.get("date") != plan.get("date"):
                continue
            if sport_family(act.get("sport") or "") != family:
                continue
            dur = act.get("duration_s")
            if dur is None:
                continue
            delta = abs(float(dur) - planned)
            if delta <= 0.25 * planned:
                heur_candidates.append((delta, pi, ai))
    linked = set(assignment)
    _greedy_assign(heur_candidates, plan_used, act_used, assignment)
    for pi in assignment:
        if pi not in linked:
            method[pi] = "heuristic"

    results: list[dict[str, Any]] = []
    for pi, plan in enumerate(plans):
        ai = assignment.get(pi)
        results.append(
            {
                "plan_id": plan.get("id", plan.get("plan_id")),
                "matched_activity_id": acts[ai].get("activity_id") if ai is not None else None,
                "match_method": method.get(pi),
            }
        )
    return results
