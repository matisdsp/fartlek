"""Tests for fartlek.sync.engine (DESIGN.md §3.3).

Hermetic: a fake fetch dict-dispatches on path prefix and returns canned
payloads modeled on real Garmin shapes; injectable sleep/clock so nothing
actually sleeps; tmp_path stores only.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import make_days

from fartlek.sync.engine import (
    ACTIVITY_HISTORY_DAYS,
    USERSTATS_DAILY_METRICS,
    RateLimiter,
    SyncEngine,
    SyncLock,
    activity_history_days,
    digest_activity,
    digest_daily_summary,
    digest_endurance_score,
    digest_hrv,
    digest_personal_records,
    digest_race_predictions,
    digest_running_tolerance,
    digest_sleep,
)

TODAY = "2026-07-20"


class RateLimited(Exception):
    def __init__(self) -> None:
        super().__init__("429 too many requests")
        self.status = 429


class FakeFetch:
    """Dict-dispatch on path prefix (longest prefix wins). Values may be a
    payload, an Exception instance (raised), or a callable(path, params)."""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, **params):
        self.calls.append((path, params))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix):
                payload = self.routes[prefix]
                if isinstance(payload, Exception):
                    raise payload
                if callable(payload):
                    return payload(path, params)
                return payload
        raise AssertionError(f"unrouted path: {path}")

    def paths(self, prefix: str) -> list[tuple[str, dict]]:
        return [(p, kw) for p, kw in self.calls if p.startswith(prefix)]


# --- canned payloads (modeled on real Garmin shapes) -------------------------

def daily_summary_payload(**over):
    p = {
        "totalSteps": 9000,
        "restingHeartRate": 47,
        "minHeartRate": 44,
        "maxHeartRate": 150,
        "averageStressLevel": 25,
        "maxStressLevel": 80,
        "bodyBatteryHighestValue": 90,
        "bodyBatteryLowestValue": 30,
        "bodyBatteryAtWakeTime": 85,
        "averageSpo2": 96.0,
        "moderateIntensityMinutes": 30,
        "vigorousIntensityMinutes": 10,
        "totalKilocalories": 2500,
        "activeKilocalories": 600,
        "totalDistanceMeters": 8000.0,
        "floorsAscended": 10.0,
    }
    p.update(over)
    return p


def sleep_payload(d: str, *, hrv=52.0, resting_hr=None, score=78):
    """Real account shape: avgOvernightHrv + restingHeartRate at the payload
    TOP level, GMT/local ms-epoch anchors in the DTO (+2h local offset here),
    sleepLevels timestamps in GMT (2h behind local)."""
    prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
    local_ms = int(datetime.fromisoformat(f"{prev}T23:00:00").replace(tzinfo=UTC).timestamp() * 1000)
    dto = {
        "sleepScores": {"overall": {"value": score, "qualifierKey": "GOOD"}},
        "sleepTimeSeconds": 27000,
        "deepSleepSeconds": 5400,
        "lightSleepSeconds": 14400,
        "remSleepSeconds": 5400,
        "awakeSleepSeconds": 1800,
        "sleepStartTimestampLocal": local_ms,
        "sleepEndTimestampLocal": local_ms + 27_000_000,
        "sleepStartTimestampGMT": local_ms - 7_200_000,  # +2h local offset
        "sleepNeed": {"actual": 480, "baseline": 480},
    }
    payload = {
        "dailySleepDTO": dto,
        "avgOvernightHrv": hrv,
        "sleepLevels": [
            {"startGMT": f"{prev}T21:00:00.0", "endGMT": f"{prev}T21:45:00.0", "activityLevel": 1.0},
            {"startGMT": f"{prev}T21:45:00.0", "endGMT": f"{prev}T23:15:00.0", "activityLevel": 0.0},
            {"startGMT": f"{prev}T23:15:00.0", "endGMT": f"{prev}T23:30:00.0", "activityLevel": 3.0},
            {"startGMT": f"{prev}T23:30:00.0", "endGMT": f"{d}T04:30:00.0", "activityLevel": 2.0},
        ],
    }
    if resting_hr is not None:
        payload["restingHeartRate"] = resting_hr
    return payload


def hrv_payload():
    return {
        "hrvSummary": {
            "weeklyAvg": 55,
            "lastNightAvg": 52,
            "status": "BALANCED",
            "baseline": {"lowUpper": 44, "balancedLow": 45, "balancedUpper": 65},
        }
    }


def activity_entry(aid, start_local, *, load=80.0, sport="running", duration=3600.0,
                   rpe=None, feel=None, zones=True, workout_id=None):
    e = {
        "activityId": aid,
        "activityName": f"act{aid}",
        "activityType": {"typeKey": sport},
        "startTimeLocal": start_local,
        "duration": duration,
        "movingDuration": duration - 60,
        "distance": 10000.0,
        "averageHR": 140,
        "maxHR": 165,
        "averageSpeed": 2.8,
        "calories": 500,
        "elevationGain": 120.0,
        "aerobicTrainingEffect": 3.1,
        "anaerobicTrainingEffect": 0.4,
        "vO2MaxValue": 52.0,
    }
    if load is not None:
        e["activityTrainingLoad"] = load
    if zones:
        for i, s in enumerate((600.0, 1200.0, 600.0, 300.0, 60.0), 1):
            e[f"hrTimeInZone_{i}"] = s
    if rpe is not None:
        e["directWorkoutRpe"] = rpe
    if feel is not None:
        e["directWorkoutFeel"] = feel
    if workout_id is not None:
        e["workoutId"] = workout_id
    return e


def userstats_range(path, params):
    """Userstats dispatches on metricId — a fixture that ignores it would let
    every metric backfill land in every column."""
    series = {
        60: ("WELLNESS_RESTING_HEART_RATE", [46, 48]),
        29: ("WELLNESS_TOTAL_STEPS", [12000, 15000]),
        63: ("WELLNESS_AVERAGE_STRESS", [30, 25]),
        82: ("WELLNESS_MIN_AVG_HEART_RATE", [41, 42]),
        28: ("WELLNESS_TOTAL_CALORIES", [2800, 3000]),
        22: ("WELLNESS_ACTIVE_CALORIES", [900, 1100]),
        39: ("WELLNESS_TOTAL_DISTANCE", [12000.0, 15000.0]),
        53: ("WELLNESS_FLOORS_ASCENDED", [10.0, 12.0]),
        51: ("WELLNESS_MODERATE_INTENSITY_MINUTES", [20, 25]),
        52: ("WELLNESS_VIGOROUS_INTENSITY_MINUTES", [60, 70]),
    }
    entry = series.get(params.get("metricId"))
    if entry is None:
        return {"allMetrics": {"metricsMap": {}}}
    key, values = entry
    return {"allMetrics": {"metricsMap": {key: [
        {"calendarDate": "2026-07-18", "value": values[0]},
        {"calendarDate": "2026-07-19", "value": values[1]},
    ]}}}


def base_routes():
    """Every endpoint the engine can hit, with realistic canned payloads."""
    return {
        "/userprofile-service/socialProfile": {"displayName": "athlete1", "fullName": "A"},
        "/userprofile-service/userprofile/user-settings": {"userData": {"weight": 70000.0}},
        "/biometric-service/heartRateZones": [
            {"sport": "DEFAULT", "trainingMethod": "LACTATE_THRESHOLD",
             "lactateThresholdHeartRateUsed": 183, "maxHeartRateUsed": 195,
             "restingHeartRateUsed": 44, "zone1Floor": 101, "zone2Floor": 121,
             "zone3Floor": 142, "zone4Floor": 164, "zone5Floor": 183},
            {"sport": "RUNNING", "trainingMethod": "LACTATE_THRESHOLD",
             "lactateThresholdHeartRateUsed": 176, "maxHeartRateUsed": 195,
             "restingHeartRateUsed": 44, "zone1Floor": 99, "zone2Floor": 117,
             "zone3Floor": 139, "zone4Floor": 156, "zone5Floor": 178},
        ],
        "/personalrecord-service/personalrecord/prs/": [
            {"typeId": 3, "value": 1112.65, "status": "ACCEPTED",
             "prStartTimeGmtFormatted": "2026-06-18T17:45:28.0", "activityId": 111},
            {"typeId": 4, "value": 2400.0, "status": "ACCEPTED",
             "prStartTimeGmtFormatted": "2026-05-01T07:00:00.0", "activityId": 222},
            {"typeId": 5, "value": 6152.0, "status": "ACCEPTED",
             "prStartTimeGmtFormatted": "2026-04-10T08:00:00.0"},
            {"typeId": 6, "value": 12560.0, "status": "ACCEPTED"},
            {"typeId": 7, "value": 102717.0, "status": "ACCEPTED"},  # longest run (m) — ignored
        ],
        "/metrics-service/metrics/racepredictions/latest/": {
            "time5K": 1500.0, "time10K": 3120.0,
            "timeHalfMarathon": 6900.0, "timeMarathon": 14400.0},
        # default: device without these depth metrics (as on the maintainer's)
        "/metrics-service/metrics/endurancescore/stats": {
            "avg": None, "max": None, "enduranceScoreDTO": None, "groupMap": {}},
        "/metrics-service/metrics/runningtolerance/stats": [],
        "/metrics-service/metrics/trainingstatus/aggregated/": {"mostRecentTrainingStatus": {}},
        "/metrics-service/metrics/trainingreadiness/": [{"score": 55, "level": "MODERATE"}],
        "/usersummary-service/usersummary/daily/": daily_summary_payload(),
        "/wellness-service/wellness/dailySleepData/": (
            lambda path, params: sleep_payload(params["date"])
        ),
        "/hrv-service/hrv/": hrv_payload(),
        "/activitylist-service/activities/search/activities": [
            activity_entry(101, "2026-07-19 08:00:00", load=80.0, rpe=70, feel=75),
            activity_entry(102, "2026-07-18 09:00:00", load=None),
        ],
        "/calendar-service/": {
            "calendarItems": [
                {"itemType": "workout", "date": "2026-07-22", "title": "Tempo 40",
                 "workoutId": 111, "sportTypeKey": "running", "duration": 2400},
                {"itemType": "activity", "date": "2026-07-19", "id": 1},
            ]
        },
        "/trainingplan-service/trainingplan/plans": {"trainingPlanList": []},
        "/goal-service/goal/goals": [],
        "/device-service/deviceregistration/devices": [{"deviceId": 123}],
        "/usersummary-service/stats/stress/weekly/": [{"calendarDate": "2026-07-13", "value": 30}],
        "/metrics-service/metrics/maxmet/daily/": [{"generic": {"vo2MaxPreciseValue": 52.3}}],
        "/userstats-service/wellness/daily/": userstats_range,
        "/weight-service/weight/dateRange": {
            "dateWeightList": [{"calendarDate": "2026-07-01", "weight": 70500.0}]
        },
        # Real shape: no startBattery/endBattery — high/low come from the
        # [epoch-ms, value] pairs of bodyBatteryValuesArray.
        "/wellness-service/wellness/bodyBattery/reports/daily": [
            {"date": "2026-07-19", "charged": 60, "drained": 55,
             "bodyBatteryValuesArray": [[1789000000000, 42], [1789010000000, 90],
                                        [1789020000000, 30], [1789030000000, 55]]},
        ],
        "/fitnessstats-service/activity": [{"countOfActivities": 42}],
    }


def make_engine(store, tmp_path, routes, *, page_limit=5, limiter=None):
    fetch = FakeFetch(routes)
    engine = SyncEngine(
        store, fetch, "athlete1", tmp_path,
        limiter=limiter or RateLimiter(sleep=lambda s: None),
        today=TODAY, page_limit=page_limit,
    )
    return engine, fetch


def fake_clock_limiter(min_interval_s=0.5):
    """Limiter whose clock only advances when it sleeps; returns (limiter, slept)."""
    t = {"now": 0.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        t["now"] += s

    return RateLimiter(min_interval_s, sleep=sleep, clock=lambda: t["now"]), slept


# --- RateLimiter -------------------------------------------------------------

def test_rate_limiter_spacing_uses_monotonic_clock():
    limiter, slept = fake_clock_limiter(2.0)
    limiter.wait()                     # first call: no delay
    assert slept == []
    limiter.wait()                     # immediate second call: full interval
    assert slept == [2.0]
    limiter.wait()
    assert slept == [2.0, 2.0]


def test_rate_limiter_backoff_ladder_caps_and_resets():
    limiter, slept = fake_clock_limiter()
    for _ in range(6):
        limiter.backoff_429()
    assert slept == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    limiter.reset()
    limiter.backoff_429()
    assert slept[-1] == 60.0


# --- SyncLock ----------------------------------------------------------------

def test_sync_lock_exclusion_staleness_and_own_pid(tmp_path):
    lock_path = tmp_path / "sync.lock"

    def write_lock(pid, age_s):
        ts = (datetime.now() - timedelta(seconds=age_s)).isoformat()
        lock_path.write_text(json.dumps({"pid": pid, "timestamp": ts}))

    # Fresh foreign lock blocks.
    write_lock(os.getpid() + 1, age_s=30)
    assert SyncLock(tmp_path).acquire() is False
    # Stale (>10 min) foreign lock is taken over.
    write_lock(os.getpid() + 1, age_s=601)
    assert SyncLock(tmp_path).acquire() is True
    # Own-pid fresh lock is re-acquirable.
    write_lock(os.getpid(), age_s=5)
    assert SyncLock(tmp_path).acquire() is True
    # Corrupt lock file counts as stale.
    lock_path.write_text("not json")
    assert SyncLock(tmp_path).acquire() is True
    # release removes the file.
    lock = SyncLock(tmp_path)
    assert lock.acquire()
    lock.release()
    assert not lock_path.exists()


def test_engine_skips_when_lock_held(store, tmp_path):
    (tmp_path / "sync.lock").write_text(
        json.dumps({"pid": os.getpid() + 1, "timestamp": datetime.now().isoformat()})
    )
    engine, fetch = make_engine(store, tmp_path, base_routes())
    result = engine.tier0()
    assert result["skipped"] is True
    assert fetch.calls == []


def test_lock_released_when_tier_raises(store, tmp_path):
    routes = base_routes()
    routes["/activitylist-service/activities/search/activities"] = RuntimeError("boom")
    engine, _ = make_engine(store, tmp_path, routes)
    with pytest.raises(RuntimeError):
        engine.tier1()  # tier1's pagination is not probe-wrapped
    assert not (tmp_path / "sync.lock").exists()
    # lock is usable again
    assert SyncLock(tmp_path).acquire() is True


# --- digesters ---------------------------------------------------------------

def test_digest_daily_summary_maps_and_drops_negative_stress():
    row = digest_daily_summary(daily_summary_payload(averageStressLevel=-1), TODAY)
    assert row["date"] == TODAY
    assert row["steps"] == 9000
    assert row["resting_hr"] == 47
    assert row["body_battery_high"] == 90
    assert row["body_battery_wake"] == 85
    assert row["intensity_vig_min"] == 10
    assert row["calories_total"] == 2500
    assert "avg_stress" not in row          # -1 = Garmin "no data"
    assert digest_daily_summary({}, TODAY) == {"date": TODAY}


def test_digest_sleep_row_timeline_and_hrv():
    row, intervals_json = digest_sleep(sleep_payload(TODAY, resting_hr=46), TODAY)
    assert row["sleep_score"] == 78
    assert row["sleep_duration_h"] == 7.5
    assert row["sleep_deep_h"] == 1.5
    assert row["sleep_need_h"] == 8.0        # sleepNeed dict is minutes
    assert row["hrv_last_night"] == 52.0     # avgOvernightHrv at payload TOP level
    assert row["resting_hr"] == 46           # restingHeartRate at payload TOP level
    assert row["sleep_start_ts"] == "2026-07-19T23:00:00"
    intervals = json.loads(intervals_json)
    assert [i[0] for i in intervals] == ["light", "deep", "awake", "rem"]
    # GMT levels shifted to athlete-local via the DTO anchor pair (+2h)
    assert intervals[0][1] == "2026-07-19T23:00:00"
    assert intervals[-1][2] == "2026-07-20T06:30:00"
    # empty night: partial row is just the date, no timeline
    row, tl = digest_sleep({}, TODAY)
    assert row == {"date": TODAY} and tl is None


def test_digest_sleep_no_anchor_keeps_gmt_and_dto_hrv_fallback():
    p = sleep_payload(TODAY)
    del p["dailySleepDTO"]["sleepStartTimestampGMT"]      # no offset derivable
    p["dailySleepDTO"]["avgOvernightHrv"] = 61.0          # legacy in-DTO shape
    del p["avgOvernightHrv"]
    row, intervals_json = digest_sleep(p, TODAY)
    assert row["hrv_last_night"] == 61.0
    intervals = json.loads(intervals_json)
    assert intervals[0][1] == "2026-07-19T21:00:00.0"     # raw GMT kept as-is


def test_digest_sleep_epoch_millis_timestamps():
    raw = {"dailySleepDTO": {"sleepStartTimestampLocal": 1789110000000,
                             "sleepTimeSeconds": 3600}}
    row, _ = digest_sleep(raw, TODAY)
    # local-epoch millis decode to a naive local ISO string
    assert row["sleep_start_ts"] == "2026-09-11T07:00:00"


def test_digest_hrv():
    row = digest_hrv(hrv_payload(), TODAY)
    assert row == {"date": TODAY, "hrv_last_night": 52,
                   "hrv_weekly_avg": 55, "hrv_status": "BALANCED"}


def test_digest_activity_full_entry():
    row = digest_activity(
        activity_entry(7, "2026-07-19 08:00:00", load=80.0, rpe=70, feel=75, workout_id=42)
    )
    assert row["activity_id"] == 7
    assert row["date"] == "2026-07-19"
    assert row["sport"] == "running"
    assert row["duration_s"] == 3600.0
    assert row["load"] == 80.0 and row["load_source"] == "garmin"
    assert row["trimp"] == pytest.approx(105.0)  # 10*1+20*2+10*3+5*4+1*5
    assert row["rpe"] == 7 and row["rpe_source"] == "watch"  # 70/10
    assert row["feel"] == 4                                  # round(75/25)+1
    assert row["hr_z3_s"] == 600.0
    assert json.loads(row["extra_json"])["workoutId"] == 42


def test_digest_activity_missing_load_left_null_for_ladder():
    row = digest_activity(activity_entry(8, "2026-07-19 08:00:00", load=None, zones=False))
    assert row["load"] is None and row["load_source"] == "none"
    assert "trimp" not in row and "rpe" not in row


def test_digest_personal_records_maps_run_typeids_and_filters():
    raw = [
        {"typeId": 3, "value": 1112.65, "status": "ACCEPTED",
         "prStartTimeGmtFormatted": "2026-06-18T17:45:28.0", "activityId": 111},
        {"typeId": 6, "value": 12560.0, "status": "ACCEPTED"},
        {"typeId": 7, "value": 102717.0, "status": "ACCEPTED"},   # longest run (m) — not a time PR
        {"typeId": 4, "value": -1.0, "status": "ACCEPTED"},       # bad value — dropped
        {"typeId": 5, "value": 6152.0, "status": "PENDING"},      # not accepted — dropped
    ]
    out = digest_personal_records(raw)
    assert set(out) == {"5k", "marathon"}
    assert out["5k"] == {"seconds": 1112.65, "date": "2026-06-18", "activity_id": 111}
    assert out["marathon"]["date"] is None       # no timestamp in payload
    assert digest_personal_records([]) is None    # empty → None, not {}
    assert digest_personal_records(None) is None   # non-list → None


def test_activity_history_days_override(monkeypatch):
    monkeypatch.delenv("FARTLEK_ACTIVITY_HISTORY_DAYS", raising=False)
    assert activity_history_days() == ACTIVITY_HISTORY_DAYS  # default 180
    monkeypatch.setenv("FARTLEK_ACTIVITY_HISTORY_DAYS", "365")
    assert activity_history_days() == 365                    # long-cycle athlete
    monkeypatch.setenv("FARTLEK_ACTIVITY_HISTORY_DAYS", "5000")
    assert activity_history_days() == 730                    # clamped to the ceiling
    monkeypatch.setenv("FARTLEK_ACTIVITY_HISTORY_DAYS", "10")
    assert activity_history_days() == 30                     # clamped to the floor
    monkeypatch.setenv("FARTLEK_ACTIVITY_HISTORY_DAYS", "not-a-number")
    assert activity_history_days() == ACTIVITY_HISTORY_DAYS  # bad value → default


def test_digest_race_predictions_maps_the_four_distances():
    raw = {"time5K": 1290.0, "time10K": 2700.0, "timeHalfMarathon": 6000.0,
           "timeMarathon": 12600.0, "someOtherField": 1}
    out = digest_race_predictions(raw)
    assert out == {"5k": 1290.0, "10k": 2700.0, "half": 6000.0, "marathon": 12600.0}
    # partial payloads keep only the present positive fields
    assert digest_race_predictions({"time5K": 1290.0, "timeMarathon": 0}) == {"5k": 1290.0}
    assert digest_race_predictions({}) is None
    assert digest_race_predictions([]) is None    # non-dict → None


def test_digest_endurance_score_walks_group_map():
    raw = {"avg": 5100, "max": 5300, "enduranceScoreDTO": {"overallScore": 5100},
           "groupMap": {
               "2026-07-06": {"groupAverage": 5000.0, "groupMax": 5200},
               "2026-07-13": {"groupAverage": 5100.0, "groupMax": 5300},
               "2026-07-20": {"groupAverage": None, "groupMax": None}}}  # partial week dropped
    assert digest_endurance_score(raw) == [("2026-07-06", 5000.0), ("2026-07-13", 5100.0)]


def test_digest_endurance_score_all_null_shell_is_empty():
    """Unsupported devices answer 200 with a fully-null shell — must yield []
    so the capability is recorded absent, not available."""
    shell = {"avg": None, "max": None, "enduranceScoreDTO": None,
             "groupMap": {"2026-07-13": {"groupAverage": None, "groupMax": None,
                                         "enduranceContributorDTOList": []}}}
    assert digest_endurance_score(shell) == []
    assert digest_endurance_score({}) == []
    assert digest_endurance_score(None) == []


def test_digest_running_tolerance_recognised_and_unknown_shapes():
    ok = [{"calendarDate": "2026-07-19", "impactLoad": 120.0, "tolerance": 100.0},
          {"calendarDate": "2026-07-20", "ratio": 0.8}]
    assert digest_running_tolerance(ok) == [("2026-07-19", 1.2), ("2026-07-20", 0.8)]
    # unknown shape → [] (absence, never a fabricated number)
    assert digest_running_tolerance([{"calendarDate": "2026-07-20", "mystery": 5}]) == []
    assert digest_running_tolerance([]) == []
    assert digest_running_tolerance(None) == []


# --- tier 0 ------------------------------------------------------------------

def test_tier0_populates_store_and_capability_map(store, tmp_path):
    routes = base_routes()
    routes["/metrics-service/metrics/racepredictions/latest/"] = RuntimeError("HTTP 500")
    engine, fetch = make_engine(store, tmp_path, routes)
    result = engine.tier0()

    assert result["calls"] == 16
    assert result["activities"] == 2
    assert result["plan_entries"] == 1  # next-month duplicate deduped

    # HR zones persisted, RUNNING entry preferred over DEFAULT (176 vs 183)
    zones = store.get_hr_zones()
    assert zones["sport"] == "RUNNING"
    assert zones["lthr"] == 176
    assert zones["zone_floors"] == [99.0, 117.0, 139.0, 156.0, 178.0]
    # weight seeded from user-settings when the range endpoint has nothing
    assert store.get_day(TODAY)["weight_g"] == 70000

    # personal records persisted: typeId 3/4/5/6 → 5k/10k/half/marathon (seconds),
    # non-run typeId 7 (longest run, metres) excluded; date trimmed to YYYY-MM-DD
    prs = store.get_personal_records()
    assert set(prs) == {"5k", "10k", "half", "marathon"}
    assert prs["5k"]["seconds"] == 1112.65
    assert prs["10k"]["seconds"] == 2400.0
    assert prs["5k"]["date"] == "2026-06-18"

    # today's rows: summary + sleep + hrv merged into one days row
    day = store.get_day(TODAY)
    assert day["steps"] == 9000
    assert day["sleep_score"] == 78
    assert day["hrv_last_night"] == 52        # hrv endpoint lastNightAvg
    assert day["hrv_status"] == "BALANCED"
    assert day["body_battery_wake"] == 85
    assert store.get_sleep_timeline(TODAY) != []

    # activities stored; missing-load one resolved by the recompute ladder
    assert store.get_activity(101)["load"] == 80.0
    assert store.get_activity(101)["rpe"] == 7
    assert store.get_activity(102)["load"] is not None
    assert store.get_activity(102)["load_source"] == "trimp_calibrated"

    caps = result["capabilities"]
    assert caps["activityTrainingLoad"]["available"] is True
    assert caps["directWorkoutRpe"]["available"] is True
    assert caps["sleepNeed"]["available"] is True
    assert caps["hrv_baseline"]["available"] is True
    # failing probe recorded, tier not aborted
    assert caps["race_predictions"]["available"] is False
    assert "RuntimeError" in caps["race_predictions"]["detail"]
    # empty payload also recorded as unavailable
    assert caps["goals"]["available"] is False
    assert caps["goals"]["detail"] == "empty response"
    # grade-adjusted speed absent from the canned page
    assert caps["avgGradeAdjustedSpeed"]["available"] is False

    # cursors + staleness
    assert store.get_sync_state("last_activity_start") == "2026-07-19 08:00:00"
    assert engine.last_sync() is not None
    assert engine.is_stale(hours=6) is False

    # idempotent: rerun does not duplicate plan entries
    engine.tier0()
    assert len(store.plan_entries("2026-07-22", "2026-07-22")) == 1


def test_tier0_persists_race_predictions(store, tmp_path):
    """Garmin's own race predictions are digested and stored for the distance
    triangulation (the main tier-0 test forces this endpoint to 500, so it is
    covered separately here on the healthy path)."""
    engine, _ = make_engine(store, tmp_path, base_routes())
    engine.tier0()
    assert store.get_race_predictions() == {
        "5k": 1500.0, "10k": 3120.0, "half": 6900.0, "marathon": 14400.0}


# --- tier 1 ------------------------------------------------------------------

def test_tier1_pagination_terminates_on_short_page(store, tmp_path):
    all_items = [
        activity_entry(200 + i, f"2026-07-{19 - i:02d} 08:00:00", load=60.0 + i)
        for i in range(8)
    ]

    def pages(path, params):
        start = params["start"]
        return all_items[start:start + params["limit"]]

    routes = base_routes()
    routes["/activitylist-service/activities/search/activities"] = pages
    engine, fetch = make_engine(store, tmp_path, routes, page_limit=5)
    result = engine.tier1()

    searches = fetch.paths("/activitylist-service/activities/search/activities")
    assert [kw["start"] for _, kw in searches] == [0, 5]  # stopped after short page
    assert result["activities"] == 8
    assert store.get_activity(207) is not None
    # 2 pages + weight + 3 bb chunks + endurance + tolerance + stress
    # + 2 maxmet + progress + one userstats range call per daily wellness
    # metric (incl. RHR).
    assert result["calls"] == 12 + len(USERSTATS_DAILY_METRICS) + 1

    # RHR range landed in days
    assert store.get_day("2026-07-18")["resting_hr"] == 46
    assert store.get_day("2026-07-19")["resting_hr"] == 48
    assert result["rhr_days"] == 2
    caps = store.get_capabilities()
    assert caps["rhr_range"]["available"] is True
    # weight range → days.weight_g
    assert store.get_day("2026-07-01")["weight_g"] == 70500
    # body battery chunk landed
    assert store.get_day("2026-07-19")["body_battery_high"] == 90


def test_tier1_backfills_daily_wellness_scalars(store, tmp_path):
    """Without the userstats range calls these columns hold TODAY only: the
    daily summary is fetched for one date, so readiness fusion loses markers
    and no trend over them is possible."""
    routes = base_routes()
    routes["/activitylist-service/activities/search/activities"] = lambda p, kw: []
    engine, fetch = make_engine(store, tmp_path, routes)
    engine.tier1()

    day = store.get_day("2026-07-19")
    assert day["steps"] == 15000
    assert day["avg_stress"] == 25
    assert day["min_hr"] == 42
    assert day["calories_total"] == 3000
    assert day["calories_active"] == 1100
    assert day["distance_m"] == 15000.0
    assert day["floors"] == 12.0
    assert day["intensity_mod_min"] == 25
    assert day["intensity_vig_min"] == 70
    assert day["resting_hr"] == 48          # still mapped, unchanged

    # One range call per metric, not one call per day.
    calls = fetch.paths("/userstats-service/wellness/daily/")
    assert len(calls) == len(USERSTATS_DAILY_METRICS) + 1
    assert {kw["metricId"] for _, kw in calls} == set(USERSTATS_DAILY_METRICS) | {60}


def test_tier1_does_not_write_max_hr_from_userstats(store, tmp_path):
    """metricId 83 is a max of AVERAGED heart rate, not the instantaneous daily
    max the daily summary reports. Writing it into days.max_hr would mix two
    definitions in one column."""
    assert 83 not in USERSTATS_DAILY_METRICS
    assert all(col != "max_hr" for col, _ in USERSTATS_DAILY_METRICS.values())


def test_tier1_survives_a_metric_the_account_does_not_serve(store, tmp_path):
    """Garmin answers 500 for metricIds an account has no data for; one dead
    metric must not abort the whole history warm-up."""
    def flaky(path, params):
        if params.get("metricId") == 29:
            raise RuntimeError("API Error 500")
        return userstats_range(path, params)

    routes = base_routes()
    routes["/activitylist-service/activities/search/activities"] = lambda p, kw: []
    routes["/userstats-service/wellness/daily/"] = flaky
    engine, _ = make_engine(store, tmp_path, routes)
    result = engine.tier1()

    assert result["errors"]
    day = store.get_day("2026-07-19")
    assert day["steps"] is None          # the one that failed
    assert day["avg_stress"] == 25       # the others still landed


def test_tier1_rhr_capability_fallback_recorded(store, tmp_path):
    routes = base_routes()
    routes["/userstats-service/wellness/daily/"] = RuntimeError("HTTP 403")
    routes["/activitylist-service/activities/search/activities"] = lambda p, kw: []
    engine, _ = make_engine(store, tmp_path, routes)
    result = engine.tier1()  # must not raise
    assert result["rhr_days"] == 0
    cap = store.get_capabilities()["rhr_range"]
    assert cap["available"] is False
    assert "building RHR forward" in cap["detail"]


def test_tier1_persists_endurance_score_when_the_device_produces_it(store, tmp_path):
    routes = base_routes()
    routes["/metrics-service/metrics/endurancescore/stats"] = {
        "avg": 5100, "max": 5300, "enduranceScoreDTO": {"overallScore": 5100},
        "groupMap": {"2026-07-06": {"groupAverage": 5000.0},
                     "2026-07-13": {"groupAverage": 5100.0}}}
    engine, _ = make_engine(store, tmp_path, routes)
    engine.tier1()
    assert dict(store.get_series("endurance_score", TODAY, 60))["2026-07-13"] == 5100.0
    assert store.get_capabilities()["endurance_score"]["available"] is True


def test_tier1_records_endurance_absent_on_the_null_shell(store, tmp_path):
    """The default route is the all-null 200 shell of an unsupported device:
    capability recorded absent, no fabricated series."""
    engine, _ = make_engine(store, tmp_path, base_routes())
    engine.tier1()
    assert store.get_capabilities()["endurance_score"]["available"] is False
    assert store.get_series("endurance_score", TODAY, 60) == []


# --- tier 2 ------------------------------------------------------------------

def sleep_routes(fail_on_call: int | None = None):
    """Sleep-by-date route; optionally raises on the Nth sleep fetch."""
    counter = {"n": 0}
    routes = base_routes()

    def handler(path, params):
        counter["n"] += 1
        if fail_on_call is not None and counter["n"] == fail_on_call:
            raise RuntimeError("mid-backfill failure")
        return sleep_payload(params["date"], resting_hr=46)

    routes["/wellness-service/wellness/dailySleepData/"] = handler
    return routes


def sleep_dates(fetch):
    return [kw["date"] for _, kw in fetch.paths("/wellness-service/wellness/dailySleepData/")]


def test_tier2_backfills_resumes_from_cursor_and_extends(store, tmp_path):
    # Run 1 fails on the 4th night: 3 nights stored, cursor persisted.
    engine, fetch = make_engine(store, tmp_path, sleep_routes(fail_on_call=4))
    with pytest.raises(RuntimeError):
        engine.tier2(backfill_days=10)
    assert sleep_dates(fetch) == ["2026-07-19", "2026-07-18", "2026-07-17", "2026-07-16"]
    cursor = json.loads(store.get_sync_state("tier2_cursor"))
    assert cursor["phase"] == "sleep"
    assert cursor["next_date"] == "2026-07-16"
    assert cursor["end_date"] == "2026-07-10"

    # Run 2 resumes exactly where it left off — completed nights not re-fetched.
    engine2, fetch2 = make_engine(store, tmp_path, sleep_routes())
    result = engine2.tier2(backfill_days=10)
    assert result["nights"] == 7 and result["done"] is True
    assert sleep_dates(fetch2)[0] == "2026-07-16"
    assert sleep_dates(fetch2)[-1] == "2026-07-10"
    assert json.loads(store.get_sync_state("tier2_cursor"))["phase"] == "done"
    assert len(store.get_sleep_timeline("2026-07-19", days_back=60)) == 10
    assert store.get_day("2026-07-12")["hrv_last_night"] == 52.0  # HRV rides in the payload

    # Done + same depth: no calls at all.
    engine3, fetch3 = make_engine(store, tmp_path, sleep_routes())
    assert engine3.tier2(backfill_days=10) == {"calls": 0, "nights": 0, "done": True}
    assert fetch3.calls == []

    # Deeper backfill extends from the previous end, never re-fetching.
    engine4, fetch4 = make_engine(store, tmp_path, sleep_routes())
    result = engine4.tier2(backfill_days=15)
    assert result["nights"] == 5
    assert sleep_dates(fetch4) == [
        "2026-07-09", "2026-07-08", "2026-07-07", "2026-07-06", "2026-07-05"
    ]
    assert len(store.get_sleep_timeline("2026-07-19", days_back=60)) == 15


def test_tier2_enforces_2s_spacing_and_restores_interval(store, tmp_path):
    limiter, slept = fake_clock_limiter(0.5)
    engine, _ = make_engine(store, tmp_path, sleep_routes(), limiter=limiter)
    engine.tier2(backfill_days=3)
    spacing = [s for s in slept if s < 60]
    assert spacing == [2.0, 2.0]  # first call free, then >=2s between nights
    assert limiter.min_interval_s == 0.5  # restored after the tier


# --- 429 backoff -------------------------------------------------------------

def test_429_backoff_ladder_and_reset_between_endpoints(store, tmp_path):
    routes = base_routes()
    summary_fails = {"n": 0}
    hrv_fails = {"n": 0}

    def summary(path, params):
        summary_fails["n"] += 1
        if summary_fails["n"] <= 3:
            raise RateLimited()
        return daily_summary_payload()

    def hrv(path, params):
        hrv_fails["n"] += 1
        if hrv_fails["n"] <= 1:
            raise RateLimited()
        return hrv_payload()

    routes["/usersummary-service/usersummary/daily/"] = summary
    routes["/hrv-service/hrv/"] = hrv
    routes["/activitylist-service/activities/search/activities"] = lambda p, kw: []
    limiter, slept = fake_clock_limiter(0.5)
    engine, _ = make_engine(store, tmp_path, routes, limiter=limiter)
    result = engine.incremental()

    backoffs = [s for s in slept if s >= 60]
    # ladder climbs 60 → 120 → 240, then success resets it: next 429 sleeps 60
    assert backoffs == [60.0, 120.0, 240.0, 60.0]
    assert result["errors"] == []
    assert store.get_day(TODAY)["steps"] == 9000
    assert store.get_day(TODAY)["hrv_status"] == "BALANCED"


# --- incremental -------------------------------------------------------------

def test_incremental_fetches_new_and_backdated_activities(store, tmp_path):
    """New-ness is by activityId (not date): a backdated late upload on the
    page is picked up; already-stored activities are not re-counted."""
    store.set_sync_state("last_activity_start", "2026-07-19 07:00:00")
    store.upsert_activity(
        {"activity_id": 302, "date": "2026-07-19", "sport": "running",
         "load": 60.0, "load_source": "garmin", "synced_at": "x"}
    )
    routes = base_routes()
    routes["/activitylist-service/activities/search/activities"] = [
        activity_entry(301, "2026-07-20 06:30:00", load=70.0),   # new
        activity_entry(302, "2026-07-19 07:00:00", load=60.0),   # already stored
        activity_entry(303, "2026-07-18 08:00:00", load=50.0),   # backdated late upload
    ]
    engine, _ = make_engine(store, tmp_path, routes)
    result = engine.incremental()

    assert result["new_activities"] == 2
    assert store.get_activity(301) is not None
    assert store.get_activity(303) is not None                   # not skipped
    assert store.get_sync_state("last_activity_start") == "2026-07-20 06:30:00"
    day = store.get_day(TODAY)
    assert day["steps"] == 9000 and day["sleep_score"] == 78
    assert engine.is_stale() is False


def test_incremental_survives_single_endpoint_failure(store, tmp_path):
    routes = base_routes()
    routes["/wellness-service/wellness/dailySleepData/"] = RuntimeError("HTTP 500")
    engine, _ = make_engine(store, tmp_path, routes)
    result = engine.incremental()
    assert len(result["errors"]) == 1 and "dailySleepData" in result["errors"][0]
    day = store.get_day(TODAY)
    assert day["steps"] == 9000            # summary still landed
    assert day["hrv_last_night"] == 52     # hrv still landed
    assert day["sleep_score"] is None


# --- staleness ---------------------------------------------------------------

def test_last_sync_and_is_stale(store, tmp_path):
    engine, _ = make_engine(store, tmp_path, {})
    assert engine.last_sync() is None
    assert engine.is_stale() is True
    store.set_sync_state("last_sync", (datetime.now() - timedelta(hours=7)).isoformat())
    assert engine.is_stale(hours=6.0) is True
    store.set_sync_state("last_sync", (datetime.now() - timedelta(hours=1)).isoformat())
    assert engine.is_stale(hours=6.0) is False
    store.set_sync_state("last_sync", "garbage")
    assert engine.is_stale() is True


# --- recompute_derived end-to-end --------------------------------------------

def test_recompute_derived_end_to_end(store, tmp_path):
    # 30 synthetic days; RHR anomaly injected over the last 3 days.
    rhr = [47] * 27 + [60, 61, 60]
    for row in make_days(TODAY, 30, resting_hr=rhr):
        store.upsert_day(row)

    now = "2026-07-20T09:00:00"
    loads = [40.0, 90.0, 60.0, 70.0]
    for k in range(14):  # >=10 same-sport pairs → regression calibration
        d = (date.fromisoformat(TODAY) - timedelta(days=2 * k + 1)).isoformat()
        row = digest_activity(
            activity_entry(200 + k, f"{d}08:00:00", load=loads[k % 4], duration=3600.0)
        )
        row["synced_at"] = now
        store.upsert_activity(row)
    # missing-load activity with HR zones → TRIMP ladder
    d999 = (date.fromisoformat(TODAY) - timedelta(days=2)).isoformat()
    row = digest_activity(activity_entry(999, f"{d999}07:00:00", load=None))
    row["synced_at"] = now
    store.upsert_activity(row)
    # activity outside the days range → day row auto-created, PMC range extends
    d500 = (date.fromisoformat(TODAY) - timedelta(days=35)).isoformat()
    row = digest_activity(activity_entry(500, f"{d500}08:00:00", load=55.0))
    row["synced_at"] = now
    store.upsert_activity(row)
    # planned workout on an activity day → heuristic match
    plan_date = (date.fromisoformat(TODAY) - timedelta(days=3)).isoformat()
    plan_id = store.upsert_plan_entry({
        "date": plan_date, "sport": "running", "name": "Steady hour",
        "source": "calendar", "planned_json": json.dumps({"duration_s": 3600}),
    })

    def no_fetch(path, **params):
        raise AssertionError(f"recompute_derived must not fetch (got {path})")

    engine = SyncEngine(store, no_fetch, "athlete1", tmp_path,
                        limiter=RateLimiter(sleep=lambda s: None), today=TODAY)
    engine.recompute_derived()

    # ladder resolved the missing load, provenance-flagged
    act = store.get_activity(999)
    assert act["load"] is not None and act["load"] > 0
    assert act["load_source"] == "trimp_calibrated"

    # daily loads materialized (rest days 0, activity days = sum)
    assert store.get_day(d999)["daily_load"] == pytest.approx(
        act["load"])  # day -2 has only activity 999 (garmin-load runs sit on odd offsets)
    assert store.get_day(TODAY)["daily_load"] == 0
    assert store.get_day(d500) is not None  # ledger completeness
    assert store.get_day(d500)["daily_load"] == 55.0

    # PMC: full-range contiguous rewrite (36 days: d500 .. today), warm CTL
    pmc_rows = store.get_pmc(TODAY, 365)
    assert len(pmc_rows) == 36
    assert pmc_rows[0]["date"] == d500 and pmc_rows[-1]["date"] == TODAY
    dates = [r["date"] for r in pmc_rows]
    expected = [(date.fromisoformat(d500) + timedelta(days=i)).isoformat() for i in range(36)]
    assert dates == expected
    assert pmc_rows[-1]["ctl"] > 0
    assert pmc_rows[-1]["tsb"] == pytest.approx(pmc_rows[-2]["ctl"] - pmc_rows[-2]["atl"])

    # baselines cached for today across windows
    b7 = store.get_baseline("resting_hr", TODAY, 7)
    b90 = store.get_baseline("resting_hr", TODAY, 90)
    assert b7 is not None and b7["n"] == 7
    assert b90 is not None and b90["n"] == 30

    # plan matched heuristically to the same-day, same-duration run
    entry = store.plan_entries(plan_date, plan_date)[0]
    assert entry["id"] == plan_id
    assert entry["matched_activity_id"] == 201
    assert entry["match_method"] == "heuristic"

    # the injected anomaly fired: 3-day severe RHR streak → AMBER
    active = store.active_alerts()
    assert [a["metric"] for a in active] == ["resting_hr"]
    assert active[0]["severity"] == "AMBER"
    assert active[0]["date"] == (date.fromisoformat(TODAY) - timedelta(days=2)).isoformat()

    # back in band for 2 days → the diff resolves the alert
    for offset in (1, 0):
        d = (date.fromisoformat(TODAY) - timedelta(days=offset)).isoformat()
        store.upsert_day({"date": d, "resting_hr": 47, "synced_at": now})
    engine.recompute_derived()
    assert store.active_alerts() == []
