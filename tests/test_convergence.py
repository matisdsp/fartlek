"""Overtraining convergence audit (DESIGN.md §3.2 #20).

The invariant these tests defend is that no single marker can raise an alarm.
Over-firing destroys trust exactly as fast as under-firing, and a server that
cries wolf on every noisy night is worse than useless — so "one group deviant
is a WATCH, never a RED" is a contract, not a tuning choice.
"""
from __future__ import annotations

from fartlek.analytics import convergence as cv


def deviant_autonomic():
    return cv.autonomic_group(hrv_below_band_days=4)


def deviant_sleep():
    return cv.sleep_group(debt={"debt_h": 30.0, "nights": 14})


def deviant_load():
    return cv.load_group(monotony=2.4)


def calm_groups():
    return [
        cv.autonomic_group(hrv_below_band_days=0),
        cv.sleep_group(debt={"debt_h": 1.0, "nights": 14}),
        cv.load_group(monotony=1.2, strain_pctile=40.0, ramp_pct=3.0, form_pct=-8.0),
    ]


# --- the core safety rule ---------------------------------------------------

def test_one_deviant_group_is_a_watch_never_an_alarm():
    res = cv.audit([deviant_autonomic(), cv.sleep_group(debt={"debt_h": 0.0, "nights": 14}),
                    cv.load_group(monotony=1.1)])
    assert res["verdict"] == "WATCH"
    assert res["triggering_groups"] == ["autonomic"]
    assert "not an alarm" in " ".join(res["reasons"])


def test_two_deviant_groups_raise_the_alarm():
    res = cv.audit([deviant_autonomic(), deviant_sleep(), cv.load_group(monotony=1.1)])
    assert res["verdict"] == "RED"
    assert set(res["triggering_groups"]) == {"autonomic", "sleep"}


def test_all_calm_is_green():
    res = cv.audit(calm_groups())
    assert res["verdict"] == "GREEN"
    assert res["triggering_groups"] == []
    assert res["reasons"] == ["no marker group deviant"]


def test_hr_response_never_triggers_alone():
    """It moves for too many benign reasons — an easy week, a flat course."""
    res = cv.audit(calm_groups() + [
        cv.hr_response_group(max_hr_suppressed=True, hrr_worsening=True)
    ])
    assert res["verdict"] == "GREEN"
    assert res["triggering_groups"] == []
    assert res["corroborating"] == ["hr_response"]
    assert res["watch_items"]


def test_hr_response_does_not_complete_a_pair():
    """One triggering group plus corroboration is still one triggering group."""
    res = cv.audit([deviant_autonomic(), cv.sleep_group(debt={"debt_h": 0.0, "nights": 14}),
                    cv.load_group(monotony=1.0),
                    cv.hr_response_group(max_hr_suppressed=True)])
    assert res["verdict"] == "WATCH"
    assert res["corroborating"] == ["hr_response"]


# --- two-sided RHR ----------------------------------------------------------

def test_suppressed_rhr_counts_as_deviant():
    """The parasympathetic pattern: a naive 'high is bad' test misses it."""
    group = cv.autonomic_group(rhr={"level": "parasympathetic_watch", "delta": -6.0,
                                    "sustained_days": 3})
    assert group["deviant"] is True
    assert "suppressed" in group["markers"][1]["detail"]


def test_elevated_rhr_counts_as_deviant():
    group = cv.autonomic_group(rhr={"level": "red", "delta": 7.0, "sustained_days": 2})
    assert group["deviant"] is True
    assert "elevated" in group["markers"][1]["detail"]


def test_rhr_caution_is_not_yet_deviant():
    group = cv.autonomic_group(rhr={"level": "caution", "delta": 3.5, "sustained_days": 1})
    assert group["deviant"] is False


# --- persistence ------------------------------------------------------------

def test_short_streaks_do_not_count():
    """Below the persistence bar a marker is noise, not a signal."""
    brief = cv.autonomic_group(hrv_below_band_days=cv.PERSISTENCE_DAYS - 1)
    assert brief["deviant"] is False
    sustained = cv.autonomic_group(hrv_below_band_days=cv.PERSISTENCE_DAYS)
    assert sustained["deviant"] is True


def test_deep_sleep_streak_needs_three_nights():
    assert cv.sleep_group(deep_sleep_low_streak=2)["deviant"] is False
    assert cv.sleep_group(deep_sleep_low_streak=3)["deviant"] is True


# --- acute override and the athlete's word ---------------------------------

def test_acute_override_bypasses_persistence():
    """Some signals mean today, not 'for three days'."""
    res = cv.audit(calm_groups(), acute={"level": "AMBER", "reason": "RHR +7 bpm overnight"})
    assert res["verdict"] == "AMBER"
    assert "RHR +7" in " ".join(res["reasons"])


def test_athlete_report_outranks_calm_sensors():
    """§3.2 #19 and the project invariant: the athlete outranks the sensors."""
    res = cv.audit(calm_groups(), subjective={"level": "RED", "reason": "illness logged today"})
    assert res["verdict"] == "RED"


def test_gates_can_only_raise_severity_never_lower_it():
    res = cv.audit([deviant_autonomic(), deviant_sleep()],
                   acute={"level": "WATCH", "reason": "mild"})
    assert res["verdict"] == "RED"


# --- reporting --------------------------------------------------------------

def test_watch_items_list_non_triggering_deviations():
    res = cv.audit([deviant_autonomic(), cv.sleep_group(debt={"debt_h": 0.0, "nights": 14}),
                    cv.load_group(monotony=1.0),
                    cv.hr_response_group(hrr_worsening=True)])
    assert any("hr_response" in item for item in res["watch_items"])


def test_absent_inputs_do_not_create_markers():
    """A metric the device never produced must not read as 'in range'."""
    group = cv.load_group()
    assert group["markers"] == []
    assert group["deviant"] is False


def test_suppressed_sri_is_not_treated_as_bad():
    """Too few nights to compute SRI is not the same as poor regularity."""
    group = cv.sleep_group(sri={"sri": None, "suppressed": True})
    assert all(m["marker"] != "sleep_regularity" for m in group["markers"])


def test_group_reports_which_markers_fired():
    group = cv.load_group(monotony=2.5, strain_pctile=95.0, ramp_pct=2.0, form_pct=-5.0)
    assert set(group["deviant_markers"]) == {"monotony", "strain"}
