"""Forward PMC projection and taper window (DESIGN.md §3.2 #17).

The projection must stay arithmetic on the athlete's own numbers: same step
function as the historical series, and a disclosed basis so a pattern-based
guess is never mistaken for a scheduled plan.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from fartlek.analytics import pmc as pmc_mod
from fartlek.analytics import projection as pj


def loads_from(start: str, values):
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


# --- the step is shared with history ---------------------------------------

def test_projection_uses_the_same_pmc_step_as_history():
    """A duplicated formula would let the projected and historical series drift
    apart — the same load must move CTL identically in both."""
    hist = pmc_mod.compute_pmc(loads_from("2026-01-01", [100.0] * 60))
    last = hist[-1]
    projected = pj.project(
        ctl=hist[-2]["ctl"], atl=hist[-2]["atl"], start_date=last["date"], days=1,
        scheduled={last["date"]: 100.0},
    )
    assert projected[0]["ctl"] == pytest.approx(last["ctl"])
    assert projected[0]["atl"] == pytest.approx(last["atl"])
    assert projected[0]["tsb"] == pytest.approx(last["tsb"])


# --- basis ------------------------------------------------------------------

def test_weekday_pattern_preserves_the_shape_of_a_week():
    """A flat average turns two rest days and a long run into three identical
    medium days, understating both peak fatigue and recovery."""
    week = [0.0, 120.0, 60.0, 0.0, 90.0, 200.0, 40.0]      # Mon..Sun
    loads = loads_from("2026-06-01", week * 4)              # 2026-06-01 is a Monday
    pattern = pj.weekday_pattern(loads, "2026-06-28")
    assert pattern[0] == pytest.approx(0.0)                 # Monday rest
    assert pattern[5] == pytest.approx(200.0)               # Saturday long
    assert len(pattern) == 7


def test_pattern_window_excludes_older_weeks():
    loads = loads_from("2026-01-01", [500.0] * 7) + loads_from("2026-06-01", [50.0] * 28)
    pattern = pj.weekday_pattern(loads, "2026-06-28", weeks=4)
    assert all(v == pytest.approx(50.0) for v in pattern.values())


def test_scheduled_workouts_win_over_the_pattern():
    series = pj.project(
        ctl=100.0, atl=100.0, start_date="2026-07-23", days=3,
        pattern={i: 50.0 for i in range(7)},
        scheduled={"2026-07-24": 300.0},
    )
    assert [r["basis"] for r in series] == ["pattern", "scheduled", "pattern"]
    assert series[1]["load"] == 300.0


def test_unknown_future_is_flagged_not_silently_a_rest_day():
    series = pj.project(ctl=100.0, atl=100.0, start_date="2026-07-23", days=2)
    assert [r["basis"] for r in series] == ["none", "none"]
    assert all(r["load"] == 0.0 for r in series)


def test_basis_mix_is_disclosed():
    loads = loads_from("2026-06-25", [80.0] * 28)
    res = pj.project_to_race(
        ctl=100.0, atl=90.0, today="2026-07-22", race_date="2026-07-29",
        daily_loads=loads, scheduled={"2026-07-25": 200.0},
    )
    assert res["basis"] == "mixed"
    assert res["scheduled_days"] == 1 and res["pattern_days"] == 6


def test_fully_scheduled_projection_says_so():
    sched = {(date(2026, 7, 22) + timedelta(days=i + 1)).isoformat(): 100.0 for i in range(7)}
    res = pj.project_to_race(ctl=100.0, atl=90.0, today="2026-07-22",
                             race_date="2026-07-29", daily_loads=[], scheduled=sched)
    assert res["basis"] == "scheduled" and res["pattern_days"] == 0


# --- direction of travel ----------------------------------------------------

def test_resting_raises_form_and_fades_fitness():
    """The taper's core tension, and the reason both numbers are reported."""
    loads = loads_from("2026-06-25", [100.0] * 28)
    res = pj.project_to_race(ctl=100.0, atl=100.0, today="2026-07-22",
                             race_date="2026-08-05", daily_loads=loads,
                             scheduled={(date(2026, 7, 22) + timedelta(days=i + 1)).isoformat(): 0.0
                                        for i in range(14)})
    assert res["form_race_pct"] > 0        # fatigue shed
    assert res["ctl_race"] < res["ctl_now"]  # fitness lost


def test_race_in_the_past_is_an_error_not_a_projection():
    res = pj.project_to_race(ctl=100.0, atl=90.0, today="2026-07-22",
                             race_date="2026-07-01", daily_loads=[])
    assert "error" in res
    assert pj.taper_guidance(res)["verdict"] == "unavailable"


# --- taper window -----------------------------------------------------------

def _projection(days_out, daily_load):
    today = date(2026, 7, 22)
    race = today + timedelta(days=days_out)
    loads = loads_from((today - timedelta(days=27)).isoformat(), [100.0] * 28)
    sched = {(today + timedelta(days=i + 1)).isoformat(): daily_load for i in range(days_out)}
    return pj.project_to_race(ctl=100.0, atl=100.0, today=today.isoformat(),
                              race_date=race.isoformat(), daily_loads=loads,
                              scheduled=sched)


def test_guidance_is_dormant_outside_the_window():
    res = pj.taper_guidance(_projection(pj.TAPER_WINDOW_DAYS + 1, 100.0))
    assert res["active"] is False and res["verdict"] == "not_yet"
    assert res["actions"] == []
    assert res["form_race_pct"] is not None   # still reported, just not acted on


def test_guidance_activates_inside_the_window():
    assert pj.taper_guidance(_projection(pj.TAPER_WINDOW_DAYS, 100.0))["active"] is True


def test_still_training_hard_reads_as_too_fatigued():
    res = pj.taper_guidance(_projection(10, 200.0))
    assert res["verdict"] == "too_fatigued"
    assert res["form_race_pct"] < pj.FRESH_BAND_PCT[0]
    assert any("reduce load" in a for a in res["actions"])


def test_complete_rest_bleeds_fitness_or_overshoots():
    res = pj.taper_guidance(_projection(21, 0.0))
    assert res["verdict"] in ("too_fresh", "fitness_bleeding")
    assert res["ctl_fade_pct"] > 0


def test_ctl_fade_ceiling_is_reported_separately_from_form():
    res = pj.taper_guidance(_projection(21, 0.0))
    assert res["fade_acceptable"] is (res["ctl_fade_pct"] <= pj.MAX_CTL_FADE_PCT)


def test_form_pct_is_none_without_fitness():
    assert pj.form_pct({"ctl": 0.0, "tsb": 5.0}) is None
    assert pj.form_pct({"ctl": 100.0, "tsb": 12.0}) == pytest.approx(12.0)
