"""Garmin Connect adapter using the `garminconnect` library.

Replaces the deprecated `garth` adapter (garth's login broke when Garmin
added TLS fingerprinting in March 2026; garminconnect ships a multi-strategy
login that survives it). Tokens live at `tokenstore` (default
~/.fartlek/tokens/garmin_tokens.json), created by the `fartlek auth` CLI.

Concurrency model:
- the library is sync — every call runs in `asyncio.to_thread`, serialized
  behind an asyncio.Lock (the HTTP session and token refresh are not
  thread-safe);
- an OS-level file lock guards the tokenstore across processes: the MCP
  server and the FastAPI app can both run against the same tokens, and the
  library refreshes + rewrites the token file mid-call (POSIX only; on
  platforms without fcntl the lock is a no-op).

Auth failures drop the cached client so the next call re-reads the
tokenstore — a fresh `ai-coach-login` heals a running server, no restart
needed. Failed connects are cached briefly to avoid hammering Garmin's SSO
with a login storm when tools are called in bursts with stale tokens.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from fartlek.health.exceptions import GarminApiError, GarminAuthError

log = logging.getLogger(__name__)

_LOGIN_HINT = "Run `fartlek auth` to (re)connect your Garmin account, then retry."
_CONNECT_RETRY_TTL = 60.0  # seconds before re-attempting a failed connect


class GarminConnectAdapter:
    """Implements GarminPort using tokens from the `fartlek auth` CLI."""

    def __init__(self, tokenstore: Path):
        self._tokenstore = tokenstore
        self._client: Garmin | None = None
        self._lock = asyncio.Lock()
        self._connect_error: GarminAuthError | None = None
        self._connect_failed_at = 0.0
        self._tokens_mtime_at_failure: float | None = None

    # ---------- token file paths ----------

    def _token_dir(self) -> Path:
        p = self._tokenstore.expanduser()
        return p.parent if p.suffix == ".json" else p

    def _token_file(self) -> Path:
        p = self._tokenstore.expanduser()
        return p if p.suffix == ".json" else p / "garmin_tokens.json"

    def _tokens_mtime(self) -> float | None:
        try:
            return self._token_file().stat().st_mtime
        except OSError:
            return None

    @contextlib.contextmanager
    def _tokenstore_oslock(self) -> Iterator[None]:
        """Cross-process lock: the library rewrites the token file on refresh."""
        if fcntl is None:
            yield
            return
        lock_path = self._token_dir() / "garmin_tokens.lock"
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ---------- session ----------

    def _connect(self) -> Garmin:
        client = Garmin()
        try:
            with self._tokenstore_oslock():
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
        """Must be called with self._lock held."""
        if self._client is not None:
            return self._client

        mtime = self._tokens_mtime()
        if (
            self._connect_error is not None
            and time.monotonic() - self._connect_failed_at < _CONNECT_RETRY_TTL
            and mtime == self._tokens_mtime_at_failure
        ):
            raise self._connect_error  # fail fast — no login storm on bursts

        try:
            self._client = await asyncio.to_thread(self._connect)
        except GarminAuthError as exc:
            self._connect_error = exc
            self._connect_failed_at = time.monotonic()
            self._tokens_mtime_at_failure = mtime
            raise
        self._connect_error = None
        return self._client

    def _do(self, client: Garmin, path: str, params: dict[str, Any]) -> Any:
        try:
            with self._tokenstore_oslock():
                return client.connectapi(path, params=params or None)
        except GarminConnectAuthenticationError as exc:
            raise GarminAuthError(f"Garmin session expired: {exc}. {_LOGIN_HINT}") from exc
        except GarminConnectTooManyRequestsError as exc:
            raise GarminApiError(
                f"Garmin rate limit hit on {path}: {exc}", status=429, endpoint=path
            ) from exc
        except GarminConnectConnectionError as exc:
            raise GarminApiError(f"Garmin API error on {path}: {exc}", endpoint=path) from exc

    async def _call(self, path: str, **params: Any) -> Any:
        async with self._lock:
            client = await self._ensure_client()
            try:
                return await asyncio.to_thread(self._do, client, path, params)
            except GarminAuthError:
                # Drop the dead client: the next call re-reads the tokenstore,
                # so a fresh `ai-coach-login` heals us without a restart.
                self._client = None
                raise

    async def _display_name(self) -> str:
        async with self._lock:
            client = await self._ensure_client()
            name = client.display_name
        if not name:
            raise GarminAuthError("Garmin profile loaded but displayName missing")
        return name

    # ---------- GarminPort ----------

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
