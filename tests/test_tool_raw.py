"""garmin_raw tool — compaction, downsampling, series, validation, cap loop.

Hermetic: FakeContext with canned payloads, no network, no real ToolContext.
"""
from __future__ import annotations

import asyncio

import pytest

from fartlek.health.exceptions import GarminAuthError
from fartlek.mcp_server.tools import raw
from fartlek.render.renderer import estimate_tokens

TODAY = "2026-07-19"


class FakeContext:
    display_name = "display-name-123"

    def __init__(self, payloads=None, banner=None, fail=None):
        self._payloads = payloads or {}
        self._banner = banner
        self._fail = fail
        self.calls: list[tuple[str, dict]] = []
        self.ready = False

    async def ensure_ready(self):
        self.ready = True

    def today(self):
        return TODAY

    def data_as_of(self):
        return "08:00"

    def banner(self):
        return self._banner

    async def fetch_raw(self, path, **params):
        assert self.ready, "ensure_ready() must run before fetch_raw"
        self.calls.append((path, params))
        if self._fail is not None:
            raise self._fail
        return self._payloads.get(path)


def run(ctx, **kw):
    return asyncio.run(raw.run(ctx, **kw))


def hr_series(n, start_val=50.0):
    return [
        {"startGMT": f"2026-07-19T0{i % 8}:00:00.0", "value": start_val + (i % 20)}
        for i in range(n)
    ]


# --- compaction rules ------------------------------------------------------

def test_daily_summary_path_and_compaction():
    path = "/usersummary-service/usersummary/daily/display-name-123"
    ctx = FakeContext(payloads={path: {
        "userProfilePK": 12345,
        "uuid": "abc-def",
        "privacyProtected": True,
        "rule": {"typeKey": "private"},
        "calendarDate": TODAY,
        "restingHeartRate": 48,
        "avgStress": 27.4567,
        "bodyBatteryChange": 0,
        "emptyList": [],
        "emptyStr": "",
        "noneField": None,
        "nested": {"calendarDate": TODAY, "sleepingSeconds": 27300},
    }})
    out = run(ctx, source="daily_summary")
    assert ctx.calls == [(path, {"calendarDate": TODAY})]
    assert "```json" in out
    for gone in ("userProfilePK", "uuid", "privacyProtected", '"rule"', "calendarDate",
                 "emptyList", "emptyStr", "noneField"):
        assert gone not in out
    assert '"restingHeartRate":48' in out
    assert '"avgStress":27.5' in out            # 3 significant places
    assert '"bodyBatteryChange":0' in out       # numeric zero survives
    assert '"sleepingSeconds":27300' in out
    assert "# Raw: daily_summary — Sun 2026-07-19 (data as of 08:00)" in out


def test_float_rounding_three_sig_places_keeps_integer_digits():
    path = f"/hrv-service/hrv/{TODAY}"
    ctx = FakeContext(payloads={path: {
        "weeklyAvg": 57.375, "lastNight": 2.9138, "tiny": 0.0012345, "big": 10023.46,
    }})
    out = run(ctx, source="hrv_detail")
    assert '"weeklyAvg":57.4' in out
    assert '"lastNight":2.91' in out
    assert '"tiny":0.00123' in out
    assert '"big":10023' in out                 # integer digits never truncated


def test_banner_prefixes_output():
    path = f"/wellness-service/wellness/dailyStress/{TODAY}"
    ctx = FakeContext(payloads={path: {"avgStressLevel": 31}},
                      banner="⚠ ACTIVE (since Thu 07-16): HRV below band — see garmin_recovery()")
    out = run(ctx, source="stress_detail")
    assert out.startswith("⚠ ACTIVE")


# --- downsampling boundaries ----------------------------------------------

def test_list_at_max_points_untouched():
    path = f"/hrv-service/hrv/{TODAY}"
    ctx = FakeContext(payloads={path: {"hrvReadings": [{"v": i} for i in range(10)]}})
    out = run(ctx, source="hrv_detail", max_points=10)
    assert "downsampled" not in out
    assert out.count('"v"') == 10


def test_list_over_max_points_downsampled_keeps_first_last():
    path = f"/hrv-service/hrv/{TODAY}"
    ctx = FakeContext(payloads={path: {"hrvReadings": [{"v": i} for i in range(51)]}})
    out = run(ctx, source="hrv_detail", max_points=10)
    assert "hrvReadings downsampled 51→10" in out
    assert '{"v":0}' in out and '{"v":50}' in out
    assert out.count('"v"') == 10
    assert "more points: garmin_raw(source='hrv_detail', max_points=200)" in out


# --- series (sleep_detail only) -------------------------------------------

def sleep_payload():
    return {
        "dailySleepDTO": {"sleepTimeSeconds": 27300, "sleepScores": {"overall": {"value": 82}}},
        "sleepLevels": [{"startGMT": "x", "activityLevel": 1.0} for _ in range(5)],
        "sleepHeartRate": hr_series(300),
        "sleepStress": [{"value": 12} for _ in range(4)],
    }


def test_series_returns_only_that_series():
    path = "/wellness-service/wellness/dailySleepData/display-name-123"
    ctx = FakeContext(payloads={path: sleep_payload()})
    out = run(ctx, source="sleep_detail", series="hr", max_points=20)
    assert ctx.calls == [(path, {"date": TODAY, "nonSleepBufferMinutes": 60})]
    assert "sleepHeartRate" in out
    assert "dailySleepDTO" not in out and "sleepLevels" not in out
    assert "sleepHeartRate downsampled 300→20" in out
    assert "series='hr'" in out


def test_series_invalid_with_other_source_no_fetch():
    ctx = FakeContext()
    out = run(ctx, source="hrv_detail", series="hr")
    assert "only valid with source='sleep_detail'" in out
    assert "hypnogram" in out
    assert ctx.calls == []


def test_series_missing_lists_available():
    path = "/wellness-service/wellness/dailySleepData/display-name-123"
    ctx = FakeContext(payloads={path: sleep_payload()})
    out = run(ctx, source="sleep_detail", series="spo2")
    assert "series 'spo2'" in out and "not present" in out
    assert "hr" in out and "stress" in out      # available alternatives named


# --- activity sources ------------------------------------------------------

@pytest.mark.parametrize("source", ["activity_summary", "activity_splits",
                                    "activity_zones", "weather"])
def test_activity_sources_require_activity_id(source):
    ctx = FakeContext()
    out = run(ctx, source=source)
    assert f"activity_id is required for source='{source}'" in out
    assert "garmin_activities()" in out
    assert ctx.calls == []


@pytest.mark.parametrize("source,suffix", [
    ("activity_summary", ""),
    ("activity_splits", "/splits"),
    ("activity_zones", "/hrTimeInZones"),
    ("weather", "/weather"),
])
def test_activity_paths(source, suffix):
    path = f"/activity-service/activity/987654{suffix}"
    ctx = FakeContext(payloads={path: {"activityId": 987654, "avgHr": 152}})
    out = run(ctx, source=source, activity_id=987654)
    assert ctx.calls == [(path, {})]
    assert '"avgHr":152' in out
    assert "garmin_activity(activity_id=987654)" in out


def test_body_battery_and_race_prediction_paths():
    bb = "/wellness-service/wellness/bodyBattery/reports/daily"
    rp = "/metrics-service/metrics/racepredictions/latest/display-name-123"
    ctx = FakeContext(payloads={bb: [{"charged": 62}], rp: {"time5K": 1290.0}})
    run(ctx, source="body_battery", date="2026-07-18")
    assert ctx.calls[-1] == (bb, {"startDate": "2026-07-18", "endDate": "2026-07-18"})
    out = run(ctx, source="race_predictions")
    assert ctx.calls[-1] == (rp, {})
    assert '"time5K":1290' in out


# --- cap enforcement -------------------------------------------------------

def test_cap_loop_halves_max_points_and_discloses():
    path = "/wellness-service/wellness/dailySleepData/display-name-123"
    payload = {
        "sleepLevels": hr_series(2000),
        "sleepMovement": hr_series(2000),
        "sleepHeartRate": hr_series(2000),
        "sleepStress": hr_series(2000),
    }
    ctx = FakeContext(payloads={path: payload})
    out = run(ctx, source="sleep_detail", max_points=200)
    assert estimate_tokens(out) <= raw.CAP
    assert "```json" in out
    assert "(re-downsampled to max_points=100 to fit the 5,000-token cap)" in out
    assert "downsampled 2000→" in out


def test_cap_unshrinkable_payload_corrective():
    path = "/usersummary-service/usersummary/daily/display-name-123"
    ctx = FakeContext(payloads={path: {f"metricKey{i}": f"value{i:05d}" for i in range(3000)}})
    out = run(ctx, source="daily_summary")
    assert "```json" not in out
    assert "exceeds the 5,000-token cap" in out
    assert "garmin_brief()" in out


# --- errors ----------------------------------------------------------------

def test_fetch_failure_corrective():
    ctx = FakeContext(fail=RuntimeError("connection reset"))
    out = run(ctx, source="training_status")
    assert "Garmin fetch failed for training_status" in out
    assert "connection reset" in out
    assert "garmin_sync()" in out


def test_empty_payload_corrective_names_today():
    ctx = FakeContext()  # fetch_raw returns None
    out = run(ctx, source="hrv_detail", date="2026-07-10")
    assert "no hrv_detail data for Fri 2026-07-10" in out
    assert TODAY in out


def test_invalid_date_corrective():
    ctx = FakeContext()
    out = run(ctx, source="daily_summary", date="19/07/2026")
    assert "Invalid date '19/07/2026'" in out
    assert "YYYY-MM-DD" in out and TODAY in out
    assert ctx.calls == []


def test_auth_error_propagates():
    ctx = FakeContext(fail=GarminAuthError("expired"))
    with pytest.raises(GarminAuthError):
        run(ctx, source="daily_summary")
