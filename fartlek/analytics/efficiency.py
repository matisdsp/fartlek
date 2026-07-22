"""Aerobic efficiency from per-lap splits (DESIGN.md §3.2 #12, #13).

Answers "am I getting fitter" from the only evidence that is not confounded by
how hard the athlete chose to run: **heart rate at a given pace**. Rising speed
at falling HR is fitness; nothing else in the catalog states it as directly.

Everything here works lap by lap, never on session averages, because a session
average is a lie for any structured workout: an interval session averaging
5:30/km is 3:30 reps plus 7:00 recoveries, and its "average HR at 5:30" belongs
to neither.

Three confounders are handled explicitly, because each one moves HR-at-pace by
more than the fitness signal being measured:

- **terrain** — `gap_speed` (Garmin's grade-adjusted speed) is preferred over
  raw speed. A 6:00/km lap up a hill is not a 6:00/km lap. When the device does
  not produce GAP the lap is still used, flagged `terrain='raw'`, so a whole
  history is not thrown away for a missing field.
- **cardiac drift** — HR rises over a long effort at constant pace, so late
  laps read "worse" for reasons that are not fitness. Measured per session as
  decoupling (first half vs second half) rather than silently averaged away.
- **heat** — above HOT_TEMP_C, HR at a given pace rises for thermoregulation.
  Hot sessions are flagged and excluded from the trend series (never deleted:
  they are real sessions, they are just not evidence about fitness).

A fourth confounder is structural: HR lags effort by a minute or more, so the
recovery lap of an interval carries the previous rep's HR. Such laps are
dropped by the qualifier rather than corrected.

**Validated against second-by-second streams (2026-07-22).** The lap method is
an approximation of the stream-exact computation §3.2 #12 allows it to stand
in for. Measured on 8 real long runs (2.3h-7.2h) by recomputing decoupling
from the raw streams held by intervals.icu under identical rules:

    median difference 1.0 percentage point, 7 of 8 within 3 points,
    worst case 3.45 points.

That is well inside the decision thresholds decoupling feeds (the classic line
is 5%), for ~1 KB per session instead of megabytes of stream.

The worst case is informative rather than random: it was the session with 9.6%
stopped time. A lap containing a long pause has its average speed distorted,
while a stream computation simply drops the stationary samples. **So this
method degrades exactly where an athlete stops a lot — which is the norm in a
fixed-time ultra**, and a drill-down on such a session should prefer streams.

The absolute EF level also runs ~6% below the stream figure, because laps
include stopped time and prefer grade-adjusted speed. It is a systematic
offset, so it cancels in trends (what EF is used for) but the two numbers are
not interchangeable in isolation.

Pure functions over lap dicts as stored by the store (`activity_laps` columns
plus `date`/`sport` when they come from `laps_in_range`).
"""
from __future__ import annotations

from collections import defaultdict
from statistics import fmean, pstdev
from typing import Any

# Steady-state session qualifier (§3.2 #12)
STEADY_MIN_MOVING_S = 40 * 60
STEADY_LAP_GAP_CV_MAX = 0.08
STEADY_MIN_EASY_LAP_SHARE = 0.80
WARMUP_EXCLUDE_S = 10 * 60
LONG_RUN_MIN_S = 90 * 60          # durability threshold (§3.2 #13)
HOT_TEMP_C = 24.0                 # heat guard (§3.2 #12)

# HR-at-pace qualifier
MIN_LAP_DISTANCE_M = 400.0        # below this, pace is dominated by lap edges
PREV_LAP_SPEED_RATIO = 1.15       # previous lap this much faster => HR contaminated
MIN_LAPS_FOR_BAND = 3

_EPS = 1e-9


# --- lap primitives ---------------------------------------------------------

def lap_speed(lap: dict[str, Any], *, prefer_gap: bool = True) -> tuple[float, str] | None:
    """(speed in m/s, basis) for a lap — grade-adjusted where the device
    provides it. Falls back to distance/time when `avg_speed` is absent, and
    returns None when neither is derivable."""
    if prefer_gap and lap.get("gap_speed"):
        return float(lap["gap_speed"]), "gap"
    if lap.get("avg_speed"):
        return float(lap["avg_speed"]), "raw"
    dist, secs = lap.get("distance_m"), lap.get("moving_s") or lap.get("duration_s")
    if dist and secs:
        return float(dist) / float(secs), "derived"
    return None


def lap_pace_s_per_km(lap: dict[str, Any], *, prefer_gap: bool = True) -> float | None:
    """Pace in seconds per km (the unit runners think in), None if underivable."""
    speed = lap_speed(lap, prefer_gap=prefer_gap)
    if speed is None or speed[0] <= _EPS:
        return None
    return 1000.0 / speed[0]


def lap_ef(lap: dict[str, Any], *, prefer_gap: bool = True) -> float | None:
    """Efficiency factor for one lap: metres per minute per heartbeat.

    Higher is better. Unlike pace, it is comparable across easy and steady
    efforts, which is what makes it trendable.
    """
    speed = lap_speed(lap, prefer_gap=prefer_gap)
    hr = lap.get("avg_hr")
    if speed is None or not hr or hr <= 0:
        return None
    return (speed[0] * 60.0) / float(hr)


def lap_is_hot(lap: dict[str, Any]) -> bool:
    temp = lap.get("temp_c")
    return temp is not None and float(temp) >= HOT_TEMP_C


# --- HR at pace: the athlete-facing question --------------------------------

def qualify_pace_band_laps(
    laps: list[dict[str, Any]],
    pace_min_s: float,
    pace_max_s: float,
    *,
    prefer_gap: bool = True,
    exclude_hot: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Laps whose pace falls in [pace_min_s, pace_max_s] and whose HR can be
    trusted to belong to that pace.

    Rejections are counted, not silently dropped — the caller discloses them,
    because "12 laps qualified out of 400" is a very different claim from
    "12 out of 14".

    A lap is rejected when it is too short to have a meaningful pace, has no
    HR, is a marked interval recovery, follows a lap more than
    PREV_LAP_SPEED_RATIO faster (HR still elevated from the previous rep), or
    — when `exclude_hot` — was run at or above HOT_TEMP_C.

    `laps` must be ordered by activity then lap index (as the store returns
    them) so "the previous lap" is meaningful.
    """
    kept: list[dict[str, Any]] = []
    rejected: dict[str, int] = defaultdict(int)
    prev_by_activity: dict[Any, tuple[float, str] | None] = {}

    for lap in laps:
        aid = lap.get("activity_id")
        prev_speed = prev_by_activity.get(aid)
        speed = lap_speed(lap, prefer_gap=prefer_gap)
        prev_by_activity[aid] = speed

        if not lap.get("avg_hr"):
            rejected["no_hr"] += 1
            continue
        if (lap.get("distance_m") or 0) < MIN_LAP_DISTANCE_M:
            rejected["too_short"] += 1
            continue
        if speed is None:
            rejected["no_pace"] += 1
            continue
        pace = 1000.0 / speed[0] if speed[0] > _EPS else None
        if pace is None or not (pace_min_s <= pace <= pace_max_s):
            rejected["out_of_band"] += 1
            continue
        if str(lap.get("intensity_type") or "").upper().endswith("REST"):
            rejected["interval_recovery"] += 1
            continue
        if prev_speed is not None and prev_speed[0] > speed[0] * PREV_LAP_SPEED_RATIO:
            rejected["hr_contaminated"] += 1
            continue
        if exclude_hot and lap_is_hot(lap):
            rejected["hot"] += 1
            continue

        enriched = dict(lap)
        enriched["_pace_s_per_km"] = pace
        enriched["_speed_basis"] = speed[1]
        enriched["_ef"] = (speed[0] * 60.0) / float(lap["avg_hr"])
        kept.append(enriched)

    return kept, dict(rejected)


def hr_at_pace(
    laps: list[dict[str, Any]],
    pace_min_s: float,
    pace_max_s: float,
    *,
    prefer_gap: bool = True,
    exclude_hot: bool = False,
) -> dict[str, Any]:
    """Aggregate HR-at-pace over qualifying laps.

    Averages are weighted by lap duration: a 5 km lap says more about the
    athlete than a 400 m one, and unweighted means would let short laps
    dominate.

    Returns {n_laps, n_sessions, avg_hr, avg_pace_s_per_km, ef, gap_share,
    hot_share, rejected, minutes}; `n_laps` 0 when nothing qualified.
    """
    kept, rejected = qualify_pace_band_laps(
        laps, pace_min_s, pace_max_s, prefer_gap=prefer_gap, exclude_hot=exclude_hot
    )
    if not kept:
        return {"n_laps": 0, "n_sessions": 0, "avg_hr": None, "avg_pace_s_per_km": None,
                "ef": None, "gap_share": 0.0, "hot_share": 0.0, "rejected": rejected,
                "minutes": 0.0}

    weights = [float(lap.get("moving_s") or lap.get("duration_s") or 0.0) or 1.0
               for lap in kept]
    total_w = sum(weights)

    def wmean(values: list[float]) -> float:
        return sum(v * w for v, w in zip(values, weights, strict=True)) / total_w

    return {
        "n_laps": len(kept),
        "n_sessions": len({lap.get("activity_id") for lap in kept}),
        "avg_hr": wmean([float(lap["avg_hr"]) for lap in kept]),
        "avg_pace_s_per_km": wmean([lap["_pace_s_per_km"] for lap in kept]),
        "ef": wmean([lap["_ef"] for lap in kept]),
        "gap_share": sum(1 for lap in kept if lap["_speed_basis"] == "gap") / len(kept),
        "hot_share": sum(1 for lap in kept if lap_is_hot(lap)) / len(kept),
        "minutes": total_w / 60.0,
        "rejected": rejected,
    }


def hr_at_pace_by_period(
    laps: list[dict[str, Any]],
    pace_min_s: float,
    pace_max_s: float,
    *,
    period: str = "month",
    **kwargs: Any,
) -> dict[str, dict[str, Any]]:
    """hr_at_pace() bucketed by 'month' or 'week' of the lap's activity date.

    Laps must carry `date` (store.laps_in_range provides it). Buckets are
    keyed 'YYYY-MM' / ISO 'YYYY-Www' and returned in chronological order.
    """
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lap in laps:
        d = lap.get("date")
        if not d:
            continue
        if period == "month":
            key = str(d)[:7]
        else:
            from datetime import date as _date
            iso = _date.fromisoformat(str(d)).isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
        buckets[key].append(lap)
    return {
        k: hr_at_pace(buckets[k], pace_min_s, pace_max_s, **kwargs)
        for k in sorted(buckets)
    }


# --- session-level EF, decoupling, durability (§3.2 #12, #13) ---------------

def _steady_laps(laps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Laps after the warm-up exclusion, with HR and a derivable speed."""
    out, elapsed = [], 0.0
    for lap in laps:
        secs = float(lap.get("moving_s") or lap.get("duration_s") or 0.0)
        elapsed += secs
        if elapsed <= WARMUP_EXCLUDE_S:
            continue
        if lap.get("avg_hr") and lap_speed(lap) is not None:
            out.append(lap)
    return out


def _half_ef(laps: list[dict[str, Any]]) -> float | None:
    efs = [lap_ef(lap) for lap in laps]
    efs = [e for e in efs if e is not None]
    return fmean(efs) if efs else None


def session_efficiency(
    laps: list[dict[str, Any]], *, z2_ceiling_hr: float | None = None
) -> dict[str, Any]:
    """EF / decoupling / durability for one session, with its qualification.

    `steady` is True only when the session meets §3.2 #12: at least
    STEADY_MIN_MOVING_S of running after the warm-up, lap-pace CV below
    STEADY_LAP_GAP_CV_MAX (i.e. it was actually run at a steady pace, not as
    intervals), and — when a Z2 ceiling is known — at least
    STEADY_MIN_EASY_LAP_SHARE of laps at or below it.

    Only steady sessions belong in an EF trend; the rest still get their
    numbers, flagged, because a drill-down should show them.

    decoupling = (EF_first_half - EF_second_half) / EF_first_half; positive
    means HR climbed relative to pace, the classic aerobic-durability
    shortfall. durability (runs >= LONG_RUN_MIN_S) compares final third to
    first third.
    """
    steady_laps = _steady_laps(laps)
    moving_s = sum(float(lap.get("moving_s") or lap.get("duration_s") or 0.0)
                   for lap in steady_laps)
    result: dict[str, Any] = {
        "n_laps": len(steady_laps), "moving_s": moving_s, "steady": False,
        "ef": None, "decoupling": None, "durability": None,
        "hot": any(lap_is_hot(lap) for lap in steady_laps),
        "method": "splits", "reason": None,
    }
    if len(steady_laps) < 2:
        result["reason"] = "fewer than 2 laps after the warm-up exclusion"
        return result

    paces = [p for p in (lap_pace_s_per_km(lap) for lap in steady_laps) if p]
    cv = (pstdev(paces) / fmean(paces)) if len(paces) > 1 and fmean(paces) > _EPS else None
    easy_share = None
    if z2_ceiling_hr:
        easy_share = sum(
            1 for lap in steady_laps if float(lap["avg_hr"]) <= z2_ceiling_hr
        ) / len(steady_laps)

    result["ef"] = _half_ef(steady_laps)
    result["pace_cv"] = cv
    result["easy_lap_share"] = easy_share

    mid = len(steady_laps) // 2
    ef1, ef2 = _half_ef(steady_laps[:mid]), _half_ef(steady_laps[mid:])
    if ef1 and ef2 and ef1 > _EPS:
        result["decoupling"] = (ef1 - ef2) / ef1

    if moving_s >= LONG_RUN_MIN_S and len(steady_laps) >= 3:
        third = len(steady_laps) // 3
        ef_first, ef_last = _half_ef(steady_laps[:third]), _half_ef(steady_laps[-third:])
        if ef_first and ef_last and ef_first > _EPS:
            result["durability"] = ef_last / ef_first

    if moving_s < STEADY_MIN_MOVING_S:
        result["reason"] = "shorter than the steady-state minimum"
    elif cv is not None and cv > STEADY_LAP_GAP_CV_MAX:
        result["reason"] = "pace too variable (structured session, not steady)"
    elif easy_share is not None and easy_share < STEADY_MIN_EASY_LAP_SHARE:
        result["reason"] = "too many laps above the aerobic ceiling"
    else:
        result["steady"] = True
    return result


def ef_trend_series(
    laps: list[dict[str, Any]], *, z2_ceiling_hr: float | None = None,
    include_hot: bool = False,
) -> list[tuple[str, float]]:
    """[(date, EF)] over qualifying steady sessions — the input to trends.analyze.

    Hot sessions are excluded by default (§3.2 #12): their EF is suppressed by
    thermoregulation, so leaving them in reads as a fitness loss every summer.
    """
    by_activity: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    dates: dict[Any, str] = {}
    for lap in laps:
        by_activity[lap["activity_id"]].append(lap)
        if lap.get("date"):
            dates[lap["activity_id"]] = str(lap["date"])

    out: list[tuple[str, float]] = []
    for aid, session in by_activity.items():
        res = session_efficiency(session, z2_ceiling_hr=z2_ceiling_hr)
        if not res["steady"] or res["ef"] is None:
            continue
        if res["hot"] and not include_hot:
            continue
        if aid in dates:
            out.append((dates[aid], res["ef"]))
    return sorted(out)
