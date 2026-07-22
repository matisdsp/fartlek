"""Tests for the exception scanner tool.

Hermetic: seeded temp Store behind a FakeContext, no real ToolContext. The
significance verdict belongs to analytics.trends, so these tests check
SELECTION (only real changes render), RANKING (safety-first bucket order),
and BUDGET (the tightest cap in the catalog holds even when almost every
metric moves at once).
"""
from __future__ import annotations

import asyncio

import pytest

from fartlek.mcp_server.tools import whats_changed
from fartlek.render.renderer import estimate_tokens
from tests.conftest import make_days

TODAY = "2026-07-20"


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
    return asyncio.run(whats_changed.run(ctx, **kw))


def _noisy(n: int, base: float, slope: float) -> list[float]:
    """A monotone drift with a small oscillating wobble — enough noise that
    the Sen-detrended ranks are not float-degenerate ties, matching the shape
    trends.py's own fixtures use to reliably clear both significance gates."""
    out = []
    for i in range(n):
        wobble = 0.6 * (0.6 if i % 3 == 0 else -0.3 if i % 3 == 1 else 0.1)
        out.append(base + slope * i + wobble)
    return out


def seed_flat(store, *, nights=90, resting_hr=46.0, hrv=88.0, sleep_score=72,
              sleep_h=7.5, need_h=8.0, bb=80.0, stress=30.0, load=80.0):
    """Enough history, nothing drifting — the 'stable' baseline."""
    rows = make_days(TODAY, nights, resting_hr=resting_hr, hrv_last_night=hrv,
                      sleep_score=sleep_score, sleep_duration_h=sleep_h,
                      sleep_need_h=need_h, body_battery_wake=bb,
                      avg_stress=stress, daily_load=load)
    for r in rows:
        store.upsert_day(r)


def seed_many_significant(store, *, nights=90):
    """Every tracked metric drifting hard in its adverse direction, plus an
    active alert on resting_hr — the budget stress case and the ranking
    fixture in one seed."""
    rows = make_days(
        TODAY, nights,
        resting_hr=_noisy(nights, 44.0, 0.12),
        hrv_last_night=[max(30.0, v) for v in _noisy(nights, 100.0, -0.5)],
        sleep_score=_noisy(nights, 85.0, -0.35),
        sleep_duration_h=_noisy(nights, 8.5, -0.02),
        body_battery_wake=_noisy(nights, 90.0, -0.35),
        avg_stress=_noisy(nights, 20.0, 0.35),
        daily_load=_noisy(nights, 60.0, 1.2),
        sleep_need_h=8.0,
    )
    for r in rows:
        store.upsert_day(r)
    store.upsert_alert(
        date="2026-07-10", metric="resting_hr", severity="WATCH",
        message="resting_hr high — 55 vs 44 (90d), 5d streak",
    )


# --- budget: the stress case -------------------------------------------------

def test_renders_within_cap_when_many_metrics_change_at_once(store):
    seed_many_significant(store)
    out = run(FakeContext(store))
    assert "significant change" in out
    assert estimate_tokens(out) <= whats_changed.CAP


def test_basic_render_names_the_window_and_metric_count(store):
    seed_flat(store)
    out = run(FakeContext(store))
    assert "# Changes — last 7d asked, 28d tested (9 metrics checked)" in out
    assert "Mon 2026-07-20" in out
    assert "VERDICT:" in out
    assert estimate_tokens(out) <= whats_changed.CAP


def test_banner_is_carried_through(store):
    seed_flat(store)
    out = run(FakeContext(store, banner="⚠ ACTIVE (since Thu 07-17): HRV below band"))
    assert out.startswith("⚠ ACTIVE")


def test_ensure_ready_is_called(store):
    seed_flat(store)
    ctx = FakeContext(store)
    run(ctx)
    assert ctx.ready_calls == 1


# --- "nothing notable" is a success state, not an error ---------------------

def test_nothing_notable_renders_cleanly(store):
    seed_flat(store)
    out = run(FakeContext(store))
    assert "Nothing notable" in out
    assert "VERDICT:" in out
    assert "Next:" in out


def test_empty_store_still_renders_a_valid_report(store):
    out = run(FakeContext(store))
    assert "# Changes" in out and "VERDICT:" in out
    assert estimate_tokens(out) <= whats_changed.CAP


# --- significance gate: only real changes render ----------------------------

def test_insignificant_change_is_not_reported(store):
    """A trivial drift far below the SWC must land in the stable line, never
    the significant table. Realistic day-to-day noise (+/-3 pts) is needed
    here, not a noiseless ramp — a perfectly monotone series has a near-zero
    MAD-SD of its own, so even a microscopic slope would swamp it and clear
    the practical gate for the wrong reason (the same trap trends.py's own
    fixtures are built to avoid)."""
    nights = 90
    tiny_drift = [72.0 + (3.0 if i % 2 == 0 else -3.0) + 0.001 * i for i in range(nights)]
    rows = make_days(TODAY, nights, sleep_score=tiny_drift, resting_hr=46.0,
                      hrv_last_night=88.0, sleep_duration_h=7.5, sleep_need_h=8.0,
                      body_battery_wake=80.0, avg_stress=30.0, daily_load=80.0)
    for r in rows:
        store.upsert_day(r)
    out = run(FakeContext(store))
    assert "Stable:" in out
    assert "sleep score" in out.split("Stable:")[1].split(".")[0]
    # never as a numbered significant-change row
    assert "sleep score" not in out.split("Stable:")[0]


def test_a_real_trend_is_reported_significant(store):
    seed_many_significant(store)
    out = run(FakeContext(store))
    assert "resting HR up" in out
    assert "significant, p=" in out


# --- ranking: safety-first, fixed order --------------------------------------

def test_ranking_is_safety_first(store):
    """1) a change correlated with an active alert · 2) load anomaly ·
    3) recovery degradation · 4) favourable change — in that order,
    regardless of scan order."""
    seed_many_significant(store)
    out = run(FakeContext(store))
    i_health = out.index("resting HR up")       # has an active alert -> bucket 1
    i_load = out.index("daily load up")         # bucket 2
    i_recovery = out.index("HRV down")          # bucket 3 (falling HRV is adverse)
    assert i_health < i_load < i_recovery


def test_favourable_change_ranks_after_adverse_ones(store):
    """Falling resting HR is GOOD news; it must rank after the adverse
    recovery-degradation findings, never ahead of them."""
    nights = 90
    rows = make_days(
        TODAY, nights,
        resting_hr=_noisy(nights, 60.0, -0.12),               # favourable
        sleep_score=_noisy(nights, 85.0, -0.35),               # adverse
        hrv_last_night=88.0, sleep_duration_h=7.5, sleep_need_h=8.0,
        body_battery_wake=80.0, avg_stress=30.0, daily_load=80.0,
    )
    for r in rows:
        store.upsert_day(r)
    out = run(FakeContext(store))
    assert out.index("sleep score down") < out.index("resting HR down")


# --- suppressed metrics are their own bucket, not "stable" -------------------

def test_suppressed_metrics_are_counted_separately_from_stable(store):
    """Fewer than 21 points in the trend window is 'not enough data to
    judge' — distinct from a metric that was judged and found flat."""
    rows = make_days(TODAY, 10, resting_hr=46.0)  # far short of the 28d window
    for r in rows:
        store.upsert_day(r)
    out = run(FakeContext(store))
    assert "too little data" in out
    after = out.split("Not enough data yet")[1]
    assert "resting HR" in after
    # nothing could have been judged stable with only 10 days on file
    assert "0 stable" in out


def test_stable_and_suppressed_do_not_overlap(store):
    """A metric with 90 flat days is judged (stable); EF/TID have no
    supporting data at all (suppressed) — the two lists must not collide."""
    seed_flat(store)
    out = run(FakeContext(store))
    stable_line = out.split("Stable:")[1].split("\n")[0]
    suppressed_line = out.split("Not enough data yet")[1].split("\n")[0]
    stable_names = {s.strip().rstrip(".") for s in stable_line.split(",")}
    suppressed_names = {s.strip().rstrip(".") for s in suppressed_line.split(":")[1].split(",")}
    assert stable_names & suppressed_names == set()
    assert "EF (steady runs)" in suppressed_names
    assert "resting HR" in stable_names


# --- parameter validation ----------------------------------------------------

@pytest.mark.parametrize("since_days", [0, -1, 61, 100])
def test_out_of_range_since_days_is_a_corrective_error(store, since_days):
    out = run(FakeContext(store), since_days=since_days)
    assert "since_days must be between 1 and 60" in out
    assert TODAY in out
    assert "since_days=7" in out


def test_in_range_since_days_is_accepted(store):
    seed_flat(store)
    out = run(FakeContext(store), since_days=30)
    assert "30d" in out
    assert estimate_tokens(out) <= whats_changed.CAP


# --- breadcrumbs --------------------------------------------------------------

def test_breadcrumb_names_only_shipped_tools(store):
    seed_flat(store)
    out = run(FakeContext(store))
    tail = out.rsplit("Next:", 1)[-1]
    for name in ("garmin_week", "garmin_load", "garmin_fitness",
                 "garmin_whats_changed", "garmin_reference", "garmin_apply_plan"):
        assert name not in tail


def test_no_phase2_tool_name_anywhere_in_module_source():
    """This tool is not registered yet; mentioning its own (or any other
    unshipped) name anywhere in its source would trip the CI guardrail once
    it scans every file in the tools package."""
    import inspect
    src = inspect.getsource(whats_changed)
    for name in ("garmin_week", "garmin_load", "garmin_fitness",
                 "garmin_whats_changed", "garmin_reference", "garmin_apply_plan"):
        assert name not in src


def test_title_declares_the_window_actually_tested(store):
    """A header reading "last 7d" above a row reading "over 4 wk" invites the
    reader to date a change to the wrong week. When the tested window is
    widened to satisfy the significance test, the header must say so — and
    when it is not widened, the header must stay simple."""
    seed_flat(store)
    widened = run(FakeContext(store), since_days=7)
    assert "asked, 28d tested" in widened

    exact = run(FakeContext(store), since_days=40)
    assert "last 40d (" in exact
    assert "tested" not in exact.split("\n")[0]
