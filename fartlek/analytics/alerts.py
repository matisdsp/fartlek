"""Anomaly scan → alerts (DESIGN.md §3.2 #21 + §4.4 resolution rule).

scan(...) is pure: given per-metric [(date, value)] series it emits the
desired alert state; the sync engine diffs it against the alerts table.

Per tracked metric (robust z vs the 90d baseline ending end_date;
mad_sd = 1.4826×MAD, floored at the metric's measurement resolution so a
degenerate MAD=0 window — e.g. weeks of identical integer RHR — cannot
turn a ±1-unit wiggle into a |z|>2 alert):
- trip when |z today| > 2 OR the trailing out-of-band streak (|z| > 1 over
  consecutive CALENDAR days — a missing day breaks the streak, matching
  baselines.streak) is ≥3 days. daily_load trips on the HIGH side only
  (z > 2 spike / z > 1 streak); a load collapse never alerts.
- at most one alert per metric — the most severe applicable.
- severity: WATCH by default; AMBER when the last ≥3 consecutive calendar
  days are all beyond |z| > 2, or when two metrics trip with the same
  since_date (deviations that started the same day). Phase 0 never emits RED.
- message: '<metric> <direction> — <value> vs <median> (90d), <n>d streak'
  with values %g-formatted; since_date = first day of the current
  out-of-band streak.

resolution_dates: an active metric resolves when the last 2 dates present
are back in band (|z| ≤ 1; daily_load one-sided z ≤ 1) AND are consecutive
calendar days ending at most one day before end_date — two isolated in-band
points weeks apart never clear an alert. The resolved date is the second
in-band day.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

_BASELINE_WINDOW = 90
_MAD_SCALE = 1.4826
_MAD_FLOOR = 1e-9

# Measurement-resolution floors for mad_sd (units of the metric). DESIGN §3.2 #7
# names floors only for trend significance; the scanner needs them for the same
# reason — below these, a deviation is instrument noise, not signal.
_RESOLUTION_FLOOR = {
    "resting_hr": 1.0,        # bpm
    "hrv_last_night": 2.0,    # ms
    "sleep_score": 2.0,
    "sleep_duration_h": 0.25,
    "body_battery_wake": 2.0,
    "avg_stress": 2.0,
    "daily_load": 5.0,
}

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


def _baseline90(
    points: list[tuple[str, float]], end_date: str, metric: str = ""
) -> tuple[float, float, int] | None:
    """(median, mad_sd, n) over the 90 calendar days ending end_date, or None if empty."""
    start = (date.fromisoformat(end_date) - timedelta(days=_BASELINE_WINDOW - 1)).isoformat()
    values = [v for d, v in points if start <= d <= end_date]
    if not values:
        return None
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    floor = _RESOLUTION_FLOOR.get(metric, _MAD_FLOOR)
    return med, max(_MAD_SCALE * mad, floor, _MAD_FLOOR), len(values)


def _out_of_band(metric: str, z: float) -> bool:
    if metric in _HIGH_SIDE_ONLY:
        return z > 1
    return abs(z) > 1


def _severe(metric: str, z: float) -> bool:
    if metric in _HIGH_SIDE_ONLY:
        return z > 2
    return abs(z) > 2


def _trailing_streak(metric: str, dated_zs: list[tuple[str, float]], severe: bool) -> int:
    """Consecutive most-recent CALENDAR days beyond the band (severe → |z|>2).

    A missing calendar day breaks the streak, mirroring baselines.streak —
    three isolated deviations weeks apart are not a '3d streak'.
    """
    check = _severe if severe else _out_of_band
    n = 0
    next_d: date | None = None
    for d_str, z in reversed(dated_zs):
        d = date.fromisoformat(d_str)
        if next_d is not None and d != next_d - timedelta(days=1):
            break
        if not check(metric, z):
            break
        n += 1
        next_d = d
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
        base = _baseline90(points, end_date, metric)
        if base is None:
            continue
        median, mad_sd, _n = base
        dated_zs = [(d, (v - median) / mad_sd) for d, v in points]
        z_today = dated_zs[-1][1]
        streak = _trailing_streak(metric, dated_zs, severe=False)
        if not (_severe(metric, z_today) or streak >= 3):
            continue
        hard_streak = _trailing_streak(metric, dated_zs, severe=True)
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
    end_d = date.fromisoformat(end_date)
    for metric in active_alert_metrics:
        points = _points(series_by_metric.get(metric) or [], end_date)
        if len(points) < 2:
            continue
        base = _baseline90(points, end_date, metric)
        if base is None:
            continue
        median, mad_sd, _n = base
        (d1, v1), (d2, v2) = points[-2:]
        # The two in-band days must be consecutive and current, not two
        # isolated points weeks apart.
        if date.fromisoformat(d2) - date.fromisoformat(d1) != timedelta(days=1):
            continue
        if end_d - date.fromisoformat(d2) > timedelta(days=1):
            continue
        if all(not _out_of_band(metric, (v - median) / mad_sd) for v in (v1, v2)):
            resolved[metric] = d2
    return resolved
