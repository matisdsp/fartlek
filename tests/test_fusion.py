"""Readiness fusion + gates (DESIGN §3.2 #18-19) — fartlek/analytics/fusion.py."""
from __future__ import annotations

import math

import pytest

from fartlek.analytics import fusion
from tests.conftest import make_days, make_series

TODAY = "2026-07-20"


# --- pure-input helpers ------------------------------------------------------

def base_dict(median: float, mad_sd: float = 1.0, n: int = 28, window: int = 28) -> dict:
    return {"mean": median, "median": median, "mad_sd": mad_sd, "n": n, "window": window}


def make_inputs(**over) -> dict:
    inputs = {
        "date": TODAY,
        "hrv_series": [],
        "hrv_band": None,
        "hrv_roll7": None,
        "hrv_last_night": None,
        "hrv_last_night_z": None,
        "hrv_90d_low": False,
        "sleep_today": None,
        "sleep_base": None,
        "sleep_debt_h": 0.0,
        "sleep_duration_h": None,
        "form": None,
        "rhr_dev": None,
        "bb_today": None,
        "bb_base": None,
        "bb_source": "wake",
        "easy_ceiling": 150,
    }
    inputs.update(over)
    return inputs


def healthy_inputs(**over) -> dict:
    """All five markers available and at baseline → GREEN."""
    inputs = make_inputs(
        hrv_band={"low": 55.0, "high": 65.0, "n": 40},
        hrv_roll7=60.0,
        hrv_last_night=61.0,
        hrv_last_night_z=0.1,
        sleep_today=80,
        sleep_base=base_dict(80, mad_sd=5.0),
        sleep_duration_h=7.5,
        form={"form_pct": -15.0, "form_band": "productive",
              "ramp_pct_per_wk": 3.0, "ramp_flag": False},
        rhr_dev={"delta": 0.0, "level": "ok", "sustained_days": 0, "median30": 44, "n": 30},
        bb_today=90,
        bb_base=base_dict(88, mad_sd=4.0, n=30, window=30),
    )
    inputs.update(over)
    return inputs


# --- compute_readiness: fusion, weights, thresholds --------------------------

def test_all_healthy_green_five_markers():
    r = fusion.compute_readiness(healthy_inputs())
    assert r["verdict"] == "GREEN"
    assert r["markers_used"] == ["HRV", "sleep", "form", "RHR", "Body Battery"]
    assert r["provisional"] is False and r["provisional_n"] is None
    assert r["gated_by"] is None and r["modification"] is None


def test_weight_renormalization_over_available_markers():
    # hrv z=-1 (roll 50 vs band 55-65, half-width 5), sleep z=-1, rhr caution=-1.
    r = fusion.compute_readiness(make_inputs(
        hrv_band={"low": 55.0, "high": 65.0, "n": 40},
        hrv_roll7=50.0,
        sleep_today=79,
        sleep_base=base_dict(80),
        rhr_dev={"delta": 4.0, "level": "caution", "sustained_days": 1, "median30": 44, "n": 30},
    ))
    # fused = (.30 + .25 + .15)·(−1) / .70 = −1.0 exactly
    assert r["score"] == pytest.approx(-1.0)
    assert r["verdict"] == "AMBER"
    assert r["markers_used"] == ["HRV", "sleep", "RHR"]


@pytest.mark.parametrize("sleep_today,expected", [
    (79.5, "GREEN"),    # z = −0.5, boundary inclusive
    (79.4, "AMBER"),    # z = −0.6
    (78.75, "AMBER"),   # z = −1.25, boundary inclusive
    (78.7, "RED"),      # z = −1.3
])
def test_verdict_thresholds(sleep_today, expected):
    r = fusion.compute_readiness(make_inputs(
        sleep_today=sleep_today, sleep_base=base_dict(80.0, mad_sd=1.0)))
    assert r["verdict"] == expected


def test_sleep_debt_worsens_score_and_is_capped():
    base = make_inputs(sleep_today=80, sleep_base=base_dict(80))
    no_debt = fusion.compute_readiness(base)
    debt10 = fusion.compute_readiness({**base, "sleep_debt_h": 10.0})   # 10/5 = 2 = cap
    debt30 = fusion.compute_readiness({**base, "sleep_debt_h": 30.0})   # capped at 2
    assert no_debt["score"] == pytest.approx(0.0)
    assert debt10["score"] == pytest.approx(-2.0)
    assert debt30["score"] == debt10["score"]
    assert debt10["verdict"] == "RED"


def test_hrv_above_band_not_credited():
    in_band = fusion.compute_readiness(healthy_inputs())
    above = fusion.compute_readiness(healthy_inputs(hrv_roll7=75.0))  # above band
    assert above["score"] == pytest.approx(in_band["score"])


def test_hrv_below_band_scaled_by_half_width():
    r = fusion.compute_readiness(make_inputs(
        hrv_band={"low": 55.0, "high": 65.0, "n": 40}, hrv_roll7=45.0,
        sleep_today=80, sleep_base=base_dict(80),
        rhr_dev={"delta": 0.0, "level": "ok", "sustained_days": 0, "median30": 44, "n": 30},
    ))
    assert r["zs"]["hrv"] == pytest.approx(-2.0)  # (45−55)/5


def test_hrv_band_without_roll7_marker_unavailable():
    r = fusion.compute_readiness(make_inputs(
        hrv_band={"low": 55.0, "high": 65.0, "n": 40}, hrv_roll7=None))
    assert "HRV" not in r["markers_used"]


@pytest.mark.parametrize("fp,z", [(-50.0, -10 / 15), (40.0, -1.0), (-20.0, 0.0), (10.0, 0.0)])
def test_form_band_mapping(fp, z):
    r = fusion.compute_readiness(healthy_inputs(
        form={"form_pct": fp, "form_band": "x", "ramp_pct_per_wk": None, "ramp_flag": None}))
    assert r["zs"]["form"] == pytest.approx(z)


@pytest.mark.parametrize("level,z", [
    ("ok", 0.0), ("caution", -1.0), ("red", -2.0), ("parasympathetic_watch", -1.0),
])
def test_rhr_level_mapping(level, z):
    r = fusion.compute_readiness(make_inputs(
        rhr_dev={"delta": 5.0, "level": level, "sustained_days": 2, "median30": 44, "n": 30}))
    assert r["zs"]["rhr"] == pytest.approx(z)


def test_rhr_insufficient_data_unavailable():
    r = fusion.compute_readiness(make_inputs(
        rhr_dev={"delta": None, "level": "insufficient_data", "sustained_days": 0,
                 "median30": None, "n": 3}))
    assert "rhr" not in r["zs"]


def test_body_battery_z_clamped():
    r = fusion.compute_readiness(healthy_inputs(bb_today=40))  # z = (40−88)/4 = −12
    assert r["zs"]["body_battery"] == pytest.approx(-2.0)


def test_body_battery_high_fallback_disclosed_in_marker_name():
    r = fusion.compute_readiness(healthy_inputs(bb_source="high"))
    assert "Body Battery (high)" in r["markers_used"]
    assert "Body Battery" not in r["markers_used"]


# --- compute_readiness: provisional ------------------------------------------

def test_provisional_when_fewer_than_three_markers():
    r = fusion.compute_readiness(make_inputs(
        sleep_today=80, sleep_base=base_dict(80),
        rhr_dev={"delta": 0.0, "level": "ok", "sustained_days": 0, "median30": 44, "n": 30}))
    assert r["provisional"] is True
    assert r["provisional_n"] == (2, 3)


def test_provisional_when_sleep_baseline_cold():
    r = fusion.compute_readiness(healthy_inputs(sleep_base=base_dict(80, n=9)))
    assert r["provisional"] is True
    assert r["provisional_n"] == (9, 14)
    assert "n=9/14" in r["rationale"]


def test_no_markers_amber_provisional():
    r = fusion.compute_readiness(make_inputs())
    assert r["verdict"] == "AMBER"
    assert r["provisional"] is True and r["provisional_n"] == (0, 3)
    assert r["markers_used"] == []
    assert r["modification"] is not None  # AMBER always carries a modification


def test_amber_modification_uses_easy_ceiling():
    r = fusion.compute_readiness(make_inputs(sleep_today=79.0, sleep_base=base_dict(80),
                                             easy_ceiling=148))
    assert r["verdict"] == "AMBER"
    assert "below HR 148" in r["modification"]


def test_red_modification_is_rest():
    r = fusion.compute_readiness(make_inputs(sleep_today=78.0, sleep_base=base_dict(80)))
    assert r["verdict"] == "RED"
    assert "rest" in r["modification"].lower()


# --- apply_gates: illness / injury -------------------------------------------

def test_illness_caps_red_from_green():
    r = fusion.compute_readiness(healthy_inputs())
    g = fusion.apply_gates(r, [{"flag": "illness", "resolved": 0}], healthy_inputs())
    assert g["verdict"] == "RED"
    assert g["gated_by"] == "illness log"
    assert "rest pending symptoms" in g["rationale"]
    assert "rest" in g["modification"]


def test_injury_caps_amber_from_green_and_never_upgrades_red():
    inputs = healthy_inputs()
    green = fusion.compute_readiness(inputs)
    g = fusion.apply_gates(green, [{"flag": "injury", "resolved": 0}], inputs)
    assert g["verdict"] == "AMBER" and g["gated_by"] == "injury log"
    assert g["modification"] is not None

    red_in = make_inputs(sleep_today=70, sleep_base=base_dict(80))  # fused −10 → RED
    red = fusion.compute_readiness(red_in)
    assert red["verdict"] == "RED"
    g2 = fusion.apply_gates(red, [{"flag": "injury", "resolved": 0}], red_in)
    assert g2["verdict"] == "RED"          # gates never upgrade
    assert g2["gated_by"] is None          # the AMBER cap did not determine RED


def test_resolved_entries_ignored():
    inputs = healthy_inputs()
    r = fusion.compute_readiness(inputs)
    g = fusion.apply_gates(
        r,
        [{"flag": "illness", "resolved": 1}, {"flag": "injury", "resolved": 1}],
        inputs,
    )
    assert g["verdict"] == "GREEN" and g["gated_by"] is None


# --- apply_gates: acute overrides --------------------------------------------

def test_acute_rhr_single_marker_at_least_amber():
    inputs = healthy_inputs(
        rhr_dev={"delta": 8.0, "level": "caution", "sustained_days": 1,
                 "median30": 44, "n": 30})
    r = fusion.compute_readiness(inputs)
    g = fusion.apply_gates(r, [], inputs)
    assert g["verdict"] == "AMBER"
    assert g["gated_by"] == "acute override"
    assert "possible illness onset" in g["rationale"]
    assert g["modification"] is not None


@pytest.mark.parametrize("over", [
    {"hrv_last_night_z": -2.6},
    {"hrv_90d_low": True},
    {"sleep_duration_h": 3.5},
])
def test_acute_single_markers(over):
    inputs = healthy_inputs(**over)
    g = fusion.apply_gates(fusion.compute_readiness(inputs), [], inputs)
    assert g["verdict"] == "AMBER" and g["gated_by"] == "acute override"


def test_hrv_z_and_90d_low_count_as_one_acute_marker():
    inputs = healthy_inputs(hrv_last_night_z=-3.0, hrv_90d_low=True)
    g = fusion.apply_gates(fusion.compute_readiness(inputs), [], inputs)
    assert g["verdict"] == "AMBER"  # one acute marker, not two → not RED


def test_two_acute_markers_red():
    inputs = healthy_inputs(
        rhr_dev={"delta": 9.0, "level": "caution", "sustained_days": 1,
                 "median30": 44, "n": 30},
        sleep_duration_h=3.2,
    )
    g = fusion.apply_gates(fusion.compute_readiness(inputs), [], inputs)
    assert g["verdict"] == "RED"
    assert g["gated_by"] == "acute override"
    assert "possible illness onset" in g["rationale"]


def test_acute_never_upgrades():
    red_in = make_inputs(
        sleep_today=70, sleep_base=base_dict(80),
        sleep_duration_h=3.0,  # single acute marker targets AMBER
    )
    red = fusion.compute_readiness(red_in)
    assert red["verdict"] == "RED"
    g = fusion.apply_gates(red, [], red_in)
    assert g["verdict"] == "RED"


def test_no_gates_returns_readiness_unchanged():
    inputs = healthy_inputs()
    r = fusion.compute_readiness(inputs)
    assert fusion.apply_gates(r, [], inputs) == r


def test_illness_outranks_double_acute_wording():
    inputs = healthy_inputs(
        rhr_dev={"delta": 9.0, "level": "caution", "sustained_days": 1,
                 "median30": 44, "n": 30},
        sleep_duration_h=3.2,
    )
    g = fusion.apply_gates(
        fusion.compute_readiness(inputs), [{"flag": "illness", "resolved": 0}], inputs)
    assert g["verdict"] == "RED"
    assert g["gated_by"] == "illness log"
    assert "pending symptoms" in g["modification"]


# --- marker_inputs (store-aware) ---------------------------------------------

def _seed(store, rows):
    for row in rows:
        store.upsert_day(row)


def test_marker_inputs_empty_store_all_none(store):
    inputs = fusion.marker_inputs(store, TODAY)
    assert inputs["hrv_band"] is None and inputs["hrv_roll7"] is None
    assert inputs["sleep_today"] is None and inputs["sleep_base"] is None
    assert inputs["sleep_debt_h"] == 0.0
    assert inputs["form"] is None
    assert inputs["rhr_dev"]["level"] == "insufficient_data"
    assert inputs["bb_today"] is None
    assert inputs["easy_ceiling"] is None
    r = fusion.compute_readiness(inputs)
    assert r["verdict"] == "AMBER" and r["provisional"]


def test_marker_inputs_hrv_band_self_computed(store):
    _seed(store, make_days(TODAY, 20, hrv_last_night=[55.0, 65.0] * 10))
    inputs = fusion.marker_inputs(store, TODAY)
    band = inputs["hrv_band"]
    assert band is not None and band["n"] == 20
    # canonical band is lnRMSSD now (§3.2 #8, E1): mean(ln) ± 0.5·MAD-SD, LOG space
    med_ln = (math.log(55) + math.log(65)) / 2       # == mean for this symmetric set
    mad_sd = 1.4826 * abs(math.log(65) - med_ln)
    assert band["low"] == pytest.approx(med_ln - 0.5 * mad_sd)
    assert band["high"] == pytest.approx(med_ln + 0.5 * mad_sd)
    # last 7 alternating nights 65,55,65,55,65,55,65 → mean of their lnRMSSD
    assert inputs["hrv_roll7"] == pytest.approx(
        sum(math.log(v) for v in (65, 55, 65, 55, 65, 55, 65)) / 7)


def test_marker_inputs_hrv_band_needs_14_nights(store):
    _seed(store, make_days(TODAY, 10, hrv_last_night=60.0))
    assert fusion.marker_inputs(store, TODAY)["hrv_band"] is None


def test_marker_inputs_hrv_90d_low(store):
    values = [60.0] * 20 + [40.0]  # today is the lowest of 90d
    _seed(store, make_days(TODAY, 21, hrv_last_night=values))
    inputs = fusion.marker_inputs(store, TODAY)
    assert inputs["hrv_90d_low"] is True
    assert inputs["hrv_last_night_z"] < 0


def test_marker_inputs_sleep_debt_with_need_and_default(store):
    _seed(store, make_days(TODAY, 14, sleep_duration_h=6.5, sleep_need_h=8.0))
    assert fusion.marker_inputs(store, TODAY)["sleep_debt_h"] == pytest.approx(21.0)


def test_marker_inputs_sleep_debt_defaults_need_to_8h(store):
    _seed(store, make_days(TODAY, 14, sleep_duration_h=7.0))
    assert fusion.marker_inputs(store, TODAY)["sleep_debt_h"] == pytest.approx(14.0)


def test_marker_inputs_bb_wake_fallback_to_high(store):
    _seed(store, make_days(TODAY, 15, body_battery_high=[80] * 15))
    inputs = fusion.marker_inputs(store, TODAY)
    assert inputs["bb_source"] == "high"
    assert inputs["bb_today"] == 80
    assert inputs["bb_base"] is not None


def test_marker_inputs_form_from_pmc(store):
    rows = [
        {"date": d, "load": 50.0, "ctl": 50.0, "atl": 55.0, "tsb": -5.0}
        for d, _ in make_series(TODAY, [0.0] * 30)
    ]
    store.replace_pmc(rows)
    form = fusion.marker_inputs(store, TODAY)["form"]
    assert form is not None
    assert form["form_pct"] == pytest.approx(-10.0)


def test_marker_inputs_easy_ceiling_profile_then_maxhr(store):
    _seed(store, make_days(TODAY, 5, max_hr=190))
    assert fusion.marker_inputs(store, TODAY)["easy_ceiling"] == 152  # 0.80·190
    store.set_profile("lt1_hr_override", "155")
    assert fusion.marker_inputs(store, TODAY)["easy_ceiling"] == 155


def test_end_to_end_store_to_gated_verdict(store):
    _seed(store, make_days(
        TODAY, 40,
        hrv_last_night=60.0,
        sleep_score=80,
        sleep_duration_h=8.0,
        sleep_need_h=8.0,
        resting_hr=44,
        body_battery_wake=90,
        max_hr=185,
    ))
    inputs = fusion.marker_inputs(store, TODAY)
    r = fusion.compute_readiness(inputs)
    assert r["verdict"] == "GREEN"
    assert r["provisional"] is False
    assert set(r["markers_used"]) == {"HRV", "sleep", "RHR", "Body Battery"}  # no PMC → no form
    store.add_log({"date": TODAY, "flag": "illness", "note": "sore throat",
                   "created_at": "2026-07-20T07:00:00"})
    g = fusion.apply_gates(r, store.logs_for(TODAY) + store.unresolved_injuries(), inputs)
    assert g["verdict"] == "RED" and g["gated_by"] == "illness log"
