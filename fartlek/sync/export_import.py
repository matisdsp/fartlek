"""Fill the local store from a Garmin account export.

Runs the same tiers as a live sync, in full, because reading a file has no rate limit. The path of
the export is remembered, so the MCP server can open the store without a Garmin login.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fartlek.health.adapters.garmin_export import GarminExport, UnlimitedRate
from fartlek.paths import account_dir, export_source_file, store_path
from fartlek.store import Store
from fartlek.sync.engine import SyncEngine

DEFAULT_DAYS = 365
MAX_PASSES = 200


@dataclass(frozen=True)
class RememberedExport:
    """What `export_source.json` holds about the last import."""

    path: Path
    display_name: str | None = None  # absent in files written by older versions
    last_date: str | None = None

    @property
    def available(self) -> bool:
        return self.path.exists()

    @property
    def describes_a_store(self) -> bool:
        """Enough is recorded to open the imported store without the export itself."""
        return bool(self.display_name and self.last_date)


def remembered_source() -> RememberedExport | None:
    """The last import, whether or not its file is still there."""
    source = export_source_file()
    if not source.is_file():
        return None
    try:
        data = json.loads(source.read_text())
        path = Path(data["path"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return RememberedExport(path, data.get("display_name"), data.get("last_date"))


def remembered_export() -> Path | None:
    """The export used last, if there is one and it still exists."""
    source = remembered_source()
    return source.path if source is not None and source.available else None


def remember_export(path: Path, display_name: str | None = None, last_date: str | None = None) -> None:
    source = export_source_file()
    source.parent.mkdir(parents=True, exist_ok=True)
    record = {"path": str(path.resolve()), "display_name": display_name, "last_date": last_date}
    source.write_text(json.dumps(record))


def build_engine(export: GarminExport, store: Store) -> SyncEngine:
    return SyncEngine(
        store,
        export.fetch,
        export.display_name,
        account_dir(export.display_name),
        limiter=UnlimitedRate(),
        today=export.last_date,
    )


def run_import(
    engine: SyncEngine,
    days: int = DEFAULT_DAYS,
    report: Callable[[str, dict[str, Any]], None] = lambda _name, _result: None,
) -> dict[str, Any]:
    """Every tier and backfill, until nothing is left. Returns merged counts."""

    def drain(step: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for _ in range(MAX_PASSES):
            result = step()
            if not result.get("remaining"):
                break
        return result

    steps: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("snapshot", engine.tier0),
        ("history", lambda: engine.tier1(history_days=days)),
        ("nights", lambda: engine.tier2(backfill_days=days)),
        ("hrv", lambda: drain(lambda: engine.backfill_hrv(days=days))),
        ("daily summaries", lambda: drain(lambda: engine.backfill_daily_summary(days=days))),
        ("laps", lambda: drain(lambda: engine.backfill_splits(days=days))),
        ("gear", lambda: drain(lambda: engine.backfill_gear(days=days))),
    ]
    merged: dict[str, Any] = {"calls": 0, "errors": []}
    for name, step in steps:
        result = step()
        report(name, result)
        if result.get("skipped"):
            merged["errors"].append(f"{name}: {result.get('reason')}")
            continue
        merged["calls"] += int(result.get("calls") or 0)
        merged["errors"].extend(result.get("errors") or [])
    return merged


def import_export(
    path: str | Path,
    days: int = DEFAULT_DAYS,
    report: Callable[[str, dict[str, Any]], None] = lambda _name, _result: None,
) -> tuple[GarminExport, dict[str, Any]]:
    """Read an export into the store of its account and remember where it is."""
    export = GarminExport(path)
    with Store(store_path(export.display_name)) as store:
        merged = run_import(build_engine(export, store), days, report)
    remember_export(Path(path).expanduser(), export.display_name, export.last_date)
    return export, merged
