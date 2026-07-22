"""Sleep debt, regularity (SRI) and social jetlag (DESIGN.md §3.2 #10).

Three measures of three different things, deliberately not blended into one
"sleep score" — they fail independently and have different fixes:

- **debt** — how much sleep is missing against the athlete's own need.
- **SRI** — how *consistent* the sleep/wake pattern is from one 24h to the
  next. An athlete can sleep 8h every night and still score poorly by moving
  those 8h around, which is why duration cannot stand in for regularity.
- **social jetlag** — the weekend-vs-weekday shift in mid-sleep time, the
  chronobiology marker of a body clock being dragged back and forth.

SRI follows Phillips et al. (2017): compare every minute of the day with the
same minute 24h later, over consecutive day pairs.

    SRI = -100 + (200 / (M * (N-1))) * sum(delta(s_i,j , s_i,j+1))

Its theoretical range is -100..+100 (the spec's "0-100" describes where real
humans land, not the formula's bounds). A perfectly regular sleeper scores
100; a coin-flip pattern scores 0.

Two modelling decisions worth stating, because both change the number:

- Intra-sleep `awake` intervals count as WAKE, not sleep. Phillips defines the
  state as sleep/wake, and a night broken into six wakings genuinely is less
  regular than an unbroken one.
- A calendar day is only usable when BOTH the night ending that morning and
  the night starting that evening are present, since a day carries the tail of
  one night and the head of the next. Missing nights are skipped rather than
  treated as wakefulness, which would fabricate irregularity out of a watch
  left on the charger.

Pure functions over store rows: `sleep_timeline` (date + intervals JSON) and
`days` (sleep_need_h, sleep_duration_h, sleep_start_ts, sleep_end_ts).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import fmean
from typing import Any

MINUTES_PER_DAY = 1440
SRI_MIN_DAY_PAIRS = 6            # 7 nights => 6 comparable 24h transitions
DEBT_WINDOW_DAYS = 14
DEFAULT_SLEEP_NEED_H = 8.0       # only when the device reports no need
SLEEP_STATES = frozenset({"deep", "light", "rem"})
_WEEKEND_WAKE_DAYS = frozenset({5, 6})   # Sat, Sun (nights before free days)


# --- timeline handling ------------------------------------------------------

def parse_intervals(intervals_json: str) -> list[tuple[str, datetime, datetime]]:
    """[[state, start_iso, end_iso], ...] → typed tuples, malformed rows skipped."""
    out: list[tuple[str, datetime, datetime]] = []
    try:
        raw = json.loads(intervals_json)
    except (TypeError, ValueError):
        return out
    for item in raw or []:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        try:
            out.append((str(item[0]), datetime.fromisoformat(item[1]),
                        datetime.fromisoformat(item[2])))
        except (TypeError, ValueError):
            continue
    return out


def occupancy_grid(timeline_rows: list[dict[str, Any]]) -> dict[str, bytearray]:
    """{calendar date: 1440-minute asleep/awake vector} from timeline rows.

    Each row is {date (wake-date), intervals_json}. Intervals are placed on the
    calendar by their real timestamps, so a night spanning midnight correctly
    fills the tail of one day and the head of the next.
    """
    grid: dict[str, bytearray] = defaultdict(lambda: bytearray(MINUTES_PER_DAY))
    for row in timeline_rows:
        for state, start, end in parse_intervals(row.get("intervals_json") or ""):
            if state not in SLEEP_STATES:
                continue  # intra-sleep wakings are wake
            cursor = start
            while cursor < end:
                day = cursor.date().isoformat()
                minute = cursor.hour * 60 + cursor.minute
                day_end = datetime.combine(cursor.date() + timedelta(days=1),
                                           datetime.min.time())
                stop = min(end, day_end)
                span = max(1, int((stop - cursor).total_seconds() // 60))
                vec = grid[day]
                for m in range(minute, min(minute + span, MINUTES_PER_DAY)):
                    vec[m] = 1
                cursor = stop
    return dict(grid)


def _covered_days(timeline_rows: list[dict[str, Any]]) -> set[str]:
    """Days whose full 24h is observed: needs the night ending that morning
    (wake-date == day) and the one starting that evening (wake-date == day+1)."""
    nights = {str(r["date"]) for r in timeline_rows if r.get("date")}
    covered = set()
    for d in nights:
        nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        if nxt in nights:
            covered.add(d)
    return covered


def sleep_regularity_index(
    timeline_rows: list[dict[str, Any]], end_date: str, days: int = 7
) -> dict[str, Any]:
    """SRI over the window ending at `end_date` (Phillips et al. 2017).

    Returns {sri, n_pairs, days_covered, suppressed, reason}. `sri` is None and
    `suppressed` True below SRI_MIN_DAY_PAIRS comparable transitions — with
    fewer, the figure swings wildly on one odd night.
    """
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=days - 1)
    in_window = [
        r for r in timeline_rows
        if r.get("date") and start_d <= date.fromisoformat(str(r["date"])) <= end_d
    ]
    grid = occupancy_grid(in_window)
    covered = _covered_days(in_window) & set(grid)

    agreements = comparisons = 0
    pairs = 0
    for d in sorted(covered):
        nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        if nxt not in covered:
            continue
        a, b = grid[d], grid[nxt]
        agreements += sum(1 for i in range(MINUTES_PER_DAY) if a[i] == b[i])
        comparisons += MINUTES_PER_DAY
        pairs += 1

    if pairs < SRI_MIN_DAY_PAIRS or not comparisons:
        return {"sri": None, "n_pairs": pairs, "days_covered": len(covered),
                "suppressed": True,
                "reason": f"{pairs} comparable 24h transitions, need {SRI_MIN_DAY_PAIRS}"}
    return {"sri": -100.0 + 200.0 * (agreements / comparisons), "n_pairs": pairs,
            "days_covered": len(covered), "suppressed": False, "reason": None}


# --- debt -------------------------------------------------------------------

def sleep_debt(
    day_rows: list[dict[str, Any]], end_date: str, window: int = DEBT_WINDOW_DAYS
) -> dict[str, Any]:
    """Cumulative shortfall vs need over the window: sum(max(0, need - actual)).

    Surplus nights do NOT offset deficits — sleep debt does not net out, and
    treating it as a balance would let one 10h Sunday erase a hard week.

    Returns {debt_h, nights, nights_short, avg_need_h, avg_actual_h,
    need_source}. `need_source` is 'device' when Garmin reported a need for
    every night, 'default' when the 8h fallback was used throughout, and
    'mixed' otherwise — a threshold must never look personally derived when it
    is a population default.
    """
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=window - 1)
    rows = [
        r for r in day_rows
        if r.get("date") and start_d <= date.fromisoformat(str(r["date"])) <= end_d
        and r.get("sleep_duration_h") is not None
    ]
    if not rows:
        return {"debt_h": None, "nights": 0, "nights_short": 0, "avg_need_h": None,
                "avg_actual_h": None, "need_source": None}

    debt = 0.0
    short = 0
    needs, actuals, from_device = [], [], 0
    for r in rows:
        need = r.get("sleep_need_h")
        if need is None:
            need = DEFAULT_SLEEP_NEED_H
        else:
            from_device += 1
        actual = float(r["sleep_duration_h"])
        gap = max(0.0, float(need) - actual)
        debt += gap
        if gap > 0:
            short += 1
        needs.append(float(need))
        actuals.append(actual)

    if from_device == len(rows):
        source = "device"
    elif from_device == 0:
        source = "default"
    else:
        source = "mixed"
    return {"debt_h": debt, "nights": len(rows), "nights_short": short,
            "avg_need_h": fmean(needs), "avg_actual_h": fmean(actuals),
            "need_source": source}


# --- social jetlag ----------------------------------------------------------

def _mid_sleep_hours(row: dict[str, Any]) -> float | None:
    """Mid-sleep expressed as hours since the preceding noon.

    Anchoring at noon rather than midnight keeps a 03:00 mid-sleep (15.0) and a
    23:00 one (11.0) on the same continuous axis, so averaging them does not
    wrap around midnight and produce a meaningless mean.
    """
    start, end = row.get("sleep_start_ts"), row.get("sleep_end_ts")
    if not start or not end:
        return None
    try:
        s, e = datetime.fromisoformat(str(start)), datetime.fromisoformat(str(end))
    except ValueError:
        return None
    if e <= s:
        return None
    mid = s + (e - s) / 2
    noon = datetime.combine(
        mid.date() if mid.hour >= 12 else mid.date() - timedelta(days=1),
        datetime.min.time(),
    ) + timedelta(hours=12)
    return (mid - noon).total_seconds() / 3600.0


def social_jetlag(
    day_rows: list[dict[str, Any]], end_date: str, window: int = 28
) -> dict[str, Any]:
    """Weekend-minus-weekday shift in mid-sleep time, in hours.

    Free nights are those whose wake-date is Saturday or Sunday. Positive means
    sleeping later at weekends (the usual direction).

    Returns {jetlag_h, weekday_mid, weekend_mid, n_weekday, n_weekend,
    suppressed, reason}; suppressed unless both groups have >= 2 nights.
    """
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=window - 1)
    weekday, weekend = [], []
    for r in day_rows:
        d = r.get("date")
        if not d:
            continue
        day = date.fromisoformat(str(d))
        if not (start_d <= day <= end_d):
            continue
        mid = _mid_sleep_hours(r)
        if mid is None:
            continue
        (weekend if day.weekday() in _WEEKEND_WAKE_DAYS else weekday).append(mid)

    if len(weekday) < 2 or len(weekend) < 2:
        return {"jetlag_h": None, "weekday_mid": None, "weekend_mid": None,
                "n_weekday": len(weekday), "n_weekend": len(weekend),
                "suppressed": True,
                "reason": "need at least 2 weekday and 2 weekend nights"}
    wd, we = fmean(weekday), fmean(weekend)
    return {"jetlag_h": we - wd, "weekday_mid": wd, "weekend_mid": we,
            "n_weekday": len(weekday), "n_weekend": len(weekend),
            "suppressed": False, "reason": None}


def format_clock(hours_since_noon: float) -> str:
    """15.5 (hours since noon) → '03:30' — for rendering mid-sleep times."""
    total = int(round((12.0 + hours_since_noon) * 60)) % MINUTES_PER_DAY
    return f"{total // 60:02d}:{total % 60:02d}"
