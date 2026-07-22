"""Tests for the garmin_load tool.

Hermetic: seeded temp Store (conftest `store` fixture) behind a FakeContext,
no real ToolContext, no network. PMC rows are seeded by running the actual
`analytics.pmc.compute_pmc` engine over a synthetic daily-load series and
writing the result with `store.replace_pmc` — exactly what sync's
`recompute_derived` does — so CTL/ATL/TSB stay internally consistent instead
of being hand-typed and potentially self-contradictory.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from fartlek.analytics import pmc as pmc_mod
from fartlek.mcp_server.tools import load as load_tool
from fartlek.render.renderer import estimate_tokens
from tests.conftest import make_series

TODAY = "2026-08-20"
_END_D = date.fromisoformat(TODAY)

# A realistic week: two rest days, the rest at varying load — steers clear of
# the degenerate zero-variance case (see monotony tests below for that case
# on purpose) so CTL/ramp/ACWR settle into ordinary, non-flagged numbers.
CALM_WEEK = [70.0, 0.0, 55.0, 40.0, 65.0, 0.0, 80.0]


class FakeContext:
    def __init__(self, store, today: str = TODAY, banner: str | None = None):
        self._store = store
        self._today = today
        self._banner = banner
        self.ready_calls = 0

    @property
    def store(self):
        return self._store

    async def ensure_ready(self) -> None:
        self.ready_calls += 1

    def today(self) -> str:
        return self._today

    def data_as_of(self) -> str:
        return "07:41"

    def banner(self) -> str | None:
        return self._banner


def run(ctx, **kw) -> str:
    return asyncio.run(load_tool.run(ctx, **kw))


def seed_pmc(store, loads: list[float], end: str = TODAY) -> None:
    store.replace_pmc(pmc_mod.compute_pmc(make_series(end, loads)))


def seed_calm(store, days: int = 400, end: str = TODAY) -> None:
    loads = (CALM_WEEK * (days // 7 + 1))[:days]
    seed_pmc(store, loads, end)


def seed_activity(
    store, activity_id: int, d: str,
    z: tuple[float, float, float, float, float],
    load_source: str = "garmin",
) -> None:
    store.upsert_activity({
        "activity_id": activity_id, "date": d, "sport": "running",
        "duration_s": 3000.0, "load": 40.0, "load_source": load_source,
        "hr_z1_s": z[0], "hr_z2_s": z[1], "hr_z3_s": z[2], "hr_z4_s": z[3], "hr_z5_s": z[4],
        "synced_at": "2026-01-01T00:00:00",
    })


def seed_weekly_activities(
    store, n_weeks: int, z_for_week, end: str = TODAY, id_start: int = 1
) -> None:
    """One activity per week, oldest first; `z_for_week(w)` returns the
    5-zone-seconds tuple for week index w (0 = oldest of the n_weeks)."""
    aid = id_start
    for w in range(n_weeks):
        d = (_END_D - timedelta(days=7 * (n_weeks - 1 - w))).isoformat()
        seed_activity(store, aid, d, z_for_week(w))
        aid += 1


# --- shape and budget -------------------------------------------------------

def test_renders_within_cap_at_default_weeks(store):
    seed_calm(store)
    out = run(FakeContext(store), weeks=8)
    assert out.startswith("# Training Load — 8 weeks — Thu 2026-08-20")
    assert "VERDICT:" in out
    assert estimate_tokens(out) <= load_tool.CAP


def test_renders_within_cap_at_max_weeks(store):
    seed_calm(store, days=400)
    out = run(FakeContext(store), weeks=52)
    assert estimate_tokens(out) <= load_tool.CAP


def test_ensure_ready_is_called(store):
    seed_calm(store)
    ctx = FakeContext(store)
    run(ctx)
    assert ctx.ready_calls == 1


def test_banner_is_carried_through(store):
    seed_calm(store)
    banner = "⚠ ACTIVE (since Thu 07-17): HRV below band"
    out = run(FakeContext(store, banner=banner))
    assert out.startswith(banner)


# --- ACWR always carries its caveat -----------------------------------------

def test_acwr_caveat_present_when_reliable(store):
    seed_calm(store, days=90)
    out = run(FakeContext(store))
    assert "ACWR (EWMA 7:28):" in out
    assert "contested spike detector, not a verdict" in out


def test_acwr_caveat_present_when_not_yet_reliable(store):
    """<28 days of history: acwr is None, but the line — and its caveat —
    must still render (never silently drop the disclosure with the number)."""
    seed_pmc(store, CALM_WEEK * 2)  # 14 days
    out = run(FakeContext(store), weeks=2)
    assert "ACWR: not yet reliable" in out
    assert "contested spike detector, not a verdict" in out


# --- absent data is omitted, never fabricated -------------------------------

def test_empty_store_still_renders_a_valid_report(store):
    out = run(FakeContext(store))
    assert "# Training Load" in out and "VERDICT:" in out
    assert "no training-load history" in out
    assert "garmin_sync()" in out
    assert estimate_tokens(out) <= load_tool.CAP


def test_tid_section_absent_without_any_activities(store):
    seed_calm(store)
    out = run(FakeContext(store))
    assert "TID" not in out


def test_precedent_line_absent_without_any_log_episode(store):
    seed_calm(store, days=200)
    out = run(FakeContext(store))
    assert "Personal precedent" not in out


def test_short_history_discloses_partial_coverage(store):
    """Only 20 days on file but 8 weeks requested — never silently pretend
    the full window was available."""
    seed_pmc(store, CALM_WEEK[:6] * 4)  # 24 days, not a clean week multiple
    out = run(FakeContext(store), weeks=8)
    assert "only 3 of the requested 8 weeks on file" in out


# --- TID: own norm, drift, and the "near-all-easy is not an error" case -----

def test_near_all_easy_distribution_is_not_reported_as_an_error(store):
    seed_calm(store, days=90)
    seed_weekly_activities(store, 12, lambda w: (1000, 900, 20, 5, 0))
    out = run(FakeContext(store), weeks=8)
    assert "base" in out
    assert "no drift" in out
    for bad in ("error", "wrong", "fault", "incorrect", "mistake"):
        assert bad not in out.lower()


def test_grey_zone_creep_is_detected_and_named(store):
    seed_calm(store, days=90)
    # Moderate-zone seconds rise every week — the pattern that is unwelcome
    # under every training-distribution model (analytics.tid docstring).
    seed_weekly_activities(
        store, 12, lambda w: (2000.0, 500.0, 300.0 + w * 150.0, 50.0, 0.0)
    )
    out = run(FakeContext(store), weeks=8)
    assert "Grey-zone creep" in out
    assert "grey-zone creep" in out.rsplit("VERDICT:", 1)[-1].split("\n")[0]


def test_tid_method_note_discloses_bucket_approximation(store):
    seed_calm(store, days=90)
    seed_weekly_activities(store, 12, lambda w: (1000, 900, 20, 5, 0))
    out = run(FakeContext(store), weeks=8)
    assert "buckets_approximate" not in out  # not the internal method-tag literal
    assert "approximated from Garmin's 5-zone buckets" in out
    assert "LT1/LT2 boundaries aren't stored yet" in out


# --- ramp and monotony flags reach the verdict ------------------------------

def test_ramp_spike_is_flagged_in_line_and_verdict(store):
    seed_pmc(store, [40.0] * 60 + [130.0] * 14)
    out = run(FakeContext(store), weeks=8)
    assert "Ramp: " in out and "exceeds the sustainable band" in out
    assert "ramp above the sustainable band" in out.split("VERDICT:")[1].split("\n")[0]


def test_degenerate_monotony_week_still_flags(store):
    """Regression: a week of identical non-zero daily loads has SD~0, so
    monotony_strain returns monotony=None but flag=True — that flag must
    still surface even when no week in the window produced a numeric value."""
    seed_pmc(store, [55.0] * 90)
    out = run(FakeContext(store), weeks=8)
    assert "spiked above 2.0" in out
    assert "monotony spiked above 2.0 in the window" in out.split("VERDICT:")[1].split("\n")[0]


def test_calm_varied_week_shows_no_monotony_spike(store):
    seed_calm(store, days=90)
    out = run(FakeContext(store), weeks=8)
    assert "Monotony" in out and "no spike" in out
    assert "durable build, no structural flags this window" in out


# --- personal precedent (load-structure analogue of recovery's check) ------

def test_precedent_line_appears_once_an_episode_exists(store):
    seed_calm(store, days=200)
    store.add_log({"date": "2026-06-01", "flag": "illness", "note": "overreached",
                   "created_at": "2026-01-01T00:00:00"})
    out = run(FakeContext(store), weeks=8)
    assert "Personal precedent" in out


def test_precedent_exceedance_reaches_the_verdict(store):
    varied = CALM_WEEK * 20       # 140 low-monotony days
    constant = [55.0] * 60        # 60 constant days -> high current monotony
    seed_pmc(store, varied + constant)
    illness_date = (_END_D - timedelta(days=61)).isoformat()
    store.add_log({"date": illness_date, "flag": "illness", "note": "sick",
                   "created_at": "2026-01-01T00:00:00"})
    out = run(FakeContext(store), weeks=8)
    assert "is above your own pre-episode level" in out
    assert "load structure above your own pre-episode trigger level" in out.split(
        "VERDICT:"
    )[1].split("\n")[0]


def test_external_episode_is_excluded_from_precedent_levels(store):
    """The salmonella case (analytics.precedent docstring): an EXTERNAL-
    tagged episode must not set load trigger levels."""
    seed_calm(store, days=200)
    store.add_log({
        "date": "2026-06-01", "flag": "illness",
        "note": "Salmonella. EXTERNAL cause — exclude from load levels.",
        "created_at": "2026-01-01T00:00:00",
    })
    out = run(FakeContext(store), weeks=8)
    assert "Personal precedent" not in out


# --- provisional gating ------------------------------------------------------

def test_provisional_when_history_is_short(store):
    seed_pmc(store, CALM_WEEK * 2)  # 14 days < 28
    out = run(FakeContext(store), weeks=2)
    assert "PROVISIONAL (n=14 days)" in out


def test_not_provisional_with_ample_history(store):
    seed_calm(store, days=90)
    out = run(FakeContext(store), weeks=8)
    assert "PROVISIONAL" not in out


# --- load currency disclosure -----------------------------------------------

def test_currency_note_names_a_non_default_load_source(store):
    seed_calm(store, days=90)
    for i in range(5):
        d = (_END_D - timedelta(days=i)).isoformat()
        seed_activity(store, 5000 + i, d, (1000, 500, 0, 0, 0), load_source="trimp_calibrated")
    out = run(FakeContext(store), weeks=8)
    assert "load currency: calibrated TRIMP" in out


# --- parameter validation ----------------------------------------------------

@pytest.mark.parametrize("weeks", [1, 53, 0, -1])
def test_out_of_range_weeks_is_a_corrective_error(store, weeks):
    out = run(FakeContext(store), weeks=weeks)
    assert "weeks must be between 2 and 52" in out
    assert "garmin_load(weeks=8)" in out


def test_malformed_anchor_date_is_corrective(store):
    out = run(FakeContext(store), anchor_date="not-a-date")
    assert "YYYY-MM-DD" in out
    assert TODAY in out


def test_anchor_date_moves_the_window(store):
    seed_calm(store, days=90, end="2026-07-10")
    out = run(FakeContext(store), anchor_date="2026-07-10")
    assert "2026-07-10" in out


# --- breadcrumbs --------------------------------------------------------------

def test_breadcrumb_names_only_shipped_tools(store):
    seed_calm(store)
    out = run(FakeContext(store))
    tail = out.rsplit("Next:", 1)[-1]
    for name in ("garmin_week", "garmin_fitness", "garmin_whats_changed", "garmin_reference"):
        assert name not in tail
    assert "garmin_recovery" in tail
    assert "garmin_activities" in tail
