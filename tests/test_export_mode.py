"""ToolContext and tools when the data comes from a Garmin export (synthetic export, no network)."""
from __future__ import annotations

import json
import sys

import pytest
from test_garmin_export import DAY, _write_export

from fartlek.health.exceptions import GarminAuthError
from fartlek.mcp_server.context import ToolContext
from fartlek.mcp_server.tools import activity, raw, sync_tool
from fartlek.paths import export_source_file
from fartlek.sync.export_import import import_export, remembered_export, remembered_source


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FARTLEK_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    return tmp_path / "home"


@pytest.fixture
def export_zip(tmp_path):
    return _write_export(tmp_path / "export.zip")


@pytest.fixture
def imported(home, export_zip):
    import_export(export_zip, days=30)
    return export_zip


def make_ctx(tmp_path) -> ToolContext:
    return ToolContext(tokenstore=tmp_path / "tokens")


# --- what the import remembers ---------------------------------------------


def test_import_records_the_account_and_the_last_day(imported):
    record = json.loads(export_source_file().read_text())
    assert record["display_name"] == "abc"
    assert record["last_date"] == DAY
    assert record["path"] == str(imported.resolve())


def test_an_old_record_without_account_still_reads(home, export_zip):
    home.mkdir()
    export_source_file().write_text(json.dumps({"path": str(export_zip)}))
    source = remembered_source()
    assert source is not None and source.available and not source.describes_a_store
    assert remembered_export() == export_zip


def test_a_damaged_record_is_ignored(home):
    home.mkdir()
    export_source_file().write_text("{not json")
    assert remembered_source() is None and remembered_export() is None


async def test_opening_from_an_old_record_upgrades_it(home, export_zip, tmp_path):
    import_export(export_zip, days=30)
    export_source_file().write_text(json.dumps({"path": str(export_zip)}))
    ctx = make_ctx(tmp_path)
    await ctx.ensure_ready()
    assert ctx.source_kind == "export"
    assert json.loads(export_source_file().read_text())["display_name"] == "abc"


# --- the export file was deleted after the import ---------------------------


async def test_the_store_keeps_serving_when_the_zip_is_gone(imported, tmp_path):
    imported.unlink()
    assert remembered_export() is None  # the file itself is gone ...
    ctx = make_ctx(tmp_path)
    await ctx.ensure_ready()  # ... and that is not a reason to ask for a login
    assert ctx.source_kind == "store"
    assert ctx.today() == DAY  # the last day of the data, not the calendar
    assert ctx.store.get_day(DAY)["resting_hr"] == 48
    banner = ctx.banner()
    assert "export imported earlier" in banner and DAY in banner
    assert "no longer" in banner


async def test_without_the_zip_nothing_is_started(imported, tmp_path):
    imported.unlink()
    ctx = make_ctx(tmp_path)
    await ctx.ensure_ready()
    assert ctx._bg_thread is None
    assert ctx.cold_started is False


async def test_a_deleted_zip_with_an_old_record_has_nothing_to_serve(home, export_zip, tmp_path):
    import_export(export_zip, days=30)
    export_source_file().write_text(json.dumps({"path": str(export_zip)}))
    export_zip.unlink()
    ctx = make_ctx(tmp_path)
    with pytest.raises(GarminAuthError):
        await ctx.ensure_ready()


async def test_a_record_whose_store_is_gone_has_nothing_to_serve(imported, home, tmp_path):
    imported.unlink()
    for db in home.glob("*/store.db*"):
        db.unlink()
    ctx = make_ctx(tmp_path)
    with pytest.raises(GarminAuthError):
        await ctx.ensure_ready()


# --- no live refresh without a connection ------------------------------------


@pytest.mark.parametrize("zip_present", [True, False])
async def test_ensure_fresh_today_starts_nothing(imported, tmp_path, zip_present):
    """Today's wellness row is empty and the last sync is old: with a live
    connection that would trigger an inline refresh. From an export it must not."""
    if not zip_present:
        imported.unlink()
    ctx = make_ctx(tmp_path)
    await ctx.ensure_ready()
    calls = []
    ctx._engine.incremental = lambda: calls.append("incremental") or {}
    ctx._engine.daily_catchup = lambda: calls.append("daily_catchup") or {}
    ctx.store.set_sync_state("last_sync", "2020-01-01T00:00:00")
    ctx.store._conn.execute("delete from days where date = ?", (ctx.today(),))
    ctx.store._conn.commit()
    await ctx.ensure_fresh_today()
    await ctx.ensure_ready()  # would start the stale-store catch-up thread on a connection
    assert calls == []
    assert ctx._bg_thread is None


# --- tools that need what an export does not hold ----------------------------

NOT_IN_EXPORT = ["activity_summary", "activity_zones", "weather", "stress_detail",
                 "body_battery", "training_status"]


async def ready(tmp_path) -> ToolContext:
    ctx = make_ctx(tmp_path)
    await ctx.ensure_ready()
    return ctx


@pytest.mark.parametrize("source", NOT_IN_EXPORT)
async def test_raw_says_the_export_lacks_the_source(imported, tmp_path, source):
    ctx = await ready(tmp_path)
    out = await raw.run(ctx, source=source, activity_id=111)
    assert f"{source} is not in a Garmin account export" in out
    # never the live-connection advice, which cannot help from an export
    assert "garmin_sync" not in out and "watch may not have synced" not in out
    assert "Traceback" not in out


async def test_raw_still_serves_what_the_export_holds(imported, tmp_path):
    ctx = await ready(tmp_path)
    out = await raw.run(ctx, source="daily_summary", date=DAY)
    assert '"restingHeartRate":48' in out


async def test_raw_with_no_matching_data_names_the_export(imported, tmp_path):
    ctx = await ready(tmp_path)
    out = await raw.run(ctx, source="daily_summary", date="2020-01-01")
    assert "The Garmin export has no daily_summary data" in out
    assert "garmin_sync" not in out
    # the synthetic export carries no FIT files, so no laps can be read for activity 111
    out = await raw.run(ctx, source="activity_splits", activity_id=111)
    assert "FIT file" in out and "none matched" in out


async def test_raw_without_the_export_file_says_so(imported, tmp_path):
    imported.unlink()
    ctx = await ready(tmp_path)
    out = await raw.run(ctx, source="daily_summary", date=DAY)
    assert "no longer on disk" in out and "fartlek import" in out


async def test_activity_splits_and_full_explain_what_is_missing(imported, tmp_path):
    ctx = await ready(tmp_path)
    splits = await activity.run(ctx, activity_id=111, detail="splits")
    assert "splits are not in the export for this session" in splits
    assert "no splits recorded" not in splits and "live fetch" not in splits
    assert 'detail="full"' not in splits  # no breadcrumb into a dead end

    full = await activity.run(ctx, activity_id=111, detail="full")
    assert "HR/pace curve is not available from a Garmin export" in full
    assert "live fetch failed" not in full and "garmin_sync" not in full
    assert "Morning run" in full  # the session itself is still described


async def test_activity_detail_without_the_export_file(imported, tmp_path):
    imported.unlink()
    ctx = await ready(tmp_path)
    for detail in ("splits", "full"):
        out = await activity.run(ctx, activity_id=111, detail=detail)
        assert "no longer on disk" in out
        assert "live fetch failed" not in out


async def test_sync_from_an_export_does_not_talk_about_the_garmin_api(imported, tmp_path):
    ctx = await ready(tmp_path)
    out = await sync_tool.run(ctx)
    assert "Re-read the Garmin export" in out and f"through {DAY}" in out
    assert "1 activity in the store" in out
    assert "fartlek import" in out
    assert "calls" not in out and "new activities" not in out


async def test_sync_after_the_export_is_deleted_asks_for_a_new_import(imported, tmp_path):
    imported.unlink()
    ctx = await ready(tmp_path)
    for days in (0, 30):
        out = await sync_tool.run(ctx, backfill_days=days)
        assert "no longer on disk" in out and "fartlek import" in out
        assert "Synced:" not in out


# --- tokens on disk, but no direct connection installed -----------------------


def _tokens(tmp_path):
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "garmin_tokens.json").write_text("{}")


async def test_an_imported_export_wins_over_tokens(imported, tmp_path):
    _tokens(tmp_path)
    ctx = make_ctx(tmp_path)
    await ctx.ensure_ready()
    assert ctx.source_kind == "export"


async def test_the_direct_connection_must_be_asked_for_once_an_export_exists(
    imported, tmp_path, monkeypatch
):
    monkeypatch.setenv("FARTLEK_SOURCE", "connect")
    _tokens(tmp_path)
    ctx = make_ctx(tmp_path)
    ctx._adapter = None
    with pytest.raises(Exception):  # noqa: B017 - fake tokens cannot log in; export is not used
        await ctx.ensure_ready()
    assert ctx.source_kind != "export"


async def test_tokens_without_the_extra_and_without_an_export_say_what_is_missing(
    home, tmp_path, monkeypatch
):
    import builtins

    from fartlek.health.exceptions import GarminConnectUnavailable

    _tokens(tmp_path)
    real_import = builtins.__import__

    def no_garminconnect(name, *args, **kwargs):
        if name == "garminconnect" or name.startswith("garminconnect."):
            raise ImportError("No module named 'garminconnect'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "fartlek.health.adapters.garmin_connect", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_garminconnect)
    with pytest.raises(GarminConnectUnavailable):
        await make_ctx(tmp_path).ensure_ready()
