"""Gear: digestion, attribution and backfill (DESIGN.md §3.3).

Garmin's odometer answers "how far on this pair"; it cannot answer "which
pair, on which session" — nothing in the activity payload names the gear. So
attribution costs one call per session, and everything here exists to make
that call happen once and only once per session.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from test_sync import GEAR_CATALOG, TODAY, base_routes, gear_entry, make_engine

from fartlek.store import Store
from fartlek.sync.engine import digest_gear, digest_gear_stats

TS = "2026-07-20T08:00:00"


def act_row(aid, date=TODAY, sport="running", **kw):
    row = {"activity_id": aid, "date": date, "sport": sport, "synced_at": TS,
           "load_source": "garmin"}
    row.update(kw)
    return row


def gear_routes_for(worn: dict[int, list[str]], catalog=None):
    """filterGear routed on its params, with `worn` mapping activity → uuids."""
    catalog = GEAR_CATALOG if catalog is None else catalog
    by_uuid = {g["uuid"]: g for g in catalog}

    def filter_gear(path, params):
        if "activityId" in params:
            return [by_uuid[u] for u in worn.get(int(params["activityId"]), [])]
        return catalog

    routes = base_routes()
    routes["/gear-service/gear/filterGear"] = filter_gear
    return routes


# --- digestion --------------------------------------------------------------

def test_digest_maps_the_fields_that_matter():
    row = digest_gear(gear_entry("u1", display_name="Bondi 9 II"))
    assert row["uuid"] == "u1" and row["gear_pk"] == 46846508
    assert row["type"] == "shoes" and row["status"] == "active"
    assert row["name"] == "Bondi 9 II"          # the athlete's label wins
    assert row["make_model"] == "Hoka Bondi 9"  # kept: it says what they are
    assert row["date_begin"] == "2025-12-02" and row["date_end"] is None
    assert row["max_meters"] == 1_000_000.0
    # odometer columns are absent, not None — a partial upsert must not blank
    # values the stats endpoint wrote
    assert "total_meters" not in row


def test_digest_name_falls_back_model_then_make_then_placeholder():
    """Garmin leaves displayName null on gear the athlete never renamed."""
    assert digest_gear(gear_entry("u1"))["name"] == "Hoka Bondi 9"
    bare = gear_entry("u1", model=None, gearMakeName="Hoka", gearModelName="Clifton")
    assert digest_gear(bare)["name"] == "Hoka Clifton"
    nameless = gear_entry("u1", model=None, gearMakeName=None, gearModelName=None)
    assert digest_gear(nameless)["name"] == "unnamed gear"  # never a bare uuid


def test_digest_normalizes_type_and_status():
    assert digest_gear(gear_entry("u1", gearTypeName="Bike"))["type"] == "bike"
    assert digest_gear(gear_entry("u1", gearTypeName="Other"))["type"] == "other"
    assert digest_gear(gear_entry("u1", gearTypeName=None))["type"] == "other"
    retired = gear_entry("u1", gearStatusName="retired", dateEnd="2026-06-01T00:00:00.0")
    assert digest_gear(retired)["status"] == "retired"
    assert digest_gear(retired)["date_end"] == "2026-06-01"


def test_digest_treats_a_zero_threshold_as_unset():
    """0 means 'no retirement distance configured', not 'retire at 0 km'."""
    assert digest_gear(gear_entry("u1", maximumMeters=0))["max_meters"] is None
    assert digest_gear(gear_entry("u1", maximumMeters=None))["max_meters"] is None


def test_digest_rejects_an_entry_with_no_uuid():
    assert digest_gear({"gearTypeName": "Shoes"}) is None


def test_digest_stats_keys_on_the_uuid_that_was_asked_for():
    row = digest_gear_stats({"totalDistance": 514248.2, "totalActivities": 51}, "u1")
    assert row == {"uuid": "u1", "total_meters": 514248.2, "total_activities": 51}
    # an echoed uuid is honoured, an empty shell yields nothing
    assert digest_gear_stats({"uuid": "u2", "totalDistance": 1.0}, "u1")["uuid"] == "u2"
    assert digest_gear_stats({"isProcessing": True}, "u1") is None
    assert digest_gear_stats(None, "u1") is None


# --- attribution backfill ----------------------------------------------------

def test_backfill_links_one_call_per_session(store: Store, tmp_path: Path):
    for aid in (1, 2):
        store.upsert_activity(act_row(aid))
    engine, fetch = make_engine(
        store, tmp_path, gear_routes_for({1: ["shoe-a"], 2: ["shoe-b"]})
    )

    res = engine.backfill_gear(days=30)

    assert res["linked"] == 2 and res["no_gear"] == 0
    assert [g["uuid"] for g in store.gear_for_activity(1)] == ["shoe-a"]
    assert [g["uuid"] for g in store.gear_for_activity(2)] == ["shoe-b"]
    # 2 attribution calls + one odometer refresh per pair touched
    assert res["calls"] == 4


def test_backfill_upserts_gear_the_catalog_never_returned(store: Store, tmp_path: Path):
    """A pair deleted in Connect still hangs off old sessions — it must land
    with a name rather than a dangling link."""
    store.upsert_activity(act_row(1))
    ghost = gear_entry("ghost", display_name="Retired Pegasus", gearStatusName="retired")
    routes = gear_routes_for({1: ["ghost"]}, catalog=[*GEAR_CATALOG, ghost])
    routes["/gear-service/gear/filterGear"] = (
        lambda path, params: [ghost] if "activityId" in params else GEAR_CATALOG
    )
    engine, _ = make_engine(store, tmp_path, routes)

    engine.backfill_gear(days=30)

    assert store.get_gear("ghost")["name"] == "Retired Pegasus"
    assert [g["uuid"] for g in store.gear_for_activity(1)] == ["ghost"]


def test_backfill_skips_sessions_already_linked(store: Store, tmp_path: Path):
    store.upsert_activity(act_row(1))
    store.upsert_gear({"uuid": "shoe-a", "type": "shoes", "name": "x",
                       "status": "active", "synced_at": TS})
    store.replace_activity_gear(1, ["shoe-a"])
    engine, fetch = make_engine(store, tmp_path, gear_routes_for({1: ["shoe-a"]}))

    res = engine.backfill_gear(days=30)

    assert res["linked"] == 0 and fetch.calls == []


def test_backfill_is_resumable_through_its_work_list(store: Store, tmp_path: Path):
    for aid in (1, 2, 3):
        store.upsert_activity(act_row(aid, date=f"2026-07-{15 + aid}"))
    worn = {1: ["shoe-a"], 2: ["shoe-a"], 3: ["shoe-b"]}

    engine, _ = make_engine(store, tmp_path, gear_routes_for(worn))
    first = engine.backfill_gear(days=30, limit=2)
    assert first["linked"] == 2 and first["remaining"] == 1

    engine2, fetch2 = make_engine(store, tmp_path, gear_routes_for(worn))
    second = engine2.backfill_gear(days=30, limit=2)
    assert second["linked"] == 1 and second["remaining"] == 0
    # newest first, so the leftover is the oldest session
    assert [p for p, kw in fetch2.calls if kw.get("activityId") == "1"] != []


def test_a_settled_session_with_no_gear_is_remembered(store: Store, tmp_path: Path):
    """One call ever for a session that genuinely carries none."""
    store.upsert_activity(act_row(1, date="2026-06-01", sport="strength"))
    engine, fetch = make_engine(store, tmp_path, gear_routes_for({}))

    first = engine.backfill_gear(days=90)
    assert first["no_gear"] == 1 and first["linked"] == 0
    assert store.get_sync_state("gear_no_link") == "[1]"

    engine2, fetch2 = make_engine(store, tmp_path, gear_routes_for({}))
    assert engine2.backfill_gear(days=90)["no_gear"] == 0
    assert fetch2.calls == []


def test_a_fresh_session_with_no_gear_is_retried(store: Store, tmp_path: Path):
    """Garmin may not have attached the default pair when we first asked, so
    a session from this week is never written off."""
    store.upsert_activity(act_row(1, date=TODAY))
    engine, _ = make_engine(store, tmp_path, gear_routes_for({}))

    assert engine.backfill_gear(days=30)["no_gear"] == 1
    assert store.get_sync_state("gear_no_link") is None

    # the pair shows up on the next pass and gets linked
    engine2, _ = make_engine(store, tmp_path, gear_routes_for({1: ["shoe-a"]}))
    assert engine2.backfill_gear(days=30)["linked"] == 1


def test_backfill_survives_a_failing_attribution_call(store: Store, tmp_path: Path):
    store.upsert_activity(act_row(1))
    store.upsert_activity(act_row(2))
    routes = base_routes()
    routes["/gear-service/gear/filterGear"] = RuntimeError("HTTP 500")
    engine, _ = make_engine(store, tmp_path, routes)

    res = engine.backfill_gear(days=30)

    assert res["linked"] == 0 and len(res["errors"]) == 2
    assert store.gear_for_activity(1) == []


# --- incremental -------------------------------------------------------------

def test_incremental_links_the_sessions_it_ingests(store: Store, tmp_path: Path):
    """The canned page holds activities 101 and 102; only 101 wears a pair."""
    engine, fetch = make_engine(store, tmp_path, base_routes())

    res = engine.incremental()

    assert res["new_activities"] == 2 and res["gear_linked"] == 1
    assert [g["uuid"] for g in store.gear_for_activity(101)] == ["shoe-a"]
    assert store.gear_for_activity(102) == []
    # the odometer is refreshed only for the pair that was actually worn
    stats = fetch.paths("/gear-service/gear/stats/")
    assert [p.rsplit("/", 1)[-1] for p, _ in stats] == ["shoe-a"]


def test_incremental_refreshes_the_locker_even_with_no_new_session(
    store: Store, tmp_path: Path
):
    engine, _ = make_engine(store, tmp_path, base_routes())
    engine.incremental()                       # ingests both, links one

    engine2, fetch2 = make_engine(store, tmp_path, base_routes())
    res = engine2.incremental()

    assert res["new_activities"] == 0 and res["gear_linked"] == 0
    assert len(fetch2.paths("/gear-service/gear/filterGear")) == 1  # the locker only
    assert fetch2.paths("/gear-service/gear/stats/") == []


def test_gear_capability_records_an_empty_locker(store: Store, tmp_path: Path):
    """No gear on file is a normal account state, not a failure — the tools
    need to be able to say so instead of rendering an empty table."""
    routes = base_routes()
    routes["/gear-service/gear/filterGear"] = []
    engine, _ = make_engine(store, tmp_path, routes)

    engine.incremental()

    cap = store.get_capabilities()["gear"]
    assert cap["available"] is False and cap["detail"] == "no gear on file"


def test_tier2_attributes_gear_after_the_sleep_backfill(store: Store, tmp_path: Path):
    """tier2 is the only tier that can afford a call per historical session,
    and it must run the pass on the heal path too — a store whose sleep cursor
    is already 'done' would otherwise never attribute anything."""
    store.upsert_activity(act_row(1, date="2026-07-19"))
    store.set_sync_state(
        "tier2_cursor", '{"phase": "done", "next_date": "2026-07-10", '
                        '"end_date": "2026-07-10"}'
    )
    store.set_sync_state("tier2_healed_until", "2026-07-19")
    engine, _ = make_engine(store, tmp_path, gear_routes_for({1: ["shoe-a"]}))

    res = engine.tier2(backfill_days=10)

    assert res["gear_linked"] == 1
    assert [g["uuid"] for g in store.gear_for_activity(1)] == ["shoe-a"]
    # the wrapper reports the whole tier's calls, not just the sleep phase's
    assert res["calls"] >= 2


def test_backfill_is_a_noop_without_activities(store: Store, tmp_path: Path):
    engine, fetch = make_engine(store, tmp_path, base_routes())
    res = engine.backfill_gear(days=30)
    assert res == {"calls": 0, "linked": 0, "no_gear": 0, "remaining": 0, "errors": []}
    assert fetch.calls == []


@pytest.mark.parametrize("sport", ["running", "cycling", "strength"])
def test_backfill_does_not_filter_by_sport(store: Store, tmp_path: Path, sport: str):
    """Shoes, bikes and straps all hang off gear; filtering by sport would
    silently drop bike gear."""
    store.upsert_activity(act_row(1, sport=sport))
    engine, _ = make_engine(store, tmp_path, gear_routes_for({1: ["shoe-a"]}))
    assert engine.backfill_gear(days=30)["linked"] == 1


def test_daily_catchup_chips_away_at_the_work_list(store: Store, tmp_path: Path):
    """A warm store never reaches tier 2 on its own, so the background refresh
    is what keeps attribution moving — bounded, and free once it is done."""
    for aid in range(1, 4):
        store.upsert_activity(act_row(aid, date=f"2026-07-{15 + aid}"))
    routes = gear_routes_for({1: ["shoe-a"], 2: ["shoe-a"], 3: ["shoe-b"]})
    # no new sessions on the wire: isolate the catch-up from incremental's own
    # attribution of what it just ingested
    routes["/activitylist-service/activities/search/activities"] = []
    engine, _ = make_engine(store, tmp_path, routes)
    engine.GEAR_CATCHUP_PER_RUN = 2

    first = engine.daily_catchup()
    assert first["gear_linked"] == 2 and first["gear_remaining"] == 1

    second = engine.daily_catchup()
    assert second["gear_linked"] == 1 and second["gear_remaining"] == 0

    # work list empty → the pass stops costing anything
    engine2, fetch2 = make_engine(store, tmp_path, gear_routes_for({}))
    engine2.daily_catchup()
    assert fetch2.paths("/gear-service/gear/stats/") == []


def test_daily_catchup_still_does_the_incremental_work(store: Store, tmp_path: Path):
    engine, _ = make_engine(store, tmp_path, base_routes())
    res = engine.daily_catchup()
    assert res["new_activities"] == 2
    assert store.get_day(TODAY)["steps"] == 9000
