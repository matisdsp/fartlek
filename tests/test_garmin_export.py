"""The export adapter: units, routing, and a full import, on a small synthetic export."""
from __future__ import annotations

import json
import zipfile

import pytest

from fartlek.health.adapters.garmin_export import KCAL_FACTOR, GarminExport
from fartlek.paths import store_path
from fartlek.store import Store
from fartlek.sync.export_import import import_export, remembered_export

DAY = "2026-03-10"
START_LOCAL = 1773136800000.0  # 2026-03-10 10:00:00 written as if UTC
ACTIVITY = {
    "activityId": 111,
    "name": "Morning run",
    "activityType": "running",
    "startTimeLocal": START_LOCAL,
    "startTimeGmt": START_LOCAL - 3600_000,
    "duration": 1_800_000.0,
    "movingDuration": 1_750_000.0,
    "elapsedDuration": 1_900_000.0,
    "distance": 600_000.0,
    "avgHr": 150.0,
    "maxHr": 170.0,
    "avgSpeed": 0.33,
    "calories": 400 * KCAL_FACTOR,
    "elevationGain": 5_000.0,
    "activityTrainingLoad": 80.0,
    "aerobicTrainingEffect": 3.1,
    "workoutRpe": 70,
    "workoutFeel": 50,
    "hrTimeInZone_3": 600_000.0,
}
UDS = {
    "calendarDate": DAY,
    "totalSteps": 9000,
    "currentDayRestingHeartRate": 48,
    "restingHeartRate": 50,
    "minAvgHeartRate": 47,
    "minHeartRate": 44,
    "maxHeartRate": 172,
    "wellnessDistanceMeters": 7000.0,
    "totalDistanceMeters": 9000.0,
    "floorsAscendedInMeters": 30.48,
    "wellnessStartTimeLocal": f"{DAY}T00:00:00.0",
    "wellnessStartTimeGmt": "2026-03-09T23:00:00.0",
    "allDayStress": {
        "aggregatorList": [
            {"type": "AWAKE", "averageStressLevel": 40, "maxStressLevel": 80},
            {"type": "TOTAL", "averageStressLevel": 30, "maxStressLevel": 90},
        ]
    },
    "bodyBattery": {
        "bodyBatteryStatList": [
            {"bodyBatteryStatType": "HIGHEST", "statsValue": 88},
            {"bodyBatteryStatType": "LOWEST", "statsValue": 20},
            {"bodyBatteryStatType": "SLEEPEND", "statsValue": 75},
            {"bodyBatteryStatType": "ENDOFDAY", "statsValue": 25},
        ]
    },
}
SLEEP = {
    "calendarDate": DAY,
    "sleepStartTimestampGMT": "2026-03-09T22:00:00.0",
    "sleepEndTimestampGMT": "2026-03-10T06:00:00.0",
    "deepSleepSeconds": 3600,
    "lightSleepSeconds": 18000,
    "remSleepSeconds": 5400,
    "awakeSleepSeconds": 1800,
    "sleepScores": {"overallScore": 81},
}
HEALTH = {"calendarDate": DAY, "metrics": [{"type": "HRV", "value": 60, "status": "IN_RANGE"}]}


def _write_export(path):
    files = {
        "DI_CONNECT/DI-Connect-Fitness/a_0_summarizedActivities.json": [
            {"summarizedActivitiesExport": [ACTIVITY]}
        ],
        "DI_CONNECT/DI-Connect-Aggregator/UDSFile_2026-03-01_2026-04-01.json": [UDS],
        "DI_CONNECT/DI-Connect-Wellness/2026-03-01_2026-04-01_1_sleepData.json": [SLEEP],
        "DI_CONNECT/DI-Connect-Wellness/2026-03-01_2026-04-01_1_healthStatusData.json": [HEALTH],
        "DI_CONNECT/DI-Connect-Wellness/1_heartRateZones.json": [
            {"sport": "DEFAULT", "zone1Floor": 100, "zone2Floor": 120, "zone3Floor": 140,
             "zone4Floor": 160, "zone5Floor": 170, "maxHeartRateUsed": 190}
        ],
        "DI_CONNECT/DI-Connect-Metrics/RunRacePredictions_1_1.json": [
            {"timestamp": "2026-03-01T00:00:00.0", "raceTime5K": 1000, "raceTime10K": 2100,
             "raceTimeHalf": 4700, "raceTimeMarathon": 10000},
            {"timestamp": "2026-03-10T00:00:00.0", "raceTime5K": 990, "raceTime10K": 2080,
             "raceTimeHalf": 4650, "raceTimeMarathon": 9900},
        ],
        "DI_CONNECT/DI-Connect-User/a-social-profile.json": {"displayName": "abc", "profileId": 7},
        "DI_CONNECT/DI-Connect-User/a_personalRecord.json": [
            {"personalRecords": [
                {"personalRecordType": "Best 5km Run", "value": 1000.0, "activityId": 111,
                 "current": True, "prStartTimeGMT": "Tue Mar 10 09:00:00 GMT 2026"},
                {"personalRecordType": "Best 5km Run", "value": 1100.0, "activityId": 99,
                 "current": False, "prStartTimeGMT": "Sun Jan 04 09:00:00 GMT 2026"},
                {"personalRecordType": "Best Full Marathon", "value": 10000.0, "activityId": 5,
                 "current": True, "prStartTimeGMT": "Sun Jan 04 09:00:00 GMT 2026"},
                {"personalRecordType": "Farthest Run", "value": 42000.0, "activityId": 5,
                 "current": True, "prStartTimeGMT": "Sun Jan 04 09:00:00 GMT 2026"},
            ]}
        ],
        "DI_CONNECT/DI-Connect-User/a_gear.json": [
            {"gearDTOS": [{"gearPk": 1, "uuid": "u1", "gearTypeName": "Shoes",
                           "customMakeModel": "Shoe", "gearStatusName": "active"}],
             "gearActivityDTOs": {"1": [{"gearPk": 1, "activityId": 111}]}}
        ],
        "DI_CONNECT/DI-Connect-Wellness/1_userBioMetrics.json": [
            {"metaData": {"calendarDate": DAY}, "weight": {"weight": 70000.0}}
        ],
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, json.dumps(payload))
    return path


@pytest.fixture()
def export_zip(tmp_path):
    return _write_export(tmp_path / "export.zip")


@pytest.fixture()
def export(export_zip):
    return GarminExport(export_zip)


def test_activity_units_become_those_of_the_api(export):
    row = export.fetch("/activitylist-service/activities/search/activities", start=0, limit=5)[0]
    assert row["activityId"] == 111
    assert row["activityType"] == {"typeKey": "running"}
    assert row["startTimeLocal"] == f"{DAY} 10:00:00"
    assert row["duration"] == 1800
    assert row["movingDuration"] == 1750
    assert row["distance"] == 6000
    assert row["averageSpeed"] == pytest.approx(3.3)
    assert row["calories"] == pytest.approx(400)
    assert row["elevationGain"] == 50
    assert row["hrTimeInZone_3"] == 600
    assert row["activityTrainingLoad"] == 80


def test_activity_paging_and_date_filters(export):
    path = "/activitylist-service/activities/search/activities"
    assert len(export.fetch(path, start=0, limit=5)) == 1
    assert export.fetch(path, start=1, limit=5) == []
    assert export.fetch(path, start=0, limit=5, startDate="2026-03-11") == []
    assert len(export.fetch(path, start=0, limit=5, startDate=DAY, endDate=DAY)) == 1


def test_daily_summary_uses_the_fields_that_match_the_api(export):
    day = export.fetch("/usersummary-service/usersummary/daily/abc", calendarDate=DAY)
    assert day["restingHeartRate"] == 48  # not the 7-day restingHeartRate
    assert day["minHeartRate"] == 47
    assert day["totalDistanceMeters"] == 7000
    assert day["averageStressLevel"] == 30  # TOTAL, not AWAKE
    assert day["bodyBatteryAtWakeTime"] == 75  # SLEEPEND, not ENDOFDAY
    assert day["floorsAscended"] == pytest.approx(10)
    assert export.fetch("/usersummary-service/usersummary/daily/abc", calendarDate="2020-01-01") is None


def test_sleep_times_use_the_local_offset_of_the_day(export):
    payload = export.fetch("/wellness-service/wellness/dailySleepData/abc", date=DAY)
    dto = payload["dailySleepDTO"]
    assert dto["sleepScores"]["overall"]["value"] == 81
    assert dto["sleepTimeSeconds"] == 3600 + 18000 + 5400
    gmt_ms = dto["sleepStartTimestampGMT"]
    assert dto["sleepStartTimestampLocal"] - gmt_ms == 3600_000
    assert payload["avgOvernightHrv"] == 60
    assert export.fetch("/wellness-service/wellness/dailySleepData/abc", date="2020-01-01") == {}


def test_hrv_keeps_no_status_because_the_export_uses_another_classification(export):
    assert export.fetch(f"/hrv-service/hrv/{DAY}") == {"hrvSummary": {"lastNightAvg": 60}}


def test_personal_records_keep_current_standard_distances(export):
    rows = export.fetch("/personalrecord-service/personalrecord/prs/abc")
    assert {r["typeId"]: r["value"] for r in rows} == {3: 1000.0, 6: 10000.0}
    assert rows[0]["prStartTimeGmtFormatted"] == DAY


def test_race_predictions_take_the_latest(export):
    assert export.fetch("/metrics-service/metrics/racepredictions/latest/abc")["time5K"] == 990


def test_userstats_serve_a_date_range(export):
    payload = export.fetch(
        "/userstats-service/wellness/daily/abc", metricId=60, fromDate=DAY, untilDate=DAY
    )
    assert payload["allMetrics"]["metricsMap"]["60"] == [{"calendarDate": DAY, "value": 48}]
    assert export.fetch(
        "/userstats-service/wellness/daily/abc", metricId=999, fromDate=DAY, untilDate=DAY
    ) == {}


def test_gear_links_and_odometer(export):
    assert export.fetch("/gear-service/gear/filterGear", userProfilePk=7)[0]["uuid"] == "u1"
    assert export.fetch("/gear-service/gear/filterGear", activityId="111")[0]["uuid"] == "u1"
    assert export.fetch("/gear-service/gear/filterGear", activityId="222") == []
    stats = export.fetch("/gear-service/gear/stats/u1")
    assert stats == {"uuid": "u1", "totalDistance": 6000, "totalActivities": 1}


def test_unknown_routes_are_empty_not_errors(export):
    assert export.fetch("/metrics-service/metrics/trainingreadiness/2026-03-10") is None


def test_a_folder_reads_like_the_zip(export_zip, tmp_path):
    folder = tmp_path / "unzipped"
    zipfile.ZipFile(export_zip).extractall(folder)
    assert GarminExport(folder).last_date == DAY


def test_missing_export_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        GarminExport(tmp_path / "nope.zip")


def test_full_import_fills_the_store_and_remembers_the_path(export_zip, tmp_path, monkeypatch):
    monkeypatch.setenv("FARTLEK_HOME", str(tmp_path / "home"))
    export, merged = import_export(export_zip, days=30)
    assert merged["errors"] == []
    assert remembered_export() == export_zip.resolve()
    with Store(store_path(export.display_name)) as store:
        activities = store._conn.execute("select * from activities").fetchall()
        assert len(activities) == 1
        day = store._conn.execute("select * from days where date = ?", (DAY,)).fetchone()
        assert day["resting_hr"] == 48
        assert day["sleep_score"] == 81
        assert day["hrv_last_night"] == 60
        assert day["weight_g"] == 70000
        assert store._conn.execute("select count(*) as n from gear").fetchone()["n"] == 1


def test_import_twice_changes_nothing(export_zip, tmp_path, monkeypatch):
    monkeypatch.setenv("FARTLEK_HOME", str(tmp_path / "home"))
    export, _ = import_export(export_zip, days=30)

    def counts():
        with Store(store_path(export.display_name)) as store:
            return [
                store._conn.execute(f"select count(*) as n from {t}").fetchone()["n"]
                for t in ("activities", "days", "gear", "activity_gear")
            ]

    first = counts()
    import_export(export_zip, days=30)
    assert counts() == first
