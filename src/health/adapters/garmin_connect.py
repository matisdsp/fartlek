"""Garmin Connect adapter using the `garminconnect` library.

Replaces the deprecated `garth` adapter (garth's login broke when Garmin
added TLS fingerprinting in March 2026; garminconnect ships a multi-strategy
login that survives it). Tokens live at `tokenstore` (default
~/.garminconnect/garmin_tokens.json), created by the `ai-coach-login` CLI.

The library is sync — every call is wrapped in `asyncio.to_thread` and
serialized behind a lock, because the underlying HTTP session and its
token-refresh path are not thread-safe.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from src.health.exceptions import GarminApiError, GarminAuthError

log = logging.getLogger(__name__)

_LOGIN_HINT = "Run `uv run ai-coach-login` to (re)connect your Garmin account."


class GarminConnectAdapter:
    """Implements GarminPort using tokens from the `ai-coach-login` CLI."""

    def __init__(self, tokenstore: Path):
        self._tokenstore = tokenstore
        self._client: Garmin | None = None
        self._lock = asyncio.Lock()

    def _connect(self) -> Garmin:
        client = Garmin()
        try:
            client.login(tokenstore=str(self._tokenstore))
        except FileNotFoundError as exc:
            raise GarminAuthError(
                f"No Garmin tokens at {self._tokenstore}. {_LOGIN_HINT}"
            ) from exc
        except GarminConnectAuthenticationError as exc:
            raise GarminAuthError(
                f"Garmin tokens at {self._tokenstore} are missing or expired. {_LOGIN_HINT}"
            ) from exc
        except (GarminConnectConnectionError, GarminConnectTooManyRequestsError) as exc:
            raise GarminAuthError(
                f"Could not resume Garmin session from {self._tokenstore}: {exc}. {_LOGIN_HINT}"
            ) from exc
        log.info("Garmin session resumed for %s", client.display_name)
        return client

    async def _ensure_client(self) -> Garmin:
        async with self._lock:
            if self._client is None:
                self._client = await asyncio.to_thread(self._connect)
            return self._client

    async def _call(self, path: str, **params: Any) -> Any:
        client = await self._ensure_client()

        def _do() -> Any:
            try:
                return client.connectapi(path, params=params or None)
            except GarminConnectAuthenticationError as exc:
                raise GarminAuthError(f"Garmin session expired: {exc}. {_LOGIN_HINT}") from exc
            except GarminConnectTooManyRequestsError as exc:
                raise GarminApiError(
                    f"Garmin rate limit hit on {path}: {exc}", status=429, endpoint=path
                ) from exc
            except GarminConnectConnectionError as exc:
                raise GarminApiError(f"Garmin API error on {path}: {exc}", endpoint=path) from exc

        async with self._lock:
            return await asyncio.to_thread(_do)

    async def _display_name(self) -> str:
        client = await self._ensure_client()
        if not client.display_name:
            raise GarminAuthError("Garmin profile loaded but displayName missing")
        return client.display_name

    async def get_daily_summary(self, target_date: date) -> dict[str, Any]:
        name = await self._display_name()
        return await self._call(
            f"/usersummary-service/usersummary/daily/{name}",
            calendarDate=target_date.isoformat(),
        ) or {}

    async def get_sleep(self, target_date: date) -> dict[str, Any]:
        name = await self._display_name()
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
        name = await self._display_name()
        result = await self._call(f"/personalrecord-service/personalrecord/prs/{name}")
        return result or []
