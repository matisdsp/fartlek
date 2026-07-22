"""Tests for the garmin_reference tool.

Hermetic: a FakeContext stands in for ToolContext, no store/network touched at all — the
glossary is a pure function of the analytics engine's own constants, so these tests check
(1) both response shapes fit the declared cap, (2) every threshold in every entry states its
provenance, (3) the required honesty notes are present verbatim, and (4) numbers imported from
an engine module actually match that module — the property that lets this tool claim it cannot
silently drift from the code it describes.
"""
from __future__ import annotations

import asyncio

import pytest

from fartlek.analytics import alerts as alerts_engine
from fartlek.analytics import baselines as baselines_engine
from fartlek.analytics import convergence as convergence_engine
from fartlek.analytics import efficiency as efficiency_engine
from fartlek.analytics import pmc as pmc_engine
from fartlek.analytics import race as race_engine
from fartlek.analytics import trends as trends_engine
from fartlek.mcp_server.tools import reference
from fartlek.render.renderer import estimate_tokens

TODAY = "2026-07-22"


class FakeContext:
    def __init__(self, today: str = TODAY, banner: str | None = None):
        self._today = today
        self._banner = banner
        self.ready_calls = 0

    async def ensure_ready(self) -> None:
        self.ready_calls += 1

    def today(self) -> str:
        return self._today

    def data_as_of(self) -> str:
        return "07:41"

    def banner(self) -> str | None:
        return self._banner


def run(ctx, **kw) -> str:
    return asyncio.run(reference.run(ctx, **kw))


# --- index -------------------------------------------------------------------

def test_index_renders_within_cap():
    out = run(FakeContext())
    assert "# Reference — Metrics Glossary" in out
    assert "VERDICT:" in out
    assert estimate_tokens(out) <= reference.CAP


def test_index_lists_every_metric_name():
    out = run(FakeContext())
    for key in reference._ENTRIES:
        assert key in out


def test_index_carries_the_required_honesty_notes():
    out = run(FakeContext())
    assert "contested" in out.lower()
    assert "fixed-time" in out.lower()
    assert "high" in out.lower() and "rmssd" in out.lower()
    assert "calibration" in out.lower() or "provenance flag" in out.lower()
    assert "heat" in out.lower()
    assert "smallest-worthwhile" in out.lower() or "smallest worthwhile" in out.lower()


def test_index_calls_ensure_ready():
    ctx = FakeContext()
    run(ctx)
    assert ctx.ready_calls == 1


def test_banner_is_carried_through():
    out = run(FakeContext(banner="⚠ ACTIVE (since Thu 07-17): HRV below band"))
    assert out.startswith("⚠ ACTIVE")


def test_index_breadcrumb_never_names_a_phase2_tool():
    out = run(FakeContext())
    tail = out.rsplit("Next:", 1)[-1]
    for name in ("garmin_load", "garmin_week", "garmin_fitness", "garmin_whats_changed"):
        assert name not in tail


# --- every metric drill-down: the real risk -----------------------------------

@pytest.mark.parametrize("key", sorted(reference._ENTRIES))
def test_every_metric_entry_renders_within_cap(key):
    out = run(FakeContext(), metric=key)
    assert f"# Reference — {reference._ENTRIES[key].title}" in out
    assert "VERDICT:" in out
    assert estimate_tokens(out) <= reference.CAP


@pytest.mark.parametrize("key", sorted(reference._ENTRIES))
def test_every_metric_entry_labels_threshold_provenance(key):
    out = run(FakeContext(), metric=key)
    assert "population default" in out
    assert "personally derived" in out


@pytest.mark.parametrize("key", sorted(reference._ENTRIES))
def test_every_metric_entry_states_its_formula_inputs_and_caveat(key):
    entry = reference._ENTRIES[key]
    out = run(FakeContext(), metric=key)
    assert "**Formula:**" in out
    assert "**Inputs:**" in out
    assert "**Honesty caveat:**" in out
    assert entry.caveat in out


@pytest.mark.parametrize("key", sorted(reference._ENTRIES))
def test_metric_lookup_is_case_and_punctuation_insensitive(key):
    mangled = key.replace("_", " ").upper()
    out = run(FakeContext(), metric=mangled)
    assert f"# Reference — {reference._ENTRIES[key].title}" in out


@pytest.mark.parametrize("key", sorted(reference._ENTRIES))
def test_metric_entry_calls_ensure_ready(key):
    ctx = FakeContext()
    run(ctx, metric=key)
    assert ctx.ready_calls == 1


def test_metric_entry_breadcrumb_never_names_a_phase2_tool():
    for key in reference._ENTRIES:
        out = run(FakeContext(), metric=key)
        tail = out.rsplit("Next:", 1)[-1]
        for name in ("garmin_load", "garmin_week", "garmin_fitness", "garmin_whats_changed"):
            assert name not in tail, f"{key} breadcrumb leaked {name}"


# --- required honesty caveats, checked at their owning entry ------------------

def test_acwr_entry_carries_the_contested_spike_detector_caveat():
    out = run(FakeContext(), metric="acwr")
    assert "contested" in out.lower()
    assert "spike detector" in out.lower()
    assert "never a standalone verdict" in out.lower() or "never" in out.lower()


def test_hrv_band_entry_carries_the_high_rmssd_caveat():
    out = run(FakeContext(), metric="hrv_band")
    low = out.lower()
    assert "abnormally high" in low
    assert "parasympathetic" in low
    assert "not automatically good" in low


def test_race_projection_entry_carries_riegel_and_fixed_time_caveats():
    out = run(FakeContext(), metric="race_projection")
    low = out.lower()
    assert "fixed-time" in low
    assert "population exponent" in low or "population default" in low
    assert "never personally fitted" in low or "never" in low


def test_efficiency_entry_carries_the_heat_guard_and_amendment():
    out = run(FakeContext(), metric="efficiency")
    low = out.lower()
    assert "24" in out and "°c" in low  # HOT_TEMP_C, rendered from the engine constant
    assert "amendment" in low
    assert "hr-at-pace" in low


def test_load_calibration_entry_carries_the_ladder_and_provenance_flags():
    out = run(FakeContext(), metric="load_calibration")
    low = out.lower()
    assert "trimp" in low
    assert "provenance flag" in low or "trimp_calibrated" in low


def test_trend_significance_entry_requires_both_gates():
    out = run(FakeContext(), metric="trend_significance")
    low = out.lower()
    assert "p-value" in low or "p <" in out
    assert "smallest-worthwhile" in low or "smallest worthwhile" in low
    assert "both" in low


def test_anomaly_alerts_entry_documents_the_tuning_notes():
    """§ constants from alerts.py: adverse-direction-only, training-days-only baseline,
    two-consecutive-short-nights — must not silently drift from the module."""
    out = run(FakeContext(), metric="anomaly_alerts")
    assert "resting_hr" in out and "high" in out
    assert "daily_load" in out
    assert "sleep_duration_h" in out or "sleep_score" in out


def test_personal_precedent_entry_documents_external_exclusion():
    out = run(FakeContext(), metric="personal_precedent")
    low = out.lower()
    assert "externally-caused" in low or "external" in low
    assert "excluded" in low


# --- constants must match the engine (no drift) -------------------------------

def test_pmc_entry_matches_pmc_engine_constants():
    out = run(FakeContext(), metric="pmc")
    assert str(pmc_engine.K_CTL) in out
    assert str(pmc_engine.K_ATL) in out


def test_monotony_entry_matches_convergence_engine_constants():
    out = run(FakeContext(), metric="monotony_strain")
    assert f"{convergence_engine.MONOTONY_FLAG:g}" in out
    assert f"{convergence_engine.STRAIN_PCTILE_FLAG:g}" in out


def test_baseline_engine_entry_matches_baselines_module_scale():
    out = run(FakeContext(), metric="baseline_engine")
    assert f"{baselines_engine.MAD_SCALE:g}" in out


def test_rhr_entry_matches_baselines_module_thresholds():
    out = run(FakeContext(), metric="rhr_deviation")
    assert f"{baselines_engine._RHR_CAUTION:g}" in out
    assert f"{baselines_engine._RHR_SEVERE:g}" in out
    assert str(baselines_engine._RHR_SUSTAINED_DAYS) in out


def test_trend_significance_entry_matches_trends_module_constants():
    out = run(FakeContext(), metric="trend_significance")
    assert str(trends_engine.MIN_POINTS) in out
    assert f"{trends_engine.P_THRESHOLD:g}" in out


def test_efficiency_entry_matches_efficiency_module_constants():
    out = run(FakeContext(), metric="efficiency")
    assert f"{efficiency_engine.HOT_TEMP_C:g}" in out
    assert f"{efficiency_engine.MIN_LAP_DISTANCE_M:g}" in out


def test_race_projection_entry_matches_race_module_constants():
    out = run(FakeContext(), metric="race_projection")
    assert f"{race_engine.RIEGEL_DEFAULT_B:g}" in out


def test_anomaly_alerts_entry_matches_alerts_module_constants():
    out = run(FakeContext(), metric="anomaly_alerts")
    assert f"{alerts_engine._MAD_SCALE:g}" in out
    for metric_name in alerts_engine._TRAINING_DAYS_ONLY:
        assert metric_name in out
    for metric_name, streak in alerts_engine._MIN_SEVERE_STREAK.items():
        assert metric_name in out
        assert f"{streak}d" in out


# --- unknown metric: never a bare failure -------------------------------------

def test_unknown_metric_lists_nearest_matches():
    out = run(FakeContext(), metric="acwrx")
    assert "acwr" in out
    assert "Unknown metric" in out
    assert "garmin_reference(metric=" in out


def test_unknown_metric_with_no_close_match_lists_all_valid_names():
    out = run(FakeContext(), metric="xyz123")
    for key in reference._ENTRIES:
        assert key in out


def test_unknown_metric_never_calls_ensure_ready():
    ctx = FakeContext()
    run(ctx, metric="not_a_metric_at_all")
    assert ctx.ready_calls == 0


def test_metric_lookup_is_never_a_bare_failure_no_exception():
    # Would raise if the tool let a bad metric propagate as a KeyError.
    out = run(FakeContext(), metric="")
    assert "Unknown metric" in out or "Valid names" in out


# --- topic validation ----------------------------------------------------------

def test_unknown_topic_is_corrective():
    out = run(FakeContext(), topic="workout_schema")
    assert "metrics_glossary" in out
    assert "workout_schema" in out
    assert "later phase" in out


def test_unknown_topic_never_calls_ensure_ready():
    ctx = FakeContext()
    run(ctx, topic="bogus")
    assert ctx.ready_calls == 0


def test_default_topic_is_metrics_glossary():
    out_default = run(FakeContext())
    out_explicit = run(FakeContext(), topic="metrics_glossary")
    assert out_default.split(" (data as of")[0] == out_explicit.split(" (data as of")[0]
