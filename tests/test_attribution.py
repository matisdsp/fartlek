"""Attribution rules (DESIGN.md §3.2 #22).

The contract is a NEGATIVE one: the server may say "because" only in the five
listed situations. These tests mostly check that it stays silent — silence is
the correct output whenever the evidence cannot discriminate, and a plausible
explanation offered without evidence is the failure mode being prevented.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from fartlek.analytics import attribution as at


def dated(start: str, values):
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


# --- rules (a) and (b): deep sleep ------------------------------------------

def test_late_bedtimes_when_schedule_is_off_and_load_is_normal():
    res = at.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=1.4, ramp_pct=3.0, strain_pctile=40.0,
    )
    assert res["rule"] == "late_bedtimes"
    assert res["statement"] == "matches late bedtimes, not load"
    assert "1.40h" in res["evidence"]


def test_load_driven_when_load_is_off_and_schedule_is_stable():
    res = at.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=0.3, ramp_pct=12.0, strain_pctile=50.0,
    )
    assert res["rule"] == "load_driven"
    assert res["statement"] == "matches load, not schedule"


def test_strain_alone_is_enough_to_implicate_load():
    res = at.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=0.2, ramp_pct=2.0, strain_pctile=95.0,
    )
    assert res["rule"] == "load_driven"


def test_no_attribution_when_both_could_explain_it():
    """The honest answer is 'we cannot tell'. Picking one would be exactly the
    fabrication this module exists to prevent."""
    assert at.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=1.5, ramp_pct=15.0, strain_pctile=95.0,
    ) is None


def test_no_attribution_when_neither_is_abnormal():
    assert at.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=0.2, ramp_pct=2.0, strain_pctile=30.0,
    ) is None


def test_silent_when_deep_sleep_is_not_declining():
    assert at.deep_sleep_attribution(
        deep_sleep_declining=False, bedtime_sd_h=2.0, ramp_pct=20.0, strain_pctile=99.0,
    ) is None


def test_silent_when_the_discriminating_input_is_missing():
    """Without bedtime variance the two rules cannot be told apart."""
    assert at.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=None, ramp_pct=20.0, strain_pctile=99.0,
    ) is None
    assert at.deep_sleep_attribution(
        deep_sleep_declining=True, bedtime_sd_h=1.5, ramp_pct=None, strain_pctile=None,
    ) is None


# --- rules (c) and (d): lagged associations --------------------------------

def test_strong_lagged_association_is_reported_as_correlation():
    n = 90
    load = dated("2026-01-01", [50 + (i % 7) * 20 for i in range(n)])
    # Next-day HRV mirrors it downward, so r is strongly negative.
    hrv = dated("2026-01-02", [90 - (i % 7) * 5 for i in range(n)])
    res = at.lagged_association(load, hrv, rule="load_hrv_lag", label="load")
    assert res["rule"] == "load_hrv_lag"
    assert res["r"] < -at.MIN_ABS_R
    assert "correlation not causation" in res["statement"]
    assert "lower HRV" in res["statement"]


def test_weak_association_stays_silent():
    n = 90
    load = dated("2026-01-01", [100 + (17 * i) % 11 for i in range(n)])
    hrv = dated("2026-01-02", [80 + (7 * i) % 5 for i in range(n)])
    res = at.lagged_association(load, hrv, rule="load_hrv_lag", label="load")
    assert res is None or abs(res["r"]) >= at.MIN_ABS_R


def test_short_history_stays_silent_however_strong():
    n = at.MIN_CORRELATION_DAYS - 10
    load = dated("2026-01-01", list(range(n)))
    hrv = dated("2026-01-02", list(range(n)))
    assert at.lagged_association(load, hrv, rule="load_hrv_lag", label="load") is None


def test_pairs_are_built_on_calendar_adjacency():
    """A gap must drop the pair, not silently pair days a week apart."""
    load = dated("2026-01-01", list(range(80)))
    hrv = [(d, v) for d, v in dated("2026-01-02", list(range(80)))
           if not d.endswith(("05", "15", "25"))]
    res = at.lagged_association(load, hrv, rule="load_hrv_lag", label="load")
    if res is not None:
        assert res["n"] < 80


def test_flat_series_produce_no_correlation():
    load = dated("2026-01-01", [100.0] * 90)
    hrv = dated("2026-01-02", [80.0] * 90)
    assert at.lagged_association(load, hrv, rule="debt_hrv_lag", label="debt") is None


# --- rule (e): heat --------------------------------------------------------

def test_heat_attribution_uses_the_athletes_own_penalty():
    """The penalty is personally derived, not a population constant."""
    res = at.heat_ef_attribution(
        ef_declining=True, hot_share=0.96, cool_ef=1.307, hot_ef=1.281,
    )
    assert res["rule"] == "heat_ef"
    assert res["penalty"] == pytest.approx((1.307 - 1.281) / 1.307)
    assert "96%" in res["evidence"]


def test_heat_attribution_needs_a_hot_majority():
    assert at.heat_ef_attribution(
        ef_declining=True, hot_share=0.2, cool_ef=1.3, hot_ef=1.25,
    ) is None


def test_heat_attribution_silent_when_hot_sessions_are_not_worse():
    assert at.heat_ef_attribution(
        ef_declining=True, hot_share=0.9, cool_ef=1.25, hot_ef=1.30,
    ) is None


def test_heat_attribution_silent_when_ef_is_not_declining():
    assert at.heat_ef_attribution(
        ef_declining=False, hot_share=0.9, cool_ef=1.3, hot_ef=1.2,
    ) is None


# --- the closed set --------------------------------------------------------

def test_every_emitted_rule_id_is_in_the_published_set():
    """CI checks rendered 'because' statements against RULE_IDS, so a rule
    emitting an unlisted id would slip a causal claim past the guardrail."""
    emitted = [
        at.deep_sleep_attribution(deep_sleep_declining=True, bedtime_sd_h=1.4,
                                  ramp_pct=3.0, strain_pctile=40.0),
        at.deep_sleep_attribution(deep_sleep_declining=True, bedtime_sd_h=0.3,
                                  ramp_pct=12.0, strain_pctile=50.0),
        at.heat_ef_attribution(ef_declining=True, hot_share=0.9,
                               cool_ef=1.3, hot_ef=1.2),
    ]
    for res in emitted:
        assert res is not None and res["rule"] in at.RULE_IDS


def test_co_occurrence_is_the_only_fallback_wording():
    text = at.co_occurrence("HRV is down", "your load is up")
    assert text == "HRV is down while your load is up"
    assert "because" not in text
