"""Tests for the garmin_week tool.

Hermetic: seeded temp Store behind a FakeContext, no real ToolContext, no
network. Checks the ISO week resolution, the incomplete-week disclosure, the
no-plan-means-no-compliance-section rule, that every session row's
activity_id survives the renderer's budget trimming, and that error text/
breadcrumbs never leak an unshipped tool name.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta

from fartlek.analytics import pmc as pmc_engine
from fartlek.analytics import sleep as sleep_engine
from fartlek.mcp_server.tools import week
from fartlek.render.renderer import estimate_tokens

TODAY = "2026-07-20"  # a Monday


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
    return asyncio.run(week.run(ctx, **kw))


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------

def seed_history(store, end: str, days: int = 120, base_load: float = 40.0):
    """Contiguous days.daily_load + a matching pmc table ending at `end`."""
    end_d = date.fromisoformat(end)
    daily = []
    for i in range(days):
        d = (end_d - timedelta(days=days - 1 - i)).isoformat()
        load = base_load + (12.0 if i % 3 == 0 else 0.0)
        store.upsert_day({"date": d, "daily_load": load, "synced_at": "2026-01-01T00:00:00"})
        daily.append((d, load))
    store.replace_pmc(pmc_engine.compute_pmc(daily))
    return daily


def seed_activity(store, activity_id: int, date_str: str, sport: str = "running", **kw):
    row = {
        "activity_id": activity_id,
        "date": date_str,
        "sport": sport,
        "synced_at": "2026-07-20T07:00:00",
    }
    row.update(kw)
    store.upsert_activity(row)


def seed_steady_laps(store, activity_id: int) -> None:
    """5 post-warm-up laps at constant pace with rising HR -> ~5-6% decoupling."""
    hrs = [140, 142, 144, 148, 152, 156]
    laps = []
    for i, hr in enumerate(hrs):
        laps.append({
            "activity_id": activity_id,
            "lap_index": i,
            "distance_m": 1800.0,
            "duration_s": 600.0,
            "moving_s": 600.0,
            "avg_hr": hr,
            "avg_speed": 3.0,
        })
    store.replace_activity_laps(activity_id, laps)


def seed_week(store, start: str = "2026-07-13"):
    """One week (Mon 2026-07-13 -> Sun 2026-07-19) of mixed sessions, fully
    inside a long enough load history that Ramp/ACWR/Monotony all compute."""
    seed_history(store, "2026-07-19")
    seed_activity(store, 1001, "2026-07-14", name="6x800", distance_m=8400,
                  duration_s=2525, avg_hr=141, load=68.0, rpe=6, rpe_source="athlete",
                  hr_z1_s=200, hr_z2_s=300, hr_z3_s=400, hr_z4_s=1200, hr_z5_s=425)
    seed_activity(store, 1002, "2026-07-16", name="10.1 km easy", distance_m=10100,
                  duration_s=3200, avg_hr=128, load=35.0,
                  hr_z1_s=2800, hr_z2_s=400, hr_z3_s=0, hr_z4_s=0, hr_z5_s=0)
    seed_steady_laps(store, 1002)
    seed_activity(store, 1003, "2026-07-18", sport="strength_training", name="Strength",
                  duration_s=1800, avg_hr=110, load=4.0)
    seed_activity(store, 1004, "2026-07-19", name="Long run", distance_m=12400,
                  duration_s=4100, avg_hr=138, load=78.0,
                  hr_z1_s=1500, hr_z2_s=1500, hr_z3_s=800, hr_z4_s=300, hr_z5_s=0)
    return start


# --- shape and budget -------------------------------------------------------

def test_renders_within_cap_for_a_full_week(store):
    seed_week(store)
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "VERDICT:" in out
    assert estimate_tokens(out) <= week.CAP


def test_ensure_ready_is_called(store):
    seed_week(store)
    ctx = FakeContext(store)
    run(ctx, anchor_date="2026-07-13")
    assert ctx.ready_calls == 1


def test_banner_is_carried_through(store):
    seed_week(store)
    banner = "⚠ ACTIVE (since Thu 07-17): HRV below band"
    out = run(FakeContext(store, banner=banner), anchor_date="2026-07-13")
    assert out.startswith(banner)


# --- ISO week resolution -----------------------------------------------------

def test_mid_week_anchor_resolves_to_monday_sunday(store):
    seed_week(store)
    out = run(FakeContext(store), anchor_date="2026-07-15")  # a Wednesday
    assert "Mon 2026-07-13" in out and "Sun 2026-07-19" in out


def test_default_anchor_is_todays_week(store):
    seed_history(store, TODAY)
    out = run(FakeContext(store))  # anchor_date omitted -> today's week
    assert "Mon 2026-07-20" in out and "Sun 2026-07-26" in out


# --- incompleteness must be disclosed in the header -------------------------

def test_week_containing_today_is_disclosed_incomplete(store):
    seed_history(store, TODAY)
    out = run(FakeContext(store), anchor_date=TODAY)
    assert "(in progress)" in out
    assert "(complete)" not in out


def test_fully_past_week_is_complete(store):
    seed_week(store)
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "(complete)" in out


def test_fully_future_week_is_disclosed_upcoming(store):
    seed_history(store, TODAY)
    out = run(FakeContext(store), anchor_date="2026-08-10")
    assert "(upcoming)" in out


# --- compliance: only when a plan exists --------------------------------------

def test_no_planned_workouts_means_no_compliance_section(store):
    seed_week(store)
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "Planned" not in out
    assert "| Status |" not in out


def test_compliance_section_shows_matched_and_missed(store):
    seed_week(store)
    store.upsert_plan_entry({
        "date": "2026-07-14", "sport": "running", "name": "Intervals",
        "source": "calendar", "matched_activity_id": 1001, "match_method": "heuristic",
    })
    store.upsert_plan_entry({
        "date": "2026-07-17", "sport": "running", "name": "Tempo",
        "source": "calendar",
    })
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "matched" in out and "(id 1001)" in out
    assert "missed" in out


def test_compliance_pending_for_a_future_planned_workout(store):
    seed_history(store, TODAY)
    store.upsert_plan_entry({
        "date": "2026-07-24", "sport": "running", "name": "Long run",
        "source": "calendar",
    })
    out = run(FakeContext(store), anchor_date=TODAY)
    assert "pending" in out


# --- every session row carries its activity_id and survives trimming --------

def test_day_rows_are_undroppable(store):
    seed_week(store)
    acts = store.list_activities("2026-07-13", "2026-07-19")
    for a in acts:
        a["_family"] = "running"
    rows = week._day_rows(store, acts)
    assert rows and all(r.undroppable for r in rows)


def test_activity_ids_survive_even_when_the_report_overflows_the_cap(store):
    seed_history(store, "2026-07-19")
    long_name = "A" * 80
    for i in range(14):  # two sessions/day, oversized names to force overflow
        d = (date.fromisoformat("2026-07-13") + timedelta(days=i // 2)).isoformat()
        seed_activity(store, 2000 + i, d, name=f"{long_name}-{i}", distance_m=9000,
                      duration_s=2700, avg_hr=130, load=40.0)
    for i in range(8):  # bulky compliance table too
        d = (date.fromisoformat("2026-07-13") + timedelta(days=i % 7)).isoformat()
        store.upsert_plan_entry({
            "date": d, "sport": "running", "name": f"{long_name}-plan-{i}",
            "source": "calendar",
        })
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert estimate_tokens(out) <= week.CAP
    for i in range(14):
        assert str(2000 + i) in out


# --- distribution: present with zone data, omitted without ------------------

def test_distribution_omitted_without_zone_data(store):
    seed_history(store, "2026-07-19")
    seed_activity(store, 3001, "2026-07-14", sport="strength_training", load=4.0)
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "Distribution" not in out


def test_distribution_present_with_zone_data(store):
    seed_week(store)
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "Distribution (3-zone)" in out
    assert "whole Garmin HR-zone buckets" in out  # bucket fallback, no zones seeded


def test_distribution_is_prorated_when_zones_are_stored(store):
    seed_week(store)
    store.set_hr_zones({"sport": "RUNNING", "zone_floors": [99, 117, 139, 156, 178],
                        "lthr": 176, "max_hr": 195, "resting_hr": 44})
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "pro-rated across the athlete's own Garmin thresholds" in out


# --- decoupling note ----------------------------------------------------------

def test_decoupling_note_from_stored_laps(store):
    seed_week(store)
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "decoupling" in out and "splits-based" in out


# --- validation --------------------------------------------------------------

def test_malformed_anchor_date_is_corrective(store):
    out = run(FakeContext(store), anchor_date="yesterday")
    assert "YYYY-MM-DD" in out
    assert TODAY in out
    assert "garmin_week(anchor_date=" in out


def test_unreal_calendar_date_is_corrective(store):
    out = run(FakeContext(store), anchor_date="2026-13-40")
    assert "YYYY-MM-DD" in out
    assert TODAY in out


# --- breadcrumbs --------------------------------------------------------------

def test_breadcrumb_names_only_shipped_tools(store):
    seed_week(store)
    out = run(FakeContext(store), anchor_date="2026-07-13")
    tail = out.rsplit("Next:", 1)[-1]
    for name in ("garmin_load", "garmin_fitness", "garmin_whats_changed", "garmin_reference"):
        assert name not in tail


def test_empty_week_still_renders_a_valid_report(store):
    out = run(FakeContext(store), anchor_date="2026-07-13")
    assert "VERDICT:" in out
    assert "full rest" in out
    assert estimate_tokens(out) <= week.CAP


def test_future_days_are_not_reported_as_rest(store):
    """Reporting "Thu/Fri/Sat/Sun: rest" on a Wednesday states four things
    that are not yet true, and would have the model judge an athlete for a
    week they have not trained."""
    seed_week(store)
    # Wed 2026-07-15 sits inside the seeded Mon 07-13 -> Sun 07-19 week.
    out = run(FakeContext(store, today="2026-07-15"))

    assert "still to come" in out
    line = next(ln for ln in out.splitlines() if "still to come" in ln)
    if ": rest" in line:
        before_rest = line.split(": rest")[0]
        for future_day in ("Thu", "Fri", "Sat", "Sun"):
            assert future_day not in before_rest


def test_completed_week_never_says_still_to_come(store):
    seed_week(store)
    out = run(FakeContext(store, today="2026-07-26"), anchor_date="2026-07-15")
    assert "still to come" not in out


def test_sleep_debt_anchors_at_today_not_the_future_week_end(store):
    """E2-B: for an in-progress week, the 14d sleep-debt line anchors at today,
    not the (future) Sunday week-end — otherwise it counts a different set of
    nights than garmin_recovery run the same day and the two tools disagree."""
    end = date.fromisoformat(TODAY)  # Mon 2026-07-20 → week 07-20..07-26, in progress
    for i in range(20):              # 20 short nights ending today, 2h deficit each
        d = (end - timedelta(days=19 - i)).isoformat()
        store.upsert_day({"date": d, "synced_at": "2026-01-01T00:00:00",
                          "sleep_duration_h": 6.0, "sleep_need_h": 8.0})
    seed_history(store, TODAY)       # load history so the rest of the week renders

    out = run(FakeContext(store))
    m = re.search(r"14d debt ([\d.]+)h", out)
    assert m, "week did not render a 14d sleep-debt line"
    rendered = float(m.group(1))

    def debt_anchored_at(anchor: str) -> float:
        days = [(date.fromisoformat(anchor) - timedelta(days=13 - i)).isoformat()
                for i in range(14)]
        rows = [store.get_day(d) or {} for d in days]
        return sleep_engine.sleep_debt(rows, anchor, window=14)["debt_h"]

    today_debt = debt_anchored_at(TODAY)                       # 14 seeded nights → 28h
    sunday_debt = debt_anchored_at((end + timedelta(days=6)).isoformat())  # buggy: 8 nights
    assert today_debt != sunday_debt, "fixture must actually exercise the anchor bug"
    assert abs(rendered - today_debt) < 0.05, (
        f"week 14d debt {rendered} anchored at the week-end, not today ({today_debt})"
    )
