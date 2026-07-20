"""Baseline engine + RHR deviation (DESIGN.md §3.2 #6, #9). CONTRACT STUB.

Pure functions over [(date, value)] series (NULLs already skipped by Store;
gaps are fine — windows are calendar-day windows, n reports actual points).
"""
from __future__ import annotations

from typing import Any

WINDOWS = (7, 28, 60, 90)
MAD_SCALE = 1.4826


def baseline(series: list[tuple[str, float]], end_date: str, window: int) -> dict[str, Any] | None:
    """Points within [end_date−window+1, end_date]. Returns
    {mean, median, mad_sd (=1.4826×MAD, floor 1e-9), n, window} or None if n=0."""
    raise NotImplementedError


def zscore(value: float, base: dict[str, Any]) -> float:
    """(value − median) / mad_sd — robust z."""
    raise NotImplementedError


def band_position(value: float, base: dict[str, Any]) -> str:
    """'in_band' (|z|≤1) | 'high'/'low' (1<|z|≤2) | 'very_high'/'very_low' (|z|>2)."""
    raise NotImplementedError


def streak(series: list[tuple[str, float]], predicate: Any) -> int:
    """Consecutive most-recent days (from series end) where predicate(value) is True."""
    raise NotImplementedError


def rhr_deviation(series: list[tuple[str, float]], end_date: str) -> dict[str, Any]:
    """Two-sided vs 30d median (§3.2 #9): deviation in EITHER direction flags.
    delta = today − median30. Levels: 'ok' |delta|<3 · 'caution' 3≤|delta|≤5 ·
    'red' delta≥+5 sustained ≥2d · 'parasympathetic_watch' delta≤−5 sustained ≥2d
    (never alarmed alone — convergence input only).
    Returns {delta, level, sustained_days, median30, n}."""
    raise NotImplementedError
