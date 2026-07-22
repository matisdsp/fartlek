"""Personal precedent flags (DESIGN.md §3.2 #5).

The value of this module is that it compares an athlete to themselves. The
risk is that it dresses up a single coincidence as a pattern, so most of these
tests are about staying silent and about never overstating how much evidence
there is.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from fartlek.analytics import precedent as pr


def series(start: str, values):
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


def days_from(start: str, n: int):
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


# --- episode detection ------------------------------------------------------

def test_consecutive_suppressed_days_form_one_episode():
    assert pr.find_hrv_episodes(days_from("2026-04-19", 4)) == ["2026-04-19"]


def test_short_dips_are_not_episodes():
    assert pr.find_hrv_episodes(days_from("2026-04-19", 2)) == []


def test_a_calendar_gap_splits_a_run():
    dips = days_from("2026-01-01", 4) + days_from("2026-03-01", 4)
    assert pr.find_hrv_episodes(dips) == ["2026-01-01", "2026-03-01"]


def test_nearby_episodes_merge():
    """The tail of a bad fortnight is not a second independent event."""
    dips = days_from("2026-04-01", 4) + days_from("2026-04-10", 4)
    assert pr.find_hrv_episodes(dips) == ["2026-04-01"]


def test_logged_illness_is_a_precedent_even_with_calm_sensors():
    """The athlete's own report outranks the sensors — that is the project
    invariant, and it applies to history as much as to today."""
    log = [{"date": "2026-04-19", "flag": "illness"},
           {"date": "2026-04-20", "flag": "illness"},
           {"date": "2026-06-01", "flag": None}]
    assert pr.episodes_from_log(log) == ["2026-04-19"]


def test_injuries_count_too_and_unflagged_notes_do_not():
    log = [{"date": "2026-02-01", "flag": "injury"},
           {"date": "2026-05-01", "flag": None},
           {"date": "2026-05-02", "rpe": 8}]
    assert pr.episodes_from_log(log) == ["2026-02-01"]


# --- mining -----------------------------------------------------------------

def test_mining_reads_the_fortnight_before_the_episode():
    mono = series("2026-04-01", [1.0] * 18 + [9.9] * 5)   # 04-19 onwards is after
    got = pr.mine(["2026-04-19"], {"monotony": mono})
    assert len(got) == 1
    stats = got[0]["metrics"]["monotony"]
    assert stats["max"] == pytest.approx(1.0), "values from the episode itself must not leak in"
    assert stats["n"] == pr.LOOKBACK_DAYS


def test_episode_without_history_is_dropped_not_blanked():
    assert pr.mine(["2026-01-01"], {"monotony": series("2026-06-01", [1.0] * 20)}) == []


def test_multiple_metrics_are_mined_together():
    got = pr.mine(["2026-04-19"], {
        "monotony": series("2026-04-01", [2.1] * 20),
        "ramp_pct": series("2026-04-01", [12.0] * 20),
    })
    assert set(got[0]["metrics"]) == {"monotony", "ramp_pct"}


# --- trigger levels ---------------------------------------------------------

def test_trigger_level_is_the_median_peak_across_episodes():
    """Median, not minimum: one calm fortnight before an illness — people do
    catch things at rest — would otherwise drag the level down until
    everything looks alarming."""
    precedents = [
        {"episode": "2026-01-10", "metrics": {"monotony": {"max": 2.2, "mean": 1.8, "n": 14}}},
        {"episode": "2026-03-10", "metrics": {"monotony": {"max": 0.9, "mean": 0.8, "n": 14}}},
        {"episode": "2026-04-19", "metrics": {"monotony": {"max": 2.0, "mean": 1.7, "n": 14}}},
    ]
    levels = pr.trigger_levels(precedents)
    assert levels["monotony"]["level"] == pytest.approx(2.0)
    assert levels["monotony"]["n"] == 3


# --- comparison -------------------------------------------------------------

def test_silent_without_any_precedent():
    """The correct state for most athletes most of the time — and it must not
    be dressed up as reassurance."""
    res = pr.compare({"monotony": 2.5}, {})
    assert res["silent"] is True
    assert res["statements"] == []
    assert "no prior episode" in res["reason"]


def test_exceeding_the_athletes_own_level_is_flagged():
    levels = {"monotony": {"level": 1.9, "n": 2, "episodes": ["a", "b"]}}
    res = pr.compare({"monotony": 2.4}, levels)
    assert res["exceeded"] == ["monotony"]
    assert "above your own pre-episode level" in res["statements"][0]


def test_being_clear_of_the_level_is_stated_positively():
    levels = {"monotony": {"level": 1.9, "n": 2, "episodes": ["a", "b"]}}
    res = pr.compare({"monotony": 1.2}, levels)
    assert res["exceeded"] == []
    assert "clear of your own pre-episode level" in res["statements"][0]


def test_single_precedent_is_never_presented_as_a_pattern():
    """n_precedents is what stops one coincidence reading as a rule."""
    levels = {"monotony": {"level": 1.9, "n": 1, "episodes": ["2026-04-19"]}}
    res = pr.compare({"monotony": 2.4}, levels)
    assert res["n_precedents"] == 1
    assert "1 episode" in res["statements"][0]
    assert "episodes" not in res["statements"][0]


def test_metrics_without_a_precedent_are_skipped():
    levels = {"monotony": {"level": 1.9, "n": 2, "episodes": ["a", "b"]}}
    res = pr.compare({"monotony": 1.0, "ramp_pct": 30.0}, levels)
    assert len(res["statements"]) == 1


def test_no_statement_claims_causation():
    """Sequence, not mechanism — causal claims belong to the attribution
    module's closed rule set."""
    levels = {"monotony": {"level": 1.9, "n": 3, "episodes": ["a", "b", "c"]}}
    res = pr.compare({"monotony": 2.4}, levels)
    for s in res["statements"]:
        assert "because" not in s.lower()
        assert "caused" not in s.lower()


# --- found on real data (2026-07-22) ---------------------------------------

def test_episodes_from_different_sources_merge_into_one_event():
    """The real case: HRV dipped 2026-04-18 and the athlete logged illness on
    04-19 — one bout of salmonella, sensed a day before it was reported.
    Counting it twice double-weights a single event in the trigger levels."""
    hrv = ["2026-04-18"]
    logged = ["2026-04-19"]
    assert pr.merge_episodes(hrv, logged) == ["2026-04-18"]


def test_distant_episodes_from_different_sources_stay_separate():
    assert pr.merge_episodes(["2026-01-05"], ["2026-06-01"]) == ["2026-01-05", "2026-06-01"]


def test_externally_caused_episodes_can_be_excluded_from_load_levels():
    """Food poisoning tells you nothing about load tolerance. Left in, its calm
    pre-episode fortnight drags the trigger level down until ordinary training
    reads as 'above your own level' — a false alarm manufactured by a bad meal.
    """
    precedents = [
        {"episode": "2026-01-10", "metrics": {"monotony": {"max": 2.2, "mean": 1.8, "n": 14}}},
        {"episode": "2026-04-19", "metrics": {"monotony": {"max": 1.1, "mean": 0.9, "n": 14}}},
    ]
    with_food_poisoning = pr.trigger_levels(precedents)
    without = pr.trigger_levels(precedents, exclude=["2026-04-19"])

    assert without["monotony"]["level"] > with_food_poisoning["monotony"]["level"]
    assert without["monotony"]["n"] == 1

    # Levels: 1.65 with the food poisoning averaged in, 2.2 without it.
    # A monotony of 1.8 — ordinary training — is alarming under the first and
    # clear under the second. That gap is the false alarm being removed.
    assert pr.compare({"monotony": 1.8}, with_food_poisoning)["exceeded"] == ["monotony"]
    assert pr.compare({"monotony": 1.8}, without)["exceeded"] == []
