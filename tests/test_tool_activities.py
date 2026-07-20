"""garmin_activities tool tests — hermetic, FakeContext over a temp Store."""
from __future__ import annotations

from typing import Any

from fartlek.mcp_server.tools import activities

TODAY = "2026-07-20"  # a Monday


class FakeContext:
    def __init__(self, store, today: str = TODAY, banner: str | None = None):
        self.store = store
        self._today = today
        self._banner = banner
        self.ready_calls = 0

    async def ensure_ready(self) -> None:
        self.ready_calls += 1

    async def ensure_fresh_today(self) -> None:
        raise AssertionError("activities must not force a today-refresh")

    def today(self) -> str:
        return self._today

    def data_as_of(self) -> str:
        return "07:41"

    def banner(self) -> str | None:
        return self._banner

    async def fetch_raw(self, path: str, **params: Any) -> Any:
        raise AssertionError("no network in tests")

    async def run_sync(self, backfill_days: int = 0) -> dict[str, Any]:
        raise AssertionError("no network in tests")


def _act(store, activity_id: int, date: str, sport: str = "running", **kw) -> None:
    row = {
        "activity_id": activity_id,
        "date": date,
        "sport": sport,
        "synced_at": "2026-07-20T07:00:00",
    }
    row.update(kw)
    store.upsert_activity(row)


async def test_default_window_table_and_sorting(store):
    _act(store, 101, "2026-07-19", name="easy", distance_m=12000, duration_s=3744,
         avg_speed=1000 / 312, avg_hr=115, load=41.0, rpe=2, rpe_source="athlete")
    _act(store, 102, "2026-07-17", name="6x800", distance_m=8400, duration_s=2525,
         avg_speed=1000 / 301, avg_hr=141, load=68.0)
    _act(store, 103, "2026-06-01", name="ancient")  # outside the default 14d window
    out = await activities.run(FakeContext(store))
    assert "# Activities — Mon 2026-07-20 (data as of 07:41)" in out
    assert "Tue 2026-07-07 → Mon 2026-07-20" in out
    assert "2 sessions" in out
    assert "runs 2 (20 km)" in out
    assert "ancient" not in out
    # newest first
    assert out.index("101") < out.index("102")
    # running pace from avg_speed (1000/avg_speed s/km)
    assert "5:12" in out and "5:01" in out
    # duration 3744s renders mm:ss per the §2.4 example
    assert "62:24" in out
    assert "garmin_activity(activity_id=101)" in out


async def test_sport_filter_running(store):
    _act(store, 201, "2026-07-18", sport="running", name="easy", distance_m=10000)
    _act(store, 202, "2026-07-18", sport="strength_training", name="gym")
    out = await activities.run(FakeContext(store), sport="running")
    assert "| easy |" in out and "| 201 |" in out
    assert "| gym |" not in out and "| 202 |" not in out
    assert "1 session" in out


async def test_sport_filter_other_matches_walking(store):
    _act(store, 301, "2026-07-18", sport="walking", name="stroll")
    _act(store, 302, "2026-07-18", sport="running", name="easy")
    out = await activities.run(FakeContext(store), sport="other")
    assert "301" in out and "walk" in out
    assert "302" not in out


async def test_limit_truncation_disclosure(store):
    for i in range(5):
        _act(store, 400 + i, f"2026-07-{15 + i:02d}", name=f"run{i}", distance_m=5000)
    out = await activities.run(FakeContext(store), limit=3)
    assert "5 sessions" in out  # counts cover the whole window, not just shown rows
    assert "| 404 |" in out and "| 403 |" in out and "| 402 |" in out
    assert "| 401 |" not in out and "| 400 |" not in out
    # disclosure names a narrower garmin_activities window ending at the newest cut row
    assert '(2 more rows — garmin_activities(start_date="2026-07-07", end_date="2026-07-16") for the rest)' in out


async def test_empty_window_names_nearest_activity(store):
    _act(store, 501, "2026-05-23", sport="running", name="old run")
    out = await activities.run(
        FakeContext(store), start_date="2026-07-01", end_date="2026-07-10"
    )
    assert "No activities Wed 2026-07-01 → Fri 2026-07-10" in out
    assert "Sat 2026-05-23" in out and "id 501" in out
    assert 'garmin_activities(start_date="2026-05-17", end_date="2026-05-23")' in out


async def test_empty_store_points_at_sync(store):
    out = await activities.run(FakeContext(store))
    assert "No activities in the store yet" in out
    assert "garmin_sync()" in out


async def test_empty_window_sport_scoped(store):
    _act(store, 601, "2026-07-18", sport="running", name="easy")
    out = await activities.run(FakeContext(store), sport="cycling")
    assert "No cycling activities in the store" in out
    assert "id 601" in out


async def test_watch_rpe_marker(store):
    _act(store, 701, "2026-07-19", name="watch-rated", rpe=6, rpe_source="watch")
    _act(store, 702, "2026-07-18", name="athlete-rated", rpe=4, rpe_source="athlete")
    _act(store, 703, "2026-07-17", name="unrated")
    out = await activities.run(FakeContext(store))
    row_701 = next(line for line in out.splitlines() if "| 701 |" in line)
    row_702 = next(line for line in out.splitlines() if "| 702 |" in line)
    row_703 = next(line for line in out.splitlines() if "| 703 |" in line)
    assert "| 6w |" in row_701
    assert "| 4 |" in row_702 and "4w" not in row_702
    assert row_703.rstrip().endswith("| — |")


async def test_invalid_date_is_corrective(store):
    out = await activities.run(FakeContext(store), start_date="yesterday")
    assert "start_date must be YYYY-MM-DD (got 'yesterday')" in out
    assert "Today is Mon 2026-07-20" in out
    assert "garmin_activities(start_date=" in out


async def test_start_after_end_is_corrective(store):
    out = await activities.run(
        FakeContext(store), start_date="2026-07-19", end_date="2026-07-01"
    )
    assert "start_date 2026-07-19 is after end_date 2026-07-01" in out


async def test_banner_is_first_line_on_report_and_error(store):
    banner = "⚠ ACTIVE (since Thu 07-16): HRV below band 3 days — see garmin_recovery()"
    _act(store, 801, "2026-07-19", name="easy")
    out = await activities.run(FakeContext(store, banner=banner))
    assert out.splitlines()[0] == banner
    err = await activities.run(FakeContext(store, banner=banner), start_date="nope")
    assert err.splitlines()[0] == banner


async def test_non_running_rows_have_no_pace_or_distance(store):
    _act(store, 901, "2026-07-16", sport="strength_training", name="gym",
         duration_s=1779, avg_hr=96, load=4.0)
    out = await activities.run(FakeContext(store))
    row = next(line for line in out.splitlines() if "| 901 |" in line)
    assert "| strength |" in row and "| 29:39 |" in row
    assert row.count("—") >= 3  # dist, pace, rpe at least
