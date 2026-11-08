"""Read a Garmin account export and answer the sync engine's data requests.

Garmin lets every user download their own data (account > Data Management > Export Your Data).
The sync engine asks for data by Garmin Connect URL. `GarminExport.fetch` routes those URLs to the
export and returns payloads of the same shape, so the engine, the store and the analytics do not
know where the data came from.

The export does not use the units of the Connect API. Conversions below were checked against API
values for the same activities and days on a real account: durations are milliseconds, distances
and elevation centimetres, speeds tenths of m/s, and 4.19002 calorie units per kcal.

Not in the export: per-minute sleep stages, intraday Body Battery, Garmin's sleep need, the HRV
status and weekly average, and grade-adjusted speed per lap. The export's own HRV status is a
different classification from the API's, so it is not converted. The features built on these
report themselves as unavailable.
"""
from __future__ import annotations

import fnmatch
import io
import json
import re
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fartlek.sync.engine import RateLimiter

KCAL_FACTOR = 4.19002

# Personal-record names in the export -> typeId of the Connect API.
_PR_TYPE_IDS = {
    "Best 5km Run": 3,
    "Best 10km Run": 4,
    "Best Half Marathon": 5,
    "Best Full Marathon": 6,
}


class UnlimitedRate(RateLimiter):
    """Reading a file needs no spacing between calls."""

    def wait(self) -> None:
        return None


def _epoch_ms(stamp: str | None) -> int | None:
    """``2026-11-01T22:14:00.0`` read as UTC, in milliseconds."""
    if not stamp:
        return None
    parsed = datetime.fromisoformat(stamp.replace(" ", "T").split(".")[0])
    return int(parsed.replace(tzinfo=UTC).timestamp() * 1000)


def _pr_date(stamp: str | None) -> str | None:
    """``Fri Aug 07 08:12:00 GMT 2026`` to ``2026-08-07``."""
    if not stamp:
        return None
    return datetime.strptime(stamp, "%a %b %d %H:%M:%S GMT %Y").date().isoformat()


def _scaled(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def _stat(uds: dict[str, Any], kind: str) -> float | None:
    battery = uds.get("bodyBattery") or {}
    for entry in battery.get("bodyBatteryStatList") or []:
        if entry.get("bodyBatteryStatType") == kind:
            return entry.get("statsValue")
    return None


def _stress(uds: dict[str, Any], key: str) -> float | None:
    for entry in (uds.get("allDayStress") or {}).get("aggregatorList") or []:
        if entry.get("type") == "TOTAL":
            value = entry.get(key)
            return value if value is None or value >= 0 else None
    return None


def _cadence(lap: Any) -> float | None:
    """Steps per minute. FIT stores strides per minute, plus a fractional part."""
    whole = lap.get_value("avg_running_cadence", fallback=None)
    if whole is None:
        return None
    return 2 * (whole + (lap.get_value("avg_fractional_cadence", fallback=0) or 0))


class _Source:
    """A zip file or an extracted directory, read by file-name pattern."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"No Garmin export at {self.path}")
        self._zip = zipfile.ZipFile(self.path) if self.path.is_file() else None

    def names(self) -> list[str]:
        if self._zip is not None:
            return self._zip.namelist()
        return [str(p.relative_to(self.path)) for p in self.path.rglob("*") if p.is_file()]

    def read(self, name: str) -> bytes:
        if self._zip is not None:
            return self._zip.read(name)
        return (self.path / name).read_bytes()

    def json_files(self, pattern: str) -> Iterator[Any]:
        for name in sorted(self.names()):
            if fnmatch.fnmatch(name.rsplit("/", 1)[-1], pattern):
                yield json.loads(self.read(name))


class GarminExport:
    """The data of one account export, with the engine's ``fetch`` interface."""

    def __init__(self, path: str | Path) -> None:
        self._source = _Source(Path(path))
        self._uds = self._by_date("UDSFile_*.json")
        self._sleep = self._by_date("*sleepData.json")
        self._health = self._by_date("*healthStatusData.json")
        self._activities = self._load_activities()
        self._profile = next(self._source.json_files("*social-profile.json"), {})
        if isinstance(self._profile, list):
            self._profile = self._profile[0] if self._profile else {}
        self._fit_index: dict[int, tuple[str, str]] | None = None
        self._routes: list[tuple[re.Pattern[str], Callable[..., Any]]] = [
            (re.compile(p), fn)
            for p, fn in [
                (r"/userprofile-service/socialProfile$", self._social_profile),
                (r"/userprofile-service/userprofile/user-settings$", self._user_settings),
                (r"/biometric-service/heartRateZones$", self._heart_rate_zones),
                (r"/weight-service/weight/dateRange$", self._weights),
                (r"/personalrecord-service/personalrecord/prs/", self._personal_records),
                (r"/metrics-service/metrics/racepredictions/latest/", self._race_predictions),
                (r"/usersummary-service/usersummary/daily/", self._daily_summary),
                (r"/wellness-service/wellness/dailySleepData/", self._sleep_day),
                (r"/hrv-service/hrv/(?P<day>[\d-]+)$", self._hrv),
                (r"/activitylist-service/activities/search/activities$", self._activity_page),
                (r"/activity-service/activity/(?P<activity_id>\d+)/splits$", self._splits),
                (r"/trainingplan-service/trainingplan/plans$", self._training_plans),
                (r"/gear-service/gear/filterGear$", self._gear),
                (r"/gear-service/gear/stats/(?P<uuid>[^/]+)$", self._gear_stats),
                (r"/userstats-service/wellness/daily/", self._userstats),
            ]
        ]

    # --- identity ---

    @property
    def display_name(self) -> str:
        """The account's Garmin identifier: the same store as a direct connection."""
        name = self._profile.get("displayName") or self._profile.get("profileId")
        if not name:
            raise ValueError("This export has no social profile, so no account identifier.")
        return str(name)

    @property
    def last_date(self) -> str:
        """The last day the export covers: the import treats it as today."""
        return max(self._uds) if self._uds else date.today().isoformat()

    # --- loading ---

    def _by_date(self, pattern: str) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for payload in self._source.json_files(pattern):
            for row in payload if isinstance(payload, list) else [payload]:
                day = row.get("calendarDate")
                if isinstance(day, str):
                    rows[day[:10]] = row
        return rows

    def _load_activities(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for payload in self._source.json_files("*summarizedActivities.json"):
            for blob in payload:
                rows.extend(blob.get("summarizedActivitiesExport") or [])
        return sorted(rows, key=lambda r: r.get("startTimeLocal") or 0, reverse=True)

    # --- fetch ---

    def fetch(self, path: str, **params: Any) -> Any:
        """Same contract as a Connect GET. Data the export does not hold comes back empty."""
        for pattern, handler in self._routes:
            match = pattern.search(path)
            if match:
                return handler(**match.groupdict(), **params)
        return None

    def _social_profile(self, **_: Any) -> dict[str, Any]:
        keys = ("profileId", "displayName")
        return {k: self._profile[k] for k in keys if k in self._profile}

    def _weight_rows(self) -> list[dict[str, Any]]:
        rows = []
        for payload in self._source.json_files("*userBioMetrics.json"):
            for row in payload:
                weight = (row.get("weight") or {}).get("weight")
                day = (row.get("metaData") or {}).get("calendarDate")
                if weight and day:
                    rows.append({"calendarDate": str(day)[:10], "weight": weight})
        return sorted(rows, key=lambda r: r["calendarDate"])

    def _user_settings(self, **_: Any) -> dict[str, Any] | None:
        rows = self._weight_rows()
        return {"userData": {"weight": rows[-1]["weight"]}} if rows else None

    def _weights(self, startDate: str, endDate: str, **_: Any) -> dict[str, Any]:  # noqa: N803
        rows = [r for r in self._weight_rows() if startDate <= r["calendarDate"] <= endDate]
        return {"dateWeightList": rows}

    def _heart_rate_zones(self, **_: Any) -> list[dict[str, Any]]:
        zones = next(self._source.json_files("*heartRateZones.json"), [])
        return zones if isinstance(zones, list) else [zones]

    def _personal_records(self, **_: Any) -> list[dict[str, Any]]:
        payload = next(self._source.json_files("*personalRecord.json"), [])
        rows: list[dict[str, Any]] = []
        for blob in payload if isinstance(payload, list) else [payload]:
            for record in blob.get("personalRecords") or []:
                type_id = _PR_TYPE_IDS.get(record.get("personalRecordType"))
                if type_id is None or not record.get("current"):
                    continue
                rows.append(
                    {
                        "typeId": type_id,
                        "value": record.get("value"),
                        "status": "ACCEPTED",
                        "activityId": record.get("activityId"),
                        "prStartTimeGmtFormatted": _pr_date(record.get("prStartTimeGMT")),
                    }
                )
        return rows

    def _race_predictions(self, **_: Any) -> dict[str, Any] | None:
        rows = [
            row
            for payload in self._source.json_files("RunRacePredictions_*.json")
            for row in payload
        ]
        if not rows:
            return None
        latest = max(rows, key=lambda r: r.get("timestamp") or "")
        return {
            "time5K": latest.get("raceTime5K"),
            "time10K": latest.get("raceTime10K"),
            "timeHalfMarathon": latest.get("raceTimeHalf"),
            "timeMarathon": latest.get("raceTimeMarathon"),
        }

    def _daily_summary(self, calendarDate: str, **_: Any) -> dict[str, Any] | None:  # noqa: N803
        uds = self._uds.get(calendarDate)
        if uds is None:
            return None
        return {
            "calendarDate": calendarDate,
            "totalSteps": uds.get("totalSteps"),
            "restingHeartRate": uds.get("currentDayRestingHeartRate"),
            "minHeartRate": uds.get("minAvgHeartRate"),
            "maxHeartRate": uds.get("maxHeartRate"),
            "averageStressLevel": _stress(uds, "averageStressLevel"),
            "maxStressLevel": _stress(uds, "maxStressLevel"),
            "bodyBatteryHighestValue": _stat(uds, "HIGHEST"),
            "bodyBatteryLowestValue": _stat(uds, "LOWEST"),
            "bodyBatteryAtWakeTime": _stat(uds, "SLEEPEND"),
            "averageSpo2": uds.get("averageSpo2Value"),
            "moderateIntensityMinutes": uds.get("moderateIntensityMinutes"),
            "vigorousIntensityMinutes": uds.get("vigorousIntensityMinutes"),
            "totalKilocalories": uds.get("totalKilocalories"),
            "activeKilocalories": uds.get("activeKilocalories"),
            "totalDistanceMeters": uds.get("wellnessDistanceMeters"),
            "floorsAscended": _scaled(uds.get("floorsAscendedInMeters"), 1 / 3.048),
        }

    def _local_offset(self, day: str) -> timedelta | None:
        """Offset of local time from UTC on a given day, from the daily summary."""
        uds = self._uds.get(day) or {}
        local, gmt = uds.get("wellnessStartTimeLocal"), uds.get("wellnessStartTimeGmt")
        if not local or not gmt:
            return None
        return datetime.fromisoformat(local.split(".")[0]) - datetime.fromisoformat(
            gmt.split(".")[0]
        )

    def _hrv_row(self, day: str) -> dict[str, Any]:
        for metric in (self._health.get(day) or {}).get("metrics") or []:
            if metric.get("type") == "HRV":
                return metric
        return {}

    def _sleep_day(self, date: str, **_: Any) -> dict[str, Any]:
        sleep = self._sleep.get(date)
        if not sleep or sleep.get("deepSleepSeconds") is None:
            return {}
        stages = [sleep.get(k) or 0 for k in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")]
        dto: dict[str, Any] = {
            "calendarDate": date,
            "sleepTimeSeconds": sum(stages),
            "deepSleepSeconds": sleep.get("deepSleepSeconds"),
            "lightSleepSeconds": sleep.get("lightSleepSeconds"),
            "remSleepSeconds": sleep.get("remSleepSeconds"),
            "awakeSleepSeconds": sleep.get("awakeSleepSeconds"),
        }
        score = (sleep.get("sleepScores") or {}).get("overallScore")
        if score is not None:
            dto["sleepScores"] = {"overall": {"value": score}}
        start, end = _epoch_ms(sleep.get("sleepStartTimestampGMT")), _epoch_ms(
            sleep.get("sleepEndTimestampGMT")
        )
        offset = self._local_offset(date)
        if start is not None:
            dto["sleepStartTimestampGMT"] = start
        if offset is not None and start is not None and end is not None:
            shift = int(offset.total_seconds() * 1000)
            dto["sleepStartTimestampLocal"] = start + shift
            dto["sleepEndTimestampLocal"] = end + shift
        payload: dict[str, Any] = {"dailySleepDTO": dto}
        hrv = self._hrv_row(date).get("value")
        if hrv is not None:
            payload["avgOvernightHrv"] = hrv
        resting = (self._uds.get(date) or {}).get("currentDayRestingHeartRate")
        if resting is not None:
            payload["restingHeartRate"] = resting
        return payload

    def _hrv(self, day: str, **_: Any) -> dict[str, Any]:
        metric = self._hrv_row(day)
        if metric.get("value") is None:
            return {}
        return {"hrvSummary": {"lastNightAvg": metric["value"]}}

    def _userstats(
        self, metricId: int, fromDate: str, untilDate: str, **_: Any  # noqa: N803
    ) -> dict[str, Any]:
        readers: dict[int, Callable[[dict[str, Any]], Any]] = {
            60: lambda u: u.get("currentDayRestingHeartRate"),
            29: lambda u: u.get("totalSteps"),
            63: lambda u: _stress(u, "averageStressLevel"),
            82: lambda u: u.get("minAvgHeartRate"),
            28: lambda u: u.get("totalKilocalories"),
            22: lambda u: u.get("activeKilocalories"),
            39: lambda u: u.get("wellnessDistanceMeters"),
            53: lambda u: _scaled(u.get("floorsAscendedInMeters"), 1 / 3.048),
            51: lambda u: u.get("moderateIntensityMinutes"),
            52: lambda u: u.get("vigorousIntensityMinutes"),
        }
        read = readers.get(int(metricId))
        if read is None:
            return {}
        values = [
            {"calendarDate": day, "value": read(uds)}
            for day, uds in sorted(self._uds.items())
            if fromDate <= day <= untilDate and read(uds) is not None
        ]
        return {"allMetrics": {"metricsMap": {str(metricId): values}}}

    @staticmethod
    def _activity(row: dict[str, Any]) -> dict[str, Any]:
        local_ms, gmt_ms = row.get("startTimeLocal"), row.get("startTimeGmt")

        def stamp(ms: float | None) -> str | None:
            if ms is None:
                return None
            return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")

        out: dict[str, Any] = {
            "activityId": row.get("activityId"),
            "activityName": row.get("name"),
            "activityType": {"typeKey": row.get("activityType") or "other"},
            "startTimeLocal": stamp(local_ms),
            "startTimeGMT": stamp(gmt_ms),
            "duration": _scaled(row.get("duration"), 1e-3),
            "movingDuration": _scaled(row.get("movingDuration"), 1e-3),
            "elapsedDuration": _scaled(row.get("elapsedDuration"), 1e-3),
            "distance": _scaled(row.get("distance"), 1e-2),
            "averageHR": row.get("avgHr"),
            "maxHR": row.get("maxHr"),
            "averageSpeed": _scaled(row.get("avgSpeed"), 10),
            "calories": _scaled(row.get("calories"), 1 / KCAL_FACTOR),
            "elevationGain": _scaled(row.get("elevationGain"), 1e-2),
            "aerobicTrainingEffect": row.get("aerobicTrainingEffect"),
            "anaerobicTrainingEffect": row.get("anaerobicTrainingEffect"),
            "vO2MaxValue": row.get("vO2MaxValue"),
            "activityTrainingLoad": row.get("activityTrainingLoad"),
            "directWorkoutRpe": row.get("workoutRpe"),
            "directWorkoutFeel": row.get("workoutFeel"),
            "workoutId": row.get("workoutId"),
            "trainingEffectLabel": row.get("trainingEffectLabel"),
            "avgGradeAdjustedSpeed": _scaled(row.get("avgGradeAdjustedSpeed"), 10),
            "averageRunningCadenceInStepsPerMinute": row.get("avgRunCadence"),
            "maxRunCadence": row.get("maxRunCadence"),
            "deviceId": row.get("deviceId"),
            "lapCount": row.get("lapCount"),
        }
        for zone in range(1, 6):
            out[f"hrTimeInZone_{zone}"] = _scaled(row.get(f"hrTimeInZone_{zone}"), 1e-3)
        return out

    def _activity_page(
        self,
        start: int = 0,
        limit: int = 50,
        startDate: str | None = None,  # noqa: N803
        endDate: str | None = None,  # noqa: N803
        **_: Any,
    ) -> list[dict[str, Any]]:
        rows = [self._activity(row) for row in self._activities]
        if startDate:
            rows = [r for r in rows if (r["startTimeLocal"] or "")[:10] >= startDate]
        if endDate:
            rows = [r for r in rows if (r["startTimeLocal"] or "")[:10] <= endDate]
        return rows[int(start) : int(start) + int(limit)]

    def _training_plans(self, **_: Any) -> Any:
        payload = next(self._source.json_files("*trainingPlan.json"), None)
        return payload or None

    # --- gear ---

    def _gear_file(self) -> dict[str, Any]:
        payload = next(self._source.json_files("*gear.json"), {})
        return (payload[0] if payload else {}) if isinstance(payload, list) else payload

    def _gear(self, activityId: str | None = None, **_: Any) -> list[dict[str, Any]]:  # noqa: N803
        data = self._gear_file()
        items = data.get("gearDTOS") or []
        if activityId is None:
            return items
        links = data.get("gearActivityDTOs") or {}
        worn = {
            str(pk)
            for pk, entries in links.items()
            if any(str(e.get("activityId")) == str(activityId) for e in entries)
        }
        return [g for g in items if str(g.get("gearPk")) in worn]

    def _gear_stats(self, uuid: str, **_: Any) -> dict[str, Any] | None:
        data = self._gear_file()
        gear = next((g for g in data.get("gearDTOS") or [] if g.get("uuid") == uuid), None)
        if gear is None:
            return None
        links = (data.get("gearActivityDTOs") or {}).get(str(gear.get("gearPk"))) or []
        ids = {e.get("activityId") for e in links}
        total = sum(
            (row.get("distance") or 0) / 100 for row in self._activities if row["activityId"] in ids
        )
        return {"uuid": uuid, "totalDistance": total, "totalActivities": len(ids)}

    # --- laps, from the FIT files in the export ---

    def _fit_archives(self) -> Iterator[tuple[str, zipfile.ZipFile]]:
        for name in sorted(self._source.names()):
            if fnmatch.fnmatch(name.rsplit("/", 1)[-1], "UploadedFiles_*.zip"):
                yield name, zipfile.ZipFile(io.BytesIO(self._source.read(name)))

    def _build_fit_index(self) -> dict[int, tuple[str, str]]:
        """FIT activity files by creation time, the only link to an activity in the export."""
        import fitdecode

        index: dict[int, tuple[str, str]] = {}
        for archive_name, archive in self._fit_archives():
            for entry in archive.namelist():
                try:
                    with fitdecode.FitReader(io.BytesIO(archive.read(entry))) as fit:
                        for frame in fit:
                            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                                continue
                            if frame.name != "file_id":
                                break
                            if frame.get_value("type", fallback=None) == "activity":
                                created = frame.get_value("time_created", fallback=None)
                                if created is not None:
                                    index[int(created.timestamp())] = (archive_name, entry)
                            break
                except Exception:  # noqa: BLE001 - one corrupt upload must not stop an import
                    continue
        return index

    def _splits(self, activity_id: str, **_: Any) -> dict[str, Any]:
        row = next((r for r in self._activities if str(r.get("activityId")) == activity_id), None)
        if row is None or not row.get("startTimeGmt"):
            return {}
        if self._fit_index is None:
            self._fit_index = self._build_fit_index()
        start = int(row["startTimeGmt"] / 1000)
        found = self._fit_index.get(start)
        if found is None:
            return {}
        import fitdecode

        archive_name, entry = found
        archive = zipfile.ZipFile(io.BytesIO(self._source.read(archive_name)))
        laps: list[dict[str, Any]] = []
        with fitdecode.FitReader(io.BytesIO(archive.read(entry))) as fit:
            for frame in fit:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA or frame.name != "lap":
                    continue
                get = frame.get_value
                intensity = get("intensity", fallback=None)
                laps.append(
                    {
                        "lapIndex": len(laps) + 1,
                        "distance": get("total_distance", fallback=None),
                        "duration": get("total_timer_time", fallback=None),
                        "movingDuration": get("total_timer_time", fallback=None),
                        "averageHR": get("avg_heart_rate", fallback=None),
                        "maxHR": get("max_heart_rate", fallback=None),
                        "averageSpeed": get("enhanced_avg_speed", fallback=None),
                        "elevationGain": get("total_ascent", fallback=None),
                        "elevationLoss": get("total_descent", fallback=None),
                        "averageRunCadence": _cadence(frame),
                        "averageTemperature": get("avg_temperature", fallback=None),
                        "intensityType": str(intensity).upper() if intensity else None,
                    }
                )
        return {"lapDTOs": laps}
