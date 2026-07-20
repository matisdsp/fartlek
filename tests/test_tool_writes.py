"""Tests for the three local-write tools: set_profile, log, sync.

Hermetic: seeded temp Store (conftest `store` fixture) behind a FakeContext —
the real ToolContext is never imported. asyncio_mode=auto runs async tests.
"""
from __future__ import annotations

from typing import Any

import pytest

from fartlek.mcp_server.tools import log_tool, set_profile, sync_tool
from fartlek.render.renderer import estimate_tokens

TODAY = "2026-07-20"
BANNER = "⚠ ACTIVE (since Thu 07-17): HRV below band 3 days — see garmin_recovery()"


class FakeContext:
    def __init__(self, store, today: str = TODAY, banner: str | None = None,
                 sync_stats: dict[str, Any] | None = None):
        self._store = store
        self._today = today
        self._banner = banner
        self.sync_stats = sync_stats or {"calls": 0, "new_activities": 0, "errors": []}
        self.ready_calls = 0
        self.sync_calls: list[int] = []

    @property
    def store(self):
        return self._store

    async def ensure_ready(self) -> None:
        self.ready_calls += 1

    async def ensure_fresh_today(self) -> None:
        pass

    def today(self) -> str:
        return self._today

    def data_as_of(self) -> str:
        return "07:41"

    def banner(self) -> str | None:
        return self._banner

    async def run_sync(self, backfill_days: int = 0) -> dict[str, Any]:
        self.sync_calls.append(backfill_days)
        if not self.sync_stats.get("skipped"):
            self._store.set_sync_state("last_sync", "2026-07-20T09:15:00")
        return self.sync_stats


def seed_activity(store, activity_id: int, date: str = TODAY, sport: str = "running",
                  duration_s: float = 3480.0, load: float | None = 78.0,
                  start: str = "07:30") -> None:
    store.upsert_activity({
        "activity_id": activity_id, "date": date, "sport": sport, "name": "x",
        "start_local": f"{date}T{start}:00", "duration_s": duration_s,
        "load": load, "synced_at": "2026-07-20T00:00:00",
    })


# ---------------------------------------------------------------------------
# garmin_set_profile
# ---------------------------------------------------------------------------

async def test_set_profile_field_subset(store):
    ctx = FakeContext(store)
    out = await set_profile.run(ctx, phase="build", phase_week=2, phase_total_weeks=6)
    assert ctx.ready_calls == 1
    profile = store.get_profile()
    assert profile["phase"] == "build"
    assert profile["phase_week"] == "2"
    assert profile["phase_total_weeks"] == "6"
    assert profile["phase_set"] == TODAY
    assert "goal_race_date" not in profile and "goal_set" not in profile
    assert "Profile updated: phase Build (wk 2 of 6)" in out
    assert "garmin_brief" in out
    assert estimate_tokens(out) <= set_profile.CAP_TOKENS


async def test_set_profile_goal_write_and_stamp(store):
    ctx = FakeContext(store)
    out = await set_profile.run(
        ctx, goal_race_date="2026-09-20", goal_distance="marathon", goal_time="2:59:00"
    )
    profile = store.get_profile()
    assert profile["goal_race_date"] == "2026-09-20"
    assert profile["goal_distance"] == "marathon"
    assert profile["goal_time"] == "2:59:00"
    assert profile["goal_set"] == TODAY
    assert "goal Marathon" in out and "2026-09-20" in out and "2:59:00" in out


async def test_set_profile_unchanged_goal_summary(store):
    store.set_profile("goal_race_date", "2026-09-20")
    store.set_profile("goal_distance", "marathon")
    store.set_profile("goal_time", "2:59:00")
    ctx = FakeContext(store)
    out = await set_profile.run(ctx, availability_days=6)
    assert "availability 6 d/wk" in out
    assert "goal unchanged (Marathon" in out
    assert store.get_profile()["availability_set"] == TODAY


async def test_set_profile_bad_date_is_corrective(store):
    ctx = FakeContext(store)
    out = await set_profile.run(ctx, goal_race_date="Sep 20")
    assert "YYYY-MM-DD" in out and "'Sep 20'" in out and TODAY in out
    assert store.get_profile() == {}  # nothing written on error


async def test_set_profile_past_date_is_corrective(store):
    ctx = FakeContext(store)
    out = await set_profile.run(ctx, goal_race_date="2025-09-20")
    assert "past" in out and TODAY in out
    assert store.get_profile() == {}


async def test_set_profile_bad_goal_time(store):
    ctx = FakeContext(store)
    out = await set_profile.run(ctx, goal_time="2h59")
    assert "H:MM:SS" in out and "2:59:00" in out
    assert store.get_profile() == {}


async def test_set_profile_custom_km_requires_custom_distance(store):
    ctx = FakeContext(store)
    out = await set_profile.run(ctx, goal_distance="marathon", goal_custom_km=25.0)
    assert "custom" in out
    assert store.get_profile() == {}
    out2 = await set_profile.run(ctx, goal_distance="custom", goal_custom_km=25.0)
    assert "25 km" in out2
    assert store.get_profile()["goal_custom_km"] == "25.0"


async def test_set_profile_nothing_provided(store):
    ctx = FakeContext(store)
    out = await set_profile.run(ctx)
    assert "Nothing to update" in out and "garmin_set_profile(" in out


async def test_set_profile_banner_prefix(store):
    ctx = FakeContext(store, banner=BANNER)
    out = await set_profile.run(ctx, phase="taper")
    assert out.startswith(BANNER)
    assert "Profile updated" in out
    assert estimate_tokens(out) <= set_profile.CAP_TOKENS


# ---------------------------------------------------------------------------
# garmin_log
# ---------------------------------------------------------------------------

async def test_log_rpe_with_activity_wiring_and_srpe_echo(store):
    seed_activity(store, 19510992, duration_s=3480.0, load=78.0)  # 58 min
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, rpe=6, activity_id=19510992, note="legs heavy late")
    rows = store.logs_for(TODAY)
    assert len(rows) == 1
    assert rows[0]["rpe"] == 6 and rows[0]["activity_id"] == 19510992
    act = store.get_activity(19510992)
    assert act["rpe"] == 6 and act["rpe_source"] == "athlete"
    assert "RPE 6/10" in out and "19510992" in out
    assert "sRPE 348 AU" in out and "Garmin load 78" in out
    assert 'note "legs heavy late"' in out
    assert estimate_tokens(out) <= log_tool.CAP_TOKENS


async def test_log_rpe_auto_attaches_to_single_activity(store):
    seed_activity(store, 101)
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, rpe=7)
    act = store.get_activity(101)
    assert act["rpe"] == 7 and act["rpe_source"] == "athlete"
    assert "101" in out and "sRPE 406 AU" in out


async def test_log_rpe_ambiguous_day_is_corrective(store):
    seed_activity(store, 101, start="07:30")
    seed_activity(store, 102, sport="strength_training", start="18:00")
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, rpe=6)
    assert "garmin_log(rpe=6, activity_id=101)" in out
    assert "garmin_log(rpe=6, activity_id=102)" in out
    assert store.logs_for(TODAY) == []  # nothing stored
    assert store.get_activity(101)["rpe"] is None


async def test_log_rpe_no_activity_that_day(store):
    seed_activity(store, 101, date="2026-07-18")
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, rpe=6)
    assert "No activity on" in out and "garmin_activities()" in out


async def test_log_unknown_activity_id_names_two_nearest(store):
    seed_activity(store, 100, date="2026-07-17")
    seed_activity(store, 105, date="2026-07-18")
    seed_activity(store, 200, date="2026-07-19")
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, rpe=6, activity_id=110)
    assert "No activity 110" in out
    assert "garmin_activity(activity_id=105)" in out
    assert "garmin_activity(activity_id=100)" in out
    assert "activity_id=200" not in out


async def test_log_resolve_open_flag(store):
    store.add_log({"date": "2026-07-14", "flag": "injury", "note": "left achilles",
                   "created_at": "2026-07-14T08:00:00"})
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, flag="injury", resolve_flag=True)
    assert store.unresolved_injuries() == []
    assert "Resolved injury flag" in out and "07-14" in out


async def test_log_resolve_without_open_flag_names_open_flags(store):
    store.add_log({"date": "2026-07-15", "flag": "illness", "note": "head cold",
                   "created_at": "2026-07-15T08:00:00"})
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, flag="injury", resolve_flag=True)
    assert "No open injury flag" in out and "illness" in out and "07-15" in out
    # after resolving the illness flag, no open flags remain → listing says none
    await log_tool.run(ctx, flag="illness", resolve_flag=True)
    out2 = await log_tool.run(ctx, flag="injury", resolve_flag=True)
    assert "Open flags: none" in out2


async def test_log_resolve_targets_newest_of_kind(store):
    store.add_log({"date": "2026-07-10", "flag": "injury", "note": "old",
                   "created_at": "2026-07-10T08:00:00"})
    store.add_log({"date": "2026-07-16", "flag": "injury", "note": "new",
                   "created_at": "2026-07-16T08:00:00"})
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, flag="injury", resolve_flag=True)
    assert "07-16" in out
    remaining = store.unresolved_injuries()
    assert len(remaining) == 1 and remaining[0]["date"] == "2026-07-10"


async def test_log_wellness_only_row_and_mood_fold(store):
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, fatigue=5, soreness=3, mood=4, note="meh")
    rows = store.logs_for(TODAY)
    assert len(rows) == 1
    assert rows[0]["fatigue"] == 5 and rows[0]["soreness"] == 3
    assert "[mood 4/7]" in rows[0]["note"] and "meh" in rows[0]["note"]
    assert "fatigue 5/7" in out and "mood 4/7" in out
    assert estimate_tokens(out) <= log_tool.CAP_TOKENS


async def test_log_flag_opens_and_range_recheck(store):
    ctx = FakeContext(store)
    out = await log_tool.run(ctx, flag="illness", note="sore throat")
    rows = store.logs_for(TODAY)
    assert rows[0]["flag"] == "illness" and rows[0]["resolved"] == 0
    assert "illness flag opened" in out
    out2 = await log_tool.run(ctx, rpe=11)
    assert "rpe must be 1-10" in out2
    out3 = await log_tool.run(ctx, fatigue=9)
    assert "fatigue must be 1-7" in out3


async def test_log_nothing_provided(store):
    ctx = FakeContext(store)
    out = await log_tool.run(ctx)
    assert "Nothing to log" in out and "garmin_log(" in out


async def test_log_banner_prefix(store):
    seed_activity(store, 101)
    ctx = FakeContext(store, banner=BANNER)
    out = await log_tool.run(ctx, rpe=5)
    assert out.startswith(BANNER)
    assert estimate_tokens(out) <= log_tool.CAP_TOKENS


# ---------------------------------------------------------------------------
# garmin_sync
# ---------------------------------------------------------------------------

async def test_sync_reports_freshness_calls_new_activities(store):
    store.set_sync_state("last_sync", "2026-07-20T07:41:00")
    ctx = FakeContext(store, sync_stats={"calls": 5, "new_activities": 1, "errors": []})
    out = await sync_tool.run(ctx, backfill_days=0)
    assert ctx.sync_calls == [0]
    assert "07:41" in out and "09:15" in out
    assert "5 calls" in out and "1 new activity" in out
    assert "backfilled" not in out
    assert estimate_tokens(out) <= sync_tool.CAP_TOKENS


async def test_sync_first_ever_says_never(store):
    ctx = FakeContext(store, sync_stats={"calls": 30, "new_activities": 12, "errors": []})
    out = await sync_tool.run(ctx, backfill_days=0)
    assert "never" in out and "30 calls" in out and "12 new activities" in out


async def test_sync_backfill_reports_nights_and_resume(store):
    store.set_sync_state("last_sync", "2026-07-20T07:41:00")
    ctx = FakeContext(
        store,
        sync_stats={"calls": 40, "new_activities": 0, "nights": 28, "done": False, "errors": []},
    )
    out = await sync_tool.run(ctx, backfill_days=30)
    assert ctx.sync_calls == [30]
    assert "28 nights backfilled" in out
    assert "garmin_sync(backfill_days=30)" in out
    assert estimate_tokens(out) <= sync_tool.CAP_TOKENS


async def test_sync_skipped_passthrough(store):
    store.set_sync_state("last_sync", "2026-07-20T07:41:00")
    ctx = FakeContext(
        store,
        sync_stats={"skipped": True, "reason": "another sync process holds sync.lock", "calls": 0},
    )
    out = await sync_tool.run(ctx)
    assert "another sync in progress" in out
    assert "07:41" in out
    # last_sync untouched by the fake on skip
    assert store.get_sync_state("last_sync") == "2026-07-20T07:41:00"


async def test_sync_banner_prefix_and_errors_line(store):
    ctx = FakeContext(
        store, banner=BANNER,
        sync_stats={"calls": 5, "new_activities": 0, "errors": ["hrv: 500"]},
    )
    out = await sync_tool.run(ctx)
    assert out.startswith(BANNER)
    assert "1 endpoint errors (non-fatal)" in out
    assert estimate_tokens(out) <= sync_tool.CAP_TOKENS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
