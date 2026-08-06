"""Gear wear and rotation (DESIGN.md §3.2).

Two numbers, two different provenances, and the difference matters enough to
keep them apart:

- **Mileage** is Garmin's odometer (`gear.total_meters`). It is authoritative
  because it counts sessions older than this store's history window, and the
  athlete's own retirement distance (`gear.max_meters`, set in Garmin Connect)
  is measured against it. Fartlek does not invent either number.
- **Rotation** is what the local attribution can prove: kilometres per pair
  over a window, from `activity_gear` links. It answers "am I actually
  rotating", which the odometer cannot — a 500 km pair could be 500 km last
  month or over two years.

The only threshold invented here is the WATCH line at 75% of the athlete's
own limit: soft enough to plan a replacement, not a claim about when a
midsole dies. Shoe-longevity research does not support a universal number, so
the server refuses to publish one — DUE simply means the athlete's own
setting was reached.

Pure functions: store rows in, GearStatus out; rendering stays in the tool.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

WATCH_FRACTION = 0.75
DUE_FRACTION = 1.0

# Verdict ordering: worst first, so the renderer's drop order keeps what matters.
_VERDICT_RANK = {"due": 0, "watch": 1, "ok": 2, "unknown": 3}


@dataclass(frozen=True)
class GearStatus:
    uuid: str
    name: str
    make_model: str | None
    type: str                     # 'shoes' | 'bike' | 'other'
    status: str                   # 'active' | 'retired'
    total_km: float | None        # Garmin odometer
    threshold_km: float | None    # athlete-set retirement distance
    fraction: float | None        # total / threshold, None when no threshold
    verdict: str                  # 'due' | 'watch' | 'ok' | 'unknown'
    sessions: int | None          # Garmin's all-time count
    window_km: float              # locally attributed over the window
    window_sessions: int
    share: float | None           # window_km / window km across same-type gear
    last_used: str | None
    days_since: int | None


def _km(meters: float | None) -> float | None:
    return round(meters / 1000.0, 1) if meters is not None else None


def _verdict(fraction: float | None) -> str:
    """'unknown' when the athlete set no retirement distance — the honest
    answer, not a guess dressed up as one."""
    if fraction is None:
        return "unknown"
    if fraction >= DUE_FRACTION:
        return "due"
    if fraction >= WATCH_FRACTION:
        return "watch"
    return "ok"


def assess(
    gear_rows: list[dict[str, Any]],
    usage: dict[str, dict[str, Any]],
    last_used: dict[str, str],
    today: str,
) -> list[GearStatus]:
    """Rank the locker worst-first.

    `usage` is store.gear_usage(window) keyed by uuid, `last_used` is
    store.gear_last_used() over the whole store — a pair's last outing is a
    fact about the pair, not about the window being asked for.

    Rotation share is computed within a gear type: a bike's kilometres would
    otherwise swamp every shoe in the locker.
    """
    today_d = date.fromisoformat(today)
    window_by_type: dict[str, float] = {}
    for row in gear_rows:
        u = usage.get(row["uuid"]) or {}
        window_by_type[row["type"]] = window_by_type.get(row["type"], 0.0) + float(
            u.get("meters") or 0.0
        )

    out: list[GearStatus] = []
    for row in gear_rows:
        u = usage.get(row["uuid"]) or {}
        window_m = float(u.get("meters") or 0.0)
        total_m = row.get("total_meters")
        max_m = row.get("max_meters")
        fraction = (
            total_m / max_m if total_m is not None and max_m else None
        )
        seen = last_used.get(row["uuid"])
        type_total = window_by_type.get(row["type"]) or 0.0
        out.append(
            GearStatus(
                uuid=row["uuid"],
                name=row["name"],
                make_model=row.get("make_model"),
                type=row["type"],
                status=row.get("status") or "active",
                total_km=_km(total_m),
                threshold_km=_km(max_m),
                fraction=fraction,
                verdict=_verdict(fraction),
                sessions=row.get("total_activities"),
                window_km=round(window_m / 1000.0, 1),
                window_sessions=int(u.get("sessions") or 0),
                share=(window_m / type_total) if type_total > 0 else None,
                last_used=seen,
                days_since=(
                    (today_d - date.fromisoformat(seen)).days if seen else None
                ),
            )
        )
    # Worst verdict first, then the most worn — the pair to act on leads.
    out.sort(key=lambda g: (_VERDICT_RANK[g.verdict], -(g.fraction or 0.0),
                            -(g.total_km or 0.0), g.name))
    return out


def headline(statuses: list[GearStatus]) -> str:
    """One line for the verdict slot: the pair that needs a decision, or the
    fact that none does."""
    if not statuses:
        return "no gear on file"
    due = [g for g in statuses if g.verdict == "due"]
    watch = [g for g in statuses if g.verdict == "watch"]
    n = len(statuses)
    if due:
        g = due[0]
        more = f" (+{len(due) - 1} more past their limit)" if len(due) > 1 else ""
        return (
            f"{g.name} has passed its {g.threshold_km:g} km limit "
            f"({g.total_km:g} km){more} — time to replace it"
        )
    if watch:
        g = watch[0]
        return (
            f"{g.name} is at {g.fraction:.0%} of its {g.threshold_km:g} km limit "
            f"({g.total_km:g} km) — plan a replacement"
        )
    unknown = [g for g in statuses if g.verdict == "unknown"]
    if len(unknown) == n:
        return (
            f"{n} item{'s' if n > 1 else ''} on file, none with a retirement "
            f"distance set in Garmin Connect — mileage tracked, no limit to judge it against"
        )
    return f"{n} item{'s' if n > 1 else ''} on file, none near its retirement limit"
