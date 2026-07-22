"""Attribution rules — the closed set of causal claims (DESIGN.md §3.2 #22).

An LLM handed a pile of correlated numbers will invent explanations: "your
sleep is down BECAUSE of your training load" is the kind of sentence that
sounds like coaching and is, on the evidence available, made up. This module
exists so the server can say "because" in exactly five situations and nowhere
else.

Anything outside this set must be phrased as co-occurrence ("X while Y"), and
`CO_OCCURRENCE_TEMPLATE` is the only sanctioned wording for it. The CI
attribution-language test (§4.5) checks rendered "because"-statements against
`RULE_IDS`, so an unlisted causal claim fails the build rather than reaching an
athlete.

The five rules:

  a  deep-sleep decline + high bedtime variance + normal load
     → "matches late bedtimes, not load"
  b  deep-sleep decline + elevated ramp/strain + stable bedtimes
     → "matches load, not schedule"
  c  lagged correlation of daily load → next-day HRV over 90d, |r| > 0.3
  d  sleep debt ↔ next-day HRV, same guard
  e  hot-day EF suppression (§3.2 #12)

Rules (c) and (d) are correlations and say so in their own wording: they are
reported as association, never as mechanism, and both need 60 days of history
before they may speak at all.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import fmean
from typing import Any

RULE_IDS = ("late_bedtimes", "load_driven", "load_hrv_lag", "debt_hrv_lag", "heat_ef")

CO_OCCURRENCE_TEMPLATE = "{a} while {b}"

MIN_CORRELATION_DAYS = 60
MIN_ABS_R = 0.3
BEDTIME_VARIANCE_HIGH_H = 1.0     # SD of mid-sleep, hours
RAMP_ELEVATED_PCT = 8.0
STRAIN_ELEVATED_PCTILE = 80.0


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = fmean(xs), fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx < 1e-12 or syy < 1e-12:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def deep_sleep_attribution(
    *,
    deep_sleep_declining: bool,
    bedtime_sd_h: float | None,
    ramp_pct: float | None,
    strain_pctile: float | None,
) -> dict[str, Any] | None:
    """Rules (a) and (b): why deep sleep is down — schedule or load.

    Returns None when deep sleep is not declining, when the inputs needed to
    discriminate are missing, or when BOTH schedule and load look abnormal —
    in that last case the honest answer is "we cannot tell", and inventing one
    of the two would be exactly the fabrication this module prevents.
    """
    if not deep_sleep_declining or bedtime_sd_h is None:
        return None
    if ramp_pct is None and strain_pctile is None:
        return None

    schedule_off = bedtime_sd_h >= BEDTIME_VARIANCE_HIGH_H
    load_off = (
        (ramp_pct is not None and ramp_pct > RAMP_ELEVATED_PCT)
        or (strain_pctile is not None and strain_pctile > STRAIN_ELEVATED_PCTILE)
    )

    if schedule_off and not load_off:
        return {"rule": "late_bedtimes",
                "statement": "matches late bedtimes, not load",
                "evidence": f"bedtime SD {bedtime_sd_h:.2f}h, load normal"}
    if load_off and not schedule_off:
        detail = []
        if ramp_pct is not None:
            detail.append(f"ramp {ramp_pct:+.1f}%/wk")
        if strain_pctile is not None:
            detail.append(f"strain {strain_pctile:.0f}th pctile")
        return {"rule": "load_driven",
                "statement": "matches load, not schedule",
                "evidence": f"{', '.join(detail)}, bedtime SD {bedtime_sd_h:.2f}h"}
    return None  # both or neither — no attribution is honest


def lagged_association(
    driver: list[tuple[str, float]],
    response: list[tuple[str, float]],
    *,
    rule: str,
    label: str,
) -> dict[str, Any] | None:
    """Rules (c) and (d): does `driver` on day D track `response` on day D+1?

    Reported as correlation, never causation, and suppressed below
    MIN_CORRELATION_DAYS pairs or |r| <= MIN_ABS_R. Pairs are built on real
    calendar adjacency, so a gap in either series simply drops that pair
    rather than silently pairing days a week apart.
    """
    by_date = {d: v for d, v in response}
    xs, ys = [], []
    for d_str, x in driver:
        nxt = (date.fromisoformat(d_str) + timedelta(days=1)).isoformat()
        if nxt in by_date:
            xs.append(float(x))
            ys.append(float(by_date[nxt]))

    if len(xs) < MIN_CORRELATION_DAYS:
        return None
    r = _correlation(xs, ys)
    if r is None or abs(r) < MIN_ABS_R:
        return None

    direction = "lower" if r < 0 else "higher"
    return {
        "rule": rule,
        "statement": (f"higher {label} tends to be followed by {direction} HRV "
                      f"next day (r={r:.2f}, correlation not causation)"),
        "evidence": f"n={len(xs)} day pairs",
        "r": r,
        "n": len(xs),
    }


def heat_ef_attribution(
    *, ef_declining: bool, hot_share: float | None, cool_ef: float | None,
    hot_ef: float | None,
) -> dict[str, Any] | None:
    """Rule (e): an EF decline explained by heat rather than lost fitness.

    Only fires when the athlete's OWN hot-vs-cool gap is measurable — the
    penalty is personally derived, not a population constant. On the sampled
    account this rule is what stopped a genuine 6-month improvement being
    reported as a summer regression.
    """
    if not ef_declining or hot_share is None or cool_ef is None or hot_ef is None:
        return None
    if hot_share < 0.5 or cool_ef <= 0:
        return None
    penalty = (cool_ef - hot_ef) / cool_ef
    if penalty <= 0:
        return None
    return {
        "rule": "heat_ef",
        "statement": "EF suppressed by heat, not lost fitness",
        "evidence": (f"{hot_share:.0%} of laps at or above the heat threshold; "
                     f"your own hot-vs-cool gap is {penalty:.1%}"),
        "penalty": penalty,
    }


def co_occurrence(a: str, b: str) -> str:
    """The only sanctioned phrasing for an unattributable pairing."""
    return CO_OCCURRENCE_TEMPLATE.format(a=a, b=b)
