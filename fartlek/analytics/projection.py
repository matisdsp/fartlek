"""Forward PMC projection and taper window (DESIGN.md §3.2 #17).

Runs the athlete's own PMC forward from today's CTL/ATL so a race-day form
figure exists before race day. It is **arithmetic on the athlete's numbers,
never a promise**: the projection says "if you keep doing what you have been
doing, here is where form lands", and every result names the basis it used so
a pattern-based guess is never mistaken for a planned one.

Basis, in priority order (§3.2 #17):

1. **scheduled** — workouts on the Garmin calendar, including enrolled Garmin
   Coach plans. The athlete has stated their intent; use it.
2. **pattern** — the trailing 4 weeks replayed by weekday. A flat daily average
   would be wrong for anyone with a weekly rhythm: it turns two rest days and a
   long run into three identical medium days, which understates both the peak
   fatigue and the recovery.

Taper guidance activates inside TAPER_WINDOW_DAYS of a stored goal race. The
target is the one the literature agrees on and the design fixed: bring form
into the fresh band (+5…+25% of CTL) while losing less than
MAX_CTL_FADE_PCT of fitness. Those two pull in opposite directions — that
tension IS the taper, and the module reports both rather than optimising one
away.

Pure functions; the PMC step itself is `pmc.advance`, shared with the
historical series so the two can never drift apart.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import fmean
from typing import Any

from fartlek.analytics.pmc import advance

PATTERN_WEEKS = 4
TAPER_WINDOW_DAYS = 21
FRESH_BAND_PCT = (5.0, 25.0)
MAX_CTL_FADE_PCT = 10.0


def weekday_pattern(
    daily_loads: list[tuple[str, float]], end_date: str, weeks: int = PATTERN_WEEKS
) -> dict[int, float]:
    """{weekday 0-6: mean load} over the trailing `weeks`.

    Weekday-shaped rather than flat because a training week has a shape: rest
    days, a long day, quality days. Averaging them together projects a week the
    athlete has never trained.
    """
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=weeks * 7 - 1)
    buckets: dict[int, list[float]] = defaultdict(list)
    for d_str, load in daily_loads:
        d = date.fromisoformat(d_str)
        if start_d <= d <= end_d:
            buckets[d.weekday()].append(float(load))
    return {wd: fmean(vals) for wd, vals in buckets.items() if vals}


def project(
    *,
    ctl: float,
    atl: float,
    start_date: str,
    days: int,
    pattern: dict[int, float] | None = None,
    scheduled: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Project [{date, load, ctl, atl, tsb, basis}] forward `days` from
    `start_date` (the first projected day, i.e. tomorrow).

    A date present in `scheduled` uses that load and is marked
    basis='scheduled'; otherwise the weekday pattern is used, marked
    'pattern'. With neither, the day carries 0 and is marked 'none' — an
    unknown future is not a rest day, and the flag says so.
    """
    out: list[dict[str, Any]] = []
    d = date.fromisoformat(start_date)
    for _ in range(days):
        key = d.isoformat()
        if scheduled and key in scheduled:
            load, basis = float(scheduled[key]), "scheduled"
        elif pattern and d.weekday() in pattern:
            load, basis = float(pattern[d.weekday()]), "pattern"
        else:
            load, basis = 0.0, "none"
        ctl, atl, tsb = advance(ctl, atl, load)
        out.append({"date": key, "load": load, "ctl": ctl, "atl": atl,
                    "tsb": tsb, "basis": basis})
        d += timedelta(days=1)
    return out


def form_pct(row: dict[str, Any]) -> float | None:
    """TSB as a percentage of CTL — the scale-invariant form ratio (§3.2 #2)."""
    ctl = row.get("ctl") or 0.0
    return None if abs(ctl) < 1e-9 else 100.0 * row["tsb"] / ctl


def project_to_race(
    *,
    ctl: float,
    atl: float,
    today: str,
    race_date: str,
    daily_loads: list[tuple[str, float]],
    scheduled: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Projection through to race day, with the basis mix disclosed.

    Returns {days_out, race_day, series, basis, scheduled_days, pattern_days,
    ctl_now, ctl_race, form_race_pct, in_fresh_band} — or {'error': ...} when
    the race is not in the future.
    """
    today_d, race_d = date.fromisoformat(today), date.fromisoformat(race_date)
    days_out = (race_d - today_d).days
    if days_out <= 0:
        return {"error": "race date is not in the future", "days_out": days_out}

    pattern = weekday_pattern(daily_loads, today)
    series = project(
        ctl=ctl, atl=atl, start_date=(today_d + timedelta(days=1)).isoformat(),
        days=days_out, pattern=pattern, scheduled=scheduled,
    )
    race_row = series[-1]
    n_sched = sum(1 for r in series if r["basis"] == "scheduled")
    if n_sched == len(series):
        basis = "scheduled"
    elif n_sched:
        basis = "mixed"
    elif pattern:
        basis = "pattern"
    else:
        basis = "none"

    fr = form_pct(race_row)
    return {
        "days_out": days_out,
        "race_day": race_row,
        "series": series,
        "basis": basis,
        "scheduled_days": n_sched,
        "pattern_days": sum(1 for r in series if r["basis"] == "pattern"),
        "ctl_now": ctl,
        "ctl_race": race_row["ctl"],
        "form_race_pct": fr,
        "in_fresh_band": (fr is not None and FRESH_BAND_PCT[0] <= fr <= FRESH_BAND_PCT[1]),
    }


def taper_guidance(projection: dict[str, Any]) -> dict[str, Any]:
    """Taper read-out for a projection, active inside TAPER_WINDOW_DAYS.

    Returns {active, days_out, form_race_pct, in_fresh_band, ctl_fade_pct,
    fade_acceptable, verdict, actions}. `verdict` is a state
    ('on_track' | 'too_fatigued' | 'too_fresh' | 'fitness_bleeding' |
    'not_yet'); wording belongs to the renderer.

    Both halves are reported because they conflict: cutting load enough to
    reach the fresh band also fades CTL, and the athlete needs to see which
    constraint is binding rather than a single "taper harder".
    """
    if projection.get("error"):
        return {"active": False, "verdict": "unavailable",
                "reason": projection["error"], "actions": []}

    days_out = projection["days_out"]
    ctl_now, ctl_race = projection["ctl_now"], projection["ctl_race"]
    fade = 100.0 * (ctl_now - ctl_race) / ctl_now if ctl_now else 0.0
    fr = projection["form_race_pct"]
    fade_ok = fade <= MAX_CTL_FADE_PCT

    if days_out > TAPER_WINDOW_DAYS:
        return {"active": False, "days_out": days_out, "verdict": "not_yet",
                "form_race_pct": fr, "in_fresh_band": projection["in_fresh_band"],
                "ctl_fade_pct": fade, "fade_acceptable": fade_ok,
                "actions": [], "reason":
                f"taper guidance activates inside {TAPER_WINDOW_DAYS} days"}

    actions: list[str] = []
    if fr is None:
        verdict = "unavailable"
    elif fr < FRESH_BAND_PCT[0]:
        verdict = "too_fatigued"
        actions.append("reduce load: projected form is below the fresh band on race day")
    elif fr > FRESH_BAND_PCT[1]:
        verdict = "too_fresh"
        actions.append("hold intensity, trim volume only: form overshoots the fresh band")
    elif not fade_ok:
        verdict = "fitness_bleeding"
        actions.append("keep intensity, cut volume: form is right but fitness is fading")
    else:
        verdict = "on_track"

    if not fade_ok and verdict != "fitness_bleeding":
        actions.append(f"CTL fade {fade:.1f}% exceeds the {MAX_CTL_FADE_PCT:.0f}% ceiling")

    return {"active": True, "days_out": days_out, "form_race_pct": fr,
            "in_fresh_band": projection["in_fresh_band"], "ctl_fade_pct": fade,
            "fade_acceptable": fade_ok, "verdict": verdict, "actions": actions,
            "reason": None}
