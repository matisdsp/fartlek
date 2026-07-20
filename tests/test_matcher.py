"""Tests for fartlek.analytics.matcher (planned-vs-executed matching)."""
from __future__ import annotations

import pytest

from fartlek.analytics.matcher import match_plan, sport_family

DATE = "2026-07-19"


def plan(pid, date=DATE, sport="running", duration_s=3600.0, garmin_workout_id=None, **kw):
    return {
        "id": pid,
        "date": date,
        "sport": sport,
        "duration_s": duration_s,
        "garmin_workout_id": garmin_workout_id,
        **kw,
    }


def act(aid, date=DATE, sport="running", duration_s=3600.0, extra_json=None):
    return {
        "activity_id": aid,
        "date": date,
        "sport": sport,
        "duration_s": duration_s,
        "extra_json": extra_json,
    }


# --- sport_family -----------------------------------------------------------

@pytest.mark.parametrize(
    ("type_key", "family"),
    [
        ("running", "running"),
        ("treadmill_running", "running"),
        ("trail_running", "running"),
        ("track_running", "running"),
        ("indoor_running", "running"),
        ("virtual_run", "running"),
        ("cycling", "cycling"),
        ("road_biking", "cycling"),
        ("mountain_biking", "cycling"),
        ("gravel_cycling", "cycling"),
        ("indoor_cycling", "cycling"),
        ("virtual_ride", "cycling"),
        ("cycling_road", "cycling"),  # cycling_* wildcard
        ("lap_swimming", "swimming"),
        ("open_water_swimming", "swimming"),
        ("swimming", "swimming"),
        ("strength_training", "strength"),
        ("indoor_cardio", "strength"),
        ("hiit", "strength"),
        ("walking", "walking"),
        ("casual_walking", "walking"),
        ("speed_walking", "walking"),
        ("hiking", "hiking"),
        ("yoga", "other"),
        ("", "other"),
    ],
)
def test_sport_family(type_key, family):
    assert sport_family(type_key) == family


# --- match_plan: garmin_link pass -------------------------------------------

def test_link_beats_heuristic():
    """The explicit workout link wins even when a heuristic-perfect candidate exists."""
    plans = [plan(1, garmin_workout_id="555")]
    perfect = act(10)  # same date, same sport, exact duration — but no link
    linked = act(11, date="2026-07-15", sport="cycling", duration_s=500.0,
                 extra_json='{"workoutId": 555}')
    result = match_plan(plans, [perfect, linked])
    assert result == [{"plan_id": 1, "matched_activity_id": 11, "match_method": "garmin_link"}]


def test_link_matches_int_vs_str_workout_id():
    plans = [plan(1, garmin_workout_id=123)]
    activities = [act(10, extra_json='{"workoutId": "123"}')]
    result = match_plan(plans, activities)
    assert result[0]["matched_activity_id"] == 10
    assert result[0]["match_method"] == "garmin_link"


def test_link_greedy_among_duplicate_workout_ids():
    plans = [
        plan(1, garmin_workout_id="W", duration_s=3600.0),
        plan(2, date="2026-07-18", garmin_workout_id="W", duration_s=1800.0),
    ]
    activities = [
        act(10, duration_s=1750.0, extra_json='{"workoutId": "W"}'),
        act(11, duration_s=3700.0, extra_json='{"workoutId": "W"}'),
    ]
    result = {r["plan_id"]: r for r in match_plan(plans, activities)}
    assert result[1]["matched_activity_id"] == 11  # delta 100
    assert result[2]["matched_activity_id"] == 10  # delta 50, assigned first
    assert result[1]["match_method"] == result[2]["match_method"] == "garmin_link"


def test_linked_activity_not_reused_by_heuristic():
    plans = [plan(1, garmin_workout_id="555"), plan(2)]
    only = act(10, extra_json='{"workoutId": 555}')  # heuristic-perfect for plan 2 too
    result = {r["plan_id"]: r for r in match_plan(plans, [only])}
    assert result[1]["matched_activity_id"] == 10
    assert result[1]["match_method"] == "garmin_link"
    assert result[2]["matched_activity_id"] is None
    assert result[2]["match_method"] is None


# --- match_plan: heuristic pass ---------------------------------------------

def test_heuristic_duration_boundary_25pct():
    plans = [plan(1, duration_s=3600.0)]
    assert match_plan(plans, [act(10, duration_s=4500.0)])[0]["matched_activity_id"] == 10
    assert match_plan(plans, [act(10, duration_s=2700.0)])[0]["matched_activity_id"] == 10
    assert match_plan(plans, [act(10, duration_s=4501.0)])[0]["matched_activity_id"] is None
    assert match_plan(plans, [act(10, duration_s=2699.0)])[0]["matched_activity_id"] is None


def test_heuristic_requires_same_date():
    plans = [plan(1)]
    result = match_plan(plans, [act(10, date="2026-07-18")])
    assert result[0]["matched_activity_id"] is None


def test_heuristic_matches_sport_family_not_exact_key():
    plans = [plan(1, sport="running")]
    assert match_plan(plans, [act(10, sport="treadmill_running")])[0]["match_method"] == "heuristic"
    assert match_plan(plans, [act(10, sport="cycling")])[0]["matched_activity_id"] is None


def test_heuristic_greedy_closest_duration_one_to_one():
    plans = [plan(1, duration_s=3600.0), plan(2, duration_s=3000.0)]
    activities = [act(10, duration_s=3450.0), act(11, duration_s=2900.0)]
    result = {r["plan_id"]: r for r in match_plan(plans, activities)}
    # (11→2) delta 100 assigned first, then (10→1) delta 150.
    assert result[1]["matched_activity_id"] == 10
    assert result[2]["matched_activity_id"] == 11


def test_heuristic_activity_used_at_most_once_stable():
    plans = [plan(1), plan(2)]  # identical plans, one activity
    result = match_plan(plans, [act(10)])
    assert result[0]["matched_activity_id"] == 10  # stable: first plan wins the tie
    assert result[1]["matched_activity_id"] is None


def test_planned_duration_from_planned_json():
    plans = [plan(1, duration_s=None, planned_json='{"duration_s": 1800}')]
    result = match_plan(plans, [act(10, duration_s=1900.0)])
    assert result[0]["matched_activity_id"] == 10
    assert result[0]["match_method"] == "heuristic"


def test_no_planned_duration_means_no_heuristic_match():
    plans = [plan(1, duration_s=None)]
    assert match_plan(plans, [act(10)])[0]["matched_activity_id"] is None


# --- match_plan: output shape -----------------------------------------------

def test_every_plan_entry_reported_in_input_order():
    plans = [plan(3), plan(7, date="2026-07-01"), plan(5, garmin_workout_id="9")]
    activities = [act(10)]
    result = match_plan(plans, activities)
    assert [r["plan_id"] for r in result] == [3, 7, 5]
    assert result[0] == {"plan_id": 3, "matched_activity_id": 10, "match_method": "heuristic"}
    assert result[1] == {"plan_id": 7, "matched_activity_id": None, "match_method": None}
    assert result[2] == {"plan_id": 5, "matched_activity_id": None, "match_method": None}


def test_empty_inputs():
    assert match_plan([], []) == []
    assert match_plan([], [act(10)]) == []
    assert match_plan([plan(1)], []) == [
        {"plan_id": 1, "matched_activity_id": None, "match_method": None}
    ]
