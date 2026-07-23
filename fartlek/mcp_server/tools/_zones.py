"""Shared zone resolution for the intensity-distribution tools.

garmin_load, garmin_week and garmin_whats_changed all map HR-zone time to the
3-zone model, and all three must resolve the athlete's zone config the same
way — otherwise the same week could read pyramidal in one tool and threshold
in another. This is the one place that turns the persisted config plus the
athlete's own resting HR and LT1 override into the mapping kwargs, and reports
whether real thresholds were used so each tool can disclose the fallback
consistently.
"""
from __future__ import annotations

from typing import Any

from fartlek.analytics import tid

BUCKET_NOTE = (
    "zone splits approximate: the athlete's LT1/LT2 boundaries were not "
    "available, so whole Garmin HR-zone buckets are used (Z1+Z2 easy / Z3 "
    "moderate / Z4+Z5 hard) — the drift direction is reliable, the split itself "
    "approximate"
)
PRORATED_NOTE = (
    "zone splits pro-rated across the athlete's own Garmin thresholds "
    "(LT2 = device lactate-threshold HR; LT1 = override or population estimate)"
)


def resolve(store: Any, end_date: str) -> tuple[dict[str, Any], str]:
    """(mapping_kwargs, disclosure_note) for tid.distribution.

    kwargs is empty (whole-bucket fallback) when zone config is absent or
    cannot anchor both thresholds; the note always matches what was actually
    used, so a tool can pass note straight to a section's method_note.
    """
    config = store.get_hr_zones()
    resting = _resting_hr(store, end_date)
    override = store.get_profile().get("lt1_hr_override")
    lt1_override = float(override) if override else None

    kwargs = tid.zone_mapping_kwargs(config, resting_hr=resting, lt1_override=lt1_override)
    return kwargs, (PRORATED_NOTE if kwargs else BUCKET_NOTE)


def _resting_hr(store: Any, end_date: str) -> float | None:
    """The athlete's trailing-month median resting HR, for the LT1 estimate."""
    from fartlek.analytics import baselines
    try:
        series = store.get_series("resting_hr", end_date, 60)
    except KeyError:
        return None
    base = baselines.baseline(series, end_date, 60)
    return base["median"] if base else None
