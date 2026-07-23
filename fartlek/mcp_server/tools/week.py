"""garmin_week — one week in session-level detail (DESIGN §2.4, budget 900 /
cap 1,200).

The coach's Monday-morning or Sunday-night question: "how was my week."
Owns the ONLY view that lists every session of a specific week next to its
activity_id, so the model can drill into any one of them with
garmin_activity(activity_id=...) without having to call garmin_activities()
first and hunt for the right date. Every session row therefore carries its
id and is built undroppable — the renderer's budget trimmer must never
silently orphan an id the model was just handed.

Design choices worth stating:

- The week is always resolved to the Mon-Sun ISO week CONTAINING the anchor
  date, never a trailing 7-day window ending at the anchor — "my week" means
  the calendar week, and a mid-week anchor (e.g. a Wednesday check-in) must
  not quietly shift Monday to three days ago. A week that contains today (or
  is wholly in the future) is disclosed as incomplete in the title itself,
  never rendered as if it had already happened.
- Compliance renders only when a planned workout actually exists in the
  window (DESIGN §2.4): an empty compliance table for a week with no plan is
  worse than no section, since it invites the model to read "no rows" as
  "nothing was planned or wanted" rather than "nothing was on file". The
  matched/missed state is read straight off plan_calendar (matched at sync
  time by analytics.matcher, the same persisted result garmin_activity
  reads) rather than re-run here, so the two tools can never disagree.
- Zone floors are not persisted yet (a known gap, tracked outside this
  module), so the 3-zone distribution line calls analytics.tid.distribution
  WITHOUT zone_floors/lt1/lt2 and gets the whole-bucket containment fallback
  (Z1+Z2 easy, Z3 moderate, Z4+Z5 hard) — that approximation is disclosed in
  the section's method_note rather than silently presented as precise.
- The per-day "Note" column shows splits-based decoupling when the session
  qualifies as steady (analytics.efficiency, reusing its own qualifier) and
  a dash otherwise. Per-rep interval fade prose ("reps 5-6 faded -4%") is
  deliberately NOT fabricated here: no shipped engine function derives it
  from stored splits, and inventing a threshold for it would be exactly the
  kind of un-sourced number this project exists to avoid. garmin_activity's
  splits/full detail is where that lives once it does.
"""
from __future__ import annotations

import re
from datetime import date as _date
from datetime import timedelta
from statistics import fmean
from typing import Any

from fartlek.analytics import baselines, convergence, efficiency, tid
from fartlek.analytics import pmc as pmc_engine
from fartlek.analytics import sleep as sleep_engine
from fartlek.analytics.matcher import sport_family
from fartlek.mcp_server.tools import _zones
from fartlek.render.renderer import Report, Row, Section, format_date, render

CAP = 1200

LOAD_WINDOW = 90            # trailing days of daily load behind ACWR/monotony/ramp
NORM_WINDOW_DAYS = 84       # 12-week TID norm the week's distribution is judged against
ACWR_BAND = (0.8, 1.3)      # population band (DESIGN §3.2 #3) — weak signal, disclosed as such
RHR_WEEK_FLAG = 3.0         # same caution threshold as the RHR deviation model (§3.2 #9)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_TITLE = {
    "running": "Run", "cycling": "Ride", "swimming": "Swim",
    "strength": "Strength", "walking": "Walk", "hiking": "Hike",
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def _wd(d: _date) -> str:
    return _WEEKDAYS[d.weekday()]


def _short(date_str: str) -> str:
    """'2026-07-16' -> 'Thu 07-16'."""
    d = _date.fromisoformat(date_str)
    return f"{_wd(d)} {date_str[5:]}"


def _fmt_hm(seconds: float | None) -> str:
    """Weekly totals always render as 'XhMM' — a week's volume is essentially
    never under an hour, and this keeps one convention for the whole table."""
    total_min = round((seconds or 0.0) / 60.0)
    return f"{total_min // 60}h{total_min % 60:02d}"


def _fmt_duration(seconds: float | None) -> str:
    """mm:ss under an hour, 'XhMM' at/above — '5h00' cannot be misread as
    five minutes the way '5:00' can."""
    if seconds is None:
        return "—"
    s = int(round(float(seconds)))
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def _signed(n: float, decimals: int = 0, suffix: str = "") -> str:
    txt = f"{abs(n):.{decimals}f}"
    if float(txt) == 0:
        return f"0{suffix}"
    return ("+" if n > 0 else "−") + txt + suffix


def _with_banner(ctx: Any, text: str) -> str:
    banner = ctx.banner()
    return f"{banner}\n\n{text}" if banner else text


def _dates(end: str, days: int) -> list[str]:
    end_d = _date.fromisoformat(end)
    return [(end_d - timedelta(days=i)).isoformat() for i in range(days)]


def _resolve_week(anchor: str) -> tuple[_date, _date]:
    """anchor -> (Monday, Sunday) of the ISO week containing it."""
    d = _date.fromisoformat(anchor)
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


# ---------------------------------------------------------------------------
# load table
# ---------------------------------------------------------------------------

def _daily_loads(store: Any, end: str, days: int) -> list[tuple[str, float]]:
    """Contiguous (date, daily_load) ascending, missing days defaulted to 0 —
    the PMC/ACWR/monotony engines require a gapless series (DESIGN §3.2 #1)."""
    return [(d, float((store.get_day(d) or {}).get("daily_load") or 0.0))
            for d in reversed(_dates(end, days))]


def _totals(acts: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "dist_m": sum(a.get("distance_m") or 0.0 for a in acts),
        "dur_s": sum(a.get("duration_s") or 0.0 for a in acts),
    }


def _form_at(pmc_rows: list[dict[str, Any]], end_s: str) -> dict[str, Any]:
    row = next((r for r in pmc_rows if r["date"] == end_s), None)
    if row is None:
        return {}
    ctl_series = [(r["date"], r["ctl"]) for r in pmc_rows]
    return pmc_engine.form_assessment(row["ctl"], row["tsb"], ctl_series)


def _load_rows(store: Any, start: _date, end: _date) -> tuple[list[Row], dict[str, Any]]:
    end_s = end.isoformat()
    loads = _daily_loads(store, end_s, LOAD_WINDOW)
    loads_prev = loads[:-7]

    mono = pmc_engine.monotony_strain(loads)
    mono_prev = pmc_engine.monotony_strain(loads_prev) if len(loads_prev) >= 7 else {}
    acwr = pmc_engine.acwr_ewma(loads)
    acwr_prev = pmc_engine.acwr_ewma(loads_prev) if loads_prev else {}

    pmc_rows = store.get_pmc(end_s, 60)
    fa = _form_at(pmc_rows, end_s)
    prev_end_s = (end - timedelta(days=7)).isoformat()
    fa_prev = _form_at([r for r in pmc_rows if r["date"] <= prev_end_s], prev_end_s)

    this_load = sum(v for _, v in loads[-7:])
    prev_load = sum(v for _, v in loads[-14:-7])
    four_load = sum(v for _, v in loads[-35:-7]) / 4.0

    hist_start = (start - timedelta(days=28)).isoformat()
    hist_end = (start - timedelta(days=1)).isoformat()
    hist_acts = store.list_activities(hist_start, hist_end)
    prev_wk_start = (start - timedelta(days=7)).isoformat()
    prev_acts = [a for a in hist_acts if a["date"] >= prev_wk_start]
    this_acts = store.list_activities(start.isoformat(), end_s)
    this_tot, prev_tot, four_tot = _totals(this_acts), _totals(prev_acts), _totals(hist_acts)

    rows = [Row([
        "Volume",
        f"{round(this_tot['dist_m'] / 1000)} km / {_fmt_hm(this_tot['dur_s'])}",
        f"{round(prev_tot['dist_m'] / 1000)} km",
        f"{round(four_tot['dist_m'] / 1000 / 4)} km",
        "✓",
    ])]

    load_delta = ((this_load - four_load) / four_load * 100) if four_load > 1e-9 else None
    load_flag = "✓"
    if load_delta is not None:
        load_flag = (f"⚠ {_signed(load_delta, 0, '%')}" if abs(load_delta) > 15
                     else f"✓ {_signed(load_delta, 0, '%')}")
    rows.append(Row([
        "Load (Garmin)",
        f"{round(this_load)}",
        f"{round(prev_load)}",
        f"{round(four_load)}",
        load_flag,
    ]))

    if fa.get("ramp_pct_per_wk") is not None:
        rows.append(Row([
            "Ramp",
            f"{_signed(fa['ramp_pct_per_wk'], 1)}%/wk of CTL",
            (f"{_signed(fa_prev['ramp_pct_per_wk'], 1)}%/wk"
             if fa_prev.get("ramp_pct_per_wk") is not None else "—"),
            "sustainable 4–8%",
            "⚠ fast" if fa.get("ramp_flag") else "✓",
        ]))

    if acwr.get("acwr") is not None:
        lo, hi = ACWR_BAND
        rows.append(Row([
            "ACWR (EWMA)",
            f"{acwr['acwr']:.2f}",
            f"{acwr_prev['acwr']:.2f}" if acwr_prev.get("acwr") is not None else "—",
            f"{lo}–{hi} (population band, weak signal)",
            "✓" if lo <= acwr["acwr"] <= hi else f"⚠ {acwr['acwr']:.2f}",
        ]))

    if mono.get("monotony") is not None:
        rows.append(Row([
            "Monotony",
            f"{mono['monotony']:.2f}",
            f"{mono_prev['monotony']:.2f}" if mono_prev.get("monotony") is not None else "—",
            "flag > 2.0",
            "⚠ high" if mono.get("flag") else "✓",
        ]))

    return rows, {"fa": fa, "mono": mono, "acwr": acwr}


# ---------------------------------------------------------------------------
# distribution (TID) — approximate until zone floors are persisted
# ---------------------------------------------------------------------------

def _distribution(store: Any, start: _date, end: _date) -> tuple[Section | None, dict[str, Any] | None]:
    end_s = end.isoformat()
    zk, tid_note = _zones.resolve(store, end_s)
    week_acts = store.list_activities(start.isoformat(), end_s)
    week_dist = tid.distribution(week_acts, **zk)
    if week_dist["total"] <= 0:
        return None, None
    week_shares = tid.shares(week_dist)

    norm_start = (start - timedelta(days=NORM_WINDOW_DAYS)).isoformat()
    norm_end = (start - timedelta(days=1)).isoformat()
    norm_acts = store.list_activities(norm_start, norm_end)
    norm_shares = tid.shares(tid.distribution(norm_acts, **zk))

    e, m, h = week_shares
    line = f"Distribution (3-zone): {e * 100:.0f}/{m * 100:.0f}/{h * 100:.0f}% easy/moderate/hard by time"
    drift = None
    if norm_shares is not None:
        drift = tid.drift_vs_norm(week_shares, norm_shares)
        label = tid.classify(norm_shares)
        line += (f" — drifting from your {label} norm" if drift["drifted"]
                 else f" — consistent with your {label} norm")
    else:
        line += " — no 12-week norm yet to compare against"

    section = Section(
        title=None, header=None, prose=line, priority="secondary",
        method_note=tid_note,
    )
    return section, drift


# ---------------------------------------------------------------------------
# recovery summary
# ---------------------------------------------------------------------------

def _recovery(store: Any, start: _date, end: _date, today: _date) -> dict[str, Any]:
    end_s = end.isoformat()
    start_s = start.isoformat()
    # Trailing rolling windows (14d sleep debt, 7d SRI) must anchor at "now",
    # not at a future week-end: for an in-progress week, anchoring at Sunday
    # shifts the 14-night window past real nights and disagrees with the same
    # figure from garmin_recovery run the same day (E2-B). The week-scoped
    # HRV/RHR selections below stay bounded to the ISO week.
    trail_s = min(end, today).isoformat()
    out: dict[str, Any] = {"lines": [], "concern": False, "concern_reason": ""}

    hrv_series = store.get_series("hrv_last_night", end_s, 90)
    week_hrv = [(d, v) for d, v in hrv_series if start_s <= d <= end_s]
    if week_hrv:
        base = baselines.baseline(hrv_series, end_s, 90)
        if base:
            in_band = sum(1 for _, v in week_hrv
                          if baselines.band_position(v, base) == "in_band")
            out["lines"].append(f"HRV in band {in_band}/{len(week_hrv)}")
            if in_band < len(week_hrv):
                out["concern"] = True
                out["concern_reason"] = "HRV left band this week"

    rhr_series = store.get_series("resting_hr", end_s, 90)
    week_rhr = [v for d, v in rhr_series if start_s <= d <= end_s]
    if week_rhr:
        base_rhr = baselines.baseline(rhr_series, end_s, 30)
        avg = fmean(week_rhr)
        if base_rhr:
            delta = avg - base_rhr["median"]
            out["lines"].append(
                f"RHR flat {avg:.0f}" if abs(delta) < 2
                else f"RHR {avg:.0f} ({_signed(delta)} vs 30d median)"
            )
            if abs(delta) >= RHR_WEEK_FLAG and not out["concern"]:
                out["concern"] = True
                out["concern_reason"] = f"RHR {_signed(delta)} vs 30d median"

    day_rows = [store.get_day(d) or {} for d in _dates(trail_s, 14)]
    debt = sleep_engine.sleep_debt(day_rows, trail_s, window=14)
    if debt["debt_h"] is not None:
        rising = debt["debt_h"] > convergence.SLEEP_DEBT_H_14D
        out["lines"].append(
            f"sleep {debt['avg_actual_h']:.1f}h avg vs {debt['avg_need_h']:.1f}h need "
            f"→ 14d debt {debt['debt_h']:.1f}h" + (" ⚠ rising" if rising else "")
        )
        if rising and not out["concern"]:
            out["concern"] = True
            out["concern_reason"] = "sleep debt rising"

    timeline = store.get_sleep_timeline(trail_s, days_back=7)
    sri = sleep_engine.sleep_regularity_index(timeline, trail_s, days=7)
    if not sri["suppressed"]:
        out["lines"].append(f"regularity {sri['sri']:.0f}/100")

    return out


# ---------------------------------------------------------------------------
# per-day session table
# ---------------------------------------------------------------------------

def _session_label(a: dict[str, Any]) -> str:
    fam = a.get("_family") or sport_family(a.get("sport") or "")
    title = _TITLE.get(fam) or (a.get("sport") or "Activity").replace("_", " ").title()
    name = str(a.get("name") or "").strip()
    if name and name.lower() != title.lower():
        body = name
    elif fam == "strength":
        body = _fmt_duration(a.get("duration_s")) if a.get("duration_s") else ""
    else:
        dist = a.get("distance_m")
        body = f"{dist / 1000:.1f} km" if dist else ""
    return f"{title} {body}".strip() if body else title


def _note(store: Any, activity_id: int) -> str:
    """Splits-based decoupling when the session qualifies as steady — the one
    per-session insight backed by a shipped engine function without a live
    fetch; everything else renders as a dash rather than an invented label."""
    laps = store.get_activity_laps(activity_id)
    if not laps:
        return "—"
    eff = efficiency.session_efficiency(laps)
    if eff.get("steady") and eff.get("decoupling") is not None:
        return f"decoupling {eff['decoupling'] * 100:.1f}% ({eff['method']}-based)"
    return "—"


def _day_rows(store: Any, acts: list[dict[str, Any]]) -> list[Row]:
    return [
        Row(
            cells=[
                _short(a["date"]),
                f"{_session_label(a)} ({a['activity_id']})",
                f"{round(a['load'])}" if a.get("load") is not None else "—",
                _note(store, a["activity_id"]),
            ],
            undroppable=True,  # carries an activity_id the model may drill into
        )
        for a in acts
    ]


def _rest_line(
    start: _date, end: _date, acts: list[dict[str, Any]], today: _date | None = None
) -> str | None:
    """Days with no session, split into rest already taken and days still to
    come.

    A day in the future is not a rest day — it has not happened. Reporting
    "Thu/Fri/Sat/Sun: rest" on a Wednesday states four things that are not yet
    true, and would have the model congratulate or scold an athlete for a
    week they have not trained.
    """
    active = {a["date"] for a in acts}
    rest, upcoming = [], []
    d = start
    while d <= end:
        if d.isoformat() not in active:
            (upcoming if (today is not None and d > today) else rest).append(_wd(d))
        d += timedelta(days=1)
    parts = []
    if rest:
        parts.append("/".join(rest) + ": rest")
    if upcoming:
        parts.append("/".join(upcoming) + ": still to come")
    return " · ".join(parts) + "." if parts else None


# ---------------------------------------------------------------------------
# plan compliance — omitted entirely when no plan exists (DESIGN §2.4)
# ---------------------------------------------------------------------------

def _compliance_section(
    store: Any,
    acts_by_id: dict[int, dict[str, Any]],
    start: _date,
    end: _date,
    today_d: _date,
) -> Section | None:
    plans = store.plan_entries(start.isoformat(), end.isoformat())
    if not plans:
        return None
    rows = []
    for p in sorted(plans, key=lambda p: (p["date"], p.get("id") or 0)):
        name = p.get("name") or p.get("sport") or "workout"
        matched_id = p.get("matched_activity_id")
        if matched_id is not None:
            act = acts_by_id.get(matched_id)
            label = _session_label(act) if act is not None else f"activity {matched_id}"
            status = f"matched — {label} (id {matched_id})"
        elif _date.fromisoformat(p["date"]) < today_d:
            status = "missed"
        else:
            status = "pending"
        rows.append(Row([_short(p["date"]), str(name), status]))
    return Section(title=None, header=["Day", "Planned", "Status"], rows=rows, priority="primary")


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

def _verdict(load_info: dict[str, Any], drift: dict[str, Any] | None, recovery: dict[str, Any]) -> str:
    fa, mono, acwr = load_info["fa"], load_info["mono"], load_info["acwr"]
    bits: list[str] = []
    if fa.get("ramp_flag"):
        bits.append(f"load ramping fast ({_signed(fa['ramp_pct_per_wk'], 1)}%/wk, "
                    f"above the sustainable 4–8% band)")
    elif mono.get("flag"):
        bits.append(f"monotony high ({mono['monotony']:.2f}, flag > 2.0)")
    elif acwr.get("acwr") is not None and not (ACWR_BAND[0] <= acwr["acwr"] <= ACWR_BAND[1]):
        bits.append(f"ACWR {acwr['acwr']:.2f} outside the typical "
                    f"{ACWR_BAND[0]}–{ACWR_BAND[1]} band")
    else:
        bits.append("a good, absorbable week")

    if drift is not None:
        bits.append("distribution drifting from your norm" if drift["drifted"]
                    else "distribution on your norm")

    bits.append("recovery held" if not recovery["concern"]
                else f"recovery strained — {recovery['concern_reason']}")
    return ", ".join(bits) + "."


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

async def run(ctx: Any, anchor_date: str | None = None) -> str:
    today = ctx.today()
    if anchor_date is not None:
        if not _DATE_RE.match(anchor_date):
            return _with_banner(
                ctx,
                f"anchor_date must be YYYY-MM-DD (got {anchor_date!r}). Today is "
                f"{format_date(today)}. Example: garmin_week(anchor_date='{today}')",
            )
        try:
            _date.fromisoformat(anchor_date)
        except ValueError:
            return _with_banner(
                ctx,
                f"anchor_date must be a real calendar date in YYYY-MM-DD format "
                f"(got {anchor_date!r}). Today is {format_date(today)}. "
                f"Example: garmin_week(anchor_date='{today}')",
            )

    await ctx.ensure_ready()
    store = ctx.store
    start, end = _resolve_week(anchor_date or today)
    today_d = _date.fromisoformat(today)

    if today_d < start:
        completeness = "upcoming"
    elif end >= today_d:
        completeness = "in progress"
    else:
        completeness = "complete"

    phase = store.get_profile().get("phase") or "none"
    title = (f"Week {format_date(start.isoformat())} → {format_date(end.isoformat())} "
             f"({completeness}) · phase on file: {phase}")

    load_rows, load_info = _load_rows(store, start, end)
    sections: list[Section] = [
        Section(title=None, header=["Load", "This wk", "Prev", "4-wk avg", "Flag"],
                rows=load_rows, priority="primary")
    ]

    dist_section, drift = _distribution(store, start, end)
    if dist_section is not None:
        sections.append(dist_section)

    recovery = _recovery(store, start, end, today_d)
    if recovery["lines"]:
        sections.append(Section(
            title=None, header=None,
            prose="Recovery: " + " · ".join(recovery["lines"]),
            priority="secondary",
        ))

    acts = store.list_activities(start.isoformat(), end.isoformat())
    for a in acts:
        a["_family"] = sport_family(a["sport"])
    acts.sort(key=lambda a: (a["date"], a.get("start_local") or "", a["activity_id"]))

    if acts:
        sections.append(Section(
            title=None, header=["Day", "Session (id)", "Load", "Note"],
            rows=_day_rows(store, acts), priority="primary",
        ))
        rest_line = _rest_line(start, end, acts, today_d)
        if rest_line:
            sections.append(Section(title=None, header=None, prose=rest_line, priority="secondary"))
    else:
        sections.append(Section(
            title=None, header=None,
            prose=("No sessions logged this week yet." if end >= today_d
                   else "No sessions logged this week — full rest."),
            priority="primary",
        ))

    acts_by_id = {a["activity_id"]: a for a in acts}
    compliance = _compliance_section(store, acts_by_id, start, end, today_d)
    if compliance is not None:
        sections.append(compliance)

    verdict = _verdict(load_info, drift, recovery)
    watch = [a["message"] for a in store.active_alerts() if a["severity"] == "WATCH"]

    next_steps: list[str] = []
    hardest = max(acts, key=lambda a: a.get("load") or 0.0) if acts else None
    if hardest is not None:
        next_steps.append(f"garmin_activity(activity_id={hardest['activity_id']}) for that session")
    next_steps.append("garmin_recovery(days=14) for the sleep/HRV trend")
    if not acts:
        next_steps.append("garmin_activities() for the surrounding weeks")

    report = Report(
        title=title,
        date=end.isoformat(),
        data_as_of=ctx.data_as_of(),
        verdict=verdict,
        banner=ctx.banner(),
        sections=sections,
        watch_list=watch,
        next_steps=next_steps,
    )
    return render(report, CAP)
