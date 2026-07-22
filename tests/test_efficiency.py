"""Aerobic efficiency from laps (DESIGN.md §3.2 #12, #13).

The contract under test is that the three confounders which move HR-at-pace
more than fitness does — terrain, cardiac drift, heat — are handled explicitly
rather than averaged away, and that a lap whose HR does not belong to its pace
is rejected instead of corrected.
"""
from __future__ import annotations

import pytest

from fartlek.analytics import efficiency as eff

# 6:00/km = 2.7778 m/s; 5:00/km = 3.3333 m/s; 7:00/km = 2.381 m/s
PACE_5, PACE_6, PACE_7 = 300.0, 360.0, 420.0
SPEED_5, SPEED_6, SPEED_7 = 1000 / 300, 1000 / 360, 1000 / 420


def lap(index=0, *, activity_id=1, distance=1000.0, moving=360.0, hr=140,
        speed=SPEED_6, gap=None, temp=None, itype=None, date="2026-07-01"):
    return {
        "activity_id": activity_id, "lap_index": index, "distance_m": distance,
        "duration_s": moving, "moving_s": moving, "avg_hr": hr,
        "avg_speed": speed, "gap_speed": gap, "temp_c": temp,
        "intensity_type": itype, "date": date,
    }


# --- lap primitives ---------------------------------------------------------

def test_grade_adjusted_speed_is_preferred_over_raw():
    """A 6:00/km lap up a hill is not a 6:00/km lap. GAP is the honest number."""
    uphill = lap(speed=SPEED_6, gap=SPEED_5)
    assert eff.lap_speed(uphill) == (pytest.approx(SPEED_5), "gap")
    assert eff.lap_pace_s_per_km(uphill) == pytest.approx(PACE_5)
    assert eff.lap_speed(uphill, prefer_gap=False) == (pytest.approx(SPEED_6), "raw")


def test_speed_falls_back_to_distance_over_time():
    downhill = lap(speed=None, distance=1000.0, moving=300.0)
    speed, basis = eff.lap_speed(downhill)
    assert speed == pytest.approx(SPEED_5) and basis == "derived"


def test_speed_is_none_when_nothing_is_derivable():
    assert eff.lap_speed(lap(speed=None, distance=None, moving=None)) is None
    assert eff.lap_ef(lap(speed=None, distance=None, moving=None)) is None
    assert eff.lap_ef(lap(hr=None)) is None


def test_ef_is_metres_per_minute_per_beat():
    assert eff.lap_ef(lap(speed=SPEED_6, hr=140)) == pytest.approx(SPEED_6 * 60 / 140)


def test_hot_lap_detection_is_inclusive_of_the_threshold():
    assert eff.lap_is_hot(lap(temp=eff.HOT_TEMP_C))
    assert not eff.lap_is_hot(lap(temp=eff.HOT_TEMP_C - 0.1))
    assert not eff.lap_is_hot(lap(temp=None))


# --- pace-band qualification ------------------------------------------------

def test_laps_outside_the_band_are_excluded():
    laps = [lap(0, speed=SPEED_6), lap(1, speed=1000 / 240)]  # 4:00/km
    kept, rejected = eff.qualify_pace_band_laps(laps, PACE_5, PACE_7)
    assert len(kept) == 1 and rejected["out_of_band"] == 1


def test_interval_recovery_laps_are_excluded():
    """A marked recovery lap carries the previous rep's HR, not its own pace's."""
    laps = [lap(0, itype="INTERVAL_ACTIVE", speed=SPEED_6),
            lap(1, itype="INTERVAL_REST", speed=SPEED_7, hr=165)]
    kept, rejected = eff.qualify_pace_band_laps(laps, PACE_5, PACE_7)
    assert len(kept) == 1 and rejected["interval_recovery"] == 1


def test_lap_following_a_much_faster_lap_is_excluded():
    """HR lags effort by a minute or more: the jog after a rep reads 6:30/km at
    interval HR. Including it would inflate 'HR at easy pace' for anyone who
    does intervals."""
    laps = [lap(0, speed=1000 / 200), lap(1, speed=SPEED_6, hr=170)]  # 3:20 then 6:00
    kept, rejected = eff.qualify_pace_band_laps(laps, PACE_5, PACE_7)
    assert kept == [] and rejected["hr_contaminated"] == 1


def test_contamination_check_does_not_leak_across_sessions():
    """The last lap of one session must not disqualify the first of the next."""
    laps = [lap(9, activity_id=1, speed=1000 / 200),
            lap(0, activity_id=2, speed=SPEED_6)]
    kept, _ = eff.qualify_pace_band_laps(laps, PACE_5, PACE_7)
    assert [k["activity_id"] for k in kept] == [2]


def test_short_laps_and_missing_hr_are_excluded():
    laps = [lap(0, distance=200.0), lap(1, hr=None), lap(2)]
    kept, rejected = eff.qualify_pace_band_laps(laps, PACE_5, PACE_7)
    assert len(kept) == 1
    assert rejected["too_short"] == 1 and rejected["no_hr"] == 1


def test_hot_laps_are_kept_by_default_and_excludable_on_request():
    laps = [lap(0, temp=28.0), lap(1, temp=15.0)]
    kept, _ = eff.qualify_pace_band_laps(laps, PACE_5, PACE_7)
    assert len(kept) == 2
    kept_cool, rejected = eff.qualify_pace_band_laps(laps, PACE_5, PACE_7, exclude_hot=True)
    assert len(kept_cool) == 1 and rejected["hot"] == 1


# --- aggregation ------------------------------------------------------------

def test_averages_are_weighted_by_lap_duration():
    """One 60-minute lap must outweigh one 6-minute lap, or short laps
    dominate the athlete's own history."""
    laps = [lap(0, moving=3600.0, hr=130), lap(1, moving=360.0, hr=160)]
    res = eff.hr_at_pace(laps, PACE_5, PACE_7)
    unweighted = (130 + 160) / 2
    assert res["n_laps"] == 2
    assert res["avg_hr"] < unweighted
    assert res["avg_hr"] == pytest.approx((130 * 3600 + 160 * 360) / 3960)
    assert res["minutes"] == pytest.approx(66.0)


def test_empty_result_reports_why_nothing_qualified():
    res = eff.hr_at_pace([lap(0, speed=1000 / 240)], PACE_5, PACE_7)
    assert res["n_laps"] == 0 and res["avg_hr"] is None
    assert res["rejected"]["out_of_band"] == 1


def test_gap_and_hot_shares_are_reported_for_disclosure():
    laps = [lap(0, gap=SPEED_6, temp=28.0), lap(1, gap=None, temp=10.0)]
    res = eff.hr_at_pace(laps, PACE_5, PACE_7)
    assert res["gap_share"] == pytest.approx(0.5)
    assert res["hot_share"] == pytest.approx(0.5)


def test_sessions_are_counted_not_just_laps():
    laps = [lap(0, activity_id=1), lap(1, activity_id=1), lap(0, activity_id=2)]
    res = eff.hr_at_pace(laps, PACE_5, PACE_7)
    assert res["n_laps"] == 3 and res["n_sessions"] == 2


def test_bucketing_by_month_and_week():
    laps = [lap(0, activity_id=1, date="2026-05-04"), lap(0, activity_id=2, date="2026-06-15")]
    monthly = eff.hr_at_pace_by_period(laps, PACE_5, PACE_7)
    assert list(monthly) == ["2026-05", "2026-06"]
    weekly = eff.hr_at_pace_by_period(laps, PACE_5, PACE_7, period="week")
    assert list(weekly) == ["2026-W19", "2026-W25"]


# --- session EF / decoupling / durability -----------------------------------

def _even_session(n=12, *, hr=140, moving=300.0, **kw):
    return [lap(i, moving=moving, hr=hr, **kw) for i in range(n)]


def test_steady_session_qualifies():
    res = eff.session_efficiency(_even_session())
    assert res["steady"] is True and res["reason"] is None
    assert res["ef"] == pytest.approx(SPEED_6 * 60 / 140)


def test_warmup_laps_are_excluded_from_the_measurement():
    """§3.2 #12 drops the first 10 minutes: HR has not yet caught up with pace,
    which makes the opening laps look artificially efficient."""
    laps = [lap(0, moving=600.0, hr=100)] + _even_session(6)
    res = eff.session_efficiency(laps)
    assert res["n_laps"] == 6
    assert res["ef"] == pytest.approx(SPEED_6 * 60 / 140)


def test_short_session_is_not_steady():
    res = eff.session_efficiency(_even_session(4, moving=300.0))
    assert res["steady"] is False
    assert "shorter than" in res["reason"]


def test_variable_pace_session_is_not_steady():
    """Intervals must never enter an EF trend: their EF is a meaningless blend."""
    laps = [lap(i, moving=300.0, speed=(1000 / 200 if i % 2 else SPEED_7)) for i in range(12)]
    res = eff.session_efficiency(laps)
    assert res["steady"] is False and "variable" in res["reason"]


def test_session_above_the_aerobic_ceiling_is_not_steady():
    res = eff.session_efficiency(_even_session(hr=175), z2_ceiling_hr=150)
    assert res["steady"] is False and "aerobic ceiling" in res["reason"]
    assert res["easy_lap_share"] == 0.0


def test_decoupling_is_positive_when_hr_drifts_up():
    """Same pace, higher HR in the second half = the classic durability
    shortfall. Sign matters: negative would read as improving mid-run.

    The leading 10-minute lap is the warm-up exclusion, so the two halves are
    exactly 6 laps at 135 bpm and 6 at 150 — otherwise the split lands
    mid-block and the expected value stops being readable.
    """
    laps = ([lap(0, moving=eff.WARMUP_EXCLUDE_S, hr=110)]
            + [lap(i + 1, moving=300.0, hr=(135 if i < 6 else 150)) for i in range(12)])
    res = eff.session_efficiency(laps)
    assert res["n_laps"] == 12
    assert res["decoupling"] > 0
    assert res["decoupling"] == pytest.approx(1 - (135 / 150), abs=1e-9)


def test_decoupling_is_none_without_two_halves():
    assert eff.session_efficiency([lap(0)])["decoupling"] is None


def test_durability_only_for_long_runs():
    short = eff.session_efficiency(_even_session(10, moving=300.0))   # 50 min
    assert short["durability"] is None
    long_run = eff.session_efficiency(_even_session(24, moving=300.0))  # 2 h
    assert long_run["durability"] == pytest.approx(1.0)


def test_durability_below_one_when_the_end_is_worse():
    laps = [lap(i, moving=300.0, hr=(135 if i < 8 else 155)) for i in range(24)]
    res = eff.session_efficiency(laps)
    assert res["durability"] < 1.0


# --- trend series -----------------------------------------------------------

def test_hot_sessions_are_excluded_from_the_trend_series():
    """Leaving them in reads as a fitness loss every summer."""
    cool = _even_session(12, temp=15.0)
    hot = [lap(i, activity_id=2, moving=300.0, temp=30.0, date="2026-07-15")
           for i in range(12)]
    series = eff.ef_trend_series(cool + hot)
    assert [d for d, _ in series] == ["2026-07-01"]
    assert len(eff.ef_trend_series(cool + hot, include_hot=True)) == 2


def test_non_steady_sessions_never_enter_the_trend():
    intervals = [lap(i, moving=300.0, speed=(1000 / 200 if i % 2 else SPEED_7))
                 for i in range(12)]
    assert eff.ef_trend_series(intervals) == []


def test_trend_series_is_sorted_by_date():
    a = [lap(i, activity_id=1, moving=300.0, date="2026-06-01") for i in range(12)]
    b = [lap(i, activity_id=2, moving=300.0, date="2026-05-01") for i in range(12)]
    assert [d for d, _ in eff.ef_trend_series(a + b)] == ["2026-05-01", "2026-06-01"]
