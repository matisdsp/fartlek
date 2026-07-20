"""Per-account SQLite store (DESIGN.md §3.3). CONTRACT STUB — implement me.

One Store per Garmin account. WAL mode, busy_timeout=5000. All methods are
synchronous (callers wrap in asyncio.to_thread when needed). Row shapes are
fixed by schema.sql — that file is the contract shared with analytics/sync.

Conventions:
- upsert_* methods take plain dicts whose keys match schema columns; unknown
  keys raise KeyError (catch schema drift early), missing keys stay NULL.
- get_series(metric, end_date, days) returns [(date, value)] ascending,
  including only rows where the value is NOT NULL — gap handling is the
  caller's job (analytics treats missing load days as 0, missing HRV as gaps).
- No business logic here: pure persistence + a few typed readers.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, db_path: Path):
        """Open (creating parents 0700 if needed), apply schema.sql, set WAL + busy_timeout."""
        raise NotImplementedError

    def close(self) -> None: ...

    # --- context manager ---
    def __enter__(self) -> "Store": ...
    def __exit__(self, *exc: object) -> None: ...

    # --- days ---
    def upsert_day(self, row: dict[str, Any]) -> None:
        """Upsert by row['date']; only overwrites columns present in row."""
        raise NotImplementedError

    def get_day(self, date: str) -> dict[str, Any] | None: ...

    def get_series(self, metric: str, end_date: str, days: int) -> list[tuple[str, float]]:
        """metric = a days column name. Ascending [(date, value)], NULLs skipped."""
        raise NotImplementedError

    def recompute_daily_loads(self) -> None:
        """days.daily_load = SUM(activities.load) per date (0 when none);
        days.srpe_load = SUM(rpe * duration_min) over activities with an rpe."""
        raise NotImplementedError

    # --- activities ---
    def upsert_activity(self, row: dict[str, Any]) -> None: ...
    def get_activity(self, activity_id: int) -> dict[str, Any] | None: ...
    def list_activities(self, start_date: str, end_date: str) -> list[dict[str, Any]]: ...
    def activities_missing_load(self) -> list[dict[str, Any]]: ...

    # --- sleep timeline / digests ---
    def upsert_sleep_timeline(self, date: str, intervals_json: str) -> None: ...
    def get_sleep_timeline(self, date: str, days_back: int = 7) -> list[dict[str, Any]]: ...
    def upsert_activity_digest(self, row: dict[str, Any]) -> None: ...
    def get_activity_digest(self, activity_id: int) -> dict[str, Any] | None: ...

    # --- pmc ---
    def replace_pmc(self, rows: list[dict[str, Any]]) -> None:
        """Full rewrite of the pmc table (recomputed from scratch each sync — cheap)."""
        raise NotImplementedError

    def get_pmc(self, end_date: str, days: int) -> list[dict[str, Any]]: ...

    # --- baselines cache ---
    def upsert_baselines(self, rows: list[dict[str, Any]]) -> None: ...
    def get_baseline(self, metric: str, date: str, window: int) -> dict[str, Any] | None: ...

    # --- alerts ---
    def active_alerts(self) -> list[dict[str, Any]]:
        """Unresolved alerts, RED first then AMBER then WATCH, newest first within severity."""
        raise NotImplementedError

    def upsert_alert(self, date: str, metric: str, severity: str, message: str) -> None:
        """One active alert per metric: update message/severity of the unresolved row
        for this metric if present, else insert."""
        raise NotImplementedError

    def resolve_alert(self, metric: str, resolved_date: str) -> None: ...

    # --- wellness log ---
    def add_log(self, row: dict[str, Any]) -> int: ...
    def logs_for(self, date: str) -> list[dict[str, Any]]: ...
    def unresolved_injuries(self) -> list[dict[str, Any]]: ...

    # --- profile / plan / capabilities / sync state ---
    def set_profile(self, key: str, value: str) -> None: ...
    def get_profile(self) -> dict[str, str]: ...
    def upsert_plan_entry(self, row: dict[str, Any]) -> int: ...
    def plan_entries(self, start_date: str, end_date: str) -> list[dict[str, Any]]: ...
    def set_plan_match(self, plan_id: int, activity_id: int | None, method: str | None) -> None: ...
    def set_capability(self, key: str, available: bool, detail: str = "") -> None: ...
    def get_capabilities(self) -> dict[str, dict[str, Any]]: ...
    def set_sync_state(self, key: str, value: str) -> None: ...
    def get_sync_state(self, key: str) -> str | None: ...

    # --- lifecycle ---
    def export_csv(self, out_dir: Path) -> list[Path]:
        """One CSV per table into out_dir; returns written paths."""
        raise NotImplementedError
