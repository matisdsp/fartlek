"""Overtraining convergence audit (DESIGN.md §3.2 #20).

The safety rule the whole design turns on: **no single marker ever raises an
alarm.** Physiological series are noisy enough that any one of them crosses a
threshold regularly in a healthy athlete, and a server that shouts on each
crossing is ignored within a fortnight — the failure mode this project exists
to avoid. So an alarm requires *convergence*: at least MIN_TRIGGERING_GROUPS of
three independent marker groups deviant for at least PERSISTENCE_DAYS.

    autonomic  — HRV 7-day roll below band, RHR deviating EITHER way,
                 HRV coefficient of variation rising
    sleep      — debt over the window, deep-sleep low streak, SRI below par
    load       — monotony, strain percentile, ramp rate, form ratio

A fourth group, **hr_response** (suppressed max HR on hard sessions, worsening
heart-rate recovery), is corroborating only: it can strengthen an alarm and
raise a watch item, but never counts toward the triggering total. It moves for
too many benign reasons — an easy week, a flat course — to be load-bearing.

Two-sided RHR is deliberate (§3.2 #9): elevation is the classic flag, but a
sustained *drop* below baseline alongside other deviant markers is the
parasympathetic pattern, which the naive "high is bad" test misses entirely.

The acute override (§3.2 #19) is the one path that bypasses persistence: some
signals mean "today", not "for three days".

This module decides; it does not phrase. `verdict` is a state, and callers
render it.
"""
from __future__ import annotations

from typing import Any

MIN_TRIGGERING_GROUPS = 2
PERSISTENCE_DAYS = 3

# Group thresholds (§3.2 #20)
SLEEP_DEBT_H_14D = 5.0
SRI_FLOOR = 75.0
DEEP_SLEEP_STREAK_DAYS = 3
MONOTONY_FLAG = 2.0
STRAIN_PCTILE_FLAG = 90.0
RAMP_PCT_FLAG = 10.0
FORM_PCT_FLOOR = -40.0
HRV_CV_RISE_FLAG = 0.20        # 20% above the athlete's own trailing CV

TRIGGERING_GROUPS = ("autonomic", "sleep", "load")
CORROBORATING_GROUPS = ("hr_response",)


def _marker(name: str, deviant: bool, detail: str, days: int = 0) -> dict[str, Any]:
    return {"marker": name, "deviant": bool(deviant), "detail": detail, "days": days}


def autonomic_group(
    *,
    hrv_below_band_days: int = 0,
    rhr: dict[str, Any] | None = None,
    hrv_cv_ratio: float | None = None,
) -> dict[str, Any]:
    """HRV band position, two-sided RHR deviation, rising HRV variability.

    `rhr` is the dict from baselines.rhr_deviation. Its 'red' and
    'parasympathetic_watch' levels both count as deviant — direction is
    information, not a filter.
    """
    markers = [
        _marker("hrv_7d_roll", hrv_below_band_days >= PERSISTENCE_DAYS,
                f"{hrv_below_band_days}d below band", hrv_below_band_days),
    ]
    if rhr:
        level, days = rhr.get("level"), int(rhr.get("sustained_days") or 0)
        delta = rhr.get("delta")
        deviant = level in ("red", "parasympathetic_watch")
        direction = "elevated" if (delta or 0) > 0 else "suppressed"
        markers.append(_marker(
            "resting_hr", deviant,
            f"{direction} {abs(delta):.1f} bpm for {days}d" if delta is not None else str(level),
            days,
        ))
    if hrv_cv_ratio is not None:
        markers.append(_marker(
            "hrv_cv", hrv_cv_ratio >= 1.0 + HRV_CV_RISE_FLAG,
            f"CV {hrv_cv_ratio:.2f}x its trailing level",
        ))
    return _group("autonomic", markers)


def sleep_group(
    *,
    debt: dict[str, Any] | None = None,
    sri: dict[str, Any] | None = None,
    deep_sleep_low_streak: int = 0,
) -> dict[str, Any]:
    markers = []
    if debt and debt.get("debt_h") is not None:
        markers.append(_marker(
            "sleep_debt", debt["debt_h"] > SLEEP_DEBT_H_14D,
            f"{debt['debt_h']:.1f}h over {debt['nights']} nights",
        ))
    if sri and not sri.get("suppressed") and sri.get("sri") is not None:
        markers.append(_marker(
            "sleep_regularity", sri["sri"] < SRI_FLOOR, f"SRI {sri['sri']:.0f}",
        ))
    markers.append(_marker(
        "deep_sleep", deep_sleep_low_streak >= DEEP_SLEEP_STREAK_DAYS,
        f"{deep_sleep_low_streak}d low streak", deep_sleep_low_streak,
    ))
    return _group("sleep", markers)


def load_group(
    *,
    monotony: float | None = None,
    strain_pctile: float | None = None,
    ramp_pct: float | None = None,
    form_pct: float | None = None,
) -> dict[str, Any]:
    markers = []
    if monotony is not None:
        markers.append(_marker("monotony", monotony > MONOTONY_FLAG, f"{monotony:.2f}"))
    if strain_pctile is not None:
        markers.append(_marker(
            "strain", strain_pctile > STRAIN_PCTILE_FLAG, f"{strain_pctile:.0f}th pctile",
        ))
    if ramp_pct is not None:
        markers.append(_marker("ramp", ramp_pct > RAMP_PCT_FLAG, f"{ramp_pct:+.1f}%/wk of CTL"))
    if form_pct is not None:
        markers.append(_marker("form", form_pct < FORM_PCT_FLOOR, f"{form_pct:+.0f}% of CTL"))
    return _group("load", markers)


def hr_response_group(
    *, max_hr_suppressed: bool = False, hrr_worsening: bool = False,
    detail: str = "",
) -> dict[str, Any]:
    """Corroborating only — never counts toward the triggering total."""
    return _group("hr_response", [
        _marker("hard_session_max_hr", max_hr_suppressed, detail or "vs 90d ceiling"),
        _marker("heart_rate_recovery", hrr_worsening, detail or "vs baseline"),
    ])


def _group(name: str, markers: list[dict[str, Any]]) -> dict[str, Any]:
    hot = [m for m in markers if m["deviant"]]
    return {
        "group": name,
        "deviant": bool(hot),
        "markers": markers,
        "deviant_markers": [m["marker"] for m in hot],
        "corroborating_only": name in CORROBORATING_GROUPS,
    }


def audit(
    groups: list[dict[str, Any]],
    *,
    acute: dict[str, Any] | None = None,
    subjective: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fuse marker groups into a verdict.

    `acute` is the §3.2 #19 single-marker escalation ({'level': 'AMBER'|'RED',
    'reason': str}); `subjective` is the athlete-reported gate ({'level': ...,
    'reason': ...}) from garmin_log. Both bypass the persistence requirement,
    and the athlete always outranks the sensors — an illness note caps the
    verdict at RED however calm the physiology looks.

    Returns {verdict RED|AMBER|WATCH|GREEN, triggering_groups, watch_items,
    corroborating, reasons, groups}.
    """
    triggering = [
        g for g in groups
        if g["deviant"] and g["group"] in TRIGGERING_GROUPS
    ]
    corroborating = [
        g for g in groups if g["deviant"] and g["group"] in CORROBORATING_GROUPS
    ]

    reasons: list[str] = []
    verdict = "GREEN"

    if len(triggering) >= MIN_TRIGGERING_GROUPS:
        verdict = "RED"
        reasons.append(
            f"{len(triggering)} of {len(TRIGGERING_GROUPS)} marker groups deviant "
            f"({', '.join(g['group'] for g in triggering)})"
        )
    elif triggering:
        verdict = "WATCH"
        reasons.append(
            f"{triggering[0]['group']} markers off "
            f"({', '.join(triggering[0]['deviant_markers'])}) — single group, not an alarm"
        )

    if corroborating:
        names = ", ".join(g["group"] for g in corroborating)
        reasons.append(f"corroborating: {names} (never triggers alone)")

    # Acute and subjective bypass persistence and can only raise, never lower.
    for gate in (acute, subjective):
        if gate and gate.get("level"):
            verdict = _max_severity(verdict, gate["level"])
            if gate.get("reason"):
                reasons.append(gate["reason"])

    watch_items = [
        f"{g['group']}: {', '.join(g['deviant_markers'])}"
        for g in groups if g["deviant"] and g not in triggering
    ]
    if verdict == "GREEN" and not reasons:
        reasons.append("no marker group deviant")

    return {
        "verdict": verdict,
        "triggering_groups": [g["group"] for g in triggering],
        "corroborating": [g["group"] for g in corroborating],
        "watch_items": watch_items,
        "reasons": reasons,
        "groups": groups,
    }


_SEVERITY_ORDER = {"GREEN": 0, "WATCH": 1, "AMBER": 2, "RED": 3}


def _max_severity(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER.get(a, 0) >= _SEVERITY_ORDER.get(b, 0) else b
