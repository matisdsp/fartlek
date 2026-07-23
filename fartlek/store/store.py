"""Per-account SQLite store (DESIGN.md §3.3).

One Store per Garmin account. WAL mode, busy_timeout=5000. All methods are
synchronous (callers wrap in asyncio.to_thread when needed). Row shapes are
fixed by schema.sql — that file is the contract shared with analytics/sync.

Conventions:
- upsert_* methods take plain dicts whose keys match schema columns; unknown
  keys raise KeyError (catch schema drift early), missing keys stay NULL, and
  an upsert only overwrites the columns present in the dict.
- get_series(metric, end_date, days) returns [(date, value)] ascending with
  NULLs skipped — gap handling is the caller's job.
- No business logic here: pure persistence + a few typed readers.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_SEVERITY_RANK = "CASE severity WHEN 'RED' THEN 0 WHEN 'AMBER' THEN 1 ELSE 2 END"


def _dict_row(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def _window_start(end_date: str, days: int) -> str:
    """First date of a `days`-long window ending at end_date (inclusive)."""
    return (_date.fromisoformat(end_date) - timedelta(days=days - 1)).isoformat()


class Store:
    """Pure persistence over the per-account SQLite database."""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        parent = db_path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            parent.chmod(0o700)
        # check_same_thread=False: the MCP layer calls Store methods from
        # asyncio.to_thread worker threads; callers serialize access.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = _dict_row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # Column/pk maps, used to validate upsert dicts and to build SQL.
        tables = [
            r["name"]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        self._columns: dict[str, list[str]] = {}
        self._pks: dict[str, list[str]] = {}
        for t in tables:
            info = list(self._conn.execute(f"PRAGMA table_info({t})"))
            self._columns[t] = [r["name"] for r in info]
            self._pks[t] = [r["name"] for r in sorted(info, key=lambda r: r["pk"]) if r["pk"]]

    def close(self) -> None:
        self._conn.close()

    # --- context manager ---
    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- internal helpers ---
    def _validate(self, table: str, keys: Any) -> None:
        unknown = set(keys) - set(self._columns[table])
        if unknown:
            raise KeyError(f"unknown column(s) for {table}: {sorted(unknown)}")

    def _upsert(self, table: str, row: dict[str, Any]) -> None:
        """Upsert touching only the keys present in row.

        UPDATE-first, INSERT on miss (callers serialize, so no race). A plain
        INSERT ... ON CONFLICT DO UPDATE cannot express partial upserts here:
        SQLite enforces NOT NULL on the attempted insert before the conflict
        clause fires, so a partial dict against an existing row would fail.
        """
        self._validate(table, row)
        pks = self._pks[table]
        missing = [c for c in pks if c not in row]
        if missing:
            raise KeyError(f"{table} upsert requires key column(s): {missing}")
        cols = list(row)
        col_sql = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        insert_sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"
        update_cols = [c for c in cols if c not in pks]
        with self._conn:
            if not update_cols:  # pk-only row: insert if absent, else nothing to update
                conflict = ", ".join(f'"{c}"' for c in pks)
                self._conn.execute(
                    f"{insert_sql} ON CONFLICT({conflict}) DO NOTHING",
                    [row[c] for c in cols],
                )
                return
            sets = ", ".join(f'"{c}" = ?' for c in update_cols)
            where = " AND ".join(f'"{c}" = ?' for c in pks)
            cur = self._conn.execute(
                f"UPDATE {table} SET {sets} WHERE {where}",
                [row[c] for c in update_cols] + [row[c] for c in pks],
            )
            if cur.rowcount == 0:
                self._conn.execute(insert_sql, [row[c] for c in cols])

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        """Plain INSERT for autoincrement tables; returns the new rowid."""
        self._validate(table, row)
        cols = list(row)
        col_sql = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        with self._conn:
            cur = self._conn.execute(
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return list(self._conn.execute(sql, params))

    # --- days ---
    def upsert_day(self, row: dict[str, Any]) -> None:
        self._upsert("days", row)

    def get_day(self, date: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM days WHERE date = ?", (date,))

    def get_series(self, metric: str, end_date: str, days: int) -> list[tuple[str, float]]:
        """metric = a days column name (KeyError otherwise). Ascending, NULLs skipped."""
        if metric not in self._columns["days"]:
            raise KeyError(f"unknown days column: {metric}")
        rows = self._all(
            f'SELECT date, "{metric}" AS value FROM days '
            f'WHERE date >= ? AND date <= ? AND "{metric}" IS NOT NULL ORDER BY date',
            (_window_start(end_date, days), end_date),
        )
        return [(r["date"], float(r["value"])) for r in rows]

    def recompute_daily_loads(self) -> None:
        """days.daily_load = SUM(activities.load) per date (0 when none);
        days.srpe_load = SUM(rpe * duration_min) over activities with an rpe (else NULL)."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE days SET
                    daily_load = COALESCE(
                        (SELECT SUM(a.load) FROM activities a WHERE a.date = days.date), 0),
                    srpe_load = (
                        SELECT SUM(a.rpe * a.duration_s / 60.0)
                        FROM activities a
                        WHERE a.date = days.date AND a.rpe IS NOT NULL)
                """
            )

    # --- activities ---
    def upsert_activity(self, row: dict[str, Any]) -> None:
        self._upsert("activities", row)

    def get_activity(self, activity_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM activities WHERE activity_id = ?", (activity_id,))

    def list_activities(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM activities WHERE date >= ? AND date <= ? "
            "ORDER BY date, start_local, activity_id",
            (start_date, end_date),
        )

    def activities_missing_load(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM activities WHERE load IS NULL ORDER BY date, activity_id"
        )

    # --- sleep timeline / digests ---
    def upsert_sleep_timeline(self, date: str, intervals_json: str) -> None:
        self._upsert("sleep_timeline", {"date": date, "intervals_json": intervals_json})

    def get_sleep_timeline(self, date: str, days_back: int = 7) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM sleep_timeline WHERE date >= ? AND date <= ? ORDER BY date",
            (_window_start(date, days_back), date),
        )

    def replace_activity_laps(self, activity_id: int, laps: list[dict[str, Any]]) -> None:
        """Replace this activity's laps wholesale — a re-fetch is authoritative
        and lap indices can shift if the athlete edits the activity."""
        for lap in laps:
            self._validate("activity_laps", lap)
        with self._conn:
            self._conn.execute(
                "DELETE FROM activity_laps WHERE activity_id = ?", (activity_id,)
            )
            for lap in laps:
                cols = list(lap)
                self._conn.execute(
                    f"INSERT INTO activity_laps ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    [lap[c] for c in cols],
                )

    def get_activity_laps(self, activity_id: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM activity_laps WHERE activity_id = ? ORDER BY lap_index",
            (activity_id,),
        )

    def laps_in_range(
        self, start_date: str, end_date: str, sport_like: str = "%"
    ) -> list[dict[str, Any]]:
        """Every stored lap in the window, carrying its activity's date and sport
        — the input to pace-band and EF analyses."""
        return self._all(
            "SELECT l.*, a.date AS date, a.sport AS sport FROM activity_laps l "
            "JOIN activities a ON a.activity_id = l.activity_id "
            "WHERE a.date >= ? AND a.date <= ? AND a.sport LIKE ? "
            "ORDER BY a.date, l.lap_index",
            (start_date, end_date, sport_like),
        )

    def activities_missing_laps(
        self, start_date: str, end_date: str, sport_like: str = "%"
    ) -> list[dict[str, Any]]:
        """Activities in the window that have no stored laps yet — the splits
        backfill work list, newest first (recent sessions matter most)."""
        return self._all(
            "SELECT a.* FROM activities a "
            "LEFT JOIN activity_laps l ON l.activity_id = a.activity_id "
            "WHERE a.date >= ? AND a.date <= ? AND a.sport LIKE ? AND l.activity_id IS NULL "
            "GROUP BY a.activity_id ORDER BY a.date DESC",
            (start_date, end_date, sport_like),
        )

    def upsert_activity_digest(self, row: dict[str, Any]) -> None:
        self._upsert("activity_digests", row)

    def get_activity_digest(self, activity_id: int) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM activity_digests WHERE activity_id = ?", (activity_id,)
        )

    # --- pmc ---
    def replace_pmc(self, rows: list[dict[str, Any]]) -> None:
        """Full rewrite of the pmc table (recomputed from scratch each sync — cheap)."""
        for row in rows:
            self._validate("pmc", row)
        with self._conn:
            self._conn.execute("DELETE FROM pmc")
            self._conn.executemany(
                "INSERT INTO pmc (date, load, ctl, atl, tsb) VALUES (?, ?, ?, ?, ?)",
                [(r["date"], r["load"], r["ctl"], r["atl"], r["tsb"]) for r in rows],
            )

    def get_pmc(self, end_date: str, days: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM pmc WHERE date >= ? AND date <= ? ORDER BY date",
            (_window_start(end_date, days), end_date),
        )

    # --- baselines cache ---
    def upsert_baselines(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self._upsert("baselines", row)

    def get_baseline(self, metric: str, date: str, window: int) -> dict[str, Any] | None:
        return self._one(
            'SELECT * FROM baselines WHERE metric = ? AND date = ? AND "window" = ?',
            (metric, date, window),
        )

    # --- alerts ---
    def active_alerts(self) -> list[dict[str, Any]]:
        """Unresolved alerts, RED first then AMBER then WATCH, newest first within severity."""
        return self._all(
            f"SELECT * FROM alerts WHERE resolved = 0 "
            f"ORDER BY {_SEVERITY_RANK}, date DESC, id DESC"
        )

    def upsert_alert(self, date: str, metric: str, severity: str, message: str) -> None:
        """One active alert per metric: update the unresolved row's severity/message
        if present (its date stays the first day the condition tripped), else insert."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE alerts SET severity = ?, message = ? WHERE metric = ? AND resolved = 0",
                (severity, message, metric),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO alerts (date, metric, severity, message) VALUES (?, ?, ?, ?)",
                    (date, metric, severity, message),
                )

    def resolve_alert(self, metric: str, resolved_date: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE alerts SET resolved = 1, resolved_date = ? "
                "WHERE metric = ? AND resolved = 0",
                (resolved_date, metric),
            )

    # --- wellness log ---
    def add_log(self, row: dict[str, Any]) -> int:
        return self._insert("wellness_log", row)

    def logs_for(self, date: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM wellness_log WHERE date = ? ORDER BY id", (date,))

    def unresolved_injuries(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM wellness_log WHERE flag = 'injury' AND resolved = 0 "
            "ORDER BY date, id"
        )

    def open_flags(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Unresolved flagged log rows (illness/injury), oldest first;
        kind=None returns both."""
        where = "flag IS NOT NULL" if kind is None else "flag = ?"
        return self._all(
            f"SELECT * FROM wellness_log WHERE {where} AND resolved = 0 ORDER BY date, id",
            () if kind is None else (kind,),
        )

    def resolve_log(self, row_id: int) -> None:
        self._upsert("wellness_log", {"id": row_id, "resolved": 1})

    # --- profile / plan / capabilities / sync state ---
    def set_profile(self, key: str, value: str) -> None:
        self._upsert("athlete_profile", {"key": key, "value": value})

    def get_profile(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._all("SELECT * FROM athlete_profile")}

    def set_hr_zones(self, config: dict[str, Any]) -> None:
        """Persist the digested HR-zone config. Kept in sync_state (it is
        sync-derived, not athlete-set) rather than athlete_profile, which is
        reserved for values the user types via garmin_set_profile."""
        self.set_sync_state("hr_zones", json.dumps(config))

    def get_hr_zones(self) -> dict[str, Any] | None:
        raw = self.get_sync_state("hr_zones")
        return json.loads(raw) if raw else None

    def set_personal_records(self, records: dict[str, Any]) -> None:
        """Persist Garmin's digested personal records ({distance: {seconds,
        date, activity_id}}). Sync-derived (Garmin's own PR list), so kept in
        sync_state, not athlete_profile — same boundary as HR zones (D8)."""
        self.set_sync_state("personal_records", json.dumps(records))

    def get_personal_records(self) -> dict[str, Any] | None:
        raw = self.get_sync_state("personal_records")
        return json.loads(raw) if raw else None

    def set_race_predictions(self, predictions: dict[str, Any]) -> None:
        """Persist Garmin's own race-time predictions ({distance: seconds}) —
        sync-derived, kept in sync_state like PRs and HR zones."""
        self.set_sync_state("race_predictions", json.dumps(predictions))

    def get_race_predictions(self) -> dict[str, Any] | None:
        raw = self.get_sync_state("race_predictions")
        return json.loads(raw) if raw else None

    def upsert_plan_entry(self, row: dict[str, Any]) -> int:
        if row.get("id") is not None:
            self._upsert("plan_calendar", row)
            return int(row["id"])
        return self._insert("plan_calendar", row)

    def plan_entries(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM plan_calendar WHERE date >= ? AND date <= ? ORDER BY date, id",
            (start_date, end_date),
        )

    def set_plan_match(self, plan_id: int, activity_id: int | None, method: str | None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE plan_calendar SET matched_activity_id = ?, match_method = ? WHERE id = ?",
                (activity_id, method, plan_id),
            )

    def set_capability(self, key: str, available: bool, detail: str = "") -> None:
        self._upsert(
            "capability_map",
            {
                "key": key,
                "available": int(available),
                "detail": detail,
                "probed_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

    def get_capabilities(self) -> dict[str, dict[str, Any]]:
        return {
            r["key"]: {
                "available": bool(r["available"]),
                "detail": r["detail"],
                "probed_at": r["probed_at"],
            }
            for r in self._all("SELECT * FROM capability_map")
        }

    def set_sync_state(self, key: str, value: str) -> None:
        self._upsert("sync_state", {"key": key, "value": value})

    def get_sync_state(self, key: str) -> str | None:
        row = self._one("SELECT value FROM sync_state WHERE key = ?", (key,))
        return row["value"] if row else None

    # --- lifecycle ---
    def export_csv(self, out_dir: Path) -> list[Path]:
        """One CSV per table into out_dir (created if needed); returns written paths."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for table in sorted(self._columns):
            cols = self._columns[table]
            path = out_dir / f"{table}.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(cols)
                for r in self._conn.execute(f"SELECT * FROM {table}"):
                    writer.writerow([r[c] for c in cols])
            paths.append(path)
        return paths
