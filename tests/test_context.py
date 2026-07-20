"""ToolContext lifecycle + accessors — fartlek/mcp_server/context.py.

No network: the adapter is replaced by a fake, SyncEngine by a recording
fake (monkeypatched at the module level), and paths are redirected to tmp.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import fartlek.mcp_server.context as context_mod
from fartlek.health.exceptions import GarminAuthError
from fartlek.store import Store

ACCOUNT = "user123"


class FakeClient:
    display_name = ACCOUNT


class FakeAdapter:
    def __init__(self):
        self.connects = 0

    def connect_sync(self):
        self.connects += 1
        return FakeClient()

    def fetch_sync(self, client, path, **params):
        return {"path": path, **params}


class FailingAdapter:
    def connect_sync(self):
        raise GarminAuthError("tokens expired")


class FakeEngine:
    stale = False  # class attr so tests can flip it before construction

    def __init__(self, store, fetch, display_name, account_dir, **kw):
        self.store = store
        self.fetch = fetch
        self.display_name = display_name
        self.account_dir = Path(account_dir)
        self.calls: list = []

    def tier0(self):
        self.calls.append("tier0")
        return {"calls": 17}

    def tier1(self):
        self.calls.append("tier1")
        return {"calls": 12}

    def tier2(self, backfill_days=60):
        self.calls.append(("tier2", backfill_days))
        return {"calls": 2, "nights": 5, "done": True}

    def incremental(self):
        self.calls.append("incremental")
        return {"calls": 3, "new_activities": 1, "errors": []}

    def is_stale(self, hours=6.0):
        return self.stale

    def _call(self, path, **params):
        self.calls.append(("_call", path, params))
        return {"ok": True}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(context_mod, "SyncEngine", FakeEngine)
    monkeypatch.setattr(context_mod, "store_path", lambda uid: tmp_path / uid / "store.db")
    monkeypatch.setattr(context_mod, "account_dir", lambda uid: tmp_path / uid)
    c = context_mod.ToolContext(tokenstore=tmp_path / "tokens")
    c._adapter = FakeAdapter()
    return c


def _seed_warm_store(tmp_path, last_sync: str | None = None) -> None:
    with Store(tmp_path / ACCOUNT / "store.db") as s:
        s.set_sync_state("last_sync", last_sync or datetime.now().isoformat(timespec="seconds"))


# --- ensure_ready ------------------------------------------------------------

async def test_ensure_ready_cold_runs_tiers_inline_then_bg_tier2(ctx, tmp_path):
    await ctx.ensure_ready()
    assert ctx.cold_started is True
    assert ctx._engine.calls[:2] == ["tier0", "tier1"]
    assert (tmp_path / ACCOUNT / "store.db").exists()
    ctx._bg_thread.join(timeout=5)
    assert ("tier2", 60) in ctx._engine.calls  # engine default backfill


async def test_ensure_ready_warm_fresh_no_sync_work(ctx, tmp_path):
    _seed_warm_store(tmp_path)
    await ctx.ensure_ready()
    assert ctx.cold_started is False
    assert ctx._engine.calls == []
    assert ctx._bg_thread is None


async def test_ensure_ready_warm_stale_spawns_background_incremental(ctx, tmp_path, monkeypatch):
    _seed_warm_store(tmp_path)
    monkeypatch.setattr(FakeEngine, "stale", True)
    await ctx.ensure_ready()
    assert ctx.cold_started is False
    ctx._bg_thread.join(timeout=5)
    assert "incremental" in ctx._engine.calls


async def test_ensure_ready_idempotent_single_connect(ctx, tmp_path):
    _seed_warm_store(tmp_path)
    await ctx.ensure_ready()
    await ctx.ensure_ready()
    assert ctx._adapter.connects == 1


async def test_ensure_ready_auth_error_propagates(ctx):
    ctx._adapter = FailingAdapter()
    with pytest.raises(GarminAuthError):
        await ctx.ensure_ready()


# --- ensure_fresh_today ------------------------------------------------------

def _ready_ctx(ctx, tmp_path) -> Store:
    store = Store(tmp_path / ACCOUNT / "store.db")
    ctx._store = store
    ctx._engine = FakeEngine(store, fetch=None, display_name=ACCOUNT,
                             account_dir=tmp_path / ACCOUNT)
    return store


async def test_fresh_today_skips_when_wellness_present(ctx, tmp_path):
    store = _ready_ctx(ctx, tmp_path)
    store.upsert_day({"date": ctx.today(), "sleep_score": 70,
                      "synced_at": "2026-01-01T00:00:00"})
    store.set_sync_state("last_sync", (datetime.now() - timedelta(hours=3)).isoformat())
    await ctx.ensure_fresh_today()
    assert "incremental" not in ctx._engine.calls


async def test_fresh_today_skips_when_recent_sync(ctx, tmp_path):
    store = _ready_ctx(ctx, tmp_path)
    store.set_sync_state("last_sync", datetime.now().isoformat(timespec="seconds"))
    await ctx.ensure_fresh_today()
    assert "incremental" not in ctx._engine.calls


async def test_fresh_today_runs_inline_when_missing_and_stale(ctx, tmp_path):
    store = _ready_ctx(ctx, tmp_path)
    store.set_sync_state("last_sync", (datetime.now() - timedelta(minutes=45)).isoformat())
    await ctx.ensure_fresh_today()
    assert "incremental" in ctx._engine.calls


async def test_fresh_today_runs_when_never_synced(ctx, tmp_path):
    _ready_ctx(ctx, tmp_path)
    await ctx.ensure_fresh_today()
    assert "incremental" in ctx._engine.calls


# --- accessors ---------------------------------------------------------------

def test_today_is_server_local_iso(ctx):
    assert ctx.today() == date.today().isoformat()


def test_data_as_of(ctx, tmp_path):
    assert ctx.data_as_of() == "--:--"  # before ensure_ready
    store = _ready_ctx(ctx, tmp_path)
    assert ctx.data_as_of() == "--:--"  # never synced
    store.set_sync_state("last_sync", "2026-07-20T07:41:12")
    assert ctx.data_as_of() == "07:41"


def test_banner_none_then_active_alert(ctx, tmp_path):
    assert ctx.banner() is None  # before ensure_ready
    store = _ready_ctx(ctx, tmp_path)
    assert ctx.banner() is None
    store.upsert_alert("2026-07-17", "hrv", "RED", "HRV below band 3 days")
    banner = ctx.banner()
    assert banner is not None and banner.startswith("⚠ ACTIVE")
    assert "HRV below band 3 days" in banner


def test_banner_ignores_watch_alerts(ctx, tmp_path):
    store = _ready_ctx(ctx, tmp_path)
    store.upsert_alert("2026-07-17", "deep_sleep", "WATCH", "deep sleep low")
    assert ctx.banner() is None


# --- fetch_raw / run_sync ----------------------------------------------------

async def test_fetch_raw_goes_through_engine_rate_limited_call(ctx, tmp_path):
    _ready_ctx(ctx, tmp_path)
    out = await ctx.fetch_raw("/hrv-service/hrv/2026-07-20", foo=1)
    assert out == {"ok": True}
    assert ("_call", "/hrv-service/hrv/2026-07-20", {"foo": 1}) in ctx._engine.calls


async def test_run_sync_incremental_only(ctx, tmp_path):
    _ready_ctx(ctx, tmp_path)
    stats = await ctx.run_sync()
    assert stats["calls"] == 3 and stats["new_activities"] == 1
    assert ctx._engine.calls == ["incremental"]


async def test_run_sync_with_backfill_merges_stats(ctx, tmp_path):
    _ready_ctx(ctx, tmp_path)
    stats = await ctx.run_sync(backfill_days=30)
    assert ctx._engine.calls == ["incremental", ("tier2", 30)]
    assert stats["calls"] == 5  # 3 + 2, summed
    assert stats["nights"] == 5 and stats["done"] is True
    assert stats["new_activities"] == 1
