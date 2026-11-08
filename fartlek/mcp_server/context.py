"""ToolContext — everything a Phase-1 tool needs, behind one seam.

Tools NEVER touch the adapter, engine, or paths directly: they receive a
ToolContext and use store / today() / banner() / fetch_raw() / run_sync().
Tests substitute a lightweight fake exposing the same surface.

Lifecycle (sync policy):
- ensure_ready(): lazy one-time init — connect Garmin (asyncio.to_thread),
  open the per-account Store at paths.store_path(<garmin user id>), build the
  SyncEngine. Cold store (no sync_state['last_sync']) → tier0+tier1 run
  INLINE (~30 calls, the first-minute cold start; the calling tool discloses
  it), then tier2 starts in a daemon background thread. Warm store stale
  >6h → background incremental() thread (serve current cache immediately).
  ensure_ready(background_refresh=False) + join_background() opt out of that
  race — garmin_sync syncs in the foreground and must be able to report what
  its own call ingested.
- ensure_fresh_today() (garmin_brief only): if today's days row is missing
  wellness (no sleep_score AND no resting_hr) and last_sync > 30 min → run
  incremental() INLINE (bounded, ~5 calls) before rendering.
- All engine work is sync → asyncio.to_thread; the engine's own sync.lock
  serializes across threads/processes. One asyncio.Lock guards init.

Data sources (`source_kind`):
- "connect": a direct Garmin connection (optional extra `connect`).
- "export": the Garmin account export remembered by `fartlek import`. Nothing
  here contacts Garmin, so no refresh runs (ensure_fresh_today, background
  catch-up) and garmin_sync only re-reads the file.
- "store": the export file is gone but `export_source.json` recorded the
  account and the last day it covered, so the store built by the import is
  still served, read-only, and the banner says so.

Auth errors surface as GarminAuthError with the fixed re-auth message:
tools convert it to the corrective error string — retrying will not
help; the user must run `fartlek auth`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from fartlek.health.exceptions import GarminAuthError, GarminConnectUnavailable
from fartlek.paths import account_dir, default_tokenstore, store_path
from fartlek.render.renderer import format_banner
from fartlek.store import Store
from fartlek.sync.engine import SyncEngine
from fartlek.sync.export_import import (
    RememberedExport,
    build_engine,
    remember_export,
    remembered_source,
    run_import,
)

log = logging.getLogger(__name__)

STALE_HOURS = 6.0
FRESH_TODAY_MINUTES = 30.0


class ToolContext:
    def __init__(self, tokenstore=None):
        self._tokenstore = tokenstore or default_tokenstore()
        self._adapter = None  # built on first use: a direct connection is optional
        self._export = None  # set when the data comes from a Garmin export
        self._remembered: RememberedExport | None = None  # set in store-only mode
        self.source_kind = "connect"
        self._client = None
        self._store: Store | None = None
        self._engine: SyncEngine | None = None
        self._init_lock = asyncio.Lock()
        self._bg_thread: threading.Thread | None = None
        # True once this process ran the cold start (tier0+tier1 inline);
        # stays set so the tool that triggered it can disclose the fresh build.
        self.cold_started = False

    # --- lifecycle ---

    async def ensure_ready(self, background_refresh: bool = True) -> None:
        if self._engine is None:
            async with self._init_lock:
                if self._engine is None:
                    await self._init()
                    if self.cold_started:
                        return  # tier2 already running in the background
        if background_refresh:
            self._maybe_background_refresh()

    async def join_background(self, timeout: float = 120.0) -> None:
        """Wait for an in-flight background refresh to finish, so the caller
        reads a settled store. Only garmin_sync needs this: every other tool
        deliberately serves the current cache while the thread runs."""
        thread = self._bg_thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, timeout)

    async def _init(self) -> None:
        """Connect Garmin, open the per-account store, build the engine.
        Cold store → tier0+tier1 inline, then tier2 in a daemon thread."""
        source = remembered_source()
        direct_requested = os.environ.get("FARTLEK_SOURCE") == "connect"
        if self._adapter is None and source is not None and not direct_requested:
            if source.available:
                await asyncio.to_thread(self._init_from_export, source)
                return
            if source.describes_a_store and store_path(source.display_name).is_file():
                await asyncio.to_thread(self._init_from_store, source)
                return
        if self._adapter is None:
            self._adapter = self._connect_adapter()
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

    def _connect_adapter(self):
        try:
            from fartlek.health.adapters.garmin_connect import GarminConnectAdapter
        except ImportError as exc:
            raise GarminConnectUnavailable(
                "Garmin tokens exist but the direct connection is not installed (extra "
                "`connect`), and no export has been imported: run `fartlek import <export.zip>`."
            ) from exc
        return GarminConnectAdapter(tokenstore=self._tokenstore)

    def _init_from_export(self, source: RememberedExport) -> None:
        from fartlek.health.adapters.garmin_export import GarminExport

        export = GarminExport(source.path)
        if not source.describes_a_store:
            # Written by an older version: record what lets the store outlive the file.
            remember_export(source.path, export.display_name, export.last_date)
        store = Store(store_path(export.display_name))
        engine = build_engine(export, store)
        self._export = export
        self._store = store
        self._engine = engine
        self.source_kind = "export"
        if store.get_sync_state("last_sync") is None:
            run_import(engine)
            self.cold_started = True

    def _init_from_store(self, source: RememberedExport) -> None:
        """The export file is gone: serve the store it filled, read-only."""
        from fartlek.health.adapters.garmin_export import UnlimitedRate

        name = source.display_name
        assert name is not None and source.last_date is not None
        store = Store(store_path(name))
        # Nothing to fetch from: every call answers "no data", and no sync is ever started.
        self._engine = SyncEngine(
            store, lambda _path, **_params: None, name, account_dir(name),
            limiter=UnlimitedRate(), today=source.last_date,
        )
        self._store = store
        self._remembered = source
        self.source_kind = "store"

    def _maybe_background_refresh(self) -> None:
        """Warm store gone stale (>6h) → one background daily_catchup() thread;
        the current cache is served immediately.

        daily_catchup rather than plain incremental: it also chips away at the
        gear-attribution work list, which costs a call per historical session
        and so belongs on a thread nobody is waiting for. The inline paths
        (ensure_fresh_today, garmin_sync) stay on incremental."""
        engine = self._engine
        if engine is None or self.source_kind != "connect":
            return
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        if engine.is_stale(STALE_HOURS):
            self._start_background(engine.daily_catchup)

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
        if self.source_kind != "connect":
            return  # nothing here can fetch newer data than what was imported
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

    @property
    def profile_id(self) -> str | None:
        """Garmin's numeric userProfilePk, which the gear service keys on —
        not display_name (a GUID). Captured by the sync engine from the social
        profile; None until a sync has run."""
        assert self._store is not None, "ensure_ready() first"
        return self._store.get_sync_state("profile_id")

    def today(self) -> str:
        """Server-local date (see the timezone rules in sync.engine), or the last day of a Garmin export."""
        if self._export is not None:
            return self._export.last_date
        if self._remembered is not None:
            return self._remembered.last_date
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
        note = self.source_note()
        alerts = format_banner(self._store.active_alerts())
        if note is None:
            return alerts
        return f"{note}\n{alerts}" if alerts else note

    def source_note(self) -> str | None:
        """One line on where the data comes from when it is not a live connection."""
        if self._export is not None:
            return f"Data from a Garmin export, through {self._export.last_date}."
        if self._remembered is not None:
            return (
                f"Data from a Garmin export imported earlier, through {self._remembered.last_date}. "
                "The export file is no longer at its path, so nothing can be re-read."
            )
        return None

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
        """garmin_sync tool: inline incremental(), plus the full history pass
        when backfill_days is requested. Returns the merged stats dict (calls
        summed).

        A depth request runs tier1 over the same window as well, not tier2
        alone: tier2 walks nights, while the ranges that fill activities,
        userstats scalars, weight and body battery all live in tier1. Without
        it garmin_sync(backfill_days=N) deepened sleep and left every other
        column at whatever depth the last CLI sync happened to reach. The
        wellness gap-fills and the splits pass follow for the same reason —
        each is capped per run and costs nothing once caught up.
        """
        engine = self._engine
        assert engine is not None, "ensure_ready() first"
        if self.source_kind == "store":
            return {"skipped": True, "reason": "export file missing"}
        if self._export is not None:
            return await asyncio.to_thread(run_import, engine, max(backfill_days, 365))
        merged = dict(await asyncio.to_thread(engine.incremental))
        if backfill_days <= 0:
            return merged

        def _merge(stats: dict[str, Any], *, keys: dict[str, str] | None = None) -> None:
            merged["calls"] = int(merged.get("calls") or 0) + int(stats.get("calls") or 0)
            for k, v in stats.items():
                if k == "calls":
                    continue
                if k == "errors":
                    merged.setdefault("errors", []).extend(v or [])
                elif keys is None:
                    merged[k] = v
                elif k in keys:
                    merged[keys[k]] = v

        _merge(await asyncio.to_thread(engine.tier1, backfill_days),
               keys={"activities": "history_activities"})
        _merge(await asyncio.to_thread(engine.tier2, backfill_days))
        _merge(await asyncio.to_thread(engine.backfill_hrv, backfill_days),
               keys={"filled": "hrv_filled", "remaining": "hrv_remaining"})
        _merge(await asyncio.to_thread(engine.backfill_daily_summary, backfill_days),
               keys={"filled": "summary_filled", "remaining": "summary_remaining"})
        _merge(await asyncio.to_thread(engine.backfill_splits),
               keys={"laps": "laps_stored"})
        return merged
