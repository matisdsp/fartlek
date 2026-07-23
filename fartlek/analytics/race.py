"""Race models (DESIGN.md §3.2 #16 + the fixed-time amendment).

Two structurally different questions, and conflating them is the bug this
module exists to prevent:

- **distance races** — "how long will D take?" Riegel's power law, Tanda's
  regression, Garmin's own prediction; triangulated, never averaged.
- **fixed-time events** (6h / 12h / 24h) — "how far in T?" Riegel extrapolated
  to a 24h target from a 10K PR is meaningless, and Tanda is fitted on
  marathons. Emitting a number from either would violate "never fabricate", so
  fixed-time gets its own model.

**The exponent is a population default, never personally fitted from training
runs.** Fitting it on the athlete's own long runs is the tempting mistake: on
the sampled account, the implied exponent between a 3.5h and a 7.6h run came
out at 0.99 — better than linear, physiologically impossible — because those
runs were sub-maximal (HR 131) and paced conservatively. A fitted exponent
there measures pacing discipline, not the athlete's limit. Riegel fitting is
therefore restricted to genuine maximal performances (PRs), and fixed-time
projection always reports a RANGE across a literature exponent band, labelled
as population-derived.

Every projection states its reference effort, its assumptions and its band, so
a coach can disagree with the inputs rather than the number.
"""
from __future__ import annotations

import math
from typing import Any

# Riegel exponent bounds for distance races (§3.2 #16).
RIEGEL_DEFAULT_B = 1.06
RIEGEL_BOUNDS = (1.03, 1.12)

# Fixed-time events degrade harder than distance races: sleep deprivation,
# cumulative muscle damage and feeding limits all bite past ~6h. Population
# band from the ultra literature — deliberately wide, and never presented as
# personally derived.
FIXED_TIME_EXPONENT_BAND = (1.06, 1.15)
MIN_REFERENCE_HOURS = 2.0        # below this, extrapolating to 24h is fantasy
MAX_EXTRAPOLATION_RATIO = 6.0    # beyond 6x the reference duration, say so

# Tanda's marathon regression (Tanda 2011; DESIGN §3.2 #16):
#   Pm = 17.1 + 140·e^(−0.0053·K) + 0.55·P
# K = mean weekly distance (km), P = mean training pace (s/km) over the ~8 weeks
# before the race, Pm = predicted marathon race pace (s/km). It is
# MARATHON-SPECIFIC and must never be applied to another distance. Coefficients
# are the paper's fixed constants — a contract, not a fit.
TANDA_MARATHON_M = 42195.0
_TANDA_INTERCEPT = 17.1
_TANDA_VOL_COEFF = 140.0
_TANDA_VOL_RATE = -0.0053
_TANDA_PACE_COEFF = 0.55
# Domain of Tanda's own dataset; outside it the estimate is unsupported.
TANDA_KM_RANGE = (30.0, 160.0)


def riegel_time(t1_s: float, d1_m: float, d2_m: float, b: float = RIEGEL_DEFAULT_B) -> float:
    """T2 = T1 * (D2/D1)^b — predicted time for distance d2."""
    if d1_m <= 0 or t1_s <= 0 or d2_m <= 0:
        raise ValueError("distances and times must be positive")
    return t1_s * (d2_m / d1_m) ** b


def riegel_distance(d1_m: float, t1_s: float, t2_s: float,
                    b: float = RIEGEL_DEFAULT_B) -> float:
    """D2 = D1 * (T2/T1)^(1/b) — the inverse: distance covered in time t2.

    This is the fixed-time form. Note 1/b < 1, so distance grows sub-linearly
    with time: tripling the duration does NOT triple the distance.
    """
    if d1_m <= 0 or t1_s <= 0 or t2_s <= 0:
        raise ValueError("distances and times must be positive")
    return d1_m * (t2_s / t1_s) ** (1.0 / b)


def fit_riegel_exponent(performances: list[tuple[float, float]]) -> dict[str, Any]:
    """Fit b by least squares on log(T) = log(k) + b*log(D) over MAXIMAL
    performances only (PRs), returning {b, n, r2, clamped, quality}.

    Callers must not pass training runs: a sub-maximal effort set fits pacing
    discipline, not physiology, and can produce b < 1 (better than linear),
    which the clamp then silently hides. `quality` is 'good' (r2 >= 0.95),
    'weak', or 'default' when fewer than 2 performances exist.
    """
    pts = [(d, t) for d, t in performances if d > 0 and t > 0]
    if len(pts) < 2:
        return {"b": RIEGEL_DEFAULT_B, "n": len(pts), "r2": None, "raw_b": None,
                "clamped": False, "quality": "default"}

    xs = [math.log(d) for d, _ in pts]
    ys = [math.log(t) for _, t in pts]
    n = len(pts)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-12:
        return {"b": RIEGEL_DEFAULT_B, "n": n, "r2": None, "raw_b": None,
                "clamped": False, "quality": "default"}
    raw_b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx

    ss_tot = sum((y - my) ** 2 for y in ys)
    intercept = my - raw_b * mx
    ss_res = sum((y - (intercept + raw_b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else None

    b = min(max(raw_b, RIEGEL_BOUNDS[0]), RIEGEL_BOUNDS[1])
    return {"b": b, "n": n, "r2": r2, "clamped": b != raw_b, "raw_b": raw_b,
            "quality": "good" if (r2 is not None and r2 >= 0.95) else "weak"}


def tanda_marathon(weekly_km: float, mean_pace_s_per_km: float) -> dict[str, Any]:
    """Tanda's regression: a predicted marathon time from training volume and
    pace, independent of any PR (so it triangulates against Riegel).

    Pm = 17.1 + 140·e^(−0.0053·K) + 0.55·P — see the constants above. Returns
    the predicted `seconds` and `pace_s_per_km`, the inputs used, an
    `in_domain` flag (Tanda fitted 30–160 km/wk; outside it the number is
    extrapolation), and the two sensitivity levers a coach reasons with: the
    change in predicted marathon time per +1 km/wk of volume (negative — more
    volume, faster) and per +1 s/km of training pace (positive). Marathon-only:
    callers must not feed it another target distance.
    """
    if weekly_km <= 0 or mean_pace_s_per_km <= 0:
        raise ValueError("weekly_km and mean_pace must be positive")
    km_factor = TANDA_MARATHON_M / 1000.0  # 42.195, s/km → whole-marathon seconds
    decay = math.exp(_TANDA_VOL_RATE * weekly_km)
    pace = _TANDA_INTERCEPT + _TANDA_VOL_COEFF * decay + _TANDA_PACE_COEFF * mean_pace_s_per_km
    return {
        "seconds": pace * km_factor,
        "pace_s_per_km": pace,
        "weekly_km": weekly_km,
        "mean_pace_s_per_km": mean_pace_s_per_km,
        "in_domain": TANDA_KM_RANGE[0] <= weekly_km <= TANDA_KM_RANGE[1],
        # ∂Pm/∂K · km_factor and ∂Pm/∂P · km_factor
        "seconds_per_km_per_week": _TANDA_VOL_COEFF * _TANDA_VOL_RATE * decay * km_factor,
        "seconds_per_training_pace_s": _TANDA_PACE_COEFF * km_factor,
    }


def stoppage_ratio(laps: list[dict[str, Any]]) -> float | None:
    """Fraction of elapsed time not moving, from lap duration vs moving time.

    On a fixed-time event this is not a detail: every minute standing at the
    aid table is a minute not covering ground, and it is the one variable an
    athlete controls completely.
    """
    total = sum(float(lap.get("duration_s") or 0.0) for lap in laps)
    moving = sum(float(lap.get("moving_s") or lap.get("duration_s") or 0.0) for lap in laps)
    if total <= 0:
        return None
    return max(0.0, 1.0 - moving / total)


def fixed_time_projection(
    *,
    reference_distance_m: float,
    reference_moving_s: float,
    target_hours: float,
    exponent_band: tuple[float, float] = FIXED_TIME_EXPONENT_BAND,
    stoppage: float | None = None,
    reference_was_maximal: bool = False,
) -> dict[str, Any]:
    """Distance range for a fixed-time event, from one long reference effort.

    `reference_moving_s` is MOVING time, and `stoppage` (0-1) is the fraction
    of race time expected to be spent stopped — modelled explicitly rather
    than folded into pace, because they are separately actionable.

    Returns {low_m, high_m, mid_m, band, moving_hours, stoppage,
    extrapolation_ratio, assumptions, confidence}. `confidence` is 'low'
    whenever the reference is sub-maximal or the extrapolation exceeds
    MAX_EXTRAPOLATION_RATIO — which, projecting 24h from a training run, is
    the normal case and must be said out loud.
    """
    if reference_distance_m <= 0 or reference_moving_s <= 0:
        raise ValueError("reference effort must be positive")
    ref_hours = reference_moving_s / 3600.0
    if ref_hours < MIN_REFERENCE_HOURS:
        return {"error": f"reference effort under {MIN_REFERENCE_HOURS}h is too short "
                         f"to project a fixed-time event", "reference_hours": ref_hours}

    # An unknown stoppage is NOT zero stoppage. Silently modelling 24h of
    # unbroken movement — which no fixed-time race achieves — would inflate the
    # distance and present the optimism as a measurement.
    modelled = stoppage is not None
    stop = max(0.0, min(stoppage, 0.9)) if modelled else 0.0
    moving_hours = target_hours * (1.0 - stop)
    ratio = moving_hours / ref_hours

    lo_b, hi_b = exponent_band
    # Higher exponent = harsher degradation = shorter distance.
    high = riegel_distance(reference_distance_m, reference_moving_s, moving_hours * 3600.0, lo_b)
    low = riegel_distance(reference_distance_m, reference_moving_s, moving_hours * 3600.0, hi_b)

    assumptions = [
        f"reference: {reference_distance_m / 1000:.1f} km in {ref_hours:.2f}h moving",
        f"exponent band {lo_b}-{hi_b} is a population default, not personally fitted",
    ]
    confidence = "moderate"
    if modelled:
        assumptions.append(
            f"{stop:.1%} of race time assumed stopped ({moving_hours:.1f}h moving)")
    else:
        assumptions.append(
            "stoppage NOT modelled — this assumes continuous movement for the whole "
            "event, which no fixed-time race achieves, so the distance is optimistic")
        confidence = "low"
    if not reference_was_maximal:
        assumptions.append("reference effort was sub-maximal — a race effort would start faster, "
                           "but degrade differently; treat as a floor-ish estimate, not a ceiling")
        confidence = "low"
    if ratio > MAX_EXTRAPOLATION_RATIO:
        assumptions.append(f"extrapolating {ratio:.1f}x beyond the reference duration")
        confidence = "low"

    return {"low_m": low, "high_m": high, "mid_m": (low + high) / 2.0,
            "band": (lo_b, hi_b), "moving_hours": moving_hours,
            "stoppage": stop if modelled else None, "stoppage_modelled": modelled,
            "extrapolation_ratio": ratio, "assumptions": assumptions,
            "confidence": confidence}


def compare_to_field(
    projected_m: float, field_results_m: list[float]
) -> dict[str, Any]:
    """Where a projected distance would have placed in a known field.

    A distance target is meaningless in isolation: 200 km is a podium on one
    course and mid-pack on another. Returns {rank, n, share_above, percentile}
    — `rank` is 1-based, counting how many finishers beat the projection.
    """
    field = sorted((float(x) for x in field_results_m if x and x > 0), reverse=True)
    if not field:
        return {"rank": None, "n": 0, "share_above": None, "percentile": None}
    above = sum(1 for x in field if x > projected_m)
    return {"rank": above + 1, "n": len(field),
            "share_above": above / len(field),
            "percentile": 100.0 * (1.0 - above / len(field))}
