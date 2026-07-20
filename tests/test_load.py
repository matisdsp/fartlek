"""Tests for fartlek.analytics.load (DESIGN.md §3.1 canonical load currency)."""
from __future__ import annotations

import pytest

from fartlek.analytics.load import (
    convert_watch_rpe,
    edwards_trimp,
    fit_calibration,
    resolve_load,
)


def act(**kw) -> dict:
    """Minimal activity dict in schema.sql shape."""
    base = {"activity_id": 1, "date": "2026-07-01", "sport": "running"}
    base.update(kw)
    return base


def zones(z1=None, z2=None, z3=None, z4=None, z5=None) -> dict:
    return {"hr_z1_s": z1, "hr_z2_s": z2, "hr_z3_s": z3, "hr_z4_s": z4, "hr_z5_s": z5}


# --- edwards_trimp -----------------------------------------------------------

class TestEdwardsTrimp:
    def test_known_value(self):
        # 10 min in each zone: 10*1 + 10*2 + 10*3 + 10*4 + 10*5 = 150
        a = act(**zones(600, 600, 600, 600, 600))
        assert edwards_trimp(a) == pytest.approx(150.0)

    def test_all_zones_null_returns_none(self):
        assert edwards_trimp(act(**zones())) is None
        assert edwards_trimp(act()) is None  # fields absent entirely

    def test_partial_nulls_count_as_zero(self):
        # 30 min z2 only: 30*2 = 60
        a = act(**zones(z2=1800.0))
        assert edwards_trimp(a) == pytest.approx(60.0)

    def test_all_zero_seconds_is_zero_not_none(self):
        a = act(**zones(0.0, 0.0, 0.0, 0.0, 0.0))
        assert edwards_trimp(a) == pytest.approx(0.0)


# --- fit_calibration ---------------------------------------------------------

def overlap(sport: str, load: float, trimp: float, i: int = 0) -> dict:
    return act(activity_id=i, sport=sport, load=load, trimp=trimp, load_source="garmin")


class TestFitCalibration:
    def test_regression_through_origin_known_numbers(self):
        # 10 pairs, trimp = 10..100, load = 2*trimp + alternating ±10 noise.
        pairs = [(10.0 * k, 20.0 * k + (10 if k % 2 else -10)) for k in range(1, 11)]
        acts = [overlap("running", ld, t, i) for i, (t, ld) in enumerate(pairs)]
        # factor = Σ(load·trimp)/Σ(trimp²)
        num = sum(ld * t for t, ld in pairs)
        den = sum(t * t for t, _ in pairs)
        cal = fit_calibration(acts)
        assert cal["running"]["method"] == "regression"
        assert cal["running"]["n"] == 10
        assert cal["running"]["factor"] == pytest.approx(num / den)
        assert cal["running"]["factor"] == pytest.approx(1.987012987, abs=1e-6)

    def test_exact_proportional_pairs_recover_factor(self):
        acts = [overlap("cycling", 2.5 * t, t, i) for i, t in enumerate(range(10, 130, 10))]
        cal = fit_calibration(acts)
        assert cal["cycling"]["method"] == "regression"
        assert cal["cycling"]["factor"] == pytest.approx(2.5)

    def test_median_ratio_below_ten_pairs(self):
        acts = [
            overlap("running", 100.0, 100.0, 1),  # ratio 1.0
            overlap("running", 200.0, 100.0, 2),  # ratio 2.0
            overlap("running", 400.0, 100.0, 3),  # ratio 4.0
        ]
        cal = fit_calibration(acts)
        assert cal["running"] == {"method": "median_ratio", "factor": 2.0, "n": 3}

    def test_nine_pairs_still_median_ratio(self):
        acts = [overlap("running", 3.0 * t, t, i) for i, t in enumerate(range(10, 100, 10))]
        cal = fit_calibration(acts)
        assert cal["running"]["method"] == "median_ratio"
        assert cal["running"]["n"] == 9
        assert cal["running"]["factor"] == pytest.approx(3.0)

    def test_sports_are_independent_and_absent_sport_has_no_entry(self):
        acts = [overlap("running", 2.0 * t, t, i) for i, t in enumerate(range(10, 120, 10))]
        acts += [overlap("cycling", 40.0, 10.0, 100)]
        cal = fit_calibration(acts)
        assert cal["running"]["method"] == "regression"
        assert cal["running"]["factor"] == pytest.approx(2.0)
        assert cal["cycling"] == {"method": "median_ratio", "factor": 4.0, "n": 1}
        assert "swimming" not in cal

    def test_ignores_incomplete_pairs(self):
        acts = [
            act(sport="running", load=None, trimp=100.0),          # no garmin load
            act(sport="running", load=100.0, trimp=None),          # no trimp, no zones
            act(sport="running", load=100.0, trimp=0.0),           # zero trimp
            act(sport="running", load=100.0, trimp=50.0,
                load_source="estimated"),                          # non-garmin load
        ]
        assert fit_calibration(acts) == {}

    def test_trimp_computed_from_zones_when_field_missing(self):
        # 60 min z1 → TRIMP 60; load 120 → ratio 2.0
        a = act(sport="running", load=120.0, **zones(z1=3600.0))
        cal = fit_calibration([a])
        assert cal["running"]["factor"] == pytest.approx(2.0)
        assert cal["running"]["n"] == 1

    def test_empty_input(self):
        assert fit_calibration([]) == {}


# --- resolve_load ladder -----------------------------------------------------

CAL = {
    "running": {
        "method": "regression", "factor": 2.0, "n": 12,
        # sRPE is calibrated separately from TRIMP (different unit scales)
        "srpe_method": "regression", "srpe_factor": 2.0, "srpe_n": 12,
    }
}
MEDIANS = {"running": 5.0}  # load-per-minute


class TestResolveLoad:
    def test_garmin_load_passes_through_even_with_zones_and_rpe(self):
        a = act(load=123.4, rpe=8, duration_s=3600.0, **zones(600, 600, 600, 600, 600))
        assert resolve_load(a, CAL, MEDIANS) == (123.4, "garmin")

    def test_trimp_calibrated(self):
        a = act(**zones(600, 600, 600, 600, 600))  # TRIMP 150
        load, source = resolve_load(a, CAL, MEDIANS)
        assert (load, source) == (pytest.approx(300.0), "trimp_calibrated")

    def test_trimp_uncalibrated_when_sport_has_no_entry(self):
        a = act(sport="swimming", **zones(600, 600, 600, 600, 600))
        assert resolve_load(a, CAL, MEDIANS) == (pytest.approx(150.0), "trimp_uncalibrated")

    def test_trimp_beats_rpe(self):
        a = act(rpe=9, duration_s=3600.0, **zones(z2=1800.0))  # TRIMP 60
        load, source = resolve_load(a, CAL, MEDIANS)
        assert (load, source) == (pytest.approx(120.0), "trimp_calibrated")

    def test_srpe_calibrated(self):
        # rpe 6 × 60 min = 360, × factor 2.0 = 720
        a = act(rpe=6, duration_s=3600.0)
        assert resolve_load(a, CAL, MEDIANS) == (pytest.approx(720.0), "srpe_calibrated")

    def test_srpe_uncalibrated(self):
        a = act(sport="swimming", rpe=5, duration_s=1800.0)
        assert resolve_load(a, CAL, MEDIANS) == (pytest.approx(150.0), "srpe_uncalibrated")

    def test_estimated_from_sport_median(self):
        # 5.0 load/min × 40 min = 200
        a = act(duration_s=2400.0)
        assert resolve_load(a, CAL, MEDIANS) == (pytest.approx(200.0), "estimated")

    def test_none_when_no_history_in_sport(self):
        a = act(sport="yoga", duration_s=2400.0)
        assert resolve_load(a, CAL, MEDIANS) == (0.0, "none")

    def test_rpe_without_duration_falls_through(self):
        a = act(sport="yoga", rpe=7)  # no duration → sRPE and estimate impossible
        assert resolve_load(a, CAL, MEDIANS) == (0.0, "none")

    def test_no_duration_with_median_history_is_none(self):
        a = act()  # running has a median but nothing to multiply
        assert resolve_load(a, CAL, MEDIANS) == (0.0, "none")

    def test_empty_calibration_and_medians(self):
        a = act(rpe=6, duration_s=3600.0)
        assert resolve_load(a, {}, {}) == (pytest.approx(360.0), "srpe_uncalibrated")


# --- convert_watch_rpe -------------------------------------------------------

class TestConvertWatchRpe:
    def test_none_inputs(self):
        assert convert_watch_rpe(None, None) == (None, None)

    def test_rpe_zero_means_unreported(self):
        assert convert_watch_rpe(0, None) == (None, None)

    def test_rpe_low_clamps_to_one(self):
        assert convert_watch_rpe(1, None)[0] == 1
        assert convert_watch_rpe(4, None)[0] == 1  # round(0.4)=0 → clamp 1

    def test_rpe_midscale(self):
        assert convert_watch_rpe(50, None)[0] == 5
        assert convert_watch_rpe(74, None)[0] == 7

    def test_rpe_top_of_scale(self):
        assert convert_watch_rpe(100, None)[0] == 10
        assert convert_watch_rpe(110, None)[0] == 10  # clamp above scale

    def test_feel_edges(self):
        assert convert_watch_rpe(None, 0)[1] == 1    # round(0)+1
        assert convert_watch_rpe(None, 100)[1] == 5  # round(4)+1
        assert convert_watch_rpe(None, 120)[1] == 5  # clamp above scale

    def test_feel_midscale(self):
        assert convert_watch_rpe(None, 50)[1] == 3
        assert convert_watch_rpe(None, 62)[1] == 3   # round(2.48)=2 → 3

    def test_independent_channels(self):
        assert convert_watch_rpe(80, 25) == (8, 2)
        assert convert_watch_rpe(0, 75) == (None, 4)
