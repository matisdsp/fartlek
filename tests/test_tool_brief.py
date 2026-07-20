"""Tests for the garmin_brief tool.

Hermetic: seeded temp Store (conftest `store` fixture) behind a FakeContext —
the real ToolContext is never imported. The fusion pipeline (sibling-owned
contract) is monkeypatched: brief must call marker_inputs → compute_readiness
→ apply_gates and render the gated result verbatim; evidence rows come from
the store via baselines/pmc and are asserted against seeded data.
"""
from __future__ import annotations

from typing import Any

from fartlek.analytics import fusion
from fartlek.mcp_server.tools import brief
from fartlek.render.renderer import estimate_tokens, format_date
from tests.conftest import make_days

TODAY = "2026-07-20"
YESTERDAY = "2026-07-19"
BANNER = "⚠ ACTIVE (since Thu 07-17): HRV below band 3 days"


class FakeContext:
    def __init__(self, store, today: str = TODAY, banner: str | None = None,
                 cold_started: bool = False):
        self._store = store
        self._today = today
        self._banner = banner
        self.cold_started = cold_started
        self.ready_calls = 0
        self.fresh_calls = 0

    @property
    def store(self):
        return self._store

    async def ensure_ready(self) -> None:
        self.ready_calls += 1

    async def ensure_fresh_today(self) -> None:
        self.fresh_calls += 1

    def today(self) -> str:
        return self._today

    def data_as_of(self) -> str:
        return "07:41"

    def banner(self) -> str | None:
        return self._banner


def patch_fusion(monkeypatch, verdict: str = "GREEN",
                 markers: tuple[str, ...] = ("hrv", "rhr", "sleep", "form", "body_battery"),
                 provisional: bool = False, provisional_n: tuple[int, int] | None = None,
                 modification: str | None = None, illness_gate: bool = False,
                 capture: dict | None = None) -> None:
    """Replace the sibling-owned fusion pipeline with a controlled fake."""

    def marker_inputs(store: Any, date: str) -> dict[str, Any]:
        if capture is not None:
            capture["inputs_date"] = date
        return {"date": date}

    def compute_readiness(inputs: dict[str, Any]) -> dict[str, Any]:
        return {
            "verdict": verdict,
            "rationale": "fused",
            "markers_used": list(markers),
            "provisional": provisional,
            "provisional_n": provisional_n,
            "gated_by": None,
            "modification": modification,
        }

    def apply_gates(readiness, log_entries, inputs):
        if capture is not None:
            capture["logs"] = log_entries
        if illness_gate:
            for e in log_entries:
                if e.get("flag") == "illness" and not e.get("resolved"):
                    return {**readiness, "verdict": "RED", "gated_by": "illness",
                            "modification": "rest pending symptoms"}
        return readiness

    monkeypatch.setattr(fusion, "marker_inputs", marker_inputs)
    monkeypatch.setattr(fusion, "compute_readiness", compute_readiness)
    monkeypatch.setattr(fusion, "apply_gates", apply_gates)


def seed_full(store, end: str = TODAY) -> None:
    """60 days of wellness mirroring the DESIGN §2.4 example shapes."""
    n = 60
    hrv = [[88, 92, 96, 100, 94][i % 5] for i in range(n)]
    hrv[-1] = 97
    deep = [0.8] * n
    deep[-2], deep[-1] = 0.10, 0.18          # two low nights → streak flag
    bb = [88] * n
    bb[-1] = 99
    for row in make_days(
        end, n,
        hrv_last_night=hrv,
        resting_hr=44,
        sleep_duration_h=9.0,
        sleep_need_h=8.0,
        sleep_score=66,
        sleep_deep_h=deep,
        body_battery_wake=bb,
    ):
        store.upsert_day(row)
    store.replace_pmc([
        {"date": r["date"], "load": 40.0, "ctl": 40.0, "atl": 43.6, "tsb": -3.6}
        for r in make_days(end, 20)
    ])


def seed_yesterday_run(store, activity_id: int = 19501244, date: str = YESTERDAY) -> None:
    store.upsert_activity({
        "activity_id": activity_id, "date": date, "sport": "running",
        "start_local": f"{date}T07:30:00", "duration_s": 3744.0,
        "distance_m": 12000.0, "avg_hr": 115, "load": 41.0, "rpe": 2,
        "rpe_source": "athlete", "synced_at": "2026-07-20T07:00:00",
    })


async def test_structure_golden(store, monkeypatch):
    patch_fusion(monkeypatch)
    seed_full(store)
    seed_yesterday_run(store)
    out = await brief.run(FakeContext(store))
    assert out.splitlines()[0] == f"# Daily Brief — {format_date(TODAY)} (data as of 07:41)"
    assert ("**VERDICT: GREEN — cleared for quality. "
            "Markers used: HRV, RHR, sleep, form, Body Battery.**") in out
    assert "| Signal | Today | Your baseline | Δ | Flag |" in out
    assert out.strip().endswith(
        "Next: garmin_activity(activity_id=19501244) · garmin_activities()"
    )


async def test_all_marker_rows_present(store, monkeypatch):
    patch_fusion(monkeypatch)
    seed_full(store)
    out = await brief.run(FakeContext(store))
    for label in ("HRV overnight", "Resting HR", "| Sleep |", "Deep sleep",
                  "Body Battery at wake", "Form (TSB/CTL)"):
        assert label in out
    assert "97 ms" in out
    assert "44 bpm" in out
    assert "9h00" in out and "score 66 (Fair)" in out and "+60 min" in out
    assert "⚠ long but light" in out
    assert "11 min" in out and "⚠ 2nd low night" in out
    assert "30d wake avg 88" in out and "+11" in out
    assert "−9%" in out and "productive −10…−30%" in out


async def test_absent_markers_omitted(store, monkeypatch):
    patch_fusion(monkeypatch, markers=("rhr",))
    for row in make_days(TODAY, 40, resting_hr=44):
        store.upsert_day(row)
    out = await brief.run(FakeContext(store))
    assert "Resting HR" in out
    for label in ("HRV overnight", "| Sleep |", "Deep sleep", "Body Battery", "Form"):
        assert label not in out
    assert "null" not in out.lower()
    assert "None" not in out


async def test_amber_verdict_carries_modification(store, monkeypatch):
    patch_fusion(monkeypatch, verdict="AMBER",
                 modification="replace today's quality with 40 min easy below HR 148")
    seed_full(store)
    out = await brief.run(FakeContext(store))
    assert ("**VERDICT: AMBER — replace today's quality with 40 min easy "
            "below HR 148. Markers used:") in out


async def test_red_without_modification_gets_default(store, monkeypatch):
    patch_fusion(monkeypatch, verdict="RED")
    out = await brief.run(FakeContext(store))
    assert "**VERDICT: RED — rest today; reassess tomorrow." in out


async def test_illness_log_caps_verdict_red(store, monkeypatch):
    capture: dict = {}
    patch_fusion(monkeypatch, illness_gate=True, capture=capture)
    seed_full(store)
    store.add_log({"date": TODAY, "flag": "illness", "note": "sore throat",
                   "created_at": "2026-07-20T06:50:00"})
    out = await brief.run(FakeContext(store))
    assert "**VERDICT: RED — rest pending symptoms." in out
    assert any(e.get("flag") == "illness" for e in capture["logs"])


async def test_yesterday_activity_line_and_breadcrumb(store, monkeypatch):
    patch_fusion(monkeypatch)
    seed_full(store)
    seed_yesterday_run(store)
    out = await brief.run(FakeContext(store))
    assert ("Yesterday: Run 12.0 km · 62:24 · HR 115 · load 41 · RPE 2/10 "
            "(id 19501244).") in out
    assert "garmin_activity(activity_id=19501244)" in out


async def test_yesterday_rest_day(store, monkeypatch):
    patch_fusion(monkeypatch)
    seed_full(store)
    out = await brief.run(FakeContext(store))
    assert "Yesterday: rest day." in out
    assert "garmin_activity(" not in out
    assert "garmin_activities()" in out


async def test_plan_and_empty_profile_lines(store, monkeypatch):
    patch_fusion(monkeypatch)
    out_empty = await brief.run(FakeContext(store))
    assert "Today's plan: nothing on the Garmin calendar. No goal-race phase on file." in out_empty
    store.upsert_plan_entry({"date": TODAY, "sport": "running",
                             "name": "45 min tempo", "source": "garmin_coach"})
    out = await brief.run(FakeContext(store))
    assert "Today's plan: 45 min tempo — Garmin Coach plan." in out


async def test_profile_goal_and_phase_rendered(store, monkeypatch):
    patch_fusion(monkeypatch)
    store.set_profile("goal_distance", "half")
    store.set_profile("goal_race_date", "2026-10-11")
    store.set_profile("phase", "build")
    store.set_profile("phase_week", "4")
    store.set_profile("phase_total_weeks", "12")
    out = await brief.run(FakeContext(store))
    assert f"Goal: half marathon on {format_date('2026-10-11')} — build wk 4/12." in out
    assert "No goal-race phase on file" not in out


async def test_banner_is_first_line(store, monkeypatch):
    patch_fusion(monkeypatch)
    seed_full(store)
    out = await brief.run(FakeContext(store, banner=BANNER))
    assert out.splitlines()[0] == BANNER


async def test_watch_list_from_watch_alerts_only(store, monkeypatch):
    patch_fusion(monkeypatch)
    seed_full(store)
    store.upsert_alert("2026-07-18", "sleep_deep_h", "WATCH",
                       "deep sleep low 2 nights running")
    store.upsert_alert("2026-07-17", "resting_hr", "AMBER", "RHR +5 vs 30d median")
    out = await brief.run(FakeContext(store))
    assert "Watch:" in out
    assert "- deep sleep low 2 nights running" in out
    assert "RHR +5 vs 30d median" not in out  # AMBER belongs to the banner, not the watch list


async def test_provisional_prefix(store, monkeypatch):
    patch_fusion(monkeypatch, markers=("rhr", "sleep"), provisional=True,
                 provisional_n=(12, 42))
    out = await brief.run(FakeContext(store))
    assert "**VERDICT: PROVISIONAL (n=12 of 42 days) — leaning GREEN. Markers used: RHR, sleep.**" in out


async def test_retrospective_date_anchors_fusion_and_yesterday(store, monkeypatch):
    capture: dict = {}
    patch_fusion(monkeypatch, capture=capture)
    seed_full(store, end="2026-07-18")
    seed_yesterday_run(store, activity_id=111, date="2026-07-17")
    out = await brief.run(FakeContext(store), date="2026-07-18")
    assert f"# Daily Brief — {format_date('2026-07-18')} (data as of 07:41)" in out
    assert capture["inputs_date"] == "2026-07-18"
    assert "(id 111)" in out
    assert "garmin_activity(activity_id=111)" in out


async def test_invalid_date_is_corrective(store, monkeypatch):
    patch_fusion(monkeypatch)
    out = await brief.run(FakeContext(store), date="yesterday")
    assert "date must be YYYY-MM-DD (got 'yesterday')" in out
    assert f"Today is {format_date(TODAY)}" in out
    assert "garmin_brief(date='2026-07-19')" in out


async def test_future_date_is_corrective(store, monkeypatch):
    patch_fusion(monkeypatch)
    out = await brief.run(FakeContext(store), date="2026-08-01")
    assert "in the future" in out
    assert f"Today is {format_date(TODAY)}" in out
    assert "garmin_brief()" in out


async def test_cold_start_disclosure(store, monkeypatch):
    patch_fusion(monkeypatch)
    seed_full(store)
    out = await brief.run(FakeContext(store, cold_started=True))
    assert ("First sync just ran (~180d of history loaded; deeper backfill "
            "continues in background).") in out


async def test_cap_respected_under_load(store, monkeypatch):
    patch_fusion(monkeypatch, verdict="AMBER",
                 modification="replace today's quality with 40 min easy below HR 148")
    seed_full(store)
    seed_yesterday_run(store)
    for i in range(8):
        store.upsert_alert("2026-07-15", f"metric_{i}", "WATCH",
                           f"metric_{i} drifting out of band for several days running ({i})")
    for j in range(4):
        store.upsert_plan_entry({"date": TODAY, "sport": "running",
                                 "name": f"session {j} with a fairly long descriptive name",
                                 "source": "calendar"})
    out = await brief.run(FakeContext(store, banner=BANNER))
    assert estimate_tokens(out) <= brief.CAP
    # undroppable skeleton intact
    assert out.splitlines()[0] == BANNER
    assert "**VERDICT: AMBER" in out
    assert out.strip().endswith(
        "Next: garmin_activity(activity_id=19501244) · garmin_activities()"
    )


async def test_lifecycle_calls(store, monkeypatch):
    patch_fusion(monkeypatch)
    ctx = FakeContext(store)
    await brief.run(ctx)
    assert ctx.ready_calls == 1
    assert ctx.fresh_calls == 1


async def test_breadcrumbs_reference_only_shipped_tools(store, monkeypatch):
    patch_fusion(monkeypatch)
    seed_full(store)
    seed_yesterday_run(store)
    out = await brief.run(FakeContext(store))
    for phantom in ("garmin_week", "garmin_load(", "garmin_fitness", "garmin_recovery",
                    "garmin_whats_changed", "garmin_reference"):
        assert phantom not in out
