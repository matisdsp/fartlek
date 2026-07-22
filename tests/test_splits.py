"""Per-lap splits: digestion, storage and backfill (DESIGN.md §3.2 #12).

Laps exist because session averages lie: an interval session averaging
5:30/km is 3:30 reps plus 7:00 recoveries, and answering "what is my HR at
easy pace" from the session average silently mixes the two.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from test_sync import TODAY, make_engine

from fartlek.store import Store
from fartlek.sync.engine import digest_laps

TS = "2026-07-20T08:00:00"


def lap(index, *, distance=1000.0, duration=300.0, hr=140, speed=3.33,
        gap=None, temp=None, itype=None, **extra):
    d = {
        "lapIndex": index, "distance": distance, "duration": duration,
        "movingDuration": duration, "averageHR": hr, "averageSpeed": speed,
    }
    if gap is not None:
        d["avgGradeAdjustedSpeed"] = gap
    if temp is not None:
        d["averageTemperature"] = temp
    if itype is not None:
        d["intensityType"] = itype
    d.update(extra)
    return d


def act_row(aid, date=TODAY, sport="running", **kw):
    row = {"activity_id": aid, "date": date, "sport": sport, "synced_at": TS,
           "load_source": "garmin"}
    row.update(kw)
    return row


# --- digestion --------------------------------------------------------------

def test_digest_maps_the_fields_that_matter():
    raw = {"lapDTOs": [lap(0, distance=1000.0, duration=365.187, hr=115,
                           speed=2.738, gap=2.81, temp=28.0,
                           elevationGain=5.0, elevationLoss=0.0,
                           averageRunCadence=168.0, maxHR=124)]}
    rows = digest_laps(raw, 42)
    assert len(rows) == 1
    r = rows[0]
    assert r["activity_id"] == 42 and r["lap_index"] == 0
    assert r["distance_m"] == 1000.0 and r["duration_s"] == pytest.approx(365.187)
    assert r["avg_hr"] == 115 and r["max_hr"] == 124
    assert r["avg_speed"] == pytest.approx(2.738)
    assert r["gap_speed"] == pytest.approx(2.81)      # grade-adjusted, kept separate
    assert r["temp_c"] == 28.0                        # heat guard input
    assert r["elev_gain"] == 5.0 and r["avg_cadence"] == 168.0


def test_absent_gap_and_temperature_stay_null_never_substituted():
    """Substituting flat-ground speed for GAP would turn a hilly lap into a
    fast one; substituting a default temperature would fake the heat guard."""
    rows = digest_laps({"lapDTOs": [lap(0)]}, 1)
    assert "gap_speed" not in rows[0]
    assert "temp_c" not in rows[0]


def test_reads_the_typed_splits_container_too():
    raw = {"splits": [lap(0, itype="INTERVAL_ACTIVE"), lap(1, itype="INTERVAL_REST")]}
    rows = digest_laps(raw, 7)
    assert [r["intensity_type"] for r in rows] == ["INTERVAL_ACTIVE", "INTERVAL_REST"]


def test_recovery_laps_are_kept():
    """Filtering by intensity is an analysis decision, not a storage one —
    decoupling and interval digests both need the recoveries."""
    raw = {"splits": [lap(0, itype="INTERVAL_ACTIVE"), lap(1, hr=110, itype="INTERVAL_REST")]}
    assert len(digest_laps(raw, 7)) == 2


def test_empty_laps_are_dropped_but_zero_distance_time_laps_survive():
    raw = {"lapDTOs": [
        lap(0),
        {"lapIndex": 1},                                    # no distance, no duration
        lap(2, distance=0.0, duration=1778.9, speed=0.0),    # strength lap: time only
    ]}
    assert [r["lap_index"] for r in digest_laps(raw, 1)] == [0, 2]


def test_duplicate_lap_indices_do_not_break_the_primary_key():
    raw = {"lapDTOs": [lap(0, hr=120), lap(0, hr=130)]}
    rows = digest_laps(raw, 1)
    assert len(rows) == 1 and rows[0]["avg_hr"] == 130   # last wins


def test_missing_payload_is_not_an_error():
    assert digest_laps({}, 1) == []
    assert digest_laps({"lapDTOs": None}, 1) == []


# --- storage ----------------------------------------------------------------

def test_laps_round_trip_and_replace_wholesale(store: Store):
    store.upsert_activity(act_row(1))
    store.replace_activity_laps(1, digest_laps({"lapDTOs": [lap(0), lap(1)]}, 1))
    assert len(store.get_activity_laps(1)) == 2

    # A re-fetch is authoritative: an edited activity with fewer laps must not
    # leave orphans behind.
    store.replace_activity_laps(1, digest_laps({"lapDTOs": [lap(0, hr=99)]}, 1))
    laps = store.get_activity_laps(1)
    assert len(laps) == 1 and laps[0]["avg_hr"] == 99


def test_laps_in_range_carries_date_and_sport(store: Store):
    store.upsert_activity(act_row(1, date="2026-07-10"))
    store.upsert_activity(act_row(2, date="2026-07-20", sport="cycling"))
    store.replace_activity_laps(1, digest_laps({"lapDTOs": [lap(0)]}, 1))
    store.replace_activity_laps(2, digest_laps({"lapDTOs": [lap(0)]}, 2))

    rows = store.laps_in_range("2026-07-01", "2026-07-31")
    assert {r["sport"] for r in rows} == {"running", "cycling"}
    assert all(r["date"] for r in rows)

    runs = store.laps_in_range("2026-07-01", "2026-07-31", "%running%")
    assert [r["activity_id"] for r in runs] == [1]

    assert store.laps_in_range("2026-07-15", "2026-07-31", "%running%") == []


def test_missing_laps_worklist_is_newest_first(store: Store):
    for aid, d in ((1, "2026-07-10"), (2, "2026-07-20"), (3, "2026-07-15")):
        store.upsert_activity(act_row(aid, date=d))
    store.replace_activity_laps(3, digest_laps({"lapDTOs": [lap(0)]}, 3))

    pending = store.activities_missing_laps("2026-07-01", "2026-07-31")
    assert [a["activity_id"] for a in pending] == [2, 1]


# --- backfill ---------------------------------------------------------------

def _splits_routes(payload_by_id):
    def handler(path, params):
        aid = int(path.split("/activity/")[1].split("/")[0])
        return payload_by_id[aid]
    return {"/activity-service/activity/": handler}


def test_backfill_fetches_one_call_per_activity_and_stores_laps(store: Store, tmp_path: Path):
    for aid in (1, 2):
        store.upsert_activity(act_row(aid, date=TODAY))
    engine, fetch = make_engine(store, tmp_path, _splits_routes({
        1: {"lapDTOs": [lap(0), lap(1)]},
        2: {"lapDTOs": [lap(0)]},
    }))

    res = engine.backfill_splits(days=30)

    assert res["activities"] == 2 and res["laps"] == 3 and res["calls"] == 2
    assert len(store.get_activity_laps(1)) == 2
    assert len(fetch.paths("/activity-service/activity/")) == 2


def test_backfill_skips_activities_that_already_have_laps(store: Store, tmp_path: Path):
    store.upsert_activity(act_row(1))
    store.replace_activity_laps(1, digest_laps({"lapDTOs": [lap(0)]}, 1))
    engine, fetch = make_engine(store, tmp_path, _splits_routes({}))

    res = engine.backfill_splits(days=30)

    assert res["activities"] == 0 and fetch.calls == []


def test_activities_without_laps_are_not_refetched_forever(store: Store, tmp_path: Path):
    """Manual entries have no splits at all; probing them every sync would
    burn a call per activity per run, forever."""
    store.upsert_activity(act_row(1))
    routes = _splits_routes({1: {"lapDTOs": []}})

    engine, fetch = make_engine(store, tmp_path, routes)
    first = engine.backfill_splits(days=30)
    assert first["no_laps"] == 1 and len(fetch.calls) == 1

    engine2, fetch2 = make_engine(store, tmp_path, routes)
    second = engine2.backfill_splits(days=30)
    assert second["activities"] == 0 and fetch2.calls == []


def test_backfill_is_capped_and_reports_the_remainder(store: Store, tmp_path: Path):
    for aid in range(1, 6):
        store.upsert_activity(act_row(aid, date=TODAY))
    engine, _ = make_engine(store, tmp_path, _splits_routes(
        {aid: {"lapDTOs": [lap(0)]} for aid in range(1, 6)}
    ))

    res = engine.backfill_splits(days=30, limit=2)

    assert res["activities"] == 2 and res["remaining"] == 3


def test_backfill_respects_the_sport_filter(store: Store, tmp_path: Path):
    store.upsert_activity(act_row(1, sport="running"))
    store.upsert_activity(act_row(2, sport="strength_training"))
    engine, fetch = make_engine(store, tmp_path, _splits_routes({1: {"lapDTOs": [lap(0)]}}))

    res = engine.backfill_splits(days=30, sport_like="%running%")

    assert res["activities"] == 1
    assert len(fetch.calls) == 1


def test_one_failing_activity_does_not_abort_the_run(store: Store, tmp_path: Path):
    for aid in (1, 2):
        store.upsert_activity(act_row(aid, date=TODAY))

    def handler(path, params):
        aid = int(path.split("/activity/")[1].split("/")[0])
        if aid == 2:
            raise RuntimeError("boom")
        return {"lapDTOs": [lap(0)]}

    engine, _ = make_engine(store, tmp_path, {"/activity-service/activity/": handler})
    res = engine.backfill_splits(days=30)

    assert res["activities"] == 1 and res["errors"]
    assert len(store.get_activity_laps(1)) == 1


def test_backfill_window_excludes_older_activities(store: Store, tmp_path: Path):
    store.upsert_activity(act_row(1, date="2026-01-01"))
    engine, fetch = make_engine(store, tmp_path, _splits_routes({}))

    assert engine.backfill_splits(days=30)["activities"] == 0
    assert fetch.calls == []
