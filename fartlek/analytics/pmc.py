"""PMC, form bands, ACWR, monotony/strain (DESIGN.md §3.2 #1-4).

All pure functions over an ascending daily load series covering EVERY calendar
day in range (missing/rest days already filled with 0 by the caller). Each
series-taking function asserts contiguity and raises ValueError on gaps.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from typing import Any

K_CTL = 42  # days
K_ATL = 7

_ALPHA_CTL = 1.0 - math.exp(-1.0 / K_CTL)
_ALPHA_ATL = 1.0 - math.exp(-1.0 / K_ATL)
_ACWR_ALPHA_ACUTE = 2.0 / (7 + 1)  # 0.25
_ACWR_ALPHA_CHRONIC = 2.0 / (28 + 1)  # ~0.0690
_EPS = 1e-9


def _assert_contiguous(daily_loads: list[tuple[str, float]]) -> None:
    """Raise ValueError unless dates are strictly ascending with no gaps."""
    prev: date | None = None
    for d_str, _ in daily_loads:
        d = date.fromisoformat(d_str)
        if prev is not None and d != prev + timedelta(days=1):
            raise ValueError(
                f"daily load series must cover every calendar day: "
                f"{prev.isoformat()} -> {d_str}"
            )
        prev = d


def compute_pmc(daily_loads: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """CTL += (L−CTL)·(1−e^(−1/42)); ATL += (L−ATL)·(1−e^(−1/7));
    TSB_today = CTL_yesterday − ATL_yesterday (first day TSB = 0). Seeds
    CTL=ATL=0 at series start (callers warm with the 180d backfill).
    Returns [{date, load, ctl, atl, tsb}] ascending."""
    _assert_contiguous(daily_loads)
    out: list[dict[str, Any]] = []
    ctl = atl = 0.0
    for d, load in daily_loads:
        tsb = ctl - atl  # yesterday's values; 0.0 on the first day
        ctl += (load - ctl) * _ALPHA_CTL
        atl += (load - atl) * _ALPHA_ATL
        out.append({"date": d, "load": float(load), "ctl": ctl, "atl": atl, "tsb": tsb})
    return out


def _form_band(form_pct: float) -> str:
    if form_pct > 25:
        return "transition/detraining risk"
    if form_pct >= 5:
        return "fresh/race-ready"
    if form_pct > -10:
        return "neutral"
    if form_pct >= -30:
        return "productive"
    if form_pct >= -40:
        return "deep"
    return "overload"


def form_assessment(ctl: float, tsb: float, ctl_series: list[tuple[str, float]]) -> dict[str, Any]:
    """Form% = TSB/CTL×100 (None with band None when CTL < 1). Bands:
    >+25 transition/detraining risk · +5..+25 fresh/race-ready · −10..+5
    neutral · −30..−10 productive · −40..−30 deep · <−40 overload.
    Ramp = (CTL_today − CTL_7d_ago)/CTL_today×100 (%CTL/wk) from the trailing
    8 points of ctl_series; None when <8 points or CTL_today < 1; sustainable
    4-8%, ramp_flag True when >10%.
    Returns {form_pct, form_band, ramp_pct_per_wk, ramp_flag}."""
    form_pct: float | None = None
    band: str | None = None
    if ctl >= 1:
        form_pct = tsb / ctl * 100.0
        band = _form_band(form_pct)
    ramp: float | None = None
    if len(ctl_series) >= 8:
        ctl_today = ctl_series[-1][1]
        if ctl_today >= 1:
            ramp = (ctl_today - ctl_series[-8][1]) / ctl_today * 100.0
    ramp_flag = None if ramp is None else ramp > 10.0
    return {
        "form_pct": form_pct,
        "form_band": band,
        "ramp_pct_per_wk": ramp,
        "ramp_flag": ramp_flag,
    }


def acwr_ewma(daily_loads: list[tuple[str, float]]) -> dict[str, Any]:
    """EWMA(7):EWMA(28) ratio; alpha = 2/(N+1) (0.25 and 2/29), both EWMAs
    seeded at the first load value. acwr is suppressed to None with
    unreliable=True when: history <28 points; chronic ~0; or chronic <30% of
    the median of the trailing 90 daily chronic values (layoff instability).
    Returns {acwr, acute, chronic, unreliable, reason}."""
    _assert_contiguous(daily_loads)
    n = len(daily_loads)
    if n == 0:
        return {
            "acwr": None,
            "acute": None,
            "chronic": None,
            "unreliable": True,
            "reason": "no load history",
        }
    acute = chronic = float(daily_loads[0][1])
    chronic_hist = [chronic]
    for _, load in daily_loads[1:]:
        acute += (load - acute) * _ACWR_ALPHA_ACUTE
        chronic += (load - chronic) * _ACWR_ALPHA_CHRONIC
        chronic_hist.append(chronic)
    result: dict[str, Any] = {
        "acwr": None,
        "acute": acute,
        "chronic": chronic,
        "unreliable": True,
        "reason": None,
    }
    if n < 28:
        result["reason"] = f"history <28d (have {n})"
        return result
    if chronic < _EPS:
        result["reason"] = "chronic load ~0"
        return result
    median_chronic = statistics.median(chronic_hist[-90:])
    if chronic < 0.30 * median_chronic:
        result["reason"] = "chronic <30% of 90d median (layoff instability)"
        return result
    result["acwr"] = acute / chronic
    result["unreliable"] = False
    return result


def _week_strain(week: list[float]) -> tuple[float | None, float | None]:
    """(monotony, strain) for one week of daily loads; (None, None) when SD~0."""
    sd = statistics.pstdev(week)
    if sd < _EPS:
        return None, None
    monotony = statistics.fmean(week) / sd
    return monotony, sum(week) * monotony


def monotony_strain(daily_loads: list[tuple[str, float]]) -> dict[str, Any]:
    """Foster over the trailing 7 days (rest days count as 0):
    monotony = mean/SD (population SD; SD<1e-9 → monotony/strain None; that
    degenerate case flags ONLY when the week had training — an all-rest week
    is not monotonous, it's rest), strain = weekly_total × monotony; flag
    also when monotony > 2.0.
    strain_percentile (0-100) = share of the athlete's trailing ≤12
    non-overlapping weekly strains ≤ current (SD~0 weeks excluded); None
    unless ≥4 such weeks exist and the current week is computable.
    Returns {monotony, strain, weekly_load, strain_percentile, flag}."""
    _assert_contiguous(daily_loads)
    n = len(daily_loads)
    window = [float(v) for _, v in daily_loads[-7:]]
    if not window:
        return {
            "monotony": None,
            "strain": None,
            "weekly_load": 0.0,
            "strain_percentile": None,
            "flag": False,
        }
    weekly_load = sum(window)
    monotony, strain = _week_strain(window)
    # SD≈0 with zero load = a full rest week — nothing to flag. SD≈0 with
    # identical non-zero loads = the pathological monotony case.
    flag = (monotony is None and weekly_load > 0) or (monotony is not None and monotony > 2.0)

    weekly_strains: list[float] = []
    full_weeks = min(n // 7, 12)
    for w in range(full_weeks):  # w=0 is the current (trailing) week
        end = n - 7 * w
        _, s = _week_strain([float(v) for _, v in daily_loads[end - 7:end]])
        if s is not None:
            weekly_strains.append(s)
    strain_percentile: float | None = None
    if strain is not None and len(weekly_strains) >= 4:
        strain_percentile = (
            100.0 * sum(1 for s in weekly_strains if s <= strain) / len(weekly_strains)
        )
    return {
        "monotony": monotony,
        "strain": strain,
        "weekly_load": weekly_load,
        "strain_percentile": strain_percentile,
        "flag": flag,
    }
