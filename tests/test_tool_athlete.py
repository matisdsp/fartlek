"""garmin_athlete tool tests — hermetic, FakeContext over a temp Store."""
from __future__ import annotations

from typing import Any

from conftest import make_days

from fartlek.mcp_server.tools import athlete

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
        raise AssertionError("athlete must not force a today-refresh")

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


def _seed_days(store, **metrics) -> None:
    for row in make_days(TODAY, 60, **metrics):
        store.upsert_day(row)


async def test_goal_phase_availability_render(store):
    store.set_profile("goal_race_date", "2026-09-20")
    store.set_profile("goal_distance", "marathon")
    store.set_profile("goal_time", "2:59:00")
    store.set_profile("phase", "build")
    store.set_profile("phase_week", "2")
    store.set_profile("phase_total_weeks", "6")
    store.set_profile("availability_days", "6")
    store.set_profile("set_date", "2026-07-12")
    out = await athlete.run(FakeContext(store))
    assert "# Athlete — Mon 2026-07-20 (data as of 07:41)" in out
    assert "**Goal (on file):** Marathon Sun 2026-09-20, target 2:59" in out
    assert "phase: Build wk 2 of 6" in out
    assert "availability 6 d/wk" in out
    assert "(set 2026-07-12 via garmin_set_profile)" in out
    verdict = next(line for line in out.splitlines() if line.startswith("**VERDICT"))
    assert "Marathon Sun 2026-09-20" in verdict


async def test_no_goal_case(store):
    out = await athlete.run(FakeContext(store))
    assert "**Goal (on file):** none" in out
    assert "garmin_set_profile(...)" in out
    assert "no goal on file" in out  # verdict


async def test_coverage_renders_check_and_cross(store):
    store.set_capability("hrv", True)
    store.set_capability("sleep", True)
    store.set_capability("race_predictions", True)
    store.set_capability("training_readiness", False, "404")
    store.set_capability("endurance_score", False, "404")
    out = await athlete.run(FakeContext(store))
    assert "✓ HRV, sleep, race predictions" in out
    assert "✗ Training Readiness, Endurance Score" in out
    assert (
        "device does not produce them — this server computes its own readiness fusion instead"
        in out
    )


async def test_coverage_renders_every_capability_row(store):
    keys = ["profile", "personal_records", "training_plans", "goals", "rhr_range"]
    for i, key in enumerate(keys):
        store.set_capability(key, i % 2 == 0)
    out = await athlete.run(FakeContext(store))
    for label in ("profile", "personal records", "Garmin Coach plans", "Garmin goals", "RHR history"):
        assert label in out


async def test_days_synced_count_and_load_currency(store):
    _seed_days(store, steps=5000)
    store.upsert_activity(
        {"activity_id": 1, "date": "2026-07-19", "sport": "running",
         "load": 41.0, "load_source": "garmin", "synced_at": "2026-07-20T07:00:00"}
    )
    store.recompute_daily_loads()
    out = await athlete.run(FakeContext(store))
    assert "60 days synced" in out
    assert "load currency: Garmin activity load" in out


async def test_injury_notebook_unresolved_and_resolved(store):
    store.add_log(
        {"date": "2026-07-14", "flag": "injury", "resolved": 0,
         "note": "L-achilles tightness", "created_at": "2026-07-14T08:00:00"}
    )
    store.add_log(
        {"date": "2026-06-28", "flag": "injury", "resolved": 1,
         "note": "R-calf twinge", "created_at": "2026-06-28T08:00:00"}
    )
    out = await athlete.run(FakeContext(store))
    assert "**Notebook (garmin_log):**" in out
    assert "L-achilles tightness (since Tue 07-14)" in out
    assert "R-calf twinge (logged Sun 06-28, resolved)" in out


async def test_notebook_omitted_when_empty(store):
    out = await athlete.run(FakeContext(store))
    assert "Notebook" not in out


async def test_hrv_band_and_baselines_line(store):
    hrv = [90 + (i % 7) - 3 for i in range(60)]  # varied values around 90
    _seed_days(store, hrv_last_night=hrv, sleep_need_h=8.0, body_battery_wake=88)
    out = await athlete.run(FakeContext(store))
    assert "**Baselines (60d):** HRV band " in out
    assert "sleep need 8h00" in out
    assert "wake Body Battery 88" in out


async def test_sleep_need_is_a_60d_baseline_not_the_latest_night(store):
    """E2-A: under a 'Baselines (60d)' header the need must be a 60d central
    value, not the most recent single night (which can spike above baseline)."""
    need = [8.0] * 59 + [12.0]  # steady 8h, one high final night
    _seed_days(store, hrv_last_night=[90] * 60, sleep_need_h=need, body_battery_wake=88)
    out = await athlete.run(FakeContext(store))
    assert "sleep need 8h00" in out   # the robust 60d median, not 12h00
    assert "12h" not in out


async def test_hrv_band_omitted_when_insufficient(store):
    _seed_days(store, steps=1000)  # no hrv at all
    for row in make_days(TODAY, 5, hrv_last_night=90):
        store.upsert_day(row)  # only 5 nights < the 14-night minimum
    out = await athlete.run(FakeContext(store))
    assert "HRV band" not in out


async def test_identity_engine_and_plan_lines(store):
    _seed_days(store, weight_g=68000, resting_hr=44)
    store.upsert_activity(
        {"activity_id": 2, "date": "2026-07-18", "sport": "running",
         "vo2max": 61.0, "synced_at": "2026-07-20T07:00:00"}
    )
    store.set_profile("lt1_hr_override", "155")
    store.set_capability("training_plans", False, "empty response")
    store.set_capability("goals", False, "empty response")
    out = await athlete.run(FakeContext(store))
    assert "68 kg" in out
    assert "VO2max 61.0" in out
    assert "primary sport: running (1 of last 1)" in out
    assert "**Engine:** LT1 155 bpm (athlete override) · RHR baseline 44" in out
    assert "**Garmin plan:** no enrolled Garmin Coach plan detected · no Garmin goals set" in out


async def test_banner_is_first_line(store):
    banner = "⚠ ACTIVE (since Thu 07-16): RHR +5 — see garmin_recovery()"
    out = await athlete.run(FakeContext(store, banner=banner))
    assert out.splitlines()[0] == banner


async def test_next_steps_reference_set_profile(store):
    out = await athlete.run(FakeContext(store))
    assert "Next: garmin_set_profile(...) to change goal/phase" in out
