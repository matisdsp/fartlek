"""Anomaly scan → alerts (DESIGN.md §3.2 #21 + §4.4 resolution rule). CONTRACT STUB.

scan(...) is pure: given per-metric series and baselines, emit the desired
alert state; the sync engine diffs it against the alerts table via Store.

Rules:
- For every tracked scalar: |robust z| > 2 vs 90d baseline OR an out-of-band
  streak ≥3 days (band = |z|>1) → alert.
- Severity: WATCH by default; AMBER when streak ≥3d at |z|>2 or two metrics
  trip the same day; RED only via the convergence/acute rules (Phase 2) —
  Phase 0 emits WATCH/AMBER from the scanner alone, never RED.
- Message format: '<metric> <direction> — <value> vs <median> (<window>d), <n>d streak'.
- Resolution: metric back within band (|z|≤1) for ≥2 consecutive days.

tracked_metrics(): the days columns scanned in Phase 0:
resting_hr, hrv_last_night, sleep_score, sleep_duration_h, body_battery_wake,
avg_stress, daily_load (spike side only: z>2).
"""
from __future__ import annotations

from typing import Any


def tracked_metrics() -> list[str]:
    raise NotImplementedError


def scan(
    series_by_metric: dict[str, list[tuple[str, float]]],
    end_date: str,
) -> list[dict[str, Any]]:
    """Returns desired active alerts: [{metric, severity, message, since_date}]."""
    raise NotImplementedError


def resolution_dates(
    series_by_metric: dict[str, list[tuple[str, float]]],
    active_alert_metrics: list[str],
    end_date: str,
) -> dict[str, str]:
    """{metric: resolved_date} for active alerts whose metric has been back
    in band ≥2 consecutive days."""
    raise NotImplementedError
