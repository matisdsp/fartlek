"""Anomaly scan → alerts (DESIGN.md §3.2 #21 + §4.4 resolution rule).

scan(...) is pure: given per-metric [(date, value)] series it emits the
desired alert state; the sync engine diffs it against the alerts table.

Per tracked metric (robust z vs the 90d baseline ending end_date;
mad_sd = 1.4826×MAD, floor 1e-9):
- trip when |z today| > 2 OR the trailing out-of-band streak (|z| > 1 over
  dates present) is ≥3 days. daily_load trips on the HIGH side only
  (z > 2 spike / z > 1 streak); a load collapse never alerts.
- at most one alert per metric — the most severe applicable.
- severity: WATCH by default; AMBER when the last ≥3 consecutive dates are
  all beyond |z| > 2, or when two metrics trip with the same since_date
  (deviations that started the same day). Phase 0 never emits RED.
- message: '<metric> <direction> — <value> vs <median> (90d), <n>d streak'
  with values %g-formatted; since_date = first day of the current
  out-of-band streak.

resolution_dates: an active metric resolves when the last 2 consecutive
dates present (≤ end_date) are back in band — |z| ≤ 1, except daily_load
where the band is one-sided (z ≤ 1: a rest day resolves a load spike).
The resolved date is the second in-band day.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

_BASELINE_WINDOW = 90
_MAD_SCALE = 1.4826
_MAD_FLOOR = 1e-9

_TRACKED = [
    "resting_hr",
    "hrv_last_night",
    "sleep_score",
    "sleep_duration_h",
    "body_battery_wake",
    "avg_stress",
    "daily_load",
]

_HIGH_SIDE_ONLY = {"daily_load"}


def tracked_metrics() -> list[str]:
    """The days columns scanned in Phase 0."""
    return list(_TRACKED)


def _points(series: list[tuple[str, float]], end_date: str) -> list[tuple[str, float]]:
    return sorted((d, float(v)) for d, v in series if d <= end_date)


def _baseline90(points: list[tuple[str, float]], end_date: str) -> tuple[float, float, int] | None:
    """(median, mad_sd, n) over the 90 calendar days ending end_date, or None if empty."""
    start = (date.fromisoformat(end_date) - timedelta(days=_BASELINE_WINDOW - 1)).isoformat()
    values = [v for d, v in points if start <= d <= end_date]
    if not values:
        return None
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return med, max(_MAD_SCALE * mad, _MAD_FLOOR), len(values)


def _out_of_band(metric: str, z: float) -> bool:
    if metric in _HIGH_SIDE_ONLY:
        return z > 1
    return abs(z) > 1


def _severe(metric: str, z: float) -> bool:
    if metric in _HIGH_SIDE_ONLY:
        return z > 2
    return abs(z) > 2


def _trailing_streak(metric: str, zs: list[float], severe: bool) -> int:
    """Consecutive most-recent points beyond the band (severe → beyond |z|>2)."""
    check = _severe if severe else _out_of_band
    n = 0
    for z in reversed(zs):
        if check(metric, z):
            n += 1
        else:
            break
    return n


def scan(
    series_by_metric: dict[str, list[tuple[str, float]]],
    end_date: str,
) -> list[dict[str, Any]]:
    """Returns desired active alerts: [{metric, severity, message, since_date}]."""
    alerts: list[dict[str, Any]] = []
    for metric in _TRACKED:
        points = _points(series_by_metric.get(metric) or [], end_date)
        if not points:
            continue
        base = _baseline90(points, end_date)
        if base is None:
            continue
        median, mad_sd, _n = base
        zs = [(v - median) / mad_sd for _d, v in points]
        z_today = zs[-1]
        streak = _trailing_streak(metric, zs, severe=False)
        if not (_severe(metric, z_today) or streak >= 3):
            continue
        hard_streak = _trailing_streak(metric, zs, severe=True)
        severity = "AMBER" if hard_streak >= 3 else "WATCH"
        direction = "high" if z_today > 0 else "low"
        value = points[-1][1]
        since_date = points[len(points) - streak][0]
        message = (
            f"{metric} {direction} — {value:g} vs {median:g} "
            f"({_BASELINE_WINDOW}d), {streak}d streak"
        )
        alerts.append(
            {
                "metric": metric,
                "severity": severity,
                "message": message,
                "since_date": since_date,
            }
        )

    # Two metrics tripping the same day escalate each other to AMBER.
    since_counts: dict[str, int] = {}
    for alert in alerts:
        since_counts[alert["since_date"]] = since_counts.get(alert["since_date"], 0) + 1
    for alert in alerts:
        if since_counts[alert["since_date"]] >= 2 and alert["severity"] == "WATCH":
            alert["severity"] = "AMBER"
    return alerts


def resolution_dates(
    series_by_metric: dict[str, list[tuple[str, float]]],
    active_alert_metrics: list[str],
    end_date: str,
) -> dict[str, str]:
    """{metric: resolved_date} for active alerts whose metric has been back
    in band for the last 2 consecutive dates present."""
    resolved: dict[str, str] = {}
    for metric in active_alert_metrics:
        points = _points(series_by_metric.get(metric) or [], end_date)
        if len(points) < 2:
            continue
        base = _baseline90(points, end_date)
        if base is None:
            continue
        median, mad_sd, _n = base
        last_two = points[-2:]
        if all(not _out_of_band(metric, (v - median) / mad_sd) for _d, v in last_two):
            resolved[metric] = last_two[-1][0]
    return resolved
