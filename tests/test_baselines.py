"""Tests for fartlek.analytics.baselines (DESIGN.md §3.2 #6, #9)."""
from __future__ import annotations

import pytest
from conftest import make_series

from fartlek.analytics.baselines import (
    MAD_SCALE,
    band_position,
    baseline,
    rhr_deviation,
    streak,
    zscore,
)

END = "2026-07-20"


# --- baseline ---------------------------------------------------------------

def test_baseline_known_mad():
    # values [10, 12, 14, 16, 100]: median 14, |x-med| = [4, 2, 0, 2, 86], MAD 2
    series = make_series(END, [10, 12, 14, 16, 100])
    base = baseline(series, END, window=7)
    assert base is not None
    assert base["median"] == 14
    assert base["mean"] == pytest.approx(30.4)
    assert base["mad_sd"] == pytest.approx(MAD_SCALE * 2)
    assert base["n"] == 5
    assert base["window"] == 7


def test_baseline_calendar_window_filter():
    # 10 points ending at END; window=7 keeps only the last 7 (start 2026-07-14)
    series = make_series(END, [100] * 3 + [1, 2, 3, 4, 5, 6, 7])
    base = baseline(series, END, window=7)
    assert base["n"] == 7
    assert base["median"] == 4
    assert base["mean"] == pytest.approx(4.0)


def test_baseline_window_boundaries_inclusive():
    series = [("2026-07-13", 99.0), ("2026-07-14", 1.0), ("2026-07-20", 2.0)]
    base = baseline(series, END, window=7)
    assert base["n"] == 2  # 07-14 in, 07-13 out
    assert base["mean"] == pytest.approx(1.5)


def test_baseline_excludes_points_after_end_date():
    series = [("2026-07-19", 5.0), ("2026-07-20", 5.0), ("2026-07-21", 999.0)]
    base = baseline(series, END, window=7)
    assert base["n"] == 2
    assert base["mean"] == pytest.approx(5.0)


def test_baseline_none_when_no_points():
    assert baseline([], END, window=7) is None
    # all data before the window
    assert baseline([("2026-01-01", 5.0)], END, window=7) is None


def test_baseline_constant_series_mad_sd_floor():
    base = baseline(make_series(END, [50.0] * 5), END, window=7)
    assert base["mad_sd"] == 1e-9


def test_baseline_gaps_ok_n_reports_actual_points():
    series = [("2026-07-14", 1.0), ("2026-07-17", 2.0), ("2026-07-20", 3.0)]
    base = baseline(series, END, window=7)
    assert base["n"] == 3


# --- zscore / band_position -------------------------------------------------

def test_zscore_robust():
    base = {"median": 14.0, "mad_sd": MAD_SCALE * 2}
    assert zscore(16.0, base) == pytest.approx(2 / (MAD_SCALE * 2))
    assert zscore(14.0, base) == 0.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (50.0, "in_band"),
        (51.0, "in_band"),
        (52.0, "in_band"),   # z = 1 boundary -> in_band
        (53.0, "high"),
        (54.0, "high"),      # z = 2 boundary -> high
        (54.1, "very_high"),
        (48.0, "in_band"),   # z = -1 boundary
        (47.0, "low"),
        (46.0, "low"),       # z = -2 boundary
        (45.0, "very_low"),
    ],
)
def test_band_position(value, expected):
    base = {"median": 50.0, "mad_sd": 2.0}
    assert band_position(value, base) == expected


# --- streak -----------------------------------------------------------------

def test_streak_counts_from_end():
    series = make_series(END, [1, 9, 9, 9])
    assert streak(series, lambda v: v > 5) == 3


def test_streak_full_series():
    series = make_series(END, [9, 9, 9])
    assert streak(series, lambda v: v > 5) == 3


def test_streak_zero_when_last_fails():
    series = make_series(END, [9, 9, 1])
    assert streak(series, lambda v: v > 5) == 0


def test_streak_empty_series():
    assert streak([], lambda v: True) == 0


def test_streak_breaks_on_calendar_gap():
    # 07-16, 07-17 pass; 07-18 missing; 07-19, 07-20 pass -> streak is 2
    series = [
        ("2026-07-16", 9.0),
        ("2026-07-17", 9.0),
        ("2026-07-19", 9.0),
        ("2026-07-20", 9.0),
    ]
    assert streak(series, lambda v: v > 5) == 2


# --- rhr_deviation ----------------------------------------------------------

def _rhr_series(trailing: list[float], today: float) -> list[tuple[str, float]]:
    return make_series(END, trailing + [today])


def test_rhr_ok():
    res = rhr_deviation(_rhr_series([50.0] * 30, 51.0), END)
    assert res["level"] == "ok"
    assert res["delta"] == pytest.approx(1.0)
    assert res["median30"] == 50.0
    assert res["sustained_days"] == 0
    assert res["n"] == 30


def test_rhr_caution_high_single_day():
    res = rhr_deviation(_rhr_series([50.0] * 30, 54.0), END)
    assert res["level"] == "caution"
    assert res["delta"] == pytest.approx(4.0)
    assert res["sustained_days"] == 1


def test_rhr_caution_boundary_delta_3():
    res = rhr_deviation(_rhr_series([50.0] * 30, 53.0), END)
    assert res["level"] == "caution"


def test_rhr_red_sustained_two_days():
    # yesterday + today at +6; yesterday sits inside the trailing window but
    # 29 baseline days keep median30 at 50
    res = rhr_deviation(_rhr_series([50.0] * 29 + [56.0], 56.0), END)
    assert res["level"] == "red"
    assert res["delta"] == pytest.approx(6.0)
    assert res["sustained_days"] == 2


def test_rhr_severe_but_not_sustained_is_caution():
    res = rhr_deviation(_rhr_series([50.0] * 30, 56.0), END)
    assert res["level"] == "caution"
    assert res["sustained_days"] == 1


def test_rhr_parasympathetic_watch_sustained_low():
    res = rhr_deviation(_rhr_series([50.0] * 29 + [44.0], 44.0), END)
    assert res["level"] == "parasympathetic_watch"
    assert res["delta"] == pytest.approx(-6.0)
    assert res["sustained_days"] == 2


def test_rhr_low_single_day_is_caution():
    res = rhr_deviation(_rhr_series([50.0] * 30, 44.0), END)
    assert res["level"] == "caution"
    assert res["sustained_days"] == 1


def test_rhr_red_exact_delta_5_sustained():
    res = rhr_deviation(_rhr_series([50.0] * 29 + [55.0], 55.0), END)
    assert res["level"] == "red"
    assert res["sustained_days"] == 2


def test_rhr_gap_breaks_sustained_streak():
    # elevated 07-18 and today 07-20, but 07-19 missing -> streak restarts at 1
    series = [
        *make_series("2026-07-17", [50.0] * 28),
        ("2026-07-18", 56.0),
        ("2026-07-20", 56.0),
    ]
    res = rhr_deviation(series, END)
    assert res["n"] == 29
    assert res["delta"] == pytest.approx(6.0)
    assert res["sustained_days"] == 1
    assert res["level"] == "caution"


def test_rhr_insufficient_data_n_below_14():
    res = rhr_deviation(_rhr_series([50.0] * 13, 60.0), END)
    assert res["level"] == "insufficient_data"
    assert res["n"] == 13
    assert res["sustained_days"] == 0
    assert res["delta"] == pytest.approx(10.0)  # still reported when computable


def test_rhr_insufficient_data_today_missing():
    series = make_series("2026-07-19", [50.0] * 30)  # ends yesterday
    res = rhr_deviation(series, END)
    assert res["level"] == "insufficient_data"
    assert res["delta"] is None
    assert res["n"] == 30
    assert res["median30"] == 50.0


def test_rhr_empty_series():
    res = rhr_deviation([], END)
    assert res == {"delta": None, "level": "insufficient_data",
                   "sustained_days": 0, "median30": None, "n": 0}


def test_rhr_median30_excludes_today():
    # today's spike must not drag the median
    res = rhr_deviation(_rhr_series([50.0] * 30, 80.0), END)
    assert res["median30"] == 50.0
    assert res["delta"] == pytest.approx(30.0)
