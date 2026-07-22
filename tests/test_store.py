"""Store unit tests — hermetic, tmp_path stores only."""
from __future__ import annotations

import csv
import sqlite3
import stat
from pathlib import Path

import pytest
from conftest import make_days

from fartlek.store import Store

TS = "2026-01-01T00:00:00"


def act(activity_id: int, date: str, **kw) -> dict:
    row = {"activity_id": activity_id, "date": date, "sport": "running", "synced_at": TS}
    row.update(kw)
    return row


# --- lifecycle ---------------------------------------------------------------


def test_parent_dir_created_0700(tmp_path: Path):
    with Store(tmp_path / "acct" / "sub" / "store.db") as s:
        s.set_sync_state("k", "v")
    parent = tmp_path / "acct" / "sub"
    assert parent.is_dir()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_existing_parent_permissions_untouched(tmp_path: Path):
    before = stat.S_IMODE(tmp_path.stat().st_mode)
    with Store(tmp_path / "store.db"):
        pass
    assert stat.S_IMODE(tmp_path.stat().st_mode) == before


def test_context_manager_closes(tmp_path: Path):
    with Store(tmp_path / "store.db") as s:
        assert isinstance(s, Store)
    with pytest.raises(sqlite3.ProgrammingError):
        s.get_day("2026-01-01")


def test_persistence_across_reopen(tmp_path: Path):
    path = tmp_path / "store.db"
    with Store(path) as s:
        s.upsert_day({"date": "2026-07-01", "steps": 1234, "synced_at": TS})
    with Store(path) as s:
        assert s.get_day("2026-07-01")["steps"] == 1234


def test_wal_and_busy_timeout(store: Store):
    assert store._conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"] == "wal"
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()["timeout"] == 5000


# --- days --------------------------------------------------------------------


def test_day_roundtrip(store: Store):
    store.upsert_day(
        {
            "date": "2026-07-10",
            "steps": 9000,
            "resting_hr": 47,
            "hrv_last_night": 55.5,
            "hrv_status": "BALANCED",
            "sleep_duration_h": 7.4,
            "synced_at": TS,
        }
    )
    row = store.get_day("2026-07-10")
    assert row["steps"] == 9000
    assert row["hrv_last_night"] == 55.5
    assert row["hrv_status"] == "BALANCED"
    assert row["sleep_score"] is None  # missing keys stay NULL
    assert row["daily_load"] == 0  # schema default


def test_day_missing_returns_none(store: Store):
    assert store.get_day("1999-01-01") is None


def test_partial_upsert_does_not_clobber(store: Store):
    store.upsert_day({"date": "2026-07-10", "steps": 9000, "resting_hr": 47, "synced_at": TS})
    store.upsert_day({"date": "2026-07-10", "steps": 10000})
    row = store.get_day("2026-07-10")
    assert row["steps"] == 10000
    assert row["resting_hr"] == 47  # untouched
    assert row["synced_at"] == TS


def test_upsert_unknown_key_raises(store: Store):
    with pytest.raises(KeyError):
        store.upsert_day({"date": "2026-07-10", "bogus_col": 1, "synced_at": TS})
    with pytest.raises(KeyError):
        store.upsert_activity(act(1, "2026-07-10", nonsense=True))
    assert store.get_day("2026-07-10") is None  # nothing written


def test_upsert_day_requires_date(store: Store):
    with pytest.raises(KeyError):
        store.upsert_day({"steps": 1, "synced_at": TS})


# --- get_series --------------------------------------------------------------


def test_get_series_ascending_skips_nulls(store: Store):
    for row in make_days("2026-07-10", 5, hrv_last_night=[50.0, None, 60.0, None, 70.0]):
        store.upsert_day(row)
    series = store.get_series("hrv_last_night", "2026-07-10", 5)
    assert series == [("2026-07-06", 50.0), ("2026-07-08", 60.0), ("2026-07-10", 70.0)]
    assert all(isinstance(v, float) for _, v in series)


def test_get_series_window_bounds(store: Store):
    for row in make_days("2026-07-10", 5, steps=[1, 2, 3, 4, 5]):
        store.upsert_day(row)
    series = store.get_series("steps", "2026-07-10", 2)
    assert series == [("2026-07-09", 4.0), ("2026-07-10", 5.0)]
    # end_date beyond stored data → only what exists in-window
    assert store.get_series("steps", "2026-07-11", 2) == [("2026-07-10", 5.0)]


def test_get_series_unknown_metric_raises(store: Store):
    with pytest.raises(KeyError):
        store.get_series("not_a_column", "2026-07-10", 7)


# --- recompute_daily_loads ---------------------------------------------------


def test_recompute_daily_loads(store: Store):
    for row in make_days("2026-07-03", 3):
        store.upsert_day(row)
    # day 1: two loaded activities, one with RPE
    store.upsert_activity(act(1, "2026-07-01", load=50.0, rpe=5, duration_s=1800.0))
    store.upsert_activity(act(2, "2026-07-01", load=30.0))
    # day 2: rest day (no activities)
    # day 3: activity with no load but an RPE
    store.upsert_activity(act(3, "2026-07-03", rpe=7, duration_s=3600.0))
    store.recompute_daily_loads()

    d1 = store.get_day("2026-07-01")
    assert d1["daily_load"] == 80.0
    assert d1["srpe_load"] == pytest.approx(5 * 30.0)  # only the RPE'd activity
    d2 = store.get_day("2026-07-02")
    assert d2["daily_load"] == 0
    assert d2["srpe_load"] is None  # no RPE'd activities → NULL, not 0
    d3 = store.get_day("2026-07-03")
    assert d3["daily_load"] == 0  # SUM of NULL loads coalesces to 0
    assert d3["srpe_load"] == pytest.approx(7 * 60.0)


def test_recompute_overwrites_stale_values(store: Store):
    store.upsert_day({"date": "2026-07-01", "daily_load": 999.0, "srpe_load": 999.0, "synced_at": TS})
    store.recompute_daily_loads()
    row = store.get_day("2026-07-01")
    assert row["daily_load"] == 0
    assert row["srpe_load"] is None


# --- activities --------------------------------------------------------------


def test_activity_roundtrip_and_partial_upsert(store: Store):
    store.upsert_activity(
        act(101, "2026-07-05", name="Tempo", duration_s=2400.0, avg_hr=155, load=88.5,
            hr_z3_s=1200.0, extra_json='{"k":1}')
    )
    row = store.get_activity(101)
    assert row["name"] == "Tempo"
    assert row["load"] == 88.5
    assert row["load_source"] == "garmin"  # schema default
    store.upsert_activity({"activity_id": 101, "rpe": 6, "rpe_source": "athlete"})
    row = store.get_activity(101)
    assert row["rpe"] == 6
    assert row["load"] == 88.5  # untouched
    assert store.get_activity(999) is None


def test_list_activities_range_and_order(store: Store):
    store.upsert_activity(act(3, "2026-07-03"))
    store.upsert_activity(act(1, "2026-07-01"))
    store.upsert_activity(act(2, "2026-07-02"))
    store.upsert_activity(act(9, "2026-07-09"))  # outside range
    rows = store.list_activities("2026-07-01", "2026-07-03")
    assert [r["activity_id"] for r in rows] == [1, 2, 3]


def test_activities_missing_load(store: Store):
    store.upsert_activity(act(1, "2026-07-01", load=50.0))
    store.upsert_activity(act(2, "2026-07-02"))
    store.upsert_activity(act(3, "2026-07-03"))
    assert [r["activity_id"] for r in store.activities_missing_load()] == [2, 3]


# --- sleep timeline / digests ------------------------------------------------


def test_sleep_timeline_roundtrip_and_window(store: Store):
    for d in ("2026-07-01", "2026-07-05", "2026-07-10"):
        store.upsert_sleep_timeline(d, f'[["deep","{d}T00:00","{d}T01:00"]]')
    store.upsert_sleep_timeline("2026-07-10", '[["rem","x","y"]]')  # overwrite
    rows = store.get_sleep_timeline("2026-07-10", days_back=7)
    assert [r["date"] for r in rows] == ["2026-07-05", "2026-07-10"]  # 07-01 outside window
    assert rows[1]["intervals_json"] == '[["rem","x","y"]]'


def test_activity_digest_roundtrip(store: Store):
    store.upsert_activity(act(7, "2026-07-01"))
    store.upsert_activity_digest(
        {"activity_id": 7, "kind": "steady", "method": "splits", "ef": 1.42,
         "decoupling": 0.03, "computed_at": TS}
    )
    row = store.get_activity_digest(7)
    assert row["kind"] == "steady"
    assert row["ef"] == 1.42
    assert row["hot"] == 0  # schema default
    store.upsert_activity_digest({"activity_id": 7, "kind": "long", "method": "stream",
                                  "computed_at": TS})
    assert store.get_activity_digest(7)["kind"] == "long"
    assert store.get_activity_digest(8) is None


# --- pmc ---------------------------------------------------------------------


def test_replace_pmc_full_rewrite(store: Store):
    store.replace_pmc(
        [{"date": "2026-07-01", "load": 50.0, "ctl": 40.0, "atl": 45.0, "tsb": -5.0}]
    )
    new = [
        {"date": "2026-07-02", "load": 0.0, "ctl": 39.0, "atl": 39.5, "tsb": -5.0},
        {"date": "2026-07-03", "load": 80.0, "ctl": 40.0, "atl": 45.0, "tsb": -0.5},
    ]
    store.replace_pmc(new)
    rows = store.get_pmc("2026-07-03", 30)
    assert [r["date"] for r in rows] == ["2026-07-02", "2026-07-03"]  # old row gone
    assert rows[1]["tsb"] == -0.5


def test_get_pmc_window(store: Store):
    store.replace_pmc(
        [{"date": f"2026-07-{d:02d}", "load": 1.0, "ctl": 1.0, "atl": 1.0, "tsb": 0.0}
         for d in range(1, 6)]
    )
    rows = store.get_pmc("2026-07-05", 2)
    assert [r["date"] for r in rows] == ["2026-07-04", "2026-07-05"]


def test_replace_pmc_unknown_key_raises(store: Store):
    with pytest.raises(KeyError):
        store.replace_pmc([{"date": "2026-07-01", "load": 1.0, "ctl": 1.0, "atl": 1.0,
                            "tsb": 0.0, "bogus": 9}])


# --- baselines ---------------------------------------------------------------


def test_baselines_roundtrip_and_update(store: Store):
    store.upsert_baselines(
        [{"metric": "resting_hr", "date": "2026-07-10", "window": 28, "mean": 47.1,
          "median": 47.0, "mad_sd": 1.5, "n": 28},
         {"metric": "resting_hr", "date": "2026-07-10", "window": 7, "mean": 48.0, "n": 7}]
    )
    row = store.get_baseline("resting_hr", "2026-07-10", 28)
    assert row["mean"] == 47.1
    assert row["n"] == 28
    store.upsert_baselines(
        [{"metric": "resting_hr", "date": "2026-07-10", "window": 28, "mean": 46.0, "n": 28}]
    )
    row = store.get_baseline("resting_hr", "2026-07-10", 28)
    assert row["mean"] == 46.0
    assert row["median"] == 47.0  # partial upsert kept it
    assert store.get_baseline("resting_hr", "2026-07-10", 60) is None


# --- alerts ------------------------------------------------------------------


def test_active_alerts_ordering(store: Store):
    store.upsert_alert("2026-07-01", "hrv", "WATCH", "w")
    store.upsert_alert("2026-07-02", "rhr", "RED", "r-old")
    store.upsert_alert("2026-07-04", "monotony", "AMBER", "a")
    store.upsert_alert("2026-07-05", "ramp", "RED", "r-new")
    rows = store.active_alerts()
    assert [(r["metric"], r["severity"]) for r in rows] == [
        ("ramp", "RED"), ("rhr", "RED"), ("monotony", "AMBER"), ("hrv", "WATCH")
    ]


def test_upsert_alert_one_active_row_per_metric(store: Store):
    store.upsert_alert("2026-07-01", "hrv", "WATCH", "first")
    store.upsert_alert("2026-07-03", "hrv", "AMBER", "worse")
    rows = store.active_alerts()
    assert len(rows) == 1
    assert rows[0]["severity"] == "AMBER"
    assert rows[0]["message"] == "worse"
    assert rows[0]["date"] == "2026-07-01"  # first trip date preserved


def test_resolve_alert_and_reopen(store: Store):
    store.upsert_alert("2026-07-01", "hrv", "AMBER", "m")
    store.resolve_alert("hrv", "2026-07-04")
    assert store.active_alerts() == []
    # a new trip after resolution inserts a fresh row
    store.upsert_alert("2026-07-06", "hrv", "RED", "again")
    rows = store.active_alerts()
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-07-06"
    assert rows[0]["resolved"] == 0


def test_resolve_alert_noop_when_absent(store: Store):
    store.resolve_alert("nope", "2026-07-04")  # must not raise
    assert store.active_alerts() == []


# --- wellness log ------------------------------------------------------------


def test_wellness_log_roundtrip(store: Store):
    id1 = store.add_log({"date": "2026-07-01", "rpe": 6, "activity_id": 101, "created_at": TS})
    id2 = store.add_log({"date": "2026-07-01", "flag": "illness", "note": "sore throat",
                         "created_at": TS})
    store.add_log({"date": "2026-07-02", "fatigue": 5, "created_at": TS})
    assert isinstance(id1, int) and id2 > id1
    rows = store.logs_for("2026-07-01")
    assert [r["id"] for r in rows] == [id1, id2]
    assert rows[0]["rpe"] == 6
    assert rows[1]["flag"] == "illness"
    with pytest.raises(KeyError):
        store.add_log({"date": "2026-07-01", "bogus": 1, "created_at": TS})


def test_unresolved_injuries(store: Store):
    i1 = store.add_log({"date": "2026-07-01", "flag": "injury", "note": "knee", "created_at": TS})
    store.add_log({"date": "2026-07-02", "flag": "injury", "resolved": 1, "created_at": TS})
    store.add_log({"date": "2026-07-03", "flag": "illness", "created_at": TS})
    rows = store.unresolved_injuries()
    assert [r["id"] for r in rows] == [i1]
    assert rows[0]["note"] == "knee"


# --- profile / plan / capabilities / sync state ------------------------------


def test_profile_set_get_overwrite(store: Store):
    assert store.get_profile() == {}
    store.set_profile("goal_race_date", "2026-10-11")
    store.set_profile("phase", "base")
    store.set_profile("phase", "build")
    assert store.get_profile() == {"goal_race_date": "2026-10-11", "phase": "build"}


def test_plan_entry_insert_update_and_match(store: Store):
    pid = store.upsert_plan_entry({"date": "2026-07-21", "sport": "running",
                                   "name": "Tempo 40min", "source": "calendar"})
    assert isinstance(pid, int)
    pid2 = store.upsert_plan_entry({"id": pid, "name": "Tempo 45min"})
    assert pid2 == pid
    rows = store.plan_entries("2026-07-20", "2026-07-22")
    assert len(rows) == 1
    assert rows[0]["name"] == "Tempo 45min"
    assert rows[0]["source"] == "calendar"  # partial update kept it
    assert rows[0]["matched_activity_id"] is None

    store.set_plan_match(pid, 555, "heuristic")
    row = store.plan_entries("2026-07-21", "2026-07-21")[0]
    assert (row["matched_activity_id"], row["match_method"]) == (555, "heuristic")
    store.set_plan_match(pid, None, None)  # unmatch
    row = store.plan_entries("2026-07-21", "2026-07-21")[0]
    assert row["matched_activity_id"] is None and row["match_method"] is None


def test_capabilities_roundtrip(store: Store):
    store.set_capability("activityTrainingLoad", True, "present on sampled activities")
    store.set_capability("training_readiness", False)
    store.set_capability("training_readiness", True, "appeared after device sync")
    caps = store.get_capabilities()
    assert caps["activityTrainingLoad"]["available"] is True
    assert caps["training_readiness"]["available"] is True
    assert caps["training_readiness"]["detail"] == "appeared after device sync"
    assert caps["training_readiness"]["probed_at"]


def test_sync_state_roundtrip(store: Store):
    assert store.get_sync_state("last_sync") is None
    store.set_sync_state("last_sync", "2026-07-19T08:00:00")
    store.set_sync_state("last_sync", "2026-07-20T08:00:00")
    assert store.get_sync_state("last_sync") == "2026-07-20T08:00:00"


# --- export ------------------------------------------------------------------


def test_export_csv_one_file_per_table(store: Store, tmp_path: Path):
    store.upsert_day({"date": "2026-07-01", "steps": 9000, "synced_at": TS})
    store.upsert_activity(act(1, "2026-07-01", load=50.0))
    out = tmp_path / "export"
    paths = store.export_csv(out)
    expected = {"schema_meta", "days", "activities", "sleep_timeline", "activity_laps",
                "activity_digests", "baselines", "pmc", "alerts", "wellness_log",
                "athlete_profile", "plan_calendar", "capability_map", "sync_state"}
    assert {p.stem for p in paths} == expected
    assert all(p.exists() and p.parent == out for p in paths)

    with (out / "days.csv").open(newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][:2] == ["date", "steps"]
    assert rows[1][0] == "2026-07-01"
    assert rows[1][1] == "9000"
    # empty table still gets a header-only CSV
    with (out / "pmc.csv").open(newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [["date", "load", "ctl", "atl", "tsb"]]
