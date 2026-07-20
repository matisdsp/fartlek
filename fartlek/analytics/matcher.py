"""Planned-vs-executed matching (DESIGN.md §3.2 #15). CONTRACT STUB.

match_plan(plan_entries, activities) → list of match decisions.
Rules, in order:
  1. Garmin's explicit workout-activity link (activity extra_json carries a
     workoutId matching plan.garmin_workout_id) → method 'garmin_link'.
  2. Heuristic: same date + same sport (typeKey family match, e.g. running
     covers treadmill_running) + duration within ±25% of planned → 'heuristic'.
     Multiple candidates → closest duration wins; an activity matches at most
     one plan entry and vice versa (greedy by closeness).
  3. No match → matched_activity_id None (renders as 'missed' once date past).

Returns [{plan_id, matched_activity_id, match_method}] for every plan entry
in range; pure function, persistence via Store.set_plan_match by the caller.
"""
from __future__ import annotations

from typing import Any


def sport_family(type_key: str) -> str:
    """Collapse Garmin typeKeys into families: running, cycling, swimming,
    strength, walking, hiking, other. Unknown keys → 'other'."""
    raise NotImplementedError


def match_plan(
    plan_entries: list[dict[str, Any]],
    activities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raise NotImplementedError
