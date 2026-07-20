"""Unit tests for fartlek.analytics.pmc (DESIGN.md §3.2 #1-4)."""
from __future__ import annotations

import math
import statistics

import pytest

from fartlek.analytics.pmc import acwr_ewma, compute_pmc, form_assessment, monotony_strain
from tests.conftest import make_series

K_C = 1 - math.exp(-1 / 42)
K_A = 1 - math.exp(-1 / 7)


# --- compute_pmc -------------------------------------------------------------

class TestComputePmc:
    def test_hand_computed_three_days(self):
        rows = compute_pmc(make_series("2026-01-03", [100.0, 0.0, 50.0]))
        assert [r["date"] for r in rows] == ["2026-01-01", "2026-01-02", "2026-01-03"]
        assert [r["load"] for r in rows] == [100.0, 0.0, 50.0]
        # Hand-computed from the recurrence with CTL=ATL=0 seeds.
        assert rows[0]["ctl"] == pytest.approx(2.3528313347756735, abs=1e-6)
        assert rows[0]["atl"] == pytest.approx(13.312210024981841, abs=1e-6)
        assert rows[0]["tsb"] == 0.0  # first day TSB is exactly 0
        assert rows[1]["ctl"] == pytest.approx(2.2974731818766507, abs=1e-6)
        assert rows[1]["atl"] == pytest.approx(11.54006066748957, abs=1e-6)
        assert rows[1]["tsb"] == pytest.approx(-10.959378690206167, abs=1e-6)
        assert rows[2]["ctl"] == pytest.approx(3.419833180333226, abs=1e-6)
        assert rows[2]["atl"] == pytest.approx(16.65992856691396, abs=1e-6)
        assert rows[2]["tsb"] == pytest.approx(-9.242587485612919, abs=1e-6)

    def test_tsb_uses_yesterday(self):
        rows = compute_pmc(make_series("2026-01-05", [80.0, 40.0, 0.0, 120.0, 60.0]))
        for prev, cur in zip(rows, rows[1:], strict=False):
            assert cur["tsb"] == pytest.approx(prev["ctl"] - prev["atl"], abs=1e-12)

    def test_constant_load_converges_to_load(self):
        rows = compute_pmc(make_series("2026-07-01", [100.0] * 300))
        last = rows[-1]
        # (1-k_ctl)^300 = e^(-300/42) ~ 8e-4 residual
        assert last["ctl"] == pytest.approx(100.0, abs=0.1)
        assert last["atl"] == pytest.approx(100.0, abs=1e-6)
        assert last["tsb"] == pytest.approx(0.0, abs=0.1)
        # monotone approach from below
        ctls = [r["ctl"] for r in rows]
        assert all(a < b for a, b in zip(ctls, ctls[1:], strict=False))

    def test_empty_series(self):
        assert compute_pmc([]) == []

    def test_gap_raises(self):
        series = [("2026-01-01", 50.0), ("2026-01-03", 50.0)]
        with pytest.raises(ValueError):
            compute_pmc(series)

    def test_duplicate_date_raises(self):
        series = [("2026-01-01", 50.0), ("2026-01-01", 50.0)]
        with pytest.raises(ValueError):
            compute_pmc(series)

    def test_descending_raises(self):
        series = [("2026-01-02", 50.0), ("2026-01-01", 50.0)]
        with pytest.raises(ValueError):
            compute_pmc(series)


# --- form_assessment ---------------------------------------------------------

class TestFormAssessment:
    def test_form_pct_none_when_ctl_below_1(self):
        out = form_assessment(0.5, 3.0, make_series("2026-01-10", [0.4] * 10))
        assert out["form_pct"] is None
        assert out["form_band"] is None

    def test_form_pct_value(self):
        out = form_assessment(50.0, -10.0, [])
        assert out["form_pct"] == pytest.approx(-20.0)
        assert out["form_band"] == "productive"

    @pytest.mark.parametrize(
        ("form_pct", "band"),
        [
            (30.0, "transition/detraining risk"),
            (25.0, "fresh/race-ready"),
            (5.0, "fresh/race-ready"),
            (4.9, "neutral"),
            (0.0, "neutral"),
            (-9.9, "neutral"),
            (-10.0, "productive"),
            (-30.0, "productive"),
            (-30.1, "deep"),
            (-40.0, "deep"),
            (-40.1, "overload"),
        ],
    )
    def test_band_thresholds(self, form_pct, band):
        # ctl=100 so tsb == form_pct
        assert form_assessment(100.0, form_pct, [])["form_band"] == band

    def test_ramp_over_trailing_7_days(self):
        # ctl 50 seven days ago -> 60 today: (60-50)/60*100 = 16.667 -> flagged
        ctl_series = make_series("2026-03-08", [50, 51, 52, 54, 56, 57, 58, 60])
        out = form_assessment(60.0, 0.0, ctl_series)
        assert out["ramp_pct_per_wk"] == pytest.approx(100 * 10 / 60, abs=1e-9)
        assert out["ramp_flag"] is True

    def test_ramp_sustainable_not_flagged(self):
        ctl_series = make_series("2026-03-08", [50, 50, 51, 51, 51, 52, 52, 52])
        out = form_assessment(52.0, 0.0, ctl_series)
        assert out["ramp_pct_per_wk"] == pytest.approx(100 * 2 / 52, abs=1e-9)
        assert out["ramp_flag"] is False

    def test_ramp_none_with_short_series(self):
        out = form_assessment(50.0, 0.0, make_series("2026-03-07", [50] * 7))
        assert out["ramp_pct_per_wk"] is None
        assert out["ramp_flag"] is None

    def test_ramp_guarded_when_ctl_today_tiny(self):
        ctl_series = make_series("2026-03-08", [0.5] * 8)
        out = form_assessment(0.5, 0.0, ctl_series)
        assert out["ramp_pct_per_wk"] is None
        assert out["ramp_flag"] is None


# --- acwr_ewma ---------------------------------------------------------------

class TestAcwrEwma:
    def test_short_history_unreliable(self):
        out = acwr_ewma(make_series("2026-01-27", [100.0] * 27))
        assert out["unreliable"] is True
        assert out["acwr"] is None
        assert "28" in out["reason"]
        # EWMAs still reported
        assert out["acute"] == pytest.approx(100.0)
        assert out["chronic"] == pytest.approx(100.0)

    def test_empty_series_unreliable(self):
        out = acwr_ewma([])
        assert out["unreliable"] is True
        assert out["acwr"] is None
        assert out["acute"] is None
        assert out["chronic"] is None

    def test_constant_load_ratio_one(self):
        out = acwr_ewma(make_series("2026-02-01", [100.0] * 30))
        assert out["unreliable"] is False
        assert out["reason"] is None
        assert out["acwr"] == pytest.approx(1.0, abs=1e-12)
        assert out["acute"] == pytest.approx(100.0)
        assert out["chronic"] == pytest.approx(100.0)

    def test_matches_hand_rolled_ewma(self):
        loads = [float(50 + (i * 37) % 80) for i in range(35)]  # deterministic, varied
        out = acwr_ewma(make_series("2026-02-04", loads))
        acute = chronic = loads[0]
        for load in loads[1:]:
            acute = acute + (load - acute) * (2 / 8)
            chronic = chronic + (load - chronic) * (2 / 29)
        assert out["acute"] == pytest.approx(acute, abs=1e-9)
        assert out["chronic"] == pytest.approx(chronic, abs=1e-9)
        assert out["acwr"] == pytest.approx(acute / chronic, abs=1e-9)
        assert out["unreliable"] is False

    def test_layoff_instability_guard(self):
        # 90 days of 100 then 30 days of 0: chronic ~11.7, 90d median chronic 100.
        out = acwr_ewma(make_series("2026-04-30", [100.0] * 90 + [0.0] * 30))
        assert out["unreliable"] is True
        assert out["acwr"] is None
        assert "layoff" in out["reason"]
        assert out["chronic"] == pytest.approx(11.72122459740686, abs=1e-6)

    def test_all_zero_loads_guarded(self):
        out = acwr_ewma(make_series("2026-01-30", [0.0] * 30))
        assert out["unreliable"] is True
        assert out["acwr"] is None

    def test_gap_raises(self):
        with pytest.raises(ValueError):
            acwr_ewma([("2026-01-01", 10.0), ("2026-01-05", 10.0)])


# --- monotony_strain ---------------------------------------------------------

class TestMonotonyStrain:
    def test_hand_computed_week(self):
        week = [100.0, 0.0, 50.0, 80.0, 0.0, 60.0, 30.0]
        out = monotony_strain(make_series("2026-01-07", week))
        assert out["weekly_load"] == pytest.approx(320.0)
        assert out["monotony"] == pytest.approx(1.2914148996340187, abs=1e-6)
        assert out["strain"] == pytest.approx(413.25276788288596, abs=1e-6)
        assert out["flag"] is False
        assert out["strain_percentile"] is None  # < 4 weeks of history

    def test_sd_zero_flags(self):
        out = monotony_strain(make_series("2026-01-07", [50.0] * 7))
        assert out["monotony"] is None
        assert out["strain"] is None
        assert out["flag"] is True
        assert out["weekly_load"] == pytest.approx(350.0)

    def test_high_monotony_flags(self):
        out = monotony_strain(make_series("2026-01-07", [50, 50, 50, 50, 50, 50, 49]))
        mean = statistics.fmean([50, 50, 50, 50, 50, 50, 49])
        sd = statistics.pstdev([50, 50, 50, 50, 50, 50, 49])
        assert out["monotony"] == pytest.approx(mean / sd, abs=1e-9)
        assert out["monotony"] > 2.0
        assert out["flag"] is True

    def test_uses_only_trailing_7_days(self):
        # heavy history, quiet trailing week -> stats come from trailing week only
        series = make_series("2026-01-14", [200.0] * 7 + [100.0, 0.0, 50.0, 80.0, 0.0, 60.0, 30.0])
        out = monotony_strain(series)
        assert out["weekly_load"] == pytest.approx(320.0)
        assert out["monotony"] == pytest.approx(1.2914148996340187, abs=1e-6)

    def test_strain_percentile_current_highest(self):
        # weeks of six rest days + one session X: monotony = 1/sqrt(6) for all,
        # strain proportional to X. Oldest->newest X = 10, 20, 30, 40, 50.
        loads: list[float] = []
        for x in [10.0, 20.0, 30.0, 40.0, 50.0]:
            loads += [0.0] * 6 + [x]
        out = monotony_strain(make_series("2026-02-04", loads))
        assert out["strain_percentile"] == pytest.approx(100.0)

    def test_strain_percentile_mid(self):
        loads: list[float] = []
        for x in [10.0, 20.0, 30.0, 40.0, 25.0]:  # current week X=25
            loads += [0.0] * 6 + [x]
        out = monotony_strain(make_series("2026-02-04", loads))
        # strains <= current among {10,20,30,40,25}*k: 10, 20, 25 -> 3/5
        assert out["strain_percentile"] == pytest.approx(60.0)

    def test_percentile_none_below_4_weeks(self):
        loads: list[float] = []
        for x in [10.0, 20.0, 30.0]:
            loads += [0.0] * 6 + [x]
        out = monotony_strain(make_series("2026-01-21", loads))
        assert out["strain_percentile"] is None

    def test_percentile_caps_at_12_weeks(self):
        # 14 weeks; only the trailing 12 count. Current week strain ranks
        # above exactly 5 of the 12 trailing weeks (incl. itself) -> 50th pct.
        xs = [200.0, 190.0] + [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0] + [55.0]
        loads: list[float] = []
        for x in xs:
            loads += [0.0] * 6 + [x]
        out = monotony_strain(make_series("2026-04-08", loads))
        # trailing 12 weeks have X in {10..110, 55}; <=55: 10,20,30,40,50,55 -> 6/12
        assert out["strain_percentile"] == pytest.approx(50.0)

    def test_sd_zero_weeks_excluded_from_distribution(self):
        # 5 weeks, one historical all-rest week (SD=0, strain undefined).
        loads: list[float] = []
        for x in [10.0, 0.0, 30.0, 40.0, 25.0]:
            loads += [0.0] * 6 + [x]
        out = monotony_strain(make_series("2026-02-04", loads))
        # distribution = {10, 30, 40, 25}; <=25: 10, 25 -> 2/4
        assert out["strain_percentile"] == pytest.approx(50.0)

    def test_gap_raises(self):
        with pytest.raises(ValueError):
            monotony_strain([("2026-01-01", 10.0), ("2026-01-03", 10.0)])

    def test_empty_series(self):
        out = monotony_strain([])
        assert out["monotony"] is None
        assert out["strain"] is None
        assert out["weekly_load"] == 0.0
        assert out["strain_percentile"] is None
        assert out["flag"] is False
