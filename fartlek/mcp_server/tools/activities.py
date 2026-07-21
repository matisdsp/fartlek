"""garmin_activities — the logbook browser (DESIGN §2.4, cap 1,300 tokens).

One compact row per session, every row carrying the activity_id that
garmin_activity accepts. Window defaults to the last 14 days (end_date −13d →
end_date, end_date defaulting to today). Sport filtering collapses Garmin
typeKeys through analytics.matcher.sport_family; sport='other' matches every
family outside running/cycling/swimming/strength (walking, hiking, other).

Rows render most-recent-first. More matching rows than `limit` → the newest
`limit` rows plus a §5-style disclosure naming the narrower
garmin_activities() call that returns the rest. Empty windows return a
corrective error (§4.3) pointing at the nearest activity actually in the
store. All plain-string (error) returns still honor the §4.4 banner
invariant.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any

from fartlek.analytics.matcher import sport_family
from fartlek.render.renderer import Report, Row, Section, format_date, render

CAP_TOKENS = 1300
DEFAULT_WINDOW_DAYS = 14

_VALID_SPORTS = ("running", "cycling", "swimming", "strength", "other")
_CORE_FAMILIES = {"running", "cycling", "swimming", "strength"}

_ROW_LABEL = {
    "running": "run", "cycling": "ride", "swimming": "swim",
    "strength": "strength", "walking": "walk", "hiking": "hike", "other": "other",
}
_PLURAL_LABEL = {
    "running": "runs", "cycling": "rides", "swimming": "swims",
    "strength": "strength", "walking": "walks", "hiking": "hikes", "other": "other",
}

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _short(date_str: str) -> str:
    """'2026-07-19' → 'Sat 07-19' (table form, §5 rule 3)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{_WEEKDAYS[dt.weekday()]} {date_str[5:]}"


def _fmt_duration(seconds: float | None) -> str:
    """mm:ss under 100 minutes (matches the §2.4 example's '62:24'), 'XhMM'
    above — '5h00' cannot be misread as five minutes, '5:00' can."""
    if seconds is None:
        return "—"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 100:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}"


def _fmt_pace(avg_speed: float | None) -> str:
    """m/s → 'm:ss' per km (pace_s_per_km = 1000 / speed)."""
    if not avg_speed or avg_speed <= 0:
        return "—"
    pace_s = 1000.0 / avg_speed
    minutes, secs = divmod(int(round(pace_s)), 60)
    return f"{minutes}:{secs:02d}"


def _with_banner(ctx: Any, text: str) -> str:
    banner = ctx.banner()
    return f"{banner}\n\n{text}" if banner else text


def _valid_date(value: str) -> bool:
    try:
        _date.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    return len(value) == 10


def _matches(family: str, sport: str) -> bool:
    if sport == "other":
        return family not in _CORE_FAMILIES
    return family == sport


def _nearest(acts: list[dict[str, Any]], start: str, end: str) -> dict[str, Any]:
    """Activity whose date is closest to the [start, end] window."""
    s, e = _date.fromisoformat(start), _date.fromisoformat(end)

    def dist(a: dict[str, Any]) -> int:
        d = _date.fromisoformat(a["date"])
        if d < s:
            return (s - d).days
        if d > e:
            return (d - e).days
        return 0

    return min(acts, key=dist)


def _empty_window_error(
    ctx: Any, start: str, end: str, sport: str | None
) -> str:
    all_acts = ctx.store.list_activities("0000-01-01", "9999-12-31")
    if not all_acts:
        return _with_banner(
            ctx, "No activities in the store yet. garmin_sync() fetches them from Garmin."
        )
    in_family = [a for a in all_acts if sport is None or _matches(sport_family(a["sport"]), sport)]
    if sport is not None and not in_family:
        near = _nearest(all_acts, start, end)
        label = _ROW_LABEL[sport_family(near["sport"])]
        win_start = (_date.fromisoformat(near["date"]) - timedelta(days=6)).isoformat()
        return _with_banner(
            ctx,
            f"No {sport} activities in the store. Nearest activity: {label} "
            f"{format_date(near['date'])} (id {near['activity_id']}) — "
            f'garmin_activities(start_date="{win_start}", end_date="{near["date"]}")',
        )
    near = _nearest(in_family, start, end)
    label = _ROW_LABEL[sport_family(near["sport"])]
    win_start = (_date.fromisoformat(near["date"]) - timedelta(days=6)).isoformat()
    scope = f"{sport} " if sport else ""
    return _with_banner(
        ctx,
        f"No {scope}activities {format_date(start)} → {format_date(end)}. "
        f"Nearest in store: {label} {format_date(near['date'])} (id {near['activity_id']}) — "
        f'garmin_activities(start_date="{win_start}", end_date="{near["date"]}")',
    )


def _summary(acts: list[dict[str, Any]]) -> str:
    """'runs 9 (98 km), strength 2' — per-family counts, count-descending."""
    counts: dict[str, int] = {}
    dists: dict[str, float] = {}
    for a in acts:
        fam = a["_family"]
        counts[fam] = counts.get(fam, 0) + 1
        dists[fam] = dists.get(fam, 0.0) + (a.get("distance_m") or 0.0)
    parts = []
    for fam in sorted(counts, key=lambda f: (-counts[f], f)):
        part = f"{_PLURAL_LABEL[fam]} {counts[fam]}"
        km = int(round(dists[fam] / 1000))
        if km >= 1:
            part += f" ({km} km)"
        parts.append(part)
    return ", ".join(parts)


def _row(a: dict[str, Any]) -> Row:
    fam = a["_family"]
    dist = a.get("distance_m")
    rpe = a.get("rpe")
    if rpe is None:
        rpe_cell = "—"
    else:
        rpe_cell = f"{rpe}w" if a.get("rpe_source") == "watch" else str(rpe)
    return Row(
        cells=[
            _short(a["date"]),
            _ROW_LABEL[fam],
            str(a.get("name") or "—").replace("|", "/"),
            str(a["activity_id"]),
            f"{dist / 1000:.1f}" if dist else "—",
            _fmt_duration(a.get("duration_s")),
            _fmt_pace(a.get("avg_speed")) if fam == "running" else "—",
            str(a["avg_hr"]) if a.get("avg_hr") is not None else "—",
            f"{round(a['load'])}" if a.get("load") is not None else "—",
            rpe_cell,
        ]
    )


async def run(
    ctx: Any,
    start_date: str | None = None,
    end_date: str | None = None,
    sport: str | None = None,
    limit: int = 25,
) -> str:
    await ctx.ensure_ready()
    today = ctx.today()

    for name, value in (("start_date", start_date), ("end_date", end_date)):
        if value is not None and not _valid_date(value):
            example_start = (_date.fromisoformat(today) - timedelta(days=13)).isoformat()
            return _with_banner(
                ctx,
                f"{name} must be YYYY-MM-DD (got '{value}'). Today is {format_date(today)}. "
                f"Example: garmin_activities(start_date='{example_start}')",
            )
    if sport is not None and sport not in _VALID_SPORTS:
        return _with_banner(
            ctx,
            f"sport must be one of {', '.join(_VALID_SPORTS)} (got '{sport}'). "
            f"Example: garmin_activities(sport='running')",
        )
    if not 1 <= limit <= 30:
        return _with_banner(
            ctx,
            f"limit must be between 1 and 30 (got {limit}) — use a narrower window instead. "
            f"Example: garmin_activities(start_date='{today}', end_date='{today}')",
        )

    end = end_date or today
    start = start_date or (_date.fromisoformat(end) - timedelta(days=DEFAULT_WINDOW_DAYS - 1)).isoformat()
    if start > end:
        return _with_banner(
            ctx,
            f"start_date {start} is after end_date {end}. Today is {format_date(today)}. "
            f"Example: garmin_activities(start_date='{end}', end_date='{start}')",
        )

    acts = ctx.store.list_activities(start, end)
    for a in acts:
        a["_family"] = sport_family(a["sport"])
    if sport is not None:
        acts = [a for a in acts if _matches(a["_family"], sport)]
    if not acts:
        return _empty_window_error(ctx, start, end, sport)

    acts.sort(
        key=lambda a: (a["date"], a.get("start_local") or "", a["activity_id"]), reverse=True
    )
    shown, cut = acts[:limit], acts[limit:]

    n = len(acts)
    verdict = (
        f"{format_date(start)} → {format_date(end)} · "
        f"{n} session{'s' if n != 1 else ''} · {_summary(acts)}"
    )

    overflow_hint = None
    if len(shown) > 6:
        overflow_hint = (
            f'garmin_activities(start_date="{start}", end_date="{shown[6]["date"]}") for the rest'
        )
    table = Section(
        title=None,
        header=["Date", "Sport", "Session", "id", "Dist", "Time", "Pace", "avgHR", "Load", "RPE"],
        rows=[_row(a) for a in shown],
        overflow_hint=overflow_hint,
    )
    sections = [table]
    if cut:
        sections.append(
            Section(
                title=None,
                header=None,
                prose=(
                    f'({len(cut)} more rows — garmin_activities(start_date="{start}", '
                    f'end_date="{cut[0]["date"]}") for the rest)'
                ),
            )
        )

    next_steps = [f"garmin_activity(activity_id={shown[0]['activity_id']}) for any session"]
    if any(a["_family"] == "running" for a in shown):
        next_steps.append('garmin_activity(sport="running") for the latest run')

    report = Report(
        title="Activities",
        date=end,
        data_as_of=ctx.data_as_of(),
        verdict=verdict,
        banner=ctx.banner(),
        sections=sections,
        next_steps=next_steps,
    )
    return render(report, CAP_TOKENS)
