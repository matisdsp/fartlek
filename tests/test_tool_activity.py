"""garmin_activity tool tests — hermetic: seeded temp store, FakeContext, no network."""
from __future__ import annotations

import asyncio

from fartlek.mcp_server.tools import activity
from fartlek.render.renderer import estimate_tokens, format_date

TODAY = "2026-07-20"


class FakeContext:
    """Minimal tool-context stand-in (tools never import the real ToolContext)."""

    def __init__(self, store, today=TODAY, banner=None, raw=None):
        self.store = store
        self._today = today
        self._banner = banner
        self._raw = raw or {}
        self.ready_calls = 0

    async def ensure_ready(self):
        self.ready_calls += 1

    def today(self):
        return self._today

    def data_as_of(self):
        return "08:12"

    def banner(self):
        return self._banner

    async def fetch_raw(self, path, **params):
        if path in self._raw:
            value = self._raw[path]
            if isinstance(value, Exception):
                raise value
            return value
        raise RuntimeError(f"unexpected fetch: {path}")


def run(ctx, **kw):
    return asyncio.run(activity.run(ctx, **kw))


# --- seeds ------------------------------------------------------------------

A_ID, B_ID, C_ID, D_ID = 19483321, 19400000, 19501244, 19488102


def seed_act(store, activity_id, date, sport="running", **kw):
    row = {"activity_id": activity_id, "date": date, "sport": sport,
           "synced_at": "2026-07-20T08:00:00"}
    row.update(kw)
    store.upsert_activity(row)


def seed_default(store):
    seed_act(  # interval run, full stats, athlete RPE
        store, A_ID, "2026-07-15", name="6×800", start_local="2026-07-15T18:04:00",
        duration_s=2525, distance_m=8400, avg_speed=3.3267, avg_hr=141, max_hr=172,
        load=68, aerobic_te=3.8, rpe=7, rpe_source="athlete",
        hr_z1_s=600, hr_z2_s=900, hr_z3_s=500, hr_z4_s=400, hr_z5_s=125,
    )
    seed_act(  # comparable past run (duration within ±25% of A)
        store, B_ID, "2026-07-01", duration_s=2500, distance_m=8300,
        avg_speed=3.32, avg_hr=145, load=61,
    )
    seed_act(  # latest run, no RPE anywhere
        store, C_ID, "2026-07-19", name="easy", duration_s=3744, distance_m=12000,
        avg_speed=3.205, avg_hr=115, load=41,
    )
    seed_act(  # strength session
        store, D_ID, "2026-07-16", sport="strength_training", name="gym",
        duration_s=1779, avg_hr=96, max_hr=132, load=4,
        load_source="srpe_uncalibrated", rpe=3, rpe_source="athlete",
        extra_json='{"totalSets": 18}',
    )


TYPED_SPLITS = {"lapDTOs": [
    {"intensityType": "INTERVAL_WARMUP", "duration": 600, "averageSpeed": 3.0,
     "averageHR": 120},
    {"intensityType": "INTERVAL_ACTIVE", "duration": 238, "distance": 1000,
     "averageSpeed": 4.2, "averageHR": 158},
    {"intensityType": "INTERVAL_RECOVERY", "duration": 120, "averageHR": 140,
     "minHR": 128},
    {"intensityType": "INTERVAL_ACTIVE", "duration": 242, "distance": 1000,
     "averageSpeed": 4.13, "averageHR": 164},
    {"intensityType": "INTERVAL_RECOVERY", "duration": 118, "averageHR": 142,
     "minHR": 135},
    {"intensityType": "INTERVAL_ACTIVE", "duration": 248, "distance": 1000,
     "averageSpeed": 4.03, "averageHR": 167},
    {"intensityType": "INTERVAL_COOLDOWN", "duration": 300, "averageSpeed": 2.8,
     "averageHR": 130},
]}

MANUAL_SPLITS = {"lapDTOs": [
    {"duration": 240, "distance": 1000, "averageSpeed": 4.2, "averageHR": 160},
    {"duration": 120, "distance": 300, "averageSpeed": 2.5, "averageHR": 138, "minHR": 130},
    {"duration": 242, "distance": 1000, "averageSpeed": 4.15, "averageHR": 163},
    {"duration": 121, "distance": 300, "averageSpeed": 2.4, "averageHR": 139, "minHR": 131},
    {"duration": 245, "distance": 1000, "averageSpeed": 4.1, "averageHR": 165},
    {"duration": 119, "distance": 300, "averageSpeed": 2.45, "averageHR": 140, "minHR": 133},
]}

FREEFORM_SPLITS = {"lapDTOs": [
    {"duration": 300 + i, "distance": 1000, "averageSpeed": 3.2 + 0.01 * i,
     "averageHR": 140 + i}
    for i in range(15)
]}

DETAILS = {
    "metricDescriptors": [
        {"key": "directHeartRate", "metricsIndex": 0},
        {"key": "directSpeed", "metricsIndex": 1},
    ],
    "activityDetailMetrics": [
        {"metrics": [120 + i, 3.0 + i * 0.01]} for i in range(60)
    ],
}

SPLITS_PATH = f"/activity-service/activity/{A_ID}/splits"
DETAILS_PATH = f"/activity-service/activity/{A_ID}/details"


# --- resolution -------------------------------------------------------------

def test_resolve_by_id(store):
    seed_default(store)
    ctx = FakeContext(store)
    out = run(ctx, activity_id=A_ID)
    assert "selected: by id" in out
    assert f"id {A_ID}" in out
    assert format_date("2026-07-15") in out
    assert "18:04" in out
    assert ctx.ready_calls == 1  # ensure_ready() awaited first


def test_resolve_by_date(store):
    seed_default(store)
    out = run(FakeContext(store), date="2026-07-16")
    assert "selected: by date" in out
    assert f"id {D_ID}" in out


def test_resolve_latest_of_sport(store):
    seed_default(store)
    out = run(FakeContext(store), sport="running")
    assert "selected: latest run" in out
    assert f"id {C_ID}" in out


def test_resolve_latest_overall(store):
    seed_default(store)
    out = run(FakeContext(store))
    assert "selected: latest activity" in out
    assert f"id {C_ID}" in out


def test_date_no_match_lists_two_nearest_with_ids(store):
    seed_default(store)
    out = run(FakeContext(store), date="2026-07-10")
    assert "No activity on" in out
    # nearest by day-distance: A (5 days), D (6 days)
    assert f"garmin_activity(activity_id={A_ID})" in out
    assert f"garmin_activity(activity_id={D_ID})" in out
    assert out.count("garmin_activity(activity_id=") == 2
    assert "run" in out and "strength" in out


def test_bad_date_format_corrective_error(store):
    seed_default(store)
    out = run(FakeContext(store), date="yesterday")
    assert "date must be YYYY-MM-DD (got 'yesterday')" in out
    assert format_date(TODAY) in out
    assert "garmin_activity(date=" in out


def test_unknown_id_corrective_error(store):
    seed_default(store)
    out = run(FakeContext(store), activity_id=999)
    assert "No activity with id 999" in out
    assert "garmin_activities()" in out
    assert f"activity_id={C_ID}" in out  # latest offered as the real alternative


def test_empty_store_corrective_error(store):
    out = run(FakeContext(store))
    assert "garmin_sync()" in out and "garmin_activities()" in out


# --- standard detail --------------------------------------------------------

def test_standard_stats_and_comparison_verdict(store):
    seed_default(store)
    out = run(FakeContext(store), activity_id=A_ID)
    assert "8.4 km" in out
    assert "42:05" in out
    assert "avg HR 141 / max 172" in out
    assert "load 68" in out
    assert "aerobic TE 3.8" in out
    assert "athlete RPE 7/10" in out
    # comparison against B (2026-07-01): same pace, lower HR
    assert "VERDICT" in out
    assert "07-01" in out
    assert "lower HR" in out


def test_no_comparable_past_session(store):
    seed_act(store, A_ID, "2026-07-15", duration_s=2525, avg_speed=3.3, avg_hr=141)
    out = run(FakeContext(store), activity_id=A_ID)
    assert "no comparable past session" in out


def test_hr_zone_distribution_line(store):
    seed_default(store)
    out = run(FakeContext(store), activity_id=A_ID)
    assert "HR zones:" in out
    assert "Z1 10:00" in out and "Z5 2:05" in out


def test_compliance_matched(store):
    seed_default(store)
    pid = store.upsert_plan_entry(
        {"date": "2026-07-15", "sport": "running", "name": "6x800", "source": "calendar"}
    )
    store.set_plan_match(pid, A_ID, "heuristic")
    out = run(FakeContext(store), activity_id=A_ID)
    assert "planned workout matched (heuristic)" in out


def test_compliance_none(store):
    seed_default(store)
    out = run(FakeContext(store), activity_id=A_ID)
    assert "No planned workout matched to this date — no compliance score." in out


def test_rpe_missing_nudge(store):
    seed_default(store)
    out = run(FakeContext(store), activity_id=C_ID)
    assert "Ask the athlete how it felt" in out
    assert f"garmin_log(rpe=..., activity_id={C_ID})" in out


def test_rpe_present_no_nudge(store):
    seed_default(store)
    out = run(FakeContext(store), activity_id=A_ID)
    assert "garmin_log(rpe=" not in out


def test_strength_summary(store):
    seed_default(store)
    out = run(FakeContext(store), activity_id=D_ID)
    assert "29:39" in out
    assert "avg HR 96 / max 132" in out
    assert "load 4 (sRPE-derived, uncalibrated)" in out
    assert "18 sets" in out
    assert "set detail not synced in v0.1" in out
    assert "km" not in out.split("VERDICT")[0]  # no distance in the stats line


# --- splits detail ----------------------------------------------------------

def test_splits_typed_intervals(store):
    seed_default(store)
    ctx = FakeContext(store, raw={SPLITS_PATH: TYPED_SPLITS})
    out = run(ctx, activity_id=A_ID, detail="splits")
    assert "| Rep | Pace | avgHR | vs rep 1 |" in out
    assert "| 1 | 3:58 | 158 | — |" in out
    assert "| 2 | 4:02 | 164 | +1.7% |" in out
    assert "| 3 | 4:08 | 167 | +4.2% |" in out
    assert "Recoveries 1:59 avg" in out
    assert "HR fell to 128–135" in out


def test_splits_manual_lap_fallback(store):
    seed_default(store)
    ctx = FakeContext(store, raw={SPLITS_PATH: MANUAL_SPLITS})
    out = run(ctx, activity_id=A_ID, detail="splits")
    assert "interval structure inferred" in out
    assert "| 1 | 3:58 | 160 | — |" in out  # fast laps as reps
    assert "Recoveries 2:00 avg" in out
    assert "130–133" in out


def test_splits_freeform_fallback(store):
    seed_default(store)
    ctx = FakeContext(store, raw={SPLITS_PATH: FREEFORM_SPLITS})
    out = run(ctx, activity_id=A_ID, detail="splits")
    assert "no interval structure detected — freeform session." in out
    assert "| Split | km | Time | Pace | avgHR |" in out
    assert "3 more splits" in out  # 15 laps, table capped at 12


def test_splits_fetch_failure_graceful(store):
    seed_default(store)
    ctx = FakeContext(store, raw={})  # any fetch raises
    out = run(ctx, activity_id=A_ID, detail="splits")
    assert "splits unavailable (live fetch failed" in out
    assert "garmin_sync()" in out
    assert "8.4 km" in out  # standard content still rendered


# --- full detail ------------------------------------------------------------

def test_full_curve(store):
    seed_default(store)
    ctx = FakeContext(store, raw={SPLITS_PATH: TYPED_SPLITS, DETAILS_PATH: DETAILS})
    out = run(ctx, activity_id=A_ID, detail="full")
    assert "HR (bpm): 120→" in out and "→179" in out
    assert "Pace (/km): 5:33→" in out and "→4:39" in out
    assert "| Rep | Pace | avgHR | vs rep 1 |" in out  # splits included too


def test_full_details_fetch_failure_graceful(store):
    seed_default(store)
    ctx = FakeContext(store, raw={SPLITS_PATH: TYPED_SPLITS})
    out = run(ctx, activity_id=A_ID, detail="full")
    assert "HR/pace curve unavailable (live fetch failed" in out
    assert "garmin_sync()" in out
    assert "| Rep | Pace | avgHR | vs rep 1 |" in out


# --- caps and banner --------------------------------------------------------

def test_caps(store):
    seed_default(store)
    out = run(FakeContext(store), activity_id=A_ID)
    assert estimate_tokens(out) <= 1000
    ctx = FakeContext(store, raw={SPLITS_PATH: TYPED_SPLITS, DETAILS_PATH: DETAILS})
    assert estimate_tokens(run(ctx, activity_id=A_ID, detail="splits")) <= 2000
    assert estimate_tokens(run(ctx, activity_id=A_ID, detail="full")) <= 4000


def test_banner_on_report_and_error(store):
    seed_default(store)
    banner = "⚠ ACTIVE (since Wed 07-16): HRV suppressed below baseline"
    out = run(FakeContext(store, banner=banner), activity_id=A_ID)
    assert out.startswith(banner)
    err = run(FakeContext(store, banner=banner), activity_id=999)
    assert err.startswith(banner)  # §4.4: banner invariant holds on every response
