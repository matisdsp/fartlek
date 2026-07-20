"""Sync engine (DESIGN.md §3.3). CONTRACT STUB — implement me.

Garmin is hit ONLY from here. Fetch-once → digest → store → serve.

Components:
- RateLimiter: sequential calls, ≥2s spacing during backfill (tier 2), ≥0.5s
  otherwise; on 429 exponential backoff 60s → ×2 → cap 15 min, then resume.
- SyncLock: advisory <account_dir>/sync.lock file (pid + timestamp inside),
  stale after 10 min; second process skips sync and reads the store.
- Digesters: pure functions raw payload → schema.sql row dicts. Raw payloads
  are never stored. Sleep digester also emits the compact interval timeline.
- SyncEngine.tier0() — first-minute snapshot (~17 calls): profile + user
  settings (zone config), PRs, race predictions, training status, today's
  daily summary / sleep / HRV, latest activities page, scheduled workouts
  (this + next month), enrolled training plans + goals, devices. Each probe
  records capability_map. All idempotent.
- SyncEngine.tier1() — history warmup (~16-21 calls): activities-by-date 180d
  paginated (fully warms PMC), RHR range (userstats metricId=60, fallback
  probe), weight range, body-battery range chunked, weekly stress 52w,
  maxmet history, progress summary.
- SyncEngine.tier2(backfill_days=60) — resumable via sync_state cursor:
  per-night sleep DTO + timeline backfill (HRV rides in the same payload).
  Splits/`/details` digestion is Phase 2 — record the cursor schema now.
- SyncEngine.incremental() — daily steady state (~8-12 calls): today + any
  new activities since last cursor.
- After any tier: recompute_derived(store) — daily loads (analytics.load),
  PMC rewrite (analytics.pmc), baselines cache, plan matching
  (analytics.matcher), alert scan diff (analytics.alerts).
- Staleness API for the MCP layer: last_sync(store), is_stale(store, hours=6).

Timezone rules (§3.3): all daily bucketing uses Garmin calendarDate; sleep
belongs to its wake-date; 'today' = server-local date.

The engine takes the GarminConnectAdapter-wrapped client for I/O? No —
Phase 0 keeps it sync and self-contained: it receives a `fetch(path, **params)`
callable (the garminconnect client's connectapi, already error-translated by
the caller) plus the display_name, so it stays unit-testable with a fake fetch.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fartlek.store import Store

Fetch = Callable[..., Any]  # fetch(path: str, **params) -> parsed JSON


class RateLimiter:
    def __init__(self, min_interval_s: float = 0.5):
        raise NotImplementedError

    def wait(self) -> None: ...
    def backoff_429(self) -> None:
        """Sleep per the 60s→×2→15min ladder; reset() clears the ladder."""
    def reset(self) -> None: ...


class SyncLock:
    def __init__(self, account_dir: Path, stale_after_s: int = 600):
        raise NotImplementedError

    def acquire(self) -> bool: ...
    def release(self) -> None: ...


# --- digesters (pure) ---

def digest_daily_summary(raw: dict[str, Any], date: str) -> dict[str, Any]: ...
def digest_sleep(raw: dict[str, Any], date: str) -> tuple[dict[str, Any], str | None]:
    """→ (days-row partial, intervals_json or None)."""
def digest_hrv(raw: dict[str, Any], date: str) -> dict[str, Any]: ...
def digest_activity(raw: dict[str, Any]) -> dict[str, Any]:
    """One entry of the activities-list payload → activities row (incl. watch
    RPE conversion via analytics.load.convert_watch_rpe, extra_json spillover)."""


class SyncEngine:
    def __init__(self, store: Store, fetch: Fetch, display_name: str, account_dir: Path):
        raise NotImplementedError

    def tier0(self) -> dict[str, Any]:
        """Returns {calls: int, capabilities: {...}} summary."""
    def tier1(self) -> dict[str, Any]: ...
    def tier2(self, backfill_days: int = 60) -> dict[str, Any]:
        """Resumable: reads/writes sync_state['tier2_cursor']."""
    def incremental(self) -> dict[str, Any]: ...
    def recompute_derived(self) -> None: ...

    # staleness API for the MCP layer
    def last_sync(self) -> str | None: ...
    def is_stale(self, hours: float = 6.0) -> bool: ...
