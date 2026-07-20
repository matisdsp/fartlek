"""ToolContext — everything a Phase-1 tool needs, behind one seam.

Tools NEVER touch the adapter, engine, or paths directly: they receive a
ToolContext and use store / today() / banner() / fetch_raw() / run_sync().
Tests substitute a lightweight fake exposing the same surface.

Lifecycle (DESIGN §3.3 sync policy):
- ensure_ready(): lazy one-time init — connect Garmin (asyncio.to_thread),
  open the per-account Store at paths.store_path(<garmin user id>), build the
  SyncEngine. Cold store (no sync_state['last_sync']) → tier0+tier1 run
  INLINE (~30 calls, the first-minute cold start; the calling tool discloses
  it), then tier2 starts in a daemon background thread. Warm store stale
  >6h → background incremental() thread (serve current cache immediately).
- ensure_fresh_today() (garmin_brief only): if today's days row is missing
  wellness (no sleep_score AND no resting_hr) and last_sync > 30 min → run
  incremental() INLINE (bounded, ~5 calls) before rendering.
- All engine work is sync → asyncio.to_thread; the engine's own sync.lock
  serializes across threads/processes. One asyncio.Lock guards init.

Auth errors surface as GarminAuthError with the fixed re-auth message
(§4.3): tools convert it to the corrective error string — retrying will not
help; the user must run `fartlek auth`.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from fartlek.health.adapters.garmin_connect import GarminConnectAdapter
from fartlek.paths import account_dir, default_tokenstore, store_path
from fartlek.render.renderer import format_banner
from fartlek.store import Store
from fartlek.sync.engine import SyncEngine

log = logging.getLogger(__name__)

STALE_HOURS = 6.0
FRESH_TODAY_MINUTES = 30.0


class ToolContext:
    def __init__(self, tokenstore=None):
        self._tokenstore = tokenstore or default_tokenstore()
        self._adapter = GarminConnectAdapter(tokenstore=self._tokenstore)
        self._client = None
        self._store: Store | None = None
        self._engine: SyncEngine | None = None
        self._init_lock = asyncio.Lock()
        self._bg_thread: threading.Thread | None = None
        self.cold_started = False  # True on the call that ran the cold start

    # --- lifecycle ---

    async def ensure_ready(self) -> None:
        raise NotImplementedError

    async def ensure_fresh_today(self) -> None:
        raise NotImplementedError

    # --- accessors (valid after ensure_ready) ---

    @property
    def store(self) -> Store:
        assert self._store is not None, "ensure_ready() first"
        return self._store

    def today(self) -> str:
        raise NotImplementedError

    def data_as_of(self) -> str:
        """HH:MM of sync_state['last_sync'] (header timestamp)."""
        raise NotImplementedError

    def banner(self) -> str | None:
        """format_banner over the store's active RED/AMBER alerts."""
        raise NotImplementedError

    async def fetch_raw(self, path: str, **params: Any) -> Any:
        """One live Garmin GET for garmin_raw / splits detail (to_thread,
        serialized with sync via the engine's rate limiter)."""
        raise NotImplementedError

    async def run_sync(self, backfill_days: int = 0) -> dict[str, Any]:
        """garmin_sync tool: inline incremental(), plus tier2(backfill_days)
        when requested. Returns the merged stats dict."""
        raise NotImplementedError
