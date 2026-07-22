"""Sync engine (DESIGN.md §3.3).

Garmin is hit ONLY from here. Fetch-once → digest → store → serve.

- RateLimiter: sequential-call spacing (≥2s during tier-2 backfill, ≥0.5s
  otherwise) + the 429 backoff ladder 60s → ×2 → cap 15 min; reset() on success.
- SyncLock: advisory <account_dir>/sync.lock (pid + ISO timestamp JSON), stale
  after 10 min; a second process skips sync and reads the store.
- Digesters: pure functions raw payload → schema.sql row dicts; raw payloads
  are never stored. The sleep digester also emits the compact interval timeline.
- SyncEngine.tier0/tier1/tier2/incremental per §3.3 cold-start tiers; every
  tier ends with recompute_derived() (loads → PMC → baselines → matcher →
  alert diff) and stamps sync_state['last_sync'].

Timezone rules (§3.3): all daily bucketing uses Garmin calendarDate; sleep
belongs to its wake-date; 'today' = server-local date (injectable for tests).

The engine is sync and self-contained: it receives a ``fetch(path, **params)``
callable (the garminconnect client's connectapi, already error-translated by
the caller) plus the display_name, so it stays unit-testable with a fake fetch.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fartlek.analytics import alerts as alerts_mod
from fartlek.analytics import baselines as baselines_mod
from fartlek.analytics import load as load_mod
from fartlek.analytics import matcher as matcher_mod
from fartlek.analytics import pmc as pmc_mod
from fartlek.store import Store

Fetch = Callable[..., Any]  # fetch(path: str, **params) -> parsed JSON

BACKOFF_START_S = 60.0
BACKOFF_CAP_S = 900.0
BASELINE_WINDOWS = (7, 28, 60, 90)
ACTIVITY_HISTORY_DAYS = 180

# userstats-service metricId -> (days column, cast). One range call per metric
# backfills the whole window, replacing ~1 daily-summary call per day.
#
# Every id here was cross-checked against a fully-elapsed day's daily summary
# and matched exactly. metricId 83 (WELLNESS_MAX_AVG_HEART_RATE) is
# deliberately ABSENT: it read 138 where the summary's maxHeartRate read 140,
# i.e. it is a max of averaged HR, not the instantaneous daily max. Writing it
# into days.max_hr would mix two definitions in one column.
USERSTATS_DAILY_METRICS: dict[int, tuple[str, Any]] = {
    29: ("steps", int),
    63: ("avg_stress", int),
    82: ("min_hr", int),
    28: ("calories_total", int),
    22: ("calories_active", int),
    39: ("distance_m", float),
    53: ("floors", float),
    51: ("intensity_mod_min", int),
    52: ("intensity_vig_min", int),
}

SPLITS_HISTORY_DAYS = 120   # §3.2 #12: 8-12 weeks of qualifying sessions
SPLITS_PER_RUN = 40         # cap per invocation, so one call never runs long
_SPLITS_SKIP_CAP = 500      # bound on the remembered "this one has no laps" list


class RateLimiter:
    """Monotonic-clock spacing between sequential calls + 429 backoff ladder.

    ``min_interval_s`` is mutable (tier 2 raises it to 2s for the backfill).
    ``sleep``/``clock`` are injectable so tests never actually sleep.
    """

    def __init__(
        self,
        min_interval_s: float = 0.5,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self._backoff_s = 0.0  # 0 = ladder cleared; else the last backoff slept

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.min_interval_s - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()

    def backoff_429(self) -> None:
        """Sleep per the 60s → ×2 → 900s-cap ladder; reset() clears the ladder."""
        if self._backoff_s <= 0:
            self._backoff_s = BACKOFF_START_S
        else:
            self._backoff_s = min(self._backoff_s * 2, BACKOFF_CAP_S)
        self._sleep(self._backoff_s)
        self._last = self._clock()

    def reset(self) -> None:
        self._backoff_s = 0.0


class SyncLock:
    """Advisory sync.lock file: {"pid": int, "timestamp": iso}.

    acquire() is False while another pid holds a fresh (<stale_after_s) lock;
    a stale or own-pid lock is taken over. Corrupt lock files count as stale.
    """

    REFRESH_INTERVAL_S = 60.0

    def __init__(self, account_dir: Path, stale_after_s: int = 600):
        self.path = Path(account_dir) / "sync.lock"
        self.stale_after_s = stale_after_s
        self._held = False
        self._last_write = 0.0

    def _read_holder(self) -> tuple[int, datetime] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return int(data["pid"]), datetime.fromisoformat(data["timestamp"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self) -> None:
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "timestamp": datetime.now().isoformat()}),
            encoding="utf-8",
        )
        self._last_write = time.monotonic()

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL first: two processes racing on a missing lock cannot both win.
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            holder = self._read_holder()
            if holder is not None:
                pid, ts = holder
                age_s = (datetime.now() - ts).total_seconds()
                if age_s < self.stale_after_s and pid != os.getpid():
                    return False
            # stale, corrupt, or our own leftover — take it over
        self._write()
        self._held = True
        return True

    def refresh(self) -> None:
        """Re-stamp the lock so a long tier (many calls + backoffs) is not
        mistaken for stale by a second process. No-op unless held."""
        if self._held and time.monotonic() - self._last_write >= self.REFRESH_INTERVAL_S:
            self._write()

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False


# --- digesters (pure) ---

_DAILY_MAP = {
    "steps": "totalSteps",
    "resting_hr": "restingHeartRate",
    "min_hr": "minHeartRate",
    "max_hr": "maxHeartRate",
    "avg_stress": "averageStressLevel",
    "max_stress": "maxStressLevel",
    "body_battery_high": "bodyBatteryHighestValue",
    "body_battery_low": "bodyBatteryLowestValue",
    "body_battery_wake": "bodyBatteryAtWakeTime",
    "spo2_avg": "averageSpo2",
    "intensity_mod_min": "moderateIntensityMinutes",
    "intensity_vig_min": "vigorousIntensityMinutes",
    "calories_total": "totalKilocalories",
    "calories_active": "activeKilocalories",
    "distance_m": "totalDistanceMeters",
    "floors": "floorsAscended",
}

_SLEEP_HOURS_MAP = {
    "sleep_duration_h": "sleepTimeSeconds",
    "sleep_deep_h": "deepSleepSeconds",
    "sleep_light_h": "lightSleepSeconds",
    "sleep_rem_h": "remSleepSeconds",
    "sleep_awake_h": "awakeSleepSeconds",
}

_SLEEP_LEVEL_NAMES = {0: "deep", 1: "light", 2: "rem", 3: "awake"}

_ACTIVITY_MAP = {
    "name": "activityName",
    "start_local": "startTimeLocal",
    "duration_s": "duration",
    "moving_s": "movingDuration",
    "distance_m": "distance",
    "avg_hr": "averageHR",
    "max_hr": "maxHR",
    "avg_speed": "averageSpeed",
    "calories": "calories",
    "elevation_gain": "elevationGain",
    "aerobic_te": "aerobicTrainingEffect",
    "anaerobic_te": "anaerobicTrainingEffect",
    "vo2max": "vO2MaxValue",
}

# Compact spillover kept from the list payload (workoutId feeds the matcher).
_ACTIVITY_EXTRA_KEYS = (
    "workoutId",
    "trainingEffectLabel",
    "avgGradeAdjustedSpeed",
    "averageRunningCadenceInStepsPerMinute",
    "maxRunCadence",
    "minTemperature",
    "maxTemperature",
    "deviceId",
    "lapCount",
)


def _local_iso(ts: Any) -> str | None:
    """Garmin local-epoch millis (or an ISO string) → naive local ISO string."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return (
            datetime.fromtimestamp(ts / 1000, tz=UTC)
            .replace(tzinfo=None)
            .isoformat()
        )
    return str(ts)


def digest_daily_summary(raw: dict[str, Any], date: str) -> dict[str, Any]:
    """Daily summary payload → days-row partial (only present, non-null fields)."""
    row: dict[str, Any] = {"date": date}
    for col, key in _DAILY_MAP.items():
        v = (raw or {}).get(key)
        if v is not None:
            row[col] = v
    # Garmin uses negative stress values for "no data".
    for col in ("avg_stress", "max_stress"):
        if row.get(col) is not None and row[col] < 0:
            del row[col]
    return row


def digest_sleep(raw: dict[str, Any], date: str) -> tuple[dict[str, Any], str | None]:
    """Sleep payload → (days-row partial, intervals_json or None).

    hrv_last_night (avgOvernightHrv) and restingHeartRate ride at the payload
    TOP level on real accounts (DTO fallback kept for older shapes); sleepNeed
    is minutes when a dict ({"actual": ...}), seconds when a bare number. The
    timeline is the compact [["deep"|"light"|"rem"|"awake", start, end], ...]
    from sleepLevels (activityLevel 0-3), shifted from GMT to athlete-local
    time using the DTO's GMT/local anchor pair (raw GMT kept when the anchors
    are absent); unknown levels are skipped.
    """
    raw = raw or {}
    dto = raw.get("dailySleepDTO") or {}
    row: dict[str, Any] = {"date": date}
    overall = ((dto.get("sleepScores") or {}).get("overall") or {}).get("value")
    if overall is not None:
        row["sleep_score"] = overall
    for col, key in _SLEEP_HOURS_MAP.items():
        v = dto.get(key)
        if v is not None:
            row[col] = round(v / 3600, 3)
    need = dto.get("sleepNeed")
    if isinstance(need, dict):
        if need.get("actual") is not None:
            row["sleep_need_h"] = round(need["actual"] / 60, 3)
    elif isinstance(need, (int, float)):
        row["sleep_need_h"] = round(need / 3600, 3)
    start = _local_iso(dto.get("sleepStartTimestampLocal"))
    if start:
        row["sleep_start_ts"] = start
    end = _local_iso(dto.get("sleepEndTimestampLocal"))
    if end:
        row["sleep_end_ts"] = end
    hrv = raw.get("avgOvernightHrv")
    if hrv is None:
        hrv = dto.get("avgOvernightHrv")
    if hrv is not None:
        row["hrv_last_night"] = hrv
    rhr = raw.get("restingHeartRate")  # RHR fallback source (§3.2 #9)
    if rhr is None:
        rhr = dto.get("restingHeartRate")
    if rhr is not None:
        row["resting_hr"] = rhr

    # GMT → athlete-local shift for the timeline (SRI/jetlag depend on local time).
    offset = None
    gmt_anchor, local_anchor = dto.get("sleepStartTimestampGMT"), dto.get("sleepStartTimestampLocal")
    if isinstance(gmt_anchor, (int, float)) and isinstance(local_anchor, (int, float)):
        offset = timedelta(milliseconds=local_anchor - gmt_anchor)

    def _to_local(ts_gmt: str) -> str:
        if offset is None:
            return ts_gmt
        return (datetime.fromisoformat(ts_gmt) + offset).isoformat()

    intervals = []
    for level in raw.get("sleepLevels") or []:
        name = _SLEEP_LEVEL_NAMES.get(int(level.get("activityLevel", -1)))
        if name and level.get("startGMT") and level.get("endGMT"):
            intervals.append([name, _to_local(level["startGMT"]), _to_local(level["endGMT"])])
    intervals_json = json.dumps(intervals, separators=(",", ":")) if intervals else None
    return row, intervals_json


def digest_body_battery_day(e: Any) -> dict[str, Any] | None:
    """One entry of the bodyBattery/reports/daily payload → days-row partial.

    The real payload carries `date`, `charged`, `drained` and a
    `bodyBatteryValuesArray` of [epoch-ms, value] pairs — high/low are the
    max/min of that series (there are no startBattery/endBattery fields).
    Returns None when the entry has no date or no usable values.
    """
    if not isinstance(e, dict) or not e.get("date"):
        return None
    row: dict[str, Any] = {"date": e["date"]}
    values = [
        pair[1]
        for pair in e.get("bodyBatteryValuesArray") or []
        if isinstance(pair, (list, tuple)) and len(pair) >= 2 and pair[1] is not None
    ]
    if values:
        row["body_battery_high"] = max(values)
        row["body_battery_low"] = min(values)
    return row if len(row) > 1 else None


def digest_hrv(raw: dict[str, Any], date: str) -> dict[str, Any]:
    """HRV daily payload (hrvSummary) → days-row partial."""
    summary = (raw or {}).get("hrvSummary") or {}
    row: dict[str, Any] = {"date": date}
    if summary.get("lastNightAvg") is not None:
        row["hrv_last_night"] = summary["lastNightAvg"]
    if summary.get("weeklyAvg") is not None:
        row["hrv_weekly_avg"] = summary["weeklyAvg"]
    if summary.get("status"):
        row["hrv_status"] = summary["status"]
    return row


def digest_activity(raw: dict[str, Any]) -> dict[str, Any]:
    """One activities-list entry → activities row.

    Includes watch-RPE conversion (analytics.load.convert_watch_rpe,
    rpe_source='watch'), the raw Edwards TRIMP when zones are present, and the
    compact extra_json spillover (workoutId etc.). Missing activityTrainingLoad
    leaves load NULL / load_source='none' for the recompute ladder.
    """
    row: dict[str, Any] = {"activity_id": raw["activityId"]}
    row["date"] = str(raw.get("startTimeLocal") or "")[:10]
    row["sport"] = (raw.get("activityType") or {}).get("typeKey") or "other"
    for col, key in _ACTIVITY_MAP.items():
        v = raw.get(key)
        if v is not None:
            row[col] = v
    for i in range(1, 6):
        v = raw.get(f"hrTimeInZone_{i}")
        if v is not None:
            row[f"hr_z{i}_s"] = v
    load = raw.get("activityTrainingLoad")
    row["load"] = load
    row["load_source"] = "garmin" if load is not None else "none"
    trimp = load_mod.edwards_trimp(row)
    if trimp is not None:
        row["trimp"] = trimp
    rpe, feel = load_mod.convert_watch_rpe(
        raw.get("directWorkoutRpe"), raw.get("directWorkoutFeel")
    )
    if rpe is not None:
        row["rpe"] = rpe
        row["rpe_source"] = "watch"
    if feel is not None:
        row["feel"] = feel
    extra = {k: raw[k] for k in _ACTIVITY_EXTRA_KEYS if raw.get(k) is not None}
    if extra:
        row["extra_json"] = json.dumps(extra, separators=(",", ":"))
    return row


_LAP_MAP = {
    "distance_m": "distance",
    "duration_s": "duration",
    "moving_s": "movingDuration",
    "avg_hr": "averageHR",
    "max_hr": "maxHR",
    "avg_speed": "averageSpeed",
    "gap_speed": "avgGradeAdjustedSpeed",
    "elev_gain": "elevationGain",
    "elev_loss": "elevationLoss",
    "avg_cadence": "averageRunCadence",
    "temp_c": "averageTemperature",
}


def digest_laps(raw: dict[str, Any], activity_id: int) -> list[dict[str, Any]]:
    """/activity/{id}/splits payload → activity_laps rows.

    Reads `lapDTOs` (auto/manual laps) and falls back to `splits` (the typed
    endpoint's container). Laps with no distance AND no duration carry no
    information and are dropped; everything else is kept as-is, including
    recovery laps — filtering by intensity is an analysis decision, not a
    storage one.

    `avgGradeAdjustedSpeed` and `averageTemperature` are device-dependent and
    stay NULL when absent (never substituted with the flat-ground speed, which
    would silently turn a hilly lap into a fast one).
    """
    laps = (raw or {}).get("lapDTOs") or (raw or {}).get("splits") or []
    rows: list[dict[str, Any]] = []
    for i, lap in enumerate(laps):
        if not isinstance(lap, dict):
            continue
        if not lap.get("distance") and not lap.get("duration"):
            continue
        # `or` would be wrong here: lap index 0 is the FIRST lap of every
        # session, not a missing value.
        index = lap.get("lapIndex")
        if index is None:
            index = lap.get("messageIndex")
        row: dict[str, Any] = {
            "activity_id": activity_id,
            "lap_index": i if index is None else int(index),
        }
        for col, key in _LAP_MAP.items():
            v = lap.get(key)
            if v is not None:
                row[col] = v
        itype = lap.get("intensityType") or lap.get("type")
        if itype:
            row["intensity_type"] = str(itype)
        rows.append(row)
    # Lap index is the primary key: de-duplicate defensively rather than
    # letting a malformed payload abort the whole activity.
    seen: dict[int, dict[str, Any]] = {}
    for row in rows:
        seen[int(row["lap_index"])] = row
    return [seen[k] for k in sorted(seen)]


class SyncEngine:
    """Fetch-once → digest → store orchestrator. All methods synchronous.

    Injectables (tests): ``limiter`` (fake sleep/clock), ``today`` (fixed
    server-local date), ``page_limit`` (activities page size).
    """

    def __init__(
        self,
        store: Store,
        fetch: Fetch,
        display_name: str,
        account_dir: Path,
        *,
        limiter: RateLimiter | None = None,
        today: str | None = None,
        page_limit: int = 50,
    ):
        self.store = store
        self.fetch = fetch
        self.display_name = display_name
        self.account_dir = Path(account_dir)
        self.limiter = limiter or RateLimiter()
        self.lock = SyncLock(self.account_dir)
        self.page_limit = page_limit
        self._today_override = today
        self._calls = 0

    # --- plumbing ---

    def _today(self) -> str:
        return self._today_override or date.today().isoformat()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")

    MAX_429_RETRIES = 6  # ~30 min of ladder — beyond that it's a lockout, not a limit

    def _call(self, path: str, **params: Any) -> Any:
        """One rate-limited fetch; retries through the 429 backoff ladder,
        giving up after MAX_429_RETRIES so a Garmin lockout fails loudly
        instead of hanging the sync forever."""
        attempts = 0
        while True:
            self.limiter.wait()
            self.lock.refresh()
            self._calls += 1
            try:
                result = self.fetch(path, **params)
            except Exception as exc:
                if getattr(exc, "status", None) == 429:
                    attempts += 1
                    if attempts > self.MAX_429_RETRIES:
                        raise
                    self.limiter.backoff_429()
                    continue
                raise
            self.limiter.reset()
            return result

    def _probe(self, key: str, path: str, **params: Any) -> Any:
        """Capability probe: any error/empty payload → available=False + detail,
        never aborts the tier. Returns the payload or None."""
        try:
            payload = self._call(path, **params)
        except Exception as exc:
            self.store.set_capability(key, False, detail=f"{type(exc).__name__}: {exc}")
            return None
        if not payload:
            self.store.set_capability(key, False, detail="empty response")
            return None
        self.store.set_capability(key, True)
        return payload

    def _try_call(self, path: str, errors: list[str], **params: Any) -> Any:
        try:
            return self._call(path, **params)
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            return None

    def _locked(self, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run one sync tier under the advisory lock; always release on exit."""
        if not self.lock.acquire():
            return {"skipped": True, "reason": "another sync process holds sync.lock", "calls": 0}
        try:
            return fn()
        finally:
            self.lock.release()

    # --- store helpers ---

    def _upsert_day(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["synced_at"] = self._now_iso()
        self.store.upsert_day(row)

    def _upsert_activity(self, row: dict[str, Any]) -> None:
        """Upsert a digested activity; an athlete-logged RPE never gets
        overwritten by the watch value (§3.1 precedence)."""
        existing = self.store.get_activity(row["activity_id"])
        if existing and existing.get("rpe_source") == "athlete":
            row = {k: v for k, v in row.items() if k not in ("rpe", "rpe_source", "feel")}
        row = dict(row)
        row["synced_at"] = self._now_iso()
        self.store.upsert_activity(row)

    def _store_sleep(self, raw: dict[str, Any], wake_date: str) -> None:
        row, intervals_json = digest_sleep(raw, wake_date)
        if len(row) > 1:  # more than just the date key
            self._upsert_day(row)
        if intervals_json:
            self.store.upsert_sleep_timeline(wake_date, intervals_json)

    def _advance_activity_cursor(self, activities: list[dict[str, Any]]) -> None:
        starts = [str(a.get("startTimeLocal") or "") for a in activities]
        newest = max((s for s in starts if s), default=None)
        if newest is None:
            return
        current = self.store.get_sync_state("last_activity_start")
        if current is None or newest > current:
            self.store.set_sync_state("last_activity_start", newest)

    def _record_activity_capabilities(self, activities: list[dict[str, Any]]) -> None:
        def probe(key: str, field: str) -> None:
            present = any(a.get(field) is not None for a in activities)
            detail = "" if present else "absent from latest activities page"
            self.store.set_capability(key, present, detail)

        probe("activityTrainingLoad", "activityTrainingLoad")
        probe("hrTimeInZone", "hrTimeInZone_1")
        probe("directWorkoutRpe", "directWorkoutRpe")
        probe("avgGradeAdjustedSpeed", "avgGradeAdjustedSpeed")

    def _store_calendar(self, payload: dict[str, Any]) -> int:
        """Digest calendar month items (itemType='workout') into plan_calendar,
        deduped by (date, garmin_workout_id|name) so re-syncs stay idempotent."""
        n = 0
        for item in (payload or {}).get("calendarItems") or []:
            if item.get("itemType") != "workout" or not item.get("date"):
                continue
            d = item["date"]
            wid = item.get("workoutId")
            name = item.get("title") or item.get("workoutName")
            existing = self.store.plan_entries(d, d)
            if wid is not None:
                dup = any(str(e.get("garmin_workout_id")) == str(wid) for e in existing)
            else:
                dup = any(e.get("name") == name and e.get("source") == "calendar" for e in existing)
            if dup:
                continue
            planned = {
                k: item[k]
                for k in ("duration", "durationSeconds", "distance", "sportTypeKey")
                if item.get(k) is not None
            }
            self.store.upsert_plan_entry(
                {
                    "date": d,
                    "sport": item.get("sportTypeKey"),
                    "name": name,
                    "source": "calendar",
                    "garmin_workout_id": str(wid) if wid is not None else None,
                    "planned_json": json.dumps(planned, separators=(",", ":")) if planned else None,
                }
            )
            n += 1
        return n

    def _store_userstats_range(self, payload: Any, column: str, cast=int) -> int:
        """A userstats-service range payload → days.<column> rows; returns count.

        One call covers the whole window, so this replaces one daily-summary
        fetch per day (~180 calls) with one call per metric.
        """
        entries: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            metrics_map = (payload.get("allMetrics") or {}).get("metricsMap") or {}
            for values in metrics_map.values():
                if isinstance(values, list):
                    entries.extend(v for v in values if isinstance(v, dict))
        elif isinstance(payload, list):
            entries = [v for v in payload if isinstance(v, dict)]
        n = 0
        for e in entries:
            d, v = e.get("calendarDate"), e.get("value")
            if d and v is not None:
                self._upsert_day({"date": d, column: cast(v)})
                n += 1
        return n

    def _store_rhr_range(self, payload: Any) -> int:
        """Userstats metricId=60 payload → days.resting_hr rows; returns count."""
        return self._store_userstats_range(payload, "resting_hr")

    # --- tiers ---


    def _digest_activities(self, page: list, errors: list[str] | None = None) -> int:
        """Digest+store every entry of an activities page; one malformed entry
        is recorded (or logged) and skipped, never fatal to the tier."""
        n = 0
        for a in page:
            try:
                self._upsert_activity(digest_activity(a))
                n += 1
            except Exception as exc:
                msg = f"activity digest failed (id={a.get('activityId') if isinstance(a, dict) else '?'}): {type(exc).__name__}: {exc}"
                if errors is not None:
                    errors.append(msg)
        return n

    def tier0(self) -> dict[str, Any]:
        """First-minute snapshot (~17 calls); every probe records capability_map.

        Returns {"calls": int, "capabilities": {...}} (plus counts). Idempotent.
        """
        return self._locked(self._tier0)

    def _tier0(self) -> dict[str, Any]:
        t = self._today()
        name = self.display_name
        start_calls = self._calls

        self._probe("profile", "/userprofile-service/socialProfile")
        self._probe("user_settings", "/userprofile-service/userprofile/user-settings")
        self._probe("personal_records", f"/personalrecord-service/personalrecord/prs/{name}")
        self._probe("race_predictions", f"/metrics-service/metrics/racepredictions/latest/{name}")
        self._probe("training_status", f"/metrics-service/metrics/trainingstatus/aggregated/{t}")
        self._probe("training_readiness", f"/metrics-service/metrics/trainingreadiness/{t}")

        summary = self._probe(
            "daily_summary", f"/usersummary-service/usersummary/daily/{name}", calendarDate=t
        )
        if summary:
            self._upsert_day(digest_daily_summary(summary, t))

        sleep_raw = self._probe(
            "sleep",
            f"/wellness-service/wellness/dailySleepData/{name}",
            date=t,
            nonSleepBufferMinutes=60,
        )
        if sleep_raw:
            self._store_sleep(sleep_raw, t)
            dto = sleep_raw.get("dailySleepDTO") or {}
            self.store.set_capability(
                "sleepNeed",
                dto.get("sleepNeed") is not None,
                "" if dto.get("sleepNeed") is not None else "absent — renderer uses default 8h",
            )

        hrv_raw = self._probe("hrv", f"/hrv-service/hrv/{t}")
        if hrv_raw:
            self._upsert_day(digest_hrv(hrv_raw, t))
            baseline = (hrv_raw.get("hrvSummary") or {}).get("baseline")
            self.store.set_capability(
                "hrv_baseline", bool(baseline), "" if baseline else "no shipped HRV baseline"
            )

        activities = self._probe(
            "activities",
            "/activitylist-service/activities/search/activities",
            start=0,
            limit=self.page_limit,
        )
        n_acts = 0
        if activities:
            n_acts = self._digest_activities(activities)
            self._record_activity_capabilities(activities)
            self._advance_activity_cursor(activities)

        # Scheduled workouts: this + next month (Garmin months are 0-indexed).
        today_d = date.fromisoformat(t)
        n_plan = 0
        for months_ahead in (0, 1):
            y = today_d.year + (today_d.month - 1 + months_ahead) // 12
            m0 = (today_d.month - 1 + months_ahead) % 12  # 0-indexed
            payload = self._probe(
                f"calendar_month_{months_ahead}", f"/calendar-service/year/{y}/month/{m0}"
            )
            if payload:
                n_plan += self._store_calendar(payload)

        self._probe("training_plans", "/trainingplan-service/trainingplan/plans")
        self._probe("goals", "/goal-service/goal/goals", status="active", start=0, limit=30)
        self._probe("devices", "/device-service/deviceregistration/devices")

        self.store.set_sync_state("last_sync", self._now_iso())
        self.recompute_derived()
        return {
            "calls": self._calls - start_calls,
            "activities": n_acts,
            "plan_entries": n_plan,
            "capabilities": self.store.get_capabilities(),
        }

    def tier1(self) -> dict[str, Any]:
        """History warmup: 180d activities (paginated), RHR range (userstats
        fallback-probed), weight range, body battery chunked, weekly stress,
        maxmet, progress summary."""
        return self._locked(self._tier1)

    def _tier1(self) -> dict[str, Any]:
        t = self._today()
        name = self.display_name
        start_calls = self._calls
        today_d = date.fromisoformat(t)
        history_start = (today_d - timedelta(days=ACTIVITY_HISTORY_DAYS)).isoformat()
        errors: list[str] = []

        # Activities-by-date, paginated until a short page or the start date.
        n_acts = 0
        start = 0
        while True:
            page = (
                self._call(
                    "/activitylist-service/activities/search/activities",
                    startDate=history_start,
                    start=start,
                    limit=self.page_limit,
                )
                or []
            )
            n_acts += self._digest_activities(page, errors)
            self._advance_activity_cursor(page)
            if len(page) < self.page_limit:
                break
            oldest = min(
                (str(a.get("startTimeLocal"))[:10] for a in page if a.get("startTimeLocal")),
                default="",
            )
            if oldest and oldest <= history_start:
                break
            start += self.page_limit

        # RHR range via userstats metricId=60, capability fallback recorded.
        try:
            payload = self._call(
                f"/userstats-service/wellness/daily/{name}",
                fromDate=history_start,
                untilDate=t,
                metricId=60,
            )
            n_rhr = self._store_rhr_range(payload)
            if n_rhr:
                self.store.set_capability("rhr_range", True)
            else:
                self.store.set_capability(
                    "rhr_range", False,
                    "empty userstats response — building RHR forward from daily summaries",
                )
        except Exception as exc:
            self.store.set_capability(
                "rhr_range", False,
                f"{type(exc).__name__}: {exc} — building RHR forward from daily summaries",
            )
            n_rhr = 0

        # Weight range → days.weight_g.
        n_weight = 0
        try:
            payload = self._call(
                "/weight-service/weight/dateRange", startDate=history_start, endDate=t
            )
            entries = (payload or {}).get("dateWeightList") or []
            for e in entries:
                d, w = e.get("calendarDate"), e.get("weight")
                if d and w is not None:
                    self._upsert_day({"date": d, "weight_g": int(round(w))})
                    n_weight += 1
            self.store.set_capability(
                "weight_range", bool(entries), "" if entries else "no weight entries"
            )
        except Exception as exc:
            self.store.set_capability("weight_range", False, f"{type(exc).__name__}: {exc}")

        # Body battery, 90d back in 30d chunks.
        for chunk in range(3):
            chunk_end = today_d - timedelta(days=30 * chunk)
            chunk_start = chunk_end - timedelta(days=29)
            payload = self._try_call(
                "/wellness-service/wellness/bodyBattery/reports/daily",
                errors,
                startDate=chunk_start.isoformat(),
                endDate=chunk_end.isoformat(),
            )
            for e in payload or []:
                row = digest_body_battery_day(e)
                if row is not None:
                    self._upsert_day(row)

        # Daily wellness scalars, one range call per metric (§3.3 tier 1).
        # Without this the only source is the daily summary, which is fetched
        # for TODAY only — so every one of these columns held a single day of
        # history, and any row written mid-day stayed frozen at its mid-day
        # value forever. Re-running this heals both.
        for metric_id, (column, cast) in USERSTATS_DAILY_METRICS.items():
            payload = self._try_call(
                f"/userstats-service/wellness/daily/{name}",
                errors,
                fromDate=history_start,
                untilDate=t,
                metricId=metric_id,
            )
            if payload is not None:
                self._store_userstats_range(payload, column, cast)

        self._probe("weekly_stress", f"/usersummary-service/stats/stress/weekly/{t}/52")
        half = (today_d - timedelta(days=90)).isoformat()
        self._probe("maxmet_history", f"/metrics-service/metrics/maxmet/daily/{history_start}/{half}")
        self._probe("maxmet_recent", f"/metrics-service/metrics/maxmet/daily/{half}/{t}")
        self._probe(
            "progress_summary",
            "/fitnessstats-service/activity",
            startDate=history_start,
            endDate=t,
            aggregation="lifetime",
            groupByParentActivityType=False,
            metric="duration",
        )

        self.store.set_sync_state("last_sync", self._now_iso())
        self.recompute_derived()
        return {
            "calls": self._calls - start_calls,
            "activities": n_acts,
            "rhr_days": n_rhr,
            "weight_days": n_weight,
            "errors": errors,
        }

    def tier2(self, backfill_days: int = 60) -> dict[str, Any]:
        """Nightly sleep DTO + timeline backfill, newest-first from yesterday.

        Resumable via sync_state['tier2_cursor'] JSON:
        {"phase": "sleep"|"done", "next_date", "end_date"}.
        The cursor is rewritten after every night; ≥2s call spacing.

        Per-activity splits are backfilled separately by backfill_splits():
        they are keyed by activity, not by date, so they resume on their own
        work list rather than on this date cursor.
        """
        return self._locked(lambda: self._tier2(backfill_days))

    def _tier2(self, backfill_days: int) -> dict[str, Any]:
        t = self._today()
        start_calls = self._calls
        raw_cursor = self.store.get_sync_state("tier2_cursor")
        cursor = json.loads(raw_cursor) if raw_cursor else None
        today_d = date.fromisoformat(t)
        default_end = (today_d - timedelta(days=backfill_days)).isoformat()

        if cursor and cursor.get("phase") == "sleep":
            next_d = date.fromisoformat(cursor["next_date"])
            end_date = cursor["end_date"]
        elif cursor and cursor.get("phase") == "done":
            prev_end = cursor.get("end_date")
            if prev_end and default_end < prev_end:  # deeper backfill requested
                next_d = date.fromisoformat(prev_end) - timedelta(days=1)
                end_date = default_end
            else:
                return self._tier2_heal_gap(start_calls)
        else:
            next_d = today_d - timedelta(days=1)
            end_date = default_end

        end_d = date.fromisoformat(end_date)
        nights = 0
        old_interval = self.limiter.min_interval_s
        self.limiter.min_interval_s = max(old_interval, 2.0)
        try:
            while next_d >= end_d:
                ds = next_d.isoformat()
                raw = self._call(
                    f"/wellness-service/wellness/dailySleepData/{self.display_name}",
                    date=ds,
                    nonSleepBufferMinutes=60,
                )
                self._store_sleep(raw or {}, ds)
                nights += 1
                next_d -= timedelta(days=1)
                self.store.set_sync_state(
                    "tier2_cursor",
                    json.dumps(
                        {
                            "phase": "sleep" if next_d >= end_d else "done",
                            "next_date": next_d.isoformat(),
                            "end_date": end_date,
                        }
                    ),
                )
        finally:
            self.limiter.min_interval_s = old_interval

        self.store.set_sync_state("last_sync", self._now_iso())
        self.recompute_derived()
        return {"calls": self._calls - start_calls, "nights": nights, "done": next_d < end_d}

    _HEAL_CAP = 14  # nights re-fetched per run when healing a sync hiatus

    def _tier2_heal_gap(self, start_calls: int) -> dict[str, Any]:
        """Backfill is done — heal nights missed since (e.g. a week without
        any sync): fetch sleep for dates after the newest stored night up to
        yesterday, capped per run. tier2_healed_until stops re-fetching
        nights that legitimately have no data (watch not worn)."""
        today_d = date.fromisoformat(self._today())
        yesterday = today_d - timedelta(days=1)
        healed_until = self.store.get_sync_state("tier2_healed_until")
        series = self.store.get_series("sleep_score", yesterday.isoformat(), 365)
        newest = max(
            ([series[-1][0]] if series else []) + ([healed_until] if healed_until else []),
            default=None,
        )
        if newest is None or newest >= yesterday.isoformat():
            return {"calls": 0, "nights": 0, "done": True}
        gap_start = date.fromisoformat(newest) + timedelta(days=1)
        nights = 0
        d = gap_start
        old_interval = self.limiter.min_interval_s
        self.limiter.min_interval_s = max(old_interval, 2.0)
        try:
            while d <= yesterday and nights < self._HEAL_CAP:
                raw = self._call(
                    f"/wellness-service/wellness/dailySleepData/{self.display_name}",
                    date=d.isoformat(),
                    nonSleepBufferMinutes=60,
                )
                self._store_sleep(raw or {}, d.isoformat())
                self.store.set_sync_state("tier2_healed_until", d.isoformat())
                nights += 1
                d += timedelta(days=1)
        finally:
            self.limiter.min_interval_s = old_interval
        if nights:
            self.store.set_sync_state("last_sync", self._now_iso())
            self.recompute_derived()
        return {"calls": self._calls - start_calls, "nights": nights, "done": d > yesterday}

    def backfill_splits(
        self,
        days: int = SPLITS_HISTORY_DAYS,
        limit: int = SPLITS_PER_RUN,
        sport_like: str = "%running%",
    ) -> dict[str, Any]:
        """Per-lap backfill for activities in the window that have none yet.

        One cheap call per activity (~1 KB). Newest first, because a recent
        session is worth more than an old one if the run is interrupted.
        Resumable by construction: the work list is "activities with no stored
        laps", so a partial run simply leaves a shorter list next time — there
        is no cursor to corrupt.

        Activities whose payload yields no laps are remembered in
        sync_state['splits_no_laps'] so they are not re-fetched forever
        (manual entries and third-party syncs have no splits at all).
        """
        return self._locked(lambda: self._backfill_splits(days, limit, sport_like))

    def _backfill_splits(self, days: int, limit: int, sport_like: str) -> dict[str, Any]:
        t = self._today()
        start_calls = self._calls
        start = (date.fromisoformat(t) - timedelta(days=days - 1)).isoformat()

        raw_skip = self.store.get_sync_state("splits_no_laps")
        skip: list[int] = json.loads(raw_skip) if raw_skip else []
        skip_set = set(skip)

        pending = [
            a for a in self.store.activities_missing_laps(start, t, sport_like)
            if a["activity_id"] not in skip_set
        ]
        errors: list[str] = []
        done = laps_stored = empty = 0

        old_interval = self.limiter.min_interval_s
        self.limiter.min_interval_s = max(old_interval, 2.0)
        try:
            for act in pending[:limit]:
                aid = int(act["activity_id"])
                raw = self._try_call(f"/activity-service/activity/{aid}/splits", errors)
                if raw is None:
                    continue
                laps = digest_laps(raw, aid)
                if laps:
                    self.store.replace_activity_laps(aid, laps)
                    laps_stored += len(laps)
                else:
                    skip.append(aid)
                    empty += 1
                done += 1
        finally:
            self.limiter.min_interval_s = old_interval

        if empty:
            # Bounded: the tail is the oldest, and re-probing a few stale ids
            # is cheaper than letting this list grow without limit.
            self.store.set_sync_state(
                "splits_no_laps", json.dumps(skip[-_SPLITS_SKIP_CAP:])
            )
        if done:
            self.store.set_sync_state("last_sync", self._now_iso())
        return {
            "calls": self._calls - start_calls,
            "activities": done,
            "laps": laps_stored,
            "no_laps": empty,
            "remaining": max(0, len(pending) - limit),
            "errors": errors,
        }

    def incremental(self) -> dict[str, Any]:
        """Daily steady state: today's summary/sleep/HRV + activities newer
        than sync_state['last_activity_start']. Individual endpoint failures
        are collected, never fatal."""
        return self._locked(self._incremental)

    def _incremental(self) -> dict[str, Any]:
        t = self._today()
        name = self.display_name
        start_calls = self._calls
        errors: list[str] = []

        summary = self._try_call(
            f"/usersummary-service/usersummary/daily/{name}", errors, calendarDate=t
        )
        if summary:
            self._upsert_day(digest_daily_summary(summary, t))

        sleep_raw = self._try_call(
            f"/wellness-service/wellness/dailySleepData/{name}",
            errors,
            date=t,
            nonSleepBufferMinutes=60,
        )
        if sleep_raw:
            self._store_sleep(sleep_raw, t)

        hrv_raw = self._try_call(f"/hrv-service/hrv/{t}", errors)
        if hrv_raw:
            self._upsert_day(digest_hrv(hrv_raw, t))

        page = (
            self._try_call(
                "/activitylist-service/activities/search/activities",
                errors,
                start=0,
                limit=self.page_limit,
            )
            or []
        )
        # Upsert anything on the page the store doesn't have yet (by id, not by
        # date) — a late-uploaded backdated activity must not be skipped forever.
        new = [
            a
            for a in page
            if a.get("activityId") is not None
            and self.store.get_activity(int(a["activityId"])) is None
        ]
        for a in new:
            self._upsert_activity(digest_activity(a))
        self._advance_activity_cursor(page)

        self.store.set_sync_state("last_sync", self._now_iso())
        self.recompute_derived()
        return {"calls": self._calls - start_calls, "new_activities": len(new), "errors": errors}

    # --- derived state ---

    def recompute_derived(self) -> None:
        """Loads (calibration + ladder) → daily loads → full-range PMC over a
        gap-filled contiguous series → today's baselines cache → plan matching
        ±30d → alert scan diff. Pure store/analytics work, no fetches."""
        store = self.store
        t = self._today()
        all_acts = store.list_activities("0000-01-01", "9999-12-31")

        # Ledger completeness: every activity date gets a days row.
        for d in sorted({a["date"] for a in all_acts if a.get("date")}):
            if store.get_day(d) is None:
                self._upsert_day({"date": d})

        # §3.1 fallback ladder over every non-Garmin-load activity — fallback
        # loads are re-resolved each pass so they pick up improving calibration
        # as more overlap pairs accumulate (not frozen at first sight).
        calibration = load_mod.fit_calibration(all_acts)
        lpm_by_sport: dict[str, list[float]] = {}
        for a in all_acts:
            if a.get("load_source") == "garmin" and a.get("load") and a.get("duration_s"):
                lpm_by_sport.setdefault(a["sport"], []).append(
                    float(a["load"]) / (float(a["duration_s"]) / 60.0)
                )
        sport_median_lpm = {
            sport: statistics.median(vals) for sport, vals in lpm_by_sport.items()
        }
        for act in all_acts:
            if act.get("load_source") == "garmin":
                continue
            # Strip the previously resolved value so the ladder re-runs instead
            # of mistaking it for a native Garmin load.
            candidate = {**act, "load": None}
            load_val, source = load_mod.resolve_load(candidate, calibration, sport_median_lpm)
            if load_val != act.get("load") or source != act.get("load_source"):
                act.update({"load": load_val, "load_source": source})
                store.upsert_activity(act)

        store.recompute_daily_loads()

        # Full-range PMC rewrite on a contiguous, 0-gap-filled daily series.
        series = store.get_series("daily_load", t, 100_000)
        if series:
            by_date = dict(series)
            first = date.fromisoformat(series[0][0])
            last = date.fromisoformat(t)
            filled = [
                ((first + timedelta(days=i)).isoformat(), by_date.get((first + timedelta(days=i)).isoformat(), 0.0))
                for i in range((last - first).days + 1)
            ]
            store.replace_pmc(pmc_mod.compute_pmc(filled))
        else:
            store.replace_pmc([])

        # Baselines cache for today, tracked metrics × windows.
        tracked = alerts_mod.tracked_metrics()
        series_by_metric = {m: store.get_series(m, t, 120) for m in tracked}
        baseline_rows = []
        for metric, s in series_by_metric.items():
            for w in BASELINE_WINDOWS:
                b = baselines_mod.baseline(s, t, w)
                if b:
                    baseline_rows.append(
                        {
                            "metric": metric,
                            "date": t,
                            "window": w,
                            "mean": b["mean"],
                            "median": b["median"],
                            "mad_sd": b["mad_sd"],
                            "n": b["n"],
                        }
                    )
        store.upsert_baselines(baseline_rows)

        # Plan matching over ±30d.
        lo = (date.fromisoformat(t) - timedelta(days=30)).isoformat()
        hi = (date.fromisoformat(t) + timedelta(days=30)).isoformat()
        plans = store.plan_entries(lo, hi)
        if plans:
            acts = store.list_activities(lo, hi)
            for m in matcher_mod.match_plan(plans, acts):
                if m["plan_id"] is not None:
                    store.set_plan_match(m["plan_id"], m["matched_activity_id"], m["match_method"])

        # Alert scan diff: new/changed → upsert, back-in-band → resolve.
        desired = alerts_mod.scan(series_by_metric, t)
        for al in desired:
            store.upsert_alert(al.get("since_date", t), al["metric"], al["severity"], al["message"])
        desired_metrics = {al["metric"] for al in desired}
        candidates = [
            a["metric"] for a in store.active_alerts() if a["metric"] not in desired_metrics
        ]
        if candidates:
            resolved = alerts_mod.resolution_dates(series_by_metric, candidates, t)
            for metric, rdate in resolved.items():
                store.resolve_alert(metric, rdate)

    # --- staleness API for the MCP layer ---

    def last_sync(self) -> str | None:
        return self.store.get_sync_state("last_sync")

    def is_stale(self, hours: float = 6.0) -> bool:
        last = self.last_sync()
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return (datetime.now() - last_dt).total_seconds() > hours * 3600
