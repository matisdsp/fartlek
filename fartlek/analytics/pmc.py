"""PMC, form bands, ACWR, monotony/strain (DESIGN.md §3.2 #1-4). CONTRACT STUB.

All pure functions over an ascending daily load series covering EVERY calendar
day in range (missing/rest days already filled with 0 by the caller).
"""
from __future__ import annotations

from typing import Any

K_CTL = 42  # days
K_ATL = 7


def compute_pmc(daily_loads: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """CTL += (L−CTL)·(1−e^(−1/42)); ATL += (L−ATL)·(1−e^(−1/7));
    TSB_today = CTL_yesterday − ATL_yesterday. Seeds: CTL=ATL=0 at series start
    (callers warm with 180d backfill so day-0 values are trustworthy).
    Returns [{date, load, ctl, atl, tsb}] ascending."""
    raise NotImplementedError


def form_assessment(ctl: float, tsb: float, ctl_series: list[tuple[str, float]]) -> dict[str, Any]:
    """Form% = TSB/CTL×100 (None when CTL < 1). Bands: +5..+25 fresh/race-ready ·
    −10..−30 productive · < −40 overload. Ramp = ΔCTL over last 7d as % of CTL/wk,
    sustainable 4-8%, flag >10%. Returns {form_pct, form_band, ramp_pct_per_wk, ramp_flag}."""
    raise NotImplementedError


def acwr_ewma(daily_loads: list[tuple[str, float]]) -> dict[str, Any]:
    """EWMA(7):EWMA(28) ratio (alpha = 2/(N+1), seeded at first value).
    Guards: {"unreliable": True, "reason": ...} when history <28d or the chronic
    EWMA < 30% of the 90d median chronic value (layoff instability).
    Returns {acwr, acute, chronic, unreliable, reason}."""
    raise NotImplementedError


def monotony_strain(daily_loads: list[tuple[str, float]]) -> dict[str, Any]:
    """Foster over the trailing 7 days (rest days count as 0):
    monotony = mean/SD (population SD; SD<1e-9 → monotony None + flag),
    strain = weekly_total × monotony. strain_percentile vs the athlete's own
    trailing 12 weekly strains (needs the full series passed in).
    Returns {monotony, strain, weekly_load, strain_percentile, flag}."""
    raise NotImplementedError
