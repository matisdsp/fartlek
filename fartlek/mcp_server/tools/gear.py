"""garmin_gear — the gear locker (DESIGN §2.4, cap 500 tokens).

One row per item, worst-first: Garmin's odometer, how far through the
athlete's own retirement distance it is, and what this store could attribute
to it over the last 90 days. The verdict names the pair that needs a
decision, or says that none does. A locker longer than MAX_ROWS has its tail
disclosed rather than printed — never silently (§8.5).

Two honesty rules shape the output. The retirement distance is the athlete's,
set in Garmin Connect — gear without one reads '—' rather than being measured
against an invented default. And the 90-day column is local attribution, so
when it covers only part of the window's sessions the method note says how
many, instead of letting a partial figure read as a total.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any

from fartlek.analytics import gear as gear_mod
from fartlek.render.renderer import Report, Row, Section, render

CAP_TOKENS = 500
WINDOW_DAYS = 90
# Rows rendered before the tail is disclosed rather than printed. The table is
# sorted worst-first, so eight covers every item that could need a decision on
# any realistic locker, and keeps the widest render clear of the cap — the
# runtime estimator undercounts dense tables, so the tool bounds itself here
# instead of relying on the renderer's drop order to notice (§4.5).
MAX_ROWS = 8

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_TYPE_LABEL = {"shoes": "shoes", "bike": "bike", "other": "other"}


def _short(date_str: str) -> str:
    """'2026-07-31' → 'Fri 07-31' (table form, §5 rule 3)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{_WEEKDAYS[dt.weekday()]} {date_str[5:]}"


def _with_banner(ctx: Any, text: str) -> str:
    banner = ctx.banner()
    return f"{banner}\n\n{text}" if banner else text


def _empty_error(ctx: Any, include_retired: bool) -> str:
    """No locker to show — say which of the three reasons it is (§4.3)."""
    if ctx.store.list_gear(include_retired=True):
        # Everything on file is retired.
        return _with_banner(
            ctx,
            "Every item in the locker is retired. "
            "garmin_gear(include_retired=True) shows them.",
        )
    cap = ctx.store.get_capabilities().get("gear")
    if cap is not None and not cap["available"]:
        return _with_banner(
            ctx,
            "No gear on file in Garmin Connect. Add shoes or a bike there "
            "(Gear → Add Gear), set a retirement distance, then garmin_sync().",
        )
    return _with_banner(
        ctx, "The gear locker has not synced yet. garmin_sync() fetches it from Garmin."
    )


def _row(g: gear_mod.GearStatus) -> Row:
    if g.fraction is None:
        of_limit = "—"
    else:
        of_limit = f"{g.fraction:.0%}"
    window = f"{g.window_km:g}" if g.window_km else "—"
    if g.window_km and g.share is not None:
        window += f" ({g.share:.0%})"
    return Row(
        cells=[
            str(g.name).replace("|", "/"),
            f"{g.total_km:g}" if g.total_km is not None else "—",
            of_limit,
            _short(g.last_used) if g.last_used else "—",
            window,
        ]
    )


def _coverage_note(ctx: Any, start: str, end: str) -> str | None:
    """How much of the window the 90-day column actually rests on. Attribution
    costs one call per session and backfills in the background, so on a young
    store this is the difference between a partial figure and a wrong one."""
    total = len(ctx.store.list_activities(start, end))
    if not total:
        return None
    unattributed = len(ctx.store.activities_missing_gear(start, end))
    attributed = total - unattributed
    if attributed >= total:
        return None
    return (
        f"90d column covers {attributed} of {total} sessions in the window — "
        f"the rest have no gear recorded yet"
    )


async def run(ctx: Any, include_retired: bool = False) -> str:
    await ctx.ensure_ready()
    today = ctx.today()
    store = ctx.store

    rows = store.list_gear(include_retired=include_retired)
    if not rows:
        return _empty_error(ctx, include_retired)

    start = (_date.fromisoformat(today) - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    statuses = gear_mod.assess(
        rows, store.gear_usage(start, today), store.gear_last_used(), today
    )

    kinds: dict[str, int] = {}
    for g in statuses:
        kinds[g.type] = kinds.get(g.type, 0) + 1
    inventory = ", ".join(
        f"{n} {_TYPE_LABEL.get(k, k)}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])
    )
    verdict = f"{inventory} · {gear_mod.headline(statuses)}"

    notes = [
        f"limits are the athlete's own, set in Garmin Connect; watch "
        f"{gear_mod.WATCH_FRACTION:.0%}, replace {gear_mod.DUE_FRACTION:.0%}. "
        f"90d = km attributed in {WINDOW_DAYS}d and share of that type"
    ]
    coverage = _coverage_note(ctx, start, today)
    if coverage:
        notes.append(coverage)

    shown, cut = statuses[:MAX_ROWS], statuses[MAX_ROWS:]
    if cut:
        # Disclosed, never silent (§8.5). The list is sorted worst-first, so
        # what falls off is always the gear furthest from needing a decision.
        notes.append(
            f"{len(cut)} further item{'s' if len(cut) > 1 else ''} not shown, "
            f"all further from their limit than the rows above"
        )

    table = Section(
        title=None,
        header=["Gear", "km", "of limit", "Last worn", "90d"],
        rows=[_row(g) for g in shown],
        method_note=" · ".join(notes),
        overflow_hint="the locker is sorted worst-first",
    )

    next_steps = ['garmin_activities(sport="running") for the sessions behind the km']
    if not include_retired:
        next_steps.append("garmin_gear(include_retired=True) for retired gear")

    report = Report(
        title="Gear",
        date=today,
        data_as_of=ctx.data_as_of(),
        verdict=verdict,
        banner=ctx.banner(),
        sections=[table],
        next_steps=next_steps,
    )
    return render(report, CAP_TOKENS)
