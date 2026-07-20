"""Canonical load currency (DESIGN.md §3.1).

Primary per-activity load = Garmin activityTrainingLoad. Activities missing it
fall through a provenance-flagged ladder (activities.load_source): calibrated
Edwards TRIMP → calibrated sRPE → per-sport median load-per-minute estimate →
0/'none'. Days never silently vanish from the ledger.

Pure functions: inputs are activity dicts (schema.sql shapes), outputs are
(load, load_source) — persistence stays in the sync engine.
"""
from __future__ import annotations

from statistics import median
from typing import Any

EDWARDS_WEIGHTS = (1, 2, 3, 4, 5)  # zones 1..5, minutes × weight

_ZONE_FIELDS = ("hr_z1_s", "hr_z2_s", "hr_z3_s", "hr_z4_s", "hr_z5_s")

# Minimum same-sport (garmin load, TRIMP) pairs for through-origin regression.
MIN_REGRESSION_PAIRS = 10


def edwards_trimp(activity: dict[str, Any]) -> float | None:
    """Edwards TRIMP: Σ minutes_in_zone_i × i over hr_z1_s..hr_z5_s.

    Returns None if all five zone fields are NULL (no HR data); individual
    NULL zones among present ones count as 0.
    """
    zones = [activity.get(f) for f in _ZONE_FIELDS]
    if all(z is None for z in zones):
        return None
    return sum((z or 0.0) / 60.0 * w for z, w in zip(zones, EDWARDS_WEIGHTS, strict=True))


def _pair_trimp(activity: dict[str, Any]) -> float | None:
    """TRIMP for calibration pairing: stored raw value, else computed from zones."""
    t = activity.get("trimp")
    if t is None:
        t = edwards_trimp(activity)
    return t


def fit_calibration(activities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-sport TRIMP→Garmin-load calibration from overlap activities.

    Pairs are activities with BOTH a Garmin load (load present and, if given,
    load_source == 'garmin') and a positive TRIMP. Returns
    {sport: {"method": "regression"|"median_ratio", "factor": float, "n": int}}.
    regression = least squares through origin, factor = Σ(load·trimp)/Σ(trimp²),
    used when n ≥ MIN_REGRESSION_PAIRS; below that the median of per-pair
    load/trimp ratios. Sports with zero pairs get no entry (uncalibrated).
    """
    pairs_by_sport: dict[str, list[tuple[float, float]]] = {}
    for a in activities:
        load = a.get("load")
        if load is None or a.get("load_source", "garmin") != "garmin":
            continue
        trimp = _pair_trimp(a)
        if trimp is None or trimp <= 0:
            continue
        pairs_by_sport.setdefault(a["sport"], []).append((float(load), float(trimp)))

    calibration: dict[str, dict[str, Any]] = {}
    for sport, pairs in pairs_by_sport.items():
        n = len(pairs)
        if n >= MIN_REGRESSION_PAIRS:
            factor = sum(ld * t for ld, t in pairs) / sum(t * t for _, t in pairs)
            method = "regression"
        else:
            factor = median(ld / t for ld, t in pairs)
            method = "median_ratio"
        calibration[sport] = {"method": method, "factor": factor, "n": n}
    return calibration


def resolve_load(
    activity: dict[str, Any],
    calibration: dict[str, dict[str, Any]],
    sport_median_load_per_min: dict[str, float],
) -> tuple[float, str]:
    """Apply the §3.1 ladder to one activity → (load, load_source).

    1. Garmin load present → passed through unchanged, 'garmin'.
    2. HR zones → Edwards TRIMP × per-sport factor ('trimp_calibrated'), or raw
       TRIMP when the sport has no calibration entry ('trimp_uncalibrated').
    3. RPE (precedence already resolved at sync into activity['rpe']) →
       sRPE = rpe × minutes, through the same per-sport factor. The factor maps
       TRIMP-units→Garmin-units; sRPE reuses it as an approximation, per design
       ('srpe_calibrated' / 'srpe_uncalibrated').
    4. Per-sport median load-per-minute × minutes → 'estimated'.
    5. No history in the sport → (0.0, 'none').

    Steps 3–4 require duration_s; without it they are skipped.
    """
    garmin_load = activity.get("load")
    if garmin_load is not None:
        return float(garmin_load), "garmin"

    sport = activity.get("sport")
    entry = calibration.get(sport)
    factor = entry["factor"] if entry else None

    trimp = edwards_trimp(activity)
    if trimp is not None:
        if factor is not None:
            return trimp * factor, "trimp_calibrated"
        return trimp, "trimp_uncalibrated"

    duration_s = activity.get("duration_s")
    rpe = activity.get("rpe")
    if rpe is not None and duration_s is not None:
        srpe = float(rpe) * float(duration_s) / 60.0
        if factor is not None:
            return srpe * factor, "srpe_calibrated"
        return srpe, "srpe_uncalibrated"

    median_lpm = sport_median_load_per_min.get(sport)
    if median_lpm is not None and duration_s is not None:
        return float(median_lpm) * float(duration_s) / 60.0, "estimated"

    return 0.0, "none"


def convert_watch_rpe(direct_rpe: int | None, direct_feel: int | None) -> tuple[int | None, int | None]:
    """Garmin on-watch 0-100 self-report scales → (CR-10 rpe, 1-5 feel).

    rpe: round(x/10) clamped to 1-10 when x > 0; 0 or None (unreported) → None.
    feel: round(x/25)+1 clamped to 1-5; None → None.
    """
    rpe: int | None = None
    if direct_rpe is not None and direct_rpe > 0:
        rpe = min(10, max(1, round(direct_rpe / 10)))
    feel: int | None = None
    if direct_feel is not None:
        feel = min(5, max(1, round(direct_feel / 25) + 1))
    return rpe, feel
