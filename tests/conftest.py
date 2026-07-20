"""Shared test fixtures. Keep tests hermetic: no network, temp stores only."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from fartlek.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "store.db") as s:
        yield s


def make_days(end: str, n: int, **values_by_metric) -> list[dict]:
    """n day-rows ending at `end`; values_by_metric maps column -> list[n] or scalar."""
    end_d = date.fromisoformat(end)
    rows = []
    for i in range(n):
        d = end_d - timedelta(days=n - 1 - i)
        row = {"date": d.isoformat(), "synced_at": "2026-01-01T00:00:00"}
        for k, v in values_by_metric.items():
            row[k] = v[i] if isinstance(v, (list, tuple)) else v
        rows.append(row)
    return rows


def make_series(end: str, values: list[float]) -> list[tuple[str, float]]:
    end_d = date.fromisoformat(end)
    n = len(values)
    return [
        ((end_d - timedelta(days=n - 1 - i)).isoformat(), float(v))
        for i, v in enumerate(values)
    ]
