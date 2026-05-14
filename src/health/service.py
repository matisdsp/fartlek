"""HealthService — application service for the health context.

Single entry point for fetching wearable data. Filters Garmin's verbose JSON
down to the fields useful for coaching (keeps LLM token cost in check).
Cross-context callers (Claude tools) come through here, never the adapter.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from src.health.domain import DateRange
from src.health.exceptions import GarminApiError
from src.health.ports import GarminPort

log = logging.getLogger(__name__)


def _pick(d: dict[str, Any] | None, keys: list[str]) -> dict[str, Any]:
    if not d:
        return {}
    return {k: d[k] for k in keys if k in d and d[k] is not None}


class HealthService:
    def __init__(self, garmin: GarminPort):
        self._garmin = garmin

    # ---------- helpers ----------

    @staticmethod
    def _resolve_date(value: str | date | None) -> date:
        if value is None:
            return date.today()
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)

    # ---------- use cases ----------

    async def get_daily_health(self, target_date: str | date | None = None) -> dict[str, Any]:
        d = self._resolve_date(target_date)
        raw = await self._garmin.get_daily_summary(d)
        return {
            "date": d.isoformat(),
            **_pick(raw, [
                "totalSteps", "dailyStepGoal", "totalKilocalories", "activeKilocalories",
                "totalDistanceMeters", "floorsAscended", "floorsAscendedGoal",
                "moderateIntensityMinutes", "vigorousIntensityMinutes", "intensityMinutesGoal",
                "restingHeartRate", "minHeartRate", "maxHeartRate", "averageHeartRate",
                "averageStressLevel", "maxStressLevel",
                "stressDuration", "restStressDuration", "activityStressDuration",
                "bodyBatteryChargedValue", "bodyBatteryDrainedValue",
                "bodyBatteryHighestValue", "bodyBatteryLowestValue",
                "averageSpo2", "lowestSpo2",
                "lastSyncTimestampGMT",
            ]),
        }

    async def get_sleep(self, target_date: str | date | None = None) -> dict[str, Any]:
        d = self._resolve_date(target_date)
        raw = await self._garmin.get_sleep(d)
        daily = raw.get("dailySleepDTO", {}) if isinstance(raw, dict) else {}

        def _hrs(seconds: int | float | None) -> float | None:
            return round(seconds / 3600, 2) if seconds else None

        return {
            "date": d.isoformat(),
            "score": (daily.get("sleepScores") or {}).get("overall", {}).get("value"),
            "quality": (daily.get("sleepScores") or {}).get("overall", {}).get("qualifierKey"),
            "duration_hours": _hrs(daily.get("sleepTimeSeconds")),
            "deep_hours": _hrs(daily.get("deepSleepSeconds")),
            "light_hours": _hrs(daily.get("lightSleepSeconds")),
            "rem_hours": _hrs(daily.get("remSleepSeconds")),
            "awake_hours": _hrs(daily.get("awakeSleepSeconds")),
            "sleep_start_local": daily.get("sleepStartTimestampLocal"),
            "sleep_end_local": daily.get("sleepEndTimestampLocal"),
            "average_spo2": daily.get("averageSpO2Value"),
            "average_respiration": daily.get("averageRespirationValue"),
            "avg_sleep_stress": daily.get("avgSleepStress"),
        }

    async def get_recent_activities(self, days_back: int = 14, limit: int = 20) -> list[dict[str, Any]]:
        start = date.today() - timedelta(days=days_back)
        activities = await self._garmin.list_activities(start, limit)
        return [
            {
                "activity_id": a.get("activityId"),
                "name": a.get("activityName"),
                "type": (a.get("activityType") or {}).get("typeKey"),
                "start_local": a.get("startTimeLocal"),
                "duration_seconds": a.get("duration"),
                "distance_meters": a.get("distance"),
                "average_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "average_speed": a.get("averageSpeed"),
                "calories": a.get("calories"),
                "elevation_gain_meters": a.get("elevationGain"),
                "training_effect": a.get("aerobicTrainingEffect"),
                "anaerobic_training_effect": a.get("anaerobicTrainingEffect"),
                "training_effect_label": a.get("trainingEffectLabel"),
                "vo2_max": a.get("vO2MaxValue"),
            }
            for a in activities
        ]

    async def get_activity_details(self, activity_id: str) -> dict[str, Any]:
        raw = await self._garmin.get_activity_details(activity_id)
        summary = raw.get("summaryDTO", {}) if isinstance(raw, dict) else {}
        activity_type = (raw.get("activityTypeDTO") or {}).get("typeKey") if isinstance(raw, dict) else None
        return {
            "activity_id": activity_id,
            "type": activity_type,
            **_pick(summary, [
                "startTimeLocal", "duration", "movingDuration", "distance",
                "averageSpeed", "maxSpeed", "averageHR", "maxHR",
                "averageRunCadence", "maxRunCadence",
                "averagePower", "maxPower", "normalizedPower",
                "trainingStressScore", "intensityFactor",
                "elevationGain", "elevationLoss", "minElevation", "maxElevation",
                "calories", "averageBikingCadence", "averageRunningCadenceInStepsPerMinute",
                "aerobicTrainingEffect", "anaerobicTrainingEffect", "trainingEffectLabel",
                "vO2MaxValue", "lactateThresholdBpm", "lactateThresholdSpeed",
                "groundContactTime", "verticalOscillation", "verticalRatio",
                "averageStrideLength",
            ]),
            "hr_zones": raw.get("hrTimeInZone_1") and {
                f"zone_{i}_seconds": raw.get(f"hrTimeInZone_{i}")
                for i in range(1, 6)
                if raw.get(f"hrTimeInZone_{i}") is not None
            } or None,
        }

    async def get_training_readiness(self, target_date: str | date | None = None) -> dict[str, Any]:
        d = self._resolve_date(target_date)
        result = await self._garmin.get_training_readiness(d)
        # Garmin returns a list (one entry per measurement); take the latest
        latest = max(result, key=lambda r: r.get("timestamp", ""), default={}) if result else {}
        return {
            "date": d.isoformat(),
            **_pick(latest, [
                "score", "level", "feedbackLong", "feedbackShort",
                "sleepScore", "sleepHistoryFactorPercent",
                "recoveryTime", "recoveryTimeFactorPercent",
                "acwrFactorPercent", "acuteLoad",
                "hrvFactorPercent", "stressHistoryFactorPercent",
                "timestamp",
            ]),
        }

    async def get_training_status(self, target_date: str | date | None = None) -> dict[str, Any]:
        d = self._resolve_date(target_date)
        raw = await self._garmin.get_training_status(d)
        latest_status = raw.get("mostRecentTrainingStatus", {}).get("latestTrainingStatusData", {})
        # latestTrainingStatusData keys are deviceIds — grab first non-empty
        first = next(iter(latest_status.values()), {}) if isinstance(latest_status, dict) else {}
        load = raw.get("mostRecentTrainingLoadBalance", {}).get("metricsTrainingLoadBalanceDTOMap", {})
        first_load = next(iter(load.values()), {}) if isinstance(load, dict) else {}
        return {
            "date": d.isoformat(),
            "training_status": first.get("trainingStatus"),
            "training_status_feedback": first.get("trainingStatusFeedbackPhrase"),
            "vo2_max": first.get("vo2MaxValue"),
            "fitness_age": first.get("fitnessAge"),
            "acute_load": first.get("acuteLoad"),
            "chronic_load": first.get("loadTunnelMin") and first.get("loadTunnelMax")
                and (first["loadTunnelMin"] + first["loadTunnelMax"]) / 2 or None,
            "load_focus_anaerobic": first_load.get("monthlyLoadAnaerobic"),
            "load_focus_high_aerobic": first_load.get("monthlyLoadAerobicHigh"),
            "load_focus_low_aerobic": first_load.get("monthlyLoadAerobicLow"),
        }

    async def get_hrv(self, target_date: str | date | None = None) -> dict[str, Any]:
        d = self._resolve_date(target_date)
        raw = await self._garmin.get_hrv(d)
        summary = raw.get("hrvSummary", {}) if isinstance(raw, dict) else {}
        baseline = summary.get("baseline", {}) if isinstance(summary, dict) else {}
        return {
            "date": d.isoformat(),
            "weekly_avg": summary.get("weeklyAvg"),
            "last_night_avg": summary.get("lastNightAvg"),
            "last_night_5_min_high": summary.get("lastNight5MinHigh"),
            "status": summary.get("status"),
            "feedback_phrase": summary.get("feedbackPhrase"),
            "baseline_low": baseline.get("lowUpper"),
            "baseline_balanced_lower": baseline.get("balancedLow"),
            "baseline_balanced_upper": baseline.get("balancedUpper"),
        }

    async def get_body_battery(self, start_date: str | date | None = None, end_date: str | date | None = None) -> list[dict[str, Any]]:
        end = self._resolve_date(end_date)
        start = self._resolve_date(start_date) if start_date else end - timedelta(days=7)
        DateRange(start=start, end=end)  # invariant check
        raw = await self._garmin.get_body_battery(start, end)
        return [
            {
                "date": e.get("date"),
                "charged": e.get("charged"),
                "drained": e.get("drained"),
                "highest": e.get("startBattery"),
                "lowest": e.get("endBattery"),
            }
            for e in raw
            if isinstance(e, dict)
        ]

    async def get_stress(self, target_date: str | date | None = None) -> dict[str, Any]:
        d = self._resolve_date(target_date)
        raw = await self._garmin.get_stress(d)
        return _pick(raw, [
            "calendarDate", "maxStressLevel", "avgStressLevel",
            "stressChartValueOffset", "stressDuration",
            "restStressDuration", "activityStressDuration",
            "uncategorizedStressDuration", "totalStressDuration",
            "lowStressDuration", "mediumStressDuration", "highStressDuration",
        ])

    async def get_user_profile(self) -> dict[str, Any]:
        raw = await self._garmin.get_user_profile()
        return _pick(raw, [
            "displayName", "fullName", "userName",
            "age", "gender", "weight", "height",
            "vo2MaxRunning", "vo2MaxCycling",
            "lactateThresholdSpeed", "lactateThresholdHeartRate",
            "ftpAutoDetected", "trainingStatus",
            "intensityMinutesWeeklyGoal", "moderateIntensityMinutesHrZone",
        ])

    async def get_morning_readiness(self, target_date: str | date | None = None) -> dict[str, Any]:
        d = self._resolve_date(target_date)
        try:
            raw = await self._garmin.get_morning_readiness(d)
        except GarminApiError as exc:
            log.warning("morning readiness unavailable: %s", exc)
            return {"date": d.isoformat(), "available": False, "error": str(exc)}
        return {
            "date": d.isoformat(),
            "available": True,
            **_pick(raw, ["overallScore", "qualifier", "feedback", "timestamp"]),
        }

    async def get_personal_records(self) -> list[dict[str, Any]]:
        records = await self._garmin.get_personal_records()
        return [
            _pick(r, [
                "personalRecordId", "typeId", "activityName", "activityType",
                "value", "prStartTimeGmtFormatted", "prTypeLabelKey",
            ])
            for r in records
        ]
