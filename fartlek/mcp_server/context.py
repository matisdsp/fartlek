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
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from fartlek.health.adapters.garmin_connect import GarminConnectAdapter
from fartlek.health.exceptions import GarminAuthError
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
        # True once this process ran the cold start (tier0+tier1 inline);
        # stays set so the tool that triggered it can disclose the fresh build.
        self.cold_started = False

    # --- lifecycle ---

    async def ensure_ready(self) -> None:
        if self._engine is None:
            async with self._init_lock:
                if self._engine is None:
                    await self._init()
                    if self.cold_started:
                        return  # tier2 already running in the background
        self._maybe_background_refresh()

    async def _init(self) -> None:
        """Connect Garmin, open the per-account store, build the engine.
        Cold store → tier0+tier1 inline, then tier2 in a daemon thread."""
        client = await asyncio.to_thread(self._adapter.connect_sync)
        account_id = client.display_name
        if not account_id:
            raise GarminAuthError("Garmin profile loaded but displayName missing")
        store = Store(store_path(account_id))
        engine = SyncEngine(
            store,
            fetch=lambda path, **p: self._adapter.fetch_sync(client, path, **p),
            display_name=account_id,
            account_dir=account_dir(account_id),
        )
        self._client = client
        self._store = store
        self._engine = engine
        if store.get_sync_state("last_sync") is None:
            log.info("cold start for %s: tier0+tier1 inline", account_id)
            await asyncio.to_thread(engine.tier0)
            await asyncio.to_thread(engine.tier1)
            self.cold_started = True
            self._start_background(engine.tier2)

    def _maybe_background_refresh(self) -> None:
        """Warm store gone stale (>6h) → one background incremental() thread;
        the current cache is served immediately."""
        engine = self._engine
        if engine is None:
            return
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        if engine.is_stale(STALE_HOURS):
            self._start_background(engine.incremental)

    def _start_background(self, target: Callable[[], Any]) -> None:
        def _run() -> None:
            try:
                target()
            except Exception:
                log.exception("background sync failed")

        self._bg_thread = threading.Thread(
            target=_run, daemon=True, name="fartlek-bg-sync"
        )
        self._bg_thread.start()

    async def ensure_fresh_today(self) -> None:
        engine, store = self._engine, self._store
        assert engine is not None and store is not None, "ensure_ready() first"
        day = store.get_day(self.today()) or {}
        if day.get("sleep_score") is not None or day.get("resting_hr") is not None:
            return
        last = store.get_sync_state("last_sync")
        if last is not None:
            try:
                age_s = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
            except ValueError:
                age_s = None
            if age_s is not None and age_s <= FRESH_TODAY_MINUTES * 60:
                return
        await asyncio.to_thread(engine.incremental)

    # --- accessors (valid after ensure_ready) ---

    @property
    def store(self) -> Store:
        assert self._store is not None, "ensure_ready() first"
        return self._store

    @property
    def display_name(self) -> str:
        """Garmin displayName used in path templates (garmin_raw)."""
        assert self._engine is not None, "ensure_ready() first"
        return self._engine.display_name

    def today(self) -> str:
        """Server-local date (§3.3 timezone rules)."""
        return date.today().isoformat()

    def data_as_of(self) -> str:
        """HH:MM of sync_state['last_sync'] (header timestamp); '--:--' when unknown."""
        if self._store is None:
            return "--:--"
        last = self._store.get_sync_state("last_sync")
        if not last or len(last) < 16:
            return "--:--"
        return last[11:16]  # 'YYYY-MM-DDTHH:MM:SS' → 'HH:MM'

    def banner(self) -> str | None:
        """format_banner over the store's active RED/AMBER alerts."""
        if self._store is None:
            return None
        return format_banner(self._store.active_alerts())

    async def fetch_raw(self, path: str, **params: Any) -> Any:
        """One live Garmin GET for garmin_raw / splits detail (to_thread,
        serialized with sync via the engine's rate limiter).

        Reuses the engine's private `_call` on purpose: same process, same
        rate limiter and 429 backoff ladder as the sync path — acceptable
        coupling, kept in one place here."""
        engine = self._engine
        assert engine is not None, "ensure_ready() first"
        return await asyncio.to_thread(engine._call, path, **params)

    async def run_sync(self, backfill_days: int = 0) -> dict[str, Any]:
        """garmin_sync tool: inline incremental(), plus tier2(backfill_days)
        when requested. Returns the merged stats dict (calls summed)."""
        engine = self._engine
        assert engine is not None, "ensure_ready() first"
        merged = dict(await asyncio.to_thread(engine.incremental))
        if backfill_days > 0:
            t2 = await asyncio.to_thread(engine.tier2, backfill_days)
            merged["calls"] = int(merged.get("calls") or 0) + int(t2.get("calls") or 0)
            for k, v in t2.items():
                if k != "calls":
                    merged[k] = v
        return merged
