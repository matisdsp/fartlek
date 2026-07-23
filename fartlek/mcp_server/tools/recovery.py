"""garmin_recovery — recovery physiology over time (DESIGN §2.4, budget 800 / cap 1100).

Owns the overtraining question. Renders the multi-marker audit as a table with
one row per AVAILABLE marker — never a row of nulls for something the device
does not produce — then the convergence verdict, the athlete's own precedent
comparison where episodes exist, and the escalation rule on file.

The verdict comes from analytics.convergence, so the "no single marker ever
alarms" rule lives in one place rather than being re-implemented here. This
module's job is selection and phrasing, not judgement.
"""
from __future__ import annotations

import math
import re
from datetime import date as _date
from datetime import timedelta
from typing import Any

from fartlek.analytics import baselines, convergence, precedent
from fartlek.analytics import pmc as pmc_engine
from fartlek.analytics import sleep as sleep_engine
from fartlek.render.renderer import Report, Row, Section, render

CAP = 1100
DEFAULT_DAYS = 28
MIN_DAYS, MAX_DAYS = 7, 90

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_VERDICT_LEAD = {
    "RED": "recovery is not keeping up with the load",
    "AMBER": "recovery is strained",
    "WATCH": "coping, with one group of markers off",
    "GREEN": "coping well",
}


def _series(store: Any, metric: str, end: str, days: int) -> list[tuple[str, float]]:
    try:
        return store.get_series(metric, end, days)
    except KeyError:
        return []


def _ordinal(value: float) -> str:
    """83 → '83rd'. Percentiles are read aloud by the model; '83th' reads as
    a typo and undermines trust in the numbers next to it."""
    n = int(round(value))
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fmt(value: float | None, unit: str = "", digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}"


def _hrv_group(store: Any, end: str, window: int) -> tuple[dict[str, Any], list[Row]]:
    """Autonomic markers plus their display rows. Rows are omitted entirely
    when the underlying series is absent."""
    rows: list[Row] = []
    raw = _series(store, "hrv_last_night", end, max(window, 90))
    ln = [(d, math.log(v)) for d, v in raw if v and v > 0]
    below_days = 0
    cv_ratio = None

    if ln:
        band = baselines.hrv_band(raw, end)       # canonical 60d lnRMSSD band (E1)
        if band:
            below_days = baselines.streak(ln, lambda v: v < band["lo"])
            recent = [v for d, v in ln
                      if _date.fromisoformat(d) > _date.fromisoformat(end) - timedelta(days=7)]
            roll = baselines.hrv_roll(raw, end)
            if len(recent) >= 3 and roll is not None:
                # Band is displayed two-sided so the read agrees with garmin_brief
                # (E1), but only a sustained drop feeds the audit: high HRV is
                # information, not a concern (§3.2 #8).
                state = f"{baselines.hrv_position(roll, band)} band"
                rows.append(Row([
                    "HRV (7d roll) vs band",
                    f"{math.exp(roll):.0f} ms, {state} "
                    f"(band {math.exp(band['lo']):.0f}–{math.exp(band['hi']):.0f})"
                    + (f", {below_days}d streak" if below_days else ""),
                    "YES" if below_days >= convergence.PERSISTENCE_DAYS else "no",
                ]))
            cv_now = _cv(ln, end, 7)
            cv_prev = _cv(ln, end, 30)
            if cv_now and cv_prev:
                cv_ratio = cv_now / cv_prev
                rows.append(Row([
                    "HRV day-to-day CV",
                    f"{cv_now:.1%} vs {cv_prev:.1%} (30d)",
                    "YES" if cv_ratio >= 1 + convergence.HRV_CV_RISE_FLAG else "no",
                ]))

    rhr = baselines.rhr_deviation(_series(store, "resting_hr", end, 90), end)
    if rhr.get("level") != "insufficient_data":
        delta = rhr.get("delta")
        rows.append(Row([
            "Resting HR (two-sided)",
            f"{_fmt(rhr.get('median30'), ' bpm', 0)} median, "
            f"{delta:+.1f} today" if delta is not None else "—",
            "YES" if rhr["level"] in ("red", "parasympathetic_watch") else "no",
        ]))

    group = convergence.autonomic_group(
        hrv_below_band_days=below_days, rhr=rhr, hrv_cv_ratio=cv_ratio,
    )
    return group, rows


def _cv(series: list[tuple[str, float]], end: str, days: int) -> float | None:
    base = baselines.baseline(series, end, days)
    if not base or not base["median"]:
        return None
    return abs(base["mad_sd"] / base["median"])


def _sleep_group(
    store: Any, end: str, window: int
) -> tuple[dict[str, Any], list[Row], dict[str, Any]]:
    rows: list[Row] = []
    day_rows = [store.get_day(d) or {}
                for d in _dates(end, min(window, 90))]
    day_rows = [r for r in day_rows if r]

    debt = sleep_engine.sleep_debt(day_rows, end, window=sleep_engine.DEBT_WINDOW_DAYS)
    if debt["debt_h"] is not None:
        source = "" if debt["need_source"] == "device" else f" ({debt['need_source']} need)"
        rows.append(Row([
            "Sleep debt (14d vs need)",
            f"{debt['debt_h']:.1f}h over {debt['nights']} nights{source}",
            "YES" if debt["debt_h"] > convergence.SLEEP_DEBT_H_14D else "no",
        ]))

    timeline = store.get_sleep_timeline(end, days_back=window)
    sri = sleep_engine.sleep_regularity_index(timeline, end, days=min(window, 14))
    if not sri["suppressed"]:
        rows.append(Row([
            "Sleep regularity (SRI)",
            f"{sri['sri']:.0f}/100",
            "YES" if sri["sri"] < convergence.SRI_FLOOR else "no",
        ]))

    deep = _series(store, "sleep_deep_h", end, 90)
    deep_streak = 0
    if deep:
        base = baselines.baseline(deep, end, 90)
        if base:
            floor = base["median"] - base["mad_sd"]
            deep_streak = baselines.streak(deep, lambda v: v < floor)
            rows.append(Row([
                "Deep sleep",
                f"{deep_streak}d below your typical" if deep_streak else "in range",
                "YES" if deep_streak >= convergence.DEEP_SLEEP_STREAK_DAYS else "no",
            ]))

    jetlag = sleep_engine.social_jetlag(day_rows, end, window=min(window, 28))
    if not jetlag["suppressed"]:
        rows.append(Row([
            "Social jetlag",
            f"{jetlag['jetlag_h']:+.1f}h "
            f"(mid-sleep {sleep_engine.format_clock(jetlag['weekday_mid'])} weekdays)",
            "no",
        ]))

    group = convergence.sleep_group(debt=debt, sri=sri, deep_sleep_low_streak=deep_streak)
    return group, rows, debt


def _dates(end: str, days: int) -> list[str]:
    end_d = _date.fromisoformat(end)
    return [(end_d - timedelta(days=i)).isoformat() for i in range(days)]


def _load_group(store: Any, end: str) -> tuple[dict[str, Any], list[Row], dict[str, Any]]:
    rows: list[Row] = []
    loads = [(d, float((store.get_day(d) or {}).get("daily_load") or 0.0))
             for d in reversed(_dates(end, 90))]
    mono = pmc_engine.monotony_strain(loads) if len(loads) >= 7 else {}

    pmc_rows = store.get_pmc(end_date=end, days=1)
    form_pct = None
    if pmc_rows and pmc_rows[-1].get("ctl"):
        last = pmc_rows[-1]
        form_pct = 100.0 * last["tsb"] / last["ctl"]

    if mono.get("monotony") is not None:
        rows.append(Row([
            "Monotony / strain",
            f"{mono['monotony']:.2f}"
            + (f" / {_ordinal(mono['strain_percentile'])} pctile"
               if mono.get("strain_percentile") is not None else ""),
            "YES" if mono["monotony"] > convergence.MONOTONY_FLAG else "no",
        ]))
    if form_pct is not None:
        rows.append(Row([
            "Form (TSB/CTL)",
            f"{form_pct:+.0f}% of CTL",
            "YES" if form_pct < convergence.FORM_PCT_FLOOR else "no",
        ]))

    group = convergence.load_group(
        monotony=mono.get("monotony"),
        strain_pctile=mono.get("strain_percentile"),
        form_pct=form_pct,
    )
    return group, rows, mono


def _subjective_gate(store: Any, end: str) -> dict[str, Any] | None:
    """Same-day illness/injury caps the verdict — the athlete outranks the
    sensors (§3.2 #19)."""
    logs = store.logs_for(end)
    for row in logs:
        if row.get("flag") == "illness":
            return {"level": "RED", "reason": "illness logged today — rest pending symptoms"}
    if store.unresolved_injuries():
        return {"level": "AMBER", "reason": "unresolved injury on file — modify, do not push"}
    return None


def _precedent_line(store: Any, end: str) -> str | None:
    """Compare current load structure to the athlete's own pre-episode levels.

    Externally-caused episodes are excluded: their pre-episode load says
    nothing about this athlete's tolerance (see analytics.precedent).
    """
    logs = store._all("SELECT date, flag, note FROM wellness_log ORDER BY date")
    episodes = precedent.episodes_from_log(logs)
    if not episodes:
        return None
    external = [r["date"] for r in logs
                if r.get("note") and "EXTERNAL" in str(r["note"]).upper()]

    loads = [(d, float((store.get_day(d) or {}).get("daily_load") or 0.0))
             for d in reversed(_dates(end, 200))]
    weekly = []
    for i in range(7, len(loads)):
        m = pmc_engine.monotony_strain(loads[i - 6:i + 1])
        if m.get("weekly_load") is not None:
            weekly.append((loads[i][0], m["weekly_load"]))
    if not weekly:
        return None

    mined = precedent.mine(episodes, {"weekly_load": weekly})
    levels = precedent.trigger_levels(mined, exclude=external)
    res = precedent.compare({"weekly_load": weekly[-1][1]}, levels)
    if res["silent"] or not res["statements"]:
        return None
    n = res["n_precedents"]
    return (f"Personal precedent ({n} episode{'s' if n > 1 else ''} on file): "
            + res["statements"][0])


async def run(ctx: Any, days: int = DEFAULT_DAYS, anchor_date: str | None = None) -> str:
    if anchor_date is not None and not _DATE_RE.match(anchor_date):
        return (f"anchor_date must be YYYY-MM-DD (got {anchor_date!r}). "
                f"Today is {ctx.today()}. Example: garmin_recovery(days=28)")
    if not MIN_DAYS <= days <= MAX_DAYS:
        return (f"days must be between {MIN_DAYS} and {MAX_DAYS} (got {days}). "
                f"Example: garmin_recovery(days={DEFAULT_DAYS})")

    await ctx.ensure_ready()
    end = anchor_date or ctx.today()
    store = ctx.store

    auto_group, auto_rows = _hrv_group(store, end, days)
    sleep_grp, sleep_rows, _debt = _sleep_group(store, end, days)
    load_grp, load_rows, _mono = _load_group(store, end)

    gate = _subjective_gate(store, end)
    audit = convergence.audit([auto_group, sleep_grp, load_grp], subjective=gate)

    rows = auto_rows + sleep_rows + load_rows
    lead = _VERDICT_LEAD.get(audit["verdict"], "")
    n_groups = len(audit["triggering_groups"])
    group_detail = audit["reasons"][0] if (audit["reasons"] and n_groups) else ""
    verdict = (
        f"{lead}. Overtraining audit: {n_groups} of "
        f"{len(convergence.TRIGGERING_GROUPS)} marker groups deviant"
        + (f" — {group_detail}" if group_detail else "")
    )
    # An athlete-reported state must reach the verdict line itself, not just
    # the severity: a capped verdict with no stated reason is unreadable.
    if gate and gate.get("reason"):
        verdict += f". {gate['reason']}"

    sections = []
    if rows:
        sections.append(Section(
            title=None,
            header=["Marker", f"State ({days}d)", "Off baseline?"],
            rows=rows,
            method_note="deviation is judged against this athlete's own "
                        "rolling baseline, not population norms",
        ))

    notes: list[str] = []
    prec = _precedent_line(store, end)
    if prec:
        notes.append(prec)
    notes.append(
        f"Escalation rule on file: HRV below band ≥{convergence.PERSISTENCE_DAYS} "
        f"consecutive days AND/OR resting HR deviating ±5 sustained → RED with "
        f"deload advice. A same-day illness note caps the verdict regardless of sensors."
    )
    if notes:
        sections.append(Section(title=None, header=None, prose="\n\n".join(notes),
                                priority="secondary"))

    report = Report(
        title=f"Recovery — {days} days",
        date=end,
        data_as_of=ctx.data_as_of(),
        verdict=verdict,
        banner=ctx.banner(),
        sections=sections,
        watch_list=audit["watch_items"],
        next_steps=[
            "garmin_brief() for today's call",
            "garmin_log(note=\"...\", flag=\"illness\") to record symptoms",
        ],
    )
    return render(report, CAP)
