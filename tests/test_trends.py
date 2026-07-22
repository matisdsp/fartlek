"""Trend significance (DESIGN.md §3.2 #7) — contract tests.

The Mann-Kendall / Sen / Hamed-Rao numbers below are NOT self-derived: they
come from pymannkendall 1.4.3 (the reference implementation of Hamed & Rao
1998), computed once on these fixtures and frozen here. That makes this an
external cross-check rather than the engine grading its own homework.

Fixtures are explicit literals on purpose. Any series whose Sen-detrended
values collapse onto exact ties is float-degenerate — rank order flips on
1e-15 differences — and would test floating-point luck, not the algorithm.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from fartlek.analytics import trends

# --- reference values from pymannkendall 1.4.3 -----------------------------

REFERENCE = {
    "rising_noisy": dict(
        data=[50.2, 49.8, 51.3, 50.9, 51.8, 50.4, 52.6, 51.9, 53.1, 52.2,
              53.8, 52.9, 54.4, 53.6, 55.1, 54.2, 55.9, 54.8, 56.4, 55.7,
              57.2, 56.1, 57.9, 56.8, 58.5, 57.4, 59.1, 58.2, 59.8, 58.9],
        s=379, var_s=3141.6666666667, slope=0.327272727273,
        tau=0.871264367816, var_ratio=0.063315783935, p=0.0,
    ),
    "autocorrelated": dict(
        data=[60.15, 60.8841275135, 62.1101616498, 63.6582202788, 65.3075806833,
              66.8215268236, 67.9847215944, 68.6372038196, 68.6996426045,
              68.1858619395, 67.2006581966, 65.9232550857, 64.5790081725,
              63.4038261889, 62.6069262321, 62.3378029848, 62.6626267921,
              63.5537968188, 64.8942986146, 66.4961781226, 68.1302154925,
              69.5621138547, 70.589489644, 71.0738245152, 70.9623343433,
              70.296302677, 69.2045629734, 67.8831586283, 66.5643920345,
              65.4801491421],
        s=151, var_s=3141.6666666667, slope=0.179057913471,
        tau=0.347126436782, var_ratio=0.941980385451, p=0.005827377390,
    ),
    "flat_alternating": dict(
        data=[45 + (1.5 if i % 2 == 0 else -1.5) for i in range(30)],
        s=-15, var_s=2325.0, slope=0.0,
        tau=-0.034482758621, var_ratio=0.056403940887, p=0.221504811411,
    ),
    "integer_ties": dict(
        data=[44, 45, 44, 44, 46, 45, 44, 43, 44, 45, 45, 46, 45, 44, 45,
              46, 47, 46, 45, 46, 46, 47, 46, 47, 48, 47, 46, 47, 48, 47],
        s=259, var_s=2974.3333333333, slope=0.117647058824,
        tau=0.595402298851, var_ratio=0.353799709635, p=0.0,
    ),
}

START = date(2026, 1, 1)


def _dated(values, start=START, step=1):
    return [((start + timedelta(days=i * step)).isoformat(), float(v))
            for i, v in enumerate(values)]


def _points(values, start=START, step=1):
    return [(start + timedelta(days=i * step), float(v)) for i, v in enumerate(values)]


@pytest.mark.parametrize("name", sorted(REFERENCE))
def test_matches_reference_implementation(name):
    """S, Var(S), Sen slope, tau, the Hamed-Rao variance ratio and the
    corrected p-value all reproduce pymannkendall on the same input."""
    ref = REFERENCE[name]
    values = [float(v) for v in ref["data"]]
    pts = _points(values)

    s = trends._mann_kendall_s(values)
    var_s = trends._var_s(values)
    slope = trends.sens_slope(pts)
    factor = trends.hamed_rao_factor(pts, slope)
    p = trends._two_sided_p(s, var_s * factor)

    assert s == pytest.approx(ref["s"])
    assert var_s == pytest.approx(ref["var_s"], abs=1e-6)
    assert slope == pytest.approx(ref["slope"], abs=1e-10)
    assert factor == pytest.approx(ref["var_ratio"], abs=1e-9)
    assert p == pytest.approx(ref["p"], abs=1e-9)

    n = len(values)
    tau = s / (n * (n - 1) / 2)
    assert tau == pytest.approx(ref["tau"], abs=1e-10)


def test_tie_correction_lowers_variance():
    """Var(S) with ties must be strictly below the untied formula — the
    integer_ties fixture is the check that the tie term is actually applied."""
    n = 30
    untied = n * (n - 1) * (2 * n + 5) / 18.0
    assert trends._var_s([float(v) for v in REFERENCE["integer_ties"]["data"]]) < untied
    assert trends._var_s(REFERENCE["autocorrelated"]["data"]) == pytest.approx(untied)


# --- the correction's purpose: never manufacture significance --------------

# AR(1), phi=0.75, over deterministic LCG noise with a mild rise — the shape a
# daily physiological series actually has (lag-1 rank ACF ~ +0.67).
AR1_SERIES = [
    50.3724, 49.9308, 50.518, 49.6248, 49.9684, 50.1915, 50.6595, 50.4825,
    50.1079, 50.139, 51.2756, 50.5912, 50.4082, 51.1306, 52.0531, 53.2506,
    53.7293, 53.3112, 53.207, 53.3664, 52.6148, 53.1704, 53.6469, 54.666,
    53.8992, 53.6335, 54.0953, 54.5472, 53.5551, 52.464, 53.5162, 52.556,
    53.2009, 52.8703, 53.3982, 53.7252, 53.7178, 53.2266, 54.0003, 54.7799,
]


def test_positive_autocorrelation_makes_the_test_more_conservative():
    """A trend riding on positively autocorrelated noise must come out with a
    LARGER p than the uncorrected test. This is the whole reason the module
    exists: plain Mann-Kendall over-detects on physiological series, which are
    autocorrelated by construction (today's HRV resembles yesterday's)."""
    pts = _points(AR1_SERIES)
    s = trends._mann_kendall_s(AR1_SERIES)
    var_s = trends._var_s(AR1_SERIES)
    factor = trends.hamed_rao_factor(pts, trends.sens_slope(pts))

    uncorrected = trends._two_sided_p(s, var_s)
    corrected = trends._two_sided_p(s, var_s * factor)

    assert factor > 1.0, "positive autocorrelation must inflate Var(S)"
    assert corrected > uncorrected
    # Concretely: 1e-07 -> 1.7e-03. Still significant here, but three orders of
    # magnitude less certain — enough to flip borderline findings.
    assert uncorrected < 1e-5 < corrected < 0.05


def test_negative_autocorrelation_shrinks_variance():
    """The correction is two-sided: an alternating series carries more
    information than white noise, so Var(S) legitimately shrinks."""
    values = REFERENCE["flat_alternating"]["data"]
    pts = _points(values)
    assert trends.hamed_rao_factor(pts, trends.sens_slope(pts)) < 1.0


def test_degenerate_factor_falls_back_to_no_correction():
    """A factor that comes out non-positive would flip the test's sign; it must
    fall back to 1.0 (no correction) instead of inventing confidence."""
    assert trends.hamed_rao_factor(_points([1.0, 2.0, 3.0]), 1.0) == 1.0
    flat = _points([7.0] * 25)
    assert trends.hamed_rao_factor(flat, 0.0) > 0


# --- Sen's slope is calendar-correct ---------------------------------------

def test_sens_slope_is_per_day_not_per_sample():
    """Points every 2 days must halve the per-day slope — the gap-correctness
    that makes trends comparable across metrics with different cadences."""
    daily = trends.sens_slope(_points([0, 2, 4, 6, 8, 10]))
    every_other = trends.sens_slope(_points([0, 2, 4, 6, 8, 10], step=2))
    assert daily == pytest.approx(2.0)
    assert every_other == pytest.approx(1.0)


def test_sens_slope_ignores_duplicate_dates():
    assert trends.sens_slope([(START, 1.0)]) == 0.0
    assert trends.sens_slope([]) == 0.0


# --- SWC: the practical gate ------------------------------------------------

def test_default_swc_is_half_the_90d_mad_sd():
    pts = _points([50.0] * 30)
    swc, basis = trends.swc_for("sleep_score", pts, mad_sd=8.0)
    assert swc == pytest.approx(4.0)
    assert "MAD-SD" in basis


def test_swc_falls_back_to_window_when_no_baseline():
    pts = _points([50.0, 54.0] * 15)
    swc, basis = trends.swc_for("sleep_score", pts, mad_sd=None)
    assert swc > 0
    assert "no 90d baseline" in basis


@pytest.mark.parametrize(
    "metric, mad_sd, expected, basis_fragment",
    [
        ("resting_hr", 1.0, 2.0, "floor"),        # floor wins over 0.5x1.0
        ("resting_hr", 20.0, 10.0, "MAD-SD"),     # default wins over the floor
        ("vo2max", 99.0, 1.0, "fixed"),           # absolute, baseline ignored
    ],
)
def test_named_swc_exceptions(metric, mad_sd, expected, basis_fragment):
    swc, basis = trends.swc_for(metric, _points([50.0] * 30), mad_sd=mad_sd)
    assert swc == pytest.approx(expected)
    assert basis_fragment in basis


def test_ef_swc_is_relative_to_level():
    """EF noise is 3% of the level, so the threshold must scale with it."""
    low, _ = trends.swc_for("ef", _points([1.30] * 30), mad_sd=1.0)
    high, _ = trends.swc_for("ef", _points([2.60] * 30), mad_sd=1.0)
    assert low == pytest.approx(0.039)
    assert high == pytest.approx(2 * low)


def _pstdev(values):
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def test_hrv_swc_uses_classical_sd_not_mad():
    """HRV literature defines the SWC in classical SD, so the 90d MAD-SD
    baseline must be ignored for this metric even when one is available."""
    values = [3.9, 4.1, 4.0, 4.3, 3.8] * 6
    swc, basis = trends.swc_for("hrv_ln_rmssd", _points(values), mad_sd=99.0)
    assert swc == pytest.approx(0.5 * _pstdev(values), rel=1e-9)
    assert "SD" in basis


# --- analyze(): both gates, and the sentence contract ----------------------

def test_suppressed_below_minimum_points():
    res = trends.analyze("sleep_score", _dated([50] * 20), "2026-01-20", 20)
    assert res["suppressed"] and res["significant"] is False
    assert res["n"] == 20
    assert "not enough data" in res["sentence"]
    assert "21" in res["sentence"]


def test_statistically_significant_but_trivial_change_is_not_reported():
    """p can clear 0.05 on a monotone drift far too small to matter; the SWC
    gate is what stops it being called a trend."""
    values = [50 + 0.001 * i for i in range(30)]
    res = trends.analyze("sleep_score", _dated(values), "2026-01-30", 30, mad_sd=10.0)
    assert res["p_value"] < 0.05
    assert res["significant"] is False
    assert res["direction"] == "flat"
    assert "smallest worthwhile change" in res["sentence"]


def test_large_change_that_is_not_statistically_significant():
    """Big swings with no monotone direction must not be called a trend."""
    values = [50, 70, 45, 75, 40, 72, 48, 68, 44, 74] * 3
    res = trends.analyze("sleep_score", _dated(values), "2026-01-30", 30, mad_sd=1.0)
    assert res["p_value"] >= 0.05
    assert res["significant"] is False


def test_real_trend_clears_both_gates():
    res = trends.analyze(
        "sleep_score", _dated(REFERENCE["rising_noisy"]["data"]), "2026-01-30", 30,
        mad_sd=2.0, unit=" pts",
    )
    assert res["significant"] is True
    assert res["direction"] == "rising"
    assert res["p_value"] < 0.05
    assert res["change"] == pytest.approx(0.327272727273 * 30, abs=1e-6)
    assert "significant" in res["sentence"] and "p=" in res["sentence"]


def test_smoothed_metric_never_speaks_of_significance():
    """VO2max is Garmin-smoothed: a p-value on it would overstate confidence."""
    values = [50 + 0.25 * i for i in range(30)]
    res = trends.analyze("vo2max", _dated(values), "2026-01-30", 30, mad_sd=2.0)
    assert res["smoothed"] is True
    assert res["p_value"] is None
    assert res["significant"] is True          # practical gate alone
    assert "p=" not in res["sentence"]
    assert "(significant" not in res["sentence"]   # never CLAIMS significance
    assert "Garmin-smoothed" in res["sentence"]


def test_every_result_carries_a_sentence():
    """§3.2 #7: output is always a sentence, never a bare p-value."""
    cases = [
        trends.analyze("sleep_score", _dated([50] * 5), "2026-01-05", 5),
        trends.analyze("vo2max", _dated([50 + 0.2 * i for i in range(30)]), "2026-01-30", 30),
        trends.analyze("resting_hr", _dated([44] * 30), "2026-01-30", 30, mad_sd=1.0),
    ]
    for res in cases:
        assert isinstance(res["sentence"], str) and res["sentence"].strip()
        assert not res["sentence"].startswith("p=")


def test_window_selects_points_by_calendar_not_position():
    """Points outside the window are excluded even when the series is longer."""
    values = list(range(60))
    series = _dated(values)                      # 2026-01-01 .. 2026-03-01
    res = trends.analyze("sleep_score", series, "2026-01-30", 30, mad_sd=1.0)
    assert res["n"] == 30
    assert res["first"] == 0.0 and res["last"] == 29.0


def test_gappy_series_still_reports_per_day_slope():
    """Every other day present, rising 2 units per sample = 1 unit per DAY.
    A per-sample slope would report 2 and overstate the trend by 2x."""
    series = _dated([2 * i for i in range(30)], step=2)
    res = trends.analyze("sleep_score", series, series[-1][0], 60, mad_sd=0.1)
    assert res["n"] == 30
    assert res["slope_per_day"] == pytest.approx(1.0)


def test_falling_metric_is_reported_as_falling():
    values = [60 - 0.3 * i for i in range(30)]
    res = trends.analyze("resting_hr", _dated(values), "2026-01-30", 30,
                         mad_sd=1.0, unit=" bpm")
    assert res["direction"] == "falling"
    assert res["change"] < 0
    assert "down" in res["sentence"] and "bpm" in res["sentence"]
