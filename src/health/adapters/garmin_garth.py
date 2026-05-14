"""Garmin Connect adapter using the `garth` library.

Reuses OAuth tokens stored at ~/.garth/ (created by the `gc` CLI in the
proper_bases project). The `garth.connectapi` call is sync — we wrap each
call in `asyncio.to_thread` so we don't block the event loop.

When Anthropic-approved OAuth lands, swap this adapter for GarminOAuthAdapter
without touching the rest of the app.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Any

import garth
from garth.exc import GarthException, GarthHTTPError

from src.health.exceptions import GarminApiError, GarminAuthError

log = logging.getLogger(__name__)


class GarminGarthAdapter:
    """Implements GarminPort using local ~/.garth/ tokens."""

    def __init__(self, garth_home: Path):
        self._garth_home = garth_home
        self._display_name: str | None = None

    async def _ensure_session(self) -> str:
        """Lazy-load tokens and cache the displayName needed for several endpoints."""
        if self._display_name is not None:
            return self._display_name

        def _resume() -> str:
            try:
                garth.resume(str(self._garth_home))
            except (FileNotFoundError, GarthException) as exc:
                raise GarminAuthError(
                    f"No valid Garmin tokens at {self._garth_home}. "
                    "Run `gc login` in the proper_bases project first."
                ) from exc

            profile = garth.client.profile
            name = (
                profile.get("displayName")
                if isinstance(profile, dict)
                else getattr(profile, "displayName", None)
            )
            if not name:
                raise GarminAuthError("Garmin profile loaded but displayName missing")
            return name

        self._display_name = await asyncio.to_thread(_resume)
        log.info("Garmin session resumed for %s", self._display_name)
        return self._display_name

    async def _call(self, path: str, **params: Any) -> Any:
        await self._ensure_session()

        def _do() -> Any:
            try:
                return garth.connectapi(path, params=params or None)
            except GarthHTTPError as exc:
                raise GarminApiError(
                    f"Garmin API error on {path}: {exc}",
                    status=getattr(exc, "status_code", None),
                    endpoint=path,
                ) from exc
            except GarthException as exc:
                raise GarminApiError(f"Garth error on {path}: {exc}", endpoint=path) from exc

        return await asyncio.to_thread(_do)

    async def get_daily_summary(self, target_date: date) -> dict[str, Any]:
        name = await self._ensure_session()
        return await self._call(
            f"/usersummary-service/usersummary/daily/{name}",
            calendarDate=target_date.isoformat(),
        ) or {}

    async def get_sleep(self, target_date: date) -> dict[str, Any]:
        name = await self._ensure_session()
        return await self._call(
            f"/wellness-service/wellness/dailySleepData/{name}",
            date=target_date.isoformat(),
            nonSleepBufferMinutes=60,
        ) or {}

    async def list_activities(self, start: date, limit: int) -> list[dict[str, Any]]:
        result = await self._call(
            "/activitylist-service/activities/search/activities",
            startDate=start.isoformat(),
            limit=limit,
            start=0,
        )
        return result or []

    async def get_activity_details(self, activity_id: str) -> dict[str, Any]:
        return await self._call(f"/activity-service/activity/{activity_id}") or {}

    async def get_hrv(self, target_date: date) -> dict[str, Any]:
        return await self._call(f"/hrv-service/hrv/{target_date.isoformat()}") or {}

    async def get_training_readiness(self, target_date: date) -> list[dict[str, Any]]:
        result = await self._call(
            f"/metrics-service/metrics/trainingreadiness/{target_date.isoformat()}"
        )
        return result or []

    async def get_training_status(self, target_date: date) -> dict[str, Any]:
        return await self._call(
            f"/metrics-service/metrics/trainingstatus/aggregated/{target_date.isoformat()}"
        ) or {}

    async def get_body_battery(self, start: date, end: date) -> list[dict[str, Any]]:
        result = await self._call(
            "/wellness-service/wellness/bodyBattery/reports/daily",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
        )
        return result or []

    async def get_stress(self, target_date: date) -> dict[str, Any]:
        return await self._call(
            f"/wellness-service/wellness/dailyStress/{target_date.isoformat()}"
        ) or {}

    async def get_user_profile(self) -> dict[str, Any]:
        return await self._call("/userprofile-service/socialProfile") or {}

    async def get_morning_readiness(self, target_date: date) -> dict[str, Any]:
        return await self._call(
            f"/wellness-service/wellness/morningReadiness/{target_date.isoformat()}"
        ) or {}

    async def get_personal_records(self) -> list[dict[str, Any]]:
        name = await self._ensure_session()
        result = await self._call(f"/personalrecord-service/personalrecord/prs/{name}")
        return result or []
