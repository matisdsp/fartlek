"""Baseline engine + RHR deviation (DESIGN.md §3.2 #6, #9).

Pure functions over [(date, value)] series (NULLs already skipped by Store;
gaps are fine — windows are calendar-day windows, n reports actual points).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import fmean, median
from typing import Any

WINDOWS = (7, 28, 60, 90)
MAD_SCALE = 1.4826
_MAD_SD_FLOOR = 1e-9

# RHR deviation thresholds (bpm) — DESIGN.md §3.2 #9.
_RHR_CAUTION = 3.0
_RHR_SEVERE = 5.0
_RHR_MIN_N = 14
_RHR_SUSTAINED_DAYS = 2


def baseline(series: list[tuple[str, float]], end_date: str, window: int) -> dict[str, Any] | None:
    """Baseline over the calendar window [end_date−window+1, end_date].

    Returns {mean, median, mad_sd, n, window} or None if no points fall in the
    window. mad_sd = 1.4826 × median(|x − median|), floored at 1e-9 so it is
    always a safe z-score divisor.
    """
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=window - 1)
    values = [v for d, v in series if start_d <= date.fromisoformat(d) <= end_d]
    if not values:
        return None
    med = median(values)
    mad = median(abs(v - med) for v in values)
    return {
        "mean": fmean(values),
        "median": med,
        "mad_sd": max(MAD_SCALE * mad, _MAD_SD_FLOOR),
        "n": len(values),
        "window": window,
    }


def zscore(value: float, base: dict[str, Any]) -> float:
    """(value − median) / mad_sd — robust z."""
    return (value - base["median"]) / base["mad_sd"]


def band_position(value: float, base: dict[str, Any]) -> str:
    """'in_band' (|z|≤1) | 'high'/'low' (1<|z|≤2) | 'very_high'/'very_low' (|z|>2)."""
    z = zscore(value, base)
    if abs(z) <= 1:
        return "in_band"
    if abs(z) <= 2:
        return "high" if z > 0 else "low"
    return "very_high" if z > 0 else "very_low"


# --- canonical HRV band (§3.2 #8) -------------------------------------------
# ONE definition, shared by garmin_brief, garmin_recovery, garmin_week and
# readiness fusion so the four cannot drift apart (defect E1): the 60d lnRMSSD
# mean ± 0.5·MAD-SD, classified against the 7-day rolling mean of lnRMSSD.
HRV_BAND_WINDOW = 60
HRV_BAND_K = 0.5          # band half-width in robust SDs
HRV_ROLL_DAYS = 7


def hrv_band(hrv_series: list[tuple[str, float]], end_date: str) -> dict[str, Any] | None:
    """Canonical HRV band (§3.2 #8): the 60d lnRMSSD mean ± 0.5·MAD-SD.

    Input is the raw avgOvernightHrv series in ms; the band is computed in LOG
    space because HRV is log-normally distributed and the spec's decision basis
    is lnRMSSD. Returns {lo, hi, mean, mad_sd, n} in LOG space — exp() the
    bounds for an ms display — or None when the window is empty.
    """
    base = baseline([(d, math.log(v)) for d, v in hrv_series if v and v > 0],
                    end_date, HRV_BAND_WINDOW)
    if base is None:
        return None
    half = HRV_BAND_K * base["mad_sd"]
    return {"lo": base["mean"] - half, "hi": base["mean"] + half,
            "mean": base["mean"], "mad_sd": base["mad_sd"], "n": base["n"]}


def hrv_roll(hrv_series: list[tuple[str, float]], end_date: str,
             days: int = HRV_ROLL_DAYS) -> float | None:
    """The decision basis (§3.2 #8): the mean of lnRMSSD over the last `days`."""
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=days - 1)
    ln = [math.log(v) for d, v in hrv_series
          if v and v > 0 and start_d <= date.fromisoformat(d) <= end_d]
    return fmean(ln) if ln else None


def hrv_position(roll_ln: float, band: dict[str, Any]) -> str:
    """'below' | 'in' | 'above' for a lnRMSSD roll vs the canonical band."""
    if roll_ln < band["lo"]:
        return "below"
    if roll_ln > band["hi"]:
        return "above"
    return "in"


def streak(series: list[tuple[str, float]], predicate: Any) -> int:
    """Consecutive most-recent days (walking back from the series end) where
    predicate(value) is True.

    Consecutive means consecutive calendar DATES: a missing calendar day
    between two points breaks the streak even if both points satisfy the
    predicate. Empty series → 0.
    """
    count = 0
    next_d: date | None = None
    for d_str, v in reversed(series):
        d = date.fromisoformat(d_str)
        if next_d is not None and d != next_d - timedelta(days=1):
            break  # calendar gap
        if not predicate(v):
            break
        count += 1
        next_d = d
    return count


def rhr_deviation(series: list[tuple[str, float]], end_date: str) -> dict[str, Any]:
    """Two-sided RHR deviation vs the trailing 30-day median (§3.2 #9).

    median30 = median over [end_date−30, end_date−1] (today EXCLUDED);
    n = points in that window. delta = today − median30.

    Levels: 'ok' |delta|<3 · 'caution' 3≤|delta| (not qualifying below) ·
    'red' delta≥+5 sustained ≥2d · 'parasympathetic_watch' delta≤−5 sustained
    ≥2d (never alarmed alone — convergence input only) ·
    'insufficient_data' when n<14 or today's value is absent.

    sustained_days = streak (calendar-gap-breaking) of days whose deviation
    from median30, in today's direction, is ≥ the threshold of today's band
    (5 when |delta|≥5, else 3); 0 when |delta|<3.

    Returns {delta, level, sustained_days, median30, n}.
    """
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=30)
    trailing = [v for d, v in series if start_d <= date.fromisoformat(d) < end_d]
    n = len(trailing)
    median30 = median(trailing) if trailing else None

    today_vals = [v for d, v in series if d == end_date]
    today = today_vals[0] if today_vals else None
    delta = (today - median30) if (today is not None and median30 is not None) else None

    if n < _RHR_MIN_N or delta is None:
        return {"delta": delta, "level": "insufficient_data", "sustained_days": 0,
                "median30": median30, "n": n}

    if abs(delta) < _RHR_CAUTION:
        return {"delta": delta, "level": "ok", "sustained_days": 0,
                "median30": median30, "n": n}

    threshold = _RHR_SEVERE if abs(delta) >= _RHR_SEVERE else _RHR_CAUTION
    sign = 1.0 if delta > 0 else -1.0
    past = [(d, v) for d, v in series if date.fromisoformat(d) <= end_d]
    sustained = streak(past, lambda v: (v - median30) * sign >= threshold)

    if delta >= _RHR_SEVERE and sustained >= _RHR_SUSTAINED_DAYS:
        level = "red"
    elif delta <= -_RHR_SEVERE and sustained >= _RHR_SUSTAINED_DAYS:
        level = "parasympathetic_watch"
    else:
        level = "caution"
    return {"delta": delta, "level": level, "sustained_days": sustained,
            "median30": median30, "n": n}
