"""Canonical load currency (DESIGN.md §3.1). CONTRACT STUB — implement me.

Primary per-activity load = Garmin activityTrainingLoad. Fallback ladder for
activities missing it, every step provenance-flagged (activities.load_source):
  1. Edwards TRIMP from hr_z1_s..hr_z5_s, rescaled to the Garmin-load scale by
     per-athlete linear regression over overlap activities (same sport, both
     values present): load ≈ a·TRIMP (through origin). min n=10 same-sport
     pairs; below that per-sport median ratio; zero overlap → raw TRIMP with
     load_source='trimp_uncalibrated'.
  2. No HR but an RPE (athlete log overrides watch-native): sRPE = RPE × min,
     through the same calibration path (srpe_calibrated/_uncalibrated).
  3. Terminal — no HR, no RPE: athlete's per-sport median load-per-minute ×
     duration, load_source='estimated'; no history in that sport → load=0,
     load_source='none'. Days never silently vanish from the ledger.

Pure functions: inputs are activity dicts (schema.sql shapes), outputs are
(load, load_source) — persistence stays in the sync engine.
"""
from __future__ import annotations

from typing import Any

EDWARDS_WEIGHTS = (1, 2, 3, 4, 5)  # zones 1..5, minutes × weight


def edwards_trimp(activity: dict[str, Any]) -> float | None:
    """Σ minutes_in_zone_i × i over hr_z1_s..hr_z5_s; None if all zone fields are NULL."""
    raise NotImplementedError


def fit_calibration(activities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-sport calibration from activities having BOTH garmin load and a TRIMP.

    Returns {sport: {"method": "regression"|"median_ratio", "factor": float, "n": int}}.
    regression = least-squares through origin (factor = Σ(load·trimp)/Σ(trimp²)),
    used when n ≥ 10; else median of load/trimp ratios; sports absent → no entry.
    """
    raise NotImplementedError


def resolve_load(
    activity: dict[str, Any],
    calibration: dict[str, dict[str, Any]],
    sport_median_load_per_min: dict[str, float],
) -> tuple[float, str]:
    """Apply the §3.1 ladder to one activity → (load, load_source)."""
    raise NotImplementedError


def convert_watch_rpe(direct_rpe: int | None, direct_feel: int | None) -> tuple[int | None, int | None]:
    """Garmin on-watch 0-100 scales → (CR-10 rpe = round(x/10), feel 1-5 = round(x/25)+1 clamped)."""
    raise NotImplementedError
