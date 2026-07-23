"""garmin_brief — fused morning readiness report (DESIGN §2.4, budget 400 / cap 600).

Structure mirrors the §2.4 example: banner → title → fused VERDICT (fusion
marker_inputs → compute_readiness → apply_gates over same-day/last-24h logs)
→ evidence table (one row per AVAILABLE marker, absent markers omitted, never
'null') → WATCH-severity watch list → Yesterday / Today's-plan context lines
→ Next breadcrumb (shipped tools only).

Evidence rows come straight from the store + baselines/pmc engines so the
table renders even while the fusion verdict is provisional; the verdict line
itself is fusion's output verbatim (markers_used declared, AMBER/RED always
carry the concrete modification, provisional ⇒ 'PROVISIONAL (n=…)' prefix).
"""
from __future__ import annotations

import math
import re
from datetime import date as _date
from datetime import timedelta
from typing import Any

from fartlek.analytics import baselines, fusion
from fartlek.analytics import pmc as pmc_engine
from fartlek.render.renderer import Report, Row, Section, format_date, render

CAP = 600

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MARKER_ORDER = ("hrv", "rhr", "sleep", "form", "body_battery")
_MARKER_NAMES = {
    "hrv": "HRV",
    "rhr": "RHR",
    "sleep": "sleep",
    "form": "form",
    "body_battery": "Body Battery",
}
_DEFAULT_MOD = {
    "AMBER": "replace today's quality with an easy session; reassess tomorrow",
    "RED": "rest today; reassess tomorrow",
}
_OK_FORM_BANDS = {"productive", "neutral", "fresh/race-ready"}
_SPORT_LABELS = {
    "running": "Run",
    "trail_running": "Trail run",
    "treadmill_running": "Treadmill run",
    "cycling": "Ride",
    "road_biking": "Ride",
    "virtual_ride": "Ride",
    "swimming": "Swim",
    "lap_swimming": "Swim",
    "open_water_swimming": "Swim",
    "strength_training": "Strength",
    "walking": "Walk",
    "hiking": "Hike",
}
_PLAN_SOURCES = {
    "garmin_coach": "Garmin Coach plan",
    "calendar": "Garmin calendar",
    "fartlek": "Fartlek plan",
}
_COLD_START_NOTE = (
    "First sync just ran (~180d of history loaded; deeper backfill continues in background)."
)


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def _signed(n: float, decimals: int = 0, suffix: str = "") -> str:
    txt = f"{abs(n):.{decimals}f}"
    if float(txt) == 0:
        return f"0{suffix}"
    return ("+" if n > 0 else "−") + txt + suffix


def _fmt_hm(hours: float) -> str:
    """9.0 → '9h00'."""
    total_min = round(hours * 60)
    return f"{total_min // 60}h{total_min % 60:02d}"


def _fmt_duration(seconds: float) -> str:
    """3744 → '62:24'; ≥100 min → '1h45'."""
    s = round(seconds)
    if s < 6000:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def _score_label(score: float) -> str:
    if score >= 90:
        return "Excellent"
    if score >= 80:
        return "Good"
    if score >= 60:
        return "Fair"
    return "Poor"


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _at(series: list[tuple[str, float]], d: str) -> float | None:
    return next((v for dd, v in series if dd == d), None)


def _prior(series: list[tuple[str, float]], d: str) -> list[tuple[str, float]]:
    return [(dd, v) for dd, v in series if dd != d]


# ---------------------------------------------------------------------------
# evidence rows (one per available marker; None ⇒ omitted)
# ---------------------------------------------------------------------------

def _hrv_row(store: Any, d: str) -> Row | None:
    series = store.get_series("hrv_last_night", d, 60)
    today = _at(series, d)
    band = baselines.hrv_band(series, d)          # canonical 60d lnRMSSD band (E1)
    roll_ln = baselines.hrv_roll(series, d)
    if today is None or band is None or roll_ln is None:
        return None
    pos = baselines.hrv_position(roll_ln, band)   # 'below' | 'in' | 'above'
    ln_series = [(dd, math.log(v)) for dd, v in series if v and v > 0]
    if pos == "below":
        flag = "⚠ below band"
        run = baselines.streak(ln_series, lambda v: v < band["lo"])
        context = f"below band, {run}d"
    elif pos == "above":
        # High HRV is not a daily warning: fusion never credits or penalises it
        # ("above is never credited", §3.2 #8), a rise is corroboration for the
        # convergence audit, not a standalone alarm, and the alert scanner is
        # tuned so only the unfavorable direction interrupts (HANDOFF §7). So we
        # surface "above band" as information, not a ⚠.
        flag = "✓"
        run = baselines.streak(ln_series, lambda v: v > band["hi"])
        context = f"above band, {run}d"
    else:
        flag = "✓"
        run = baselines.streak(ln_series, lambda v: band["lo"] <= v <= band["hi"])
        context = f"in band, {run}d stable" if run else "in band"
    n_txt = f", n={band['n']}" if band["n"] < 60 else ""
    return Row(cells=[
        "HRV overnight",
        f"{today:.0f} ms (7d avg {pos} band)",
        f"band {math.exp(band['lo']):.0f}–{math.exp(band['hi']):.0f} (60d{n_txt})",
        context,
        flag,
    ])


def _rhr_row(store: Any, d: str) -> Row | None:
    series = store.get_series("resting_hr", d, 91)
    today = _at(series, d)
    dev = baselines.rhr_deviation(series, d)
    if today is None or dev["median30"] is None:
        return None
    median30 = dev["median30"]
    base = baselines.baseline(_prior(series, d), d, 30)
    if base is not None:
        base_txt = (
            f"30d median {median30:.0f} "
            f"({median30 - base['mad_sd']:.0f}–{median30 + base['mad_sd']:.0f})"
        )
    else:
        base_txt = f"30d median {median30:.0f}"
    level = dev["level"]
    if level == "ok":
        flag = "✓"
    elif level == "insufficient_data":
        flag = f"n={dev['n']} (warming)"
    elif level == "red":
        flag = f"⚠ elevated {dev['sustained_days']}d"
    elif level == "parasympathetic_watch":
        flag = f"⚠ low {dev['sustained_days']}d"
    else:
        flag = "⚠ caution"
    return Row(cells=[
        "Resting HR",
        f"{today:.0f} bpm",
        base_txt,
        _signed(dev["delta"]) if dev["delta"] is not None else "—",
        flag,
    ])


def _sleep_row(store: Any, d: str) -> Row | None:
    day = store.get_day(d) or {}
    dur = day.get("sleep_duration_h")
    score = day.get("sleep_score")
    need = day.get("sleep_need_h")
    if dur is None and score is None:
        return None
    need_val = need if need is not None else 8.0
    today_bits = []
    if dur is not None:
        today_bits.append(_fmt_hm(dur))
    if score is not None:
        today_bits.append(f"score {score:.0f} ({_score_label(score)})")
    delta = _signed(round((dur - need_val) * 60), suffix=" min") if dur is not None else "—"
    if dur is not None and dur < need_val - 0.5:
        flag = "⚠ short"
    elif score is not None and score < 80:
        flag = "⚠ long but light" if (dur is not None and dur >= need_val) else "⚠ light"
    else:
        flag = "✓"
    return Row(cells=[
        "Sleep",
        ", ".join(today_bits),
        f"need {_fmt_hm(need_val)}" + ("" if need is not None else " (default)"),
        delta,
        flag,
    ])


def _deep_sleep_row(store: Any, d: str) -> Row | None:
    series = store.get_series("sleep_deep_h", d, 28)
    today = _at(series, d)
    base = baselines.baseline(_prior(series, d), d, 28)
    if today is None or base is None:
        return None
    med = base["median"]
    lo_h = max(0.0, med - base["mad_sd"])
    hi_h = med + base["mad_sd"]
    delta = _signed((today - med) / med * 100, suffix="%") if med * 60 >= 1 else _signed(
        (today - med) * 60, suffix=" min"
    )
    low_run = baselines.streak(series, lambda v: v < lo_h)
    if low_run >= 2:
        flag = f"⚠ {_ordinal(low_run)} low night"
    elif low_run == 1:
        flag = "⚠ low"
    else:
        flag = "✓"
    return Row(cells=[
        "Deep sleep",
        f"{today * 60:.0f} min",
        f"typical {lo_h * 60:.0f}–{hi_h * 60:.0f} (n={base['n']} nights)",
        delta,
        flag,
    ])


def _body_battery_row(store: Any, d: str) -> Row | None:
    series = store.get_series("body_battery_wake", d, 30)
    today = _at(series, d)
    base = baselines.baseline(_prior(series, d), d, 30)
    if today is None or base is None:
        return None
    pos = baselines.band_position(today, base)
    flag = {"low": "⚠ low", "very_low": "⚠ very low"}.get(pos, "✓")
    n_txt = f", n={base['n']}" if base["n"] < 30 else ""
    return Row(cells=[
        "Body Battery at wake",
        f"{today:.0f}",
        f"30d wake avg {base['mean']:.0f}{n_txt}",
        _signed(today - base["mean"]),
        flag,
    ])


def _form_row(store: Any, d: str) -> Row | None:
    rows = store.get_pmc(d, 60)
    today_row = next((r for r in rows if r["date"] == d), None)
    if today_row is None:
        return None
    ctl_series = [(r["date"], r["ctl"]) for r in rows]
    fa = pmc_engine.form_assessment(today_row["ctl"], today_row["tsb"], ctl_series)
    if fa["form_pct"] is None:
        return None
    band = fa["form_band"]
    flag = "✓" if band in _OK_FORM_BANDS else f"⚠ {band}"
    return Row(cells=[
        "Form (TSB/CTL)",
        _signed(fa["form_pct"], suffix="%"),
        "productive −10…−30%",
        "—",
        flag,
    ])


# ---------------------------------------------------------------------------
# verdict + context lines
# ---------------------------------------------------------------------------

def _verdict_text(gated: dict[str, Any]) -> str:
    verdict = gated.get("verdict", "GREEN")
    provisional = bool(gated.get("provisional"))
    if verdict == "GREEN":
        core = "leaning GREEN" if provisional else "GREEN — cleared for quality"
    else:
        mod = gated.get("modification") or _DEFAULT_MOD.get(
            verdict, "train easy; reassess tomorrow"
        )
        lead = f"leaning {verdict}" if provisional else verdict
        core = f"{lead} — {mod}"
    if not core.endswith("."):
        core += "."
    used = list(gated.get("markers_used") or [])
    names = [_MARKER_NAMES[m] for m in _MARKER_ORDER if m in used]
    names += [m for m in used if m not in _MARKER_NAMES]
    if names:
        core += f" Markers used: {', '.join(names)}."
    if provisional:
        pn = gated.get("provisional_n")
        n_txt = f"n={pn[0]} of {pn[1]} days" if pn else "limited history"
        core = f"PROVISIONAL ({n_txt}) — {core}"
    return core


def _gate_logs(store: Any, d: str) -> list[dict[str, Any]]:
    """Same-day + previous-day log entries plus unresolved injuries (any date)."""
    prev = (_date.fromisoformat(d) - timedelta(days=1)).isoformat()
    entries = {e["id"]: e for e in store.logs_for(prev)}
    entries.update({e["id"]: e for e in store.logs_for(d)})
    for e in store.unresolved_injuries():
        entries.setdefault(e["id"], e)
    return [entries[k] for k in sorted(entries)]


def _yesterday_line(store: Any, d: str) -> tuple[str, int | None]:
    y = (_date.fromisoformat(d) - timedelta(days=1)).isoformat()
    acts = store.list_activities(y, y)
    if not acts:
        return "Yesterday: rest day.", None
    a = max(acts, key=lambda r: r.get("duration_s") or 0)
    label = _SPORT_LABELS.get(a["sport"], a["sport"].replace("_", " ").capitalize())
    parts = [label + (f" {a['distance_m'] / 1000:.1f} km" if a.get("distance_m") else "")]
    if a.get("duration_s"):
        parts.append(_fmt_duration(a["duration_s"]))
    if a.get("avg_hr"):
        parts.append(f"HR {a['avg_hr']:.0f}")
    if a.get("load") is not None:
        parts.append(f"load {a['load']:.0f}")
    if a.get("rpe") is not None:
        parts.append(f"RPE {a['rpe']:.0f}/10")
    line = "Yesterday: " + " · ".join(parts) + f" (id {a['activity_id']})."
    if len(acts) > 1:
        line = line[:-1] + f"; +{len(acts) - 1} more."
    return line, int(a["activity_id"])


def _plan_line(store: Any, d: str) -> str:
    plans = store.plan_entries(d, d)
    if plans:
        items = []
        for p in plans:
            name = p.get("name") or p.get("sport") or "workout"
            src = _PLAN_SOURCES.get(p.get("source"), p.get("source"))
            items.append(f"{name} — {src}")
        plan_txt = "Today's plan: " + "; ".join(items) + "."
    else:
        plan_txt = "Today's plan: nothing on the Garmin calendar."
    return f"{plan_txt} {_goal_line(store.get_profile())}"


def _goal_line(profile: dict[str, str]) -> str:
    dist = profile.get("goal_distance")
    if dist == "custom" and profile.get("goal_custom_km"):
        dist = f"{profile['goal_custom_km']} km"
    elif dist == "half":
        dist = "half marathon"
    goal_part = None
    if dist or profile.get("goal_race_date"):
        goal_part = f"Goal: {dist}" if dist else "Goal: race"
        if profile.get("goal_race_date"):
            goal_part += f" on {format_date(profile['goal_race_date'])}"
        if profile.get("goal_time"):
            goal_part += f" in {profile['goal_time']}"
    phase = profile.get("phase")
    phase_part = None
    if phase and phase != "none":
        phase_part = phase
        if profile.get("phase_week") and profile.get("phase_total_weeks"):
            phase_part += f" wk {profile['phase_week']}/{profile['phase_total_weeks']}"
    if goal_part and phase_part:
        return f"{goal_part} — {phase_part}."
    if goal_part:
        return f"{goal_part}."
    if phase_part:
        return f"Phase: {phase_part}."
    return "No goal-race phase on file."


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

async def run(ctx: Any, date: str | None = None) -> str:
    await ctx.ensure_ready()
    await ctx.ensure_fresh_today()
    today = ctx.today()

    def _err(msg: str) -> str:
        b = ctx.banner()
        return f"{b}\n\n{msg}" if b else msg

    d = date or today
    if date is not None:
        yesterday = (_date.fromisoformat(today) - timedelta(days=1)).isoformat()
        if not _DATE_RE.match(date):
            return _err(
                f"date must be YYYY-MM-DD (got '{date}'). Today is {format_date(today)}. "
                f"Example: garmin_brief(date='{yesterday}')"
            )
        try:
            parsed = _date.fromisoformat(date)
        except ValueError:
            return _err(
                f"date must be a real calendar date (got '{date}'). Today is "
                f"{format_date(today)}. Example: garmin_brief(date='{yesterday}')"
            )
        if parsed > _date.fromisoformat(today):
            return _err(
                f"date {date} is in the future. Today is {format_date(today)}. "
                f"Example: garmin_brief() for today, garmin_brief(date='{yesterday}') "
                f"for yesterday"
            )

    store = ctx.store
    inputs = fusion.marker_inputs(store, d)
    readiness = fusion.compute_readiness(inputs)
    gated = fusion.apply_gates(readiness, _gate_logs(store, d), inputs)

    rows = [
        r
        for r in (
            _hrv_row(store, d),
            _rhr_row(store, d),
            _sleep_row(store, d),
            _deep_sleep_row(store, d),
            _body_battery_row(store, d),
            _form_row(store, d),
        )
        if r is not None
    ]

    sections: list[Section] = []
    if rows:
        sections.append(
            Section(
                title=None,
                header=["Signal", "Today", "Your baseline", "Δ", "Flag"],
                rows=rows,
                priority="primary",
            )
        )
    y_line, y_id = _yesterday_line(store, d)
    sections.append(
        Section(
            title=None,
            header=None,
            prose=f"{y_line}\n{_plan_line(store, d)}",
            priority="primary",
        )
    )
    if getattr(ctx, "cold_started", False):
        sections.append(Section(title=None, header=None, prose=_COLD_START_NOTE, priority="secondary"))

    watch = [a["message"] for a in store.active_alerts() if a["severity"] == "WATCH"]

    next_steps = []
    if y_id is not None:
        next_steps.append(f"garmin_activity(activity_id={y_id})")
    next_steps.append("garmin_activities()")

    report = Report(
        title="Daily Brief",
        date=d,
        data_as_of=ctx.data_as_of(),
        verdict=_verdict_text(gated),
        provisional=bool(gated.get("provisional")),
        banner=ctx.banner(),
        sections=sections,
        watch_list=watch,
        next_steps=next_steps,
    )
    return render(report, CAP)
