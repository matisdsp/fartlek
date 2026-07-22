"""garmin_load — multi-week training dose (DESIGN §2.4, budget 800 / cap 1100).

Owns the load-STRUCTURE side of "am I training right": fitness/fatigue/form
trajectory, ramp rate, ACWR, monotony/strain history, and intensity-
distribution drift against the athlete's own norm. garmin_recovery owns the
*physiology* side of overtraining (HRV/RHR/sleep convergence) — this tool
cross-references it rather than re-deriving a verdict from sensors it does
not own.

All maths is pulled from analytics.pmc/tid/precedent/trends/baselines; this
module's job is choosing which numbers apply to a given window and phrasing
them, never re-deriving a formula that already has a tested home.

Unlike recovery/week/brief, the §2.4 example for this tool has no natural
per-row entity (no one marker, no one session) — it is prose throughout. The
primary section is therefore a single header=None block, trimmed as a whole
under the cap rather than row-by-row.

ACWR is never allowed to stand alone: every line that mentions it carries the
"contested spike detector, not a verdict" caveat (§3.2 #3), in both the
reliable and not-yet-reliable branches.

TID drift is judged against the athlete's OWN trailing 12-week distribution,
never a population 80/20 template — an ultra athlete's near-all-easy block is
the normal state of a base phase, not an error (see analytics.tid docstring).
Zone-boundary data (LT1/LT2, zone floors) is not persisted yet, so
tid.distribution is called with no mapping kwargs, which falls back to
whole-bucket containment; that approximation is disclosed via this section's
method_note rather than presented as exact.
"""
from __future__ import annotations

import re
from datetime import date as _date
from datetime import timedelta
from typing import Any

from fartlek.analytics import baselines, precedent, tid, trends
from fartlek.analytics import pmc as pmc_engine
from fartlek.render.renderer import Report, Section, arrow_series, render

CAP = 1100
DEFAULT_WEEKS = 8
MIN_WEEKS, MAX_WEEKS = 2, 52
NORM_WEEKS = 12          # the athlete's OWN norm window — never a population template
RECENT_WEEKS = 2         # "recent" snapshot compared against that norm
FULL_HISTORY_DAYS = 100_000  # store.get_pmc caps to actual rows anyway — "all of it"
_MONOTONY_LOOKBACK_DAYS = 90  # >= 12 wk, matches monotony_strain's own percentile cap

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_ACWR_BAND = (0.8, 1.3)

_CURRENCY_LABEL = {
    "garmin": "Garmin activity load",
    "trimp_calibrated": "calibrated TRIMP",
    "trimp_uncalibrated": "uncalibrated TRIMP",
    "srpe_calibrated": "calibrated sRPE",
    "srpe_uncalibrated": "uncalibrated sRPE",
    "estimated": "duration-estimated",
    "none": "none",
}

_TID_LABEL = {
    "base": "base — almost entirely easy, the normal state of a base/ultra block",
    "polarized": "polarized",
    "pyramidal": "pyramidal",
    "threshold": "threshold-heavy",
    "unknown": "not enough zone data yet",
}


def _ordinal(value: float) -> str:
    """83 → '83rd'. Read aloud by the model; '83th' reads as a typo."""
    n = int(round(value))
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---------------------------------------------------------------------------
# CTL/ATL/form/ramp
# ---------------------------------------------------------------------------

def _fitness_lines(
    all_pmc: list[dict[str, Any]],
    tail: list[dict[str, Any]],
    n_weeks_avail: int,
    weeks: int,
) -> tuple[list[str], dict[str, Any]]:
    """CTL/ATL/form/ramp straight from pmc.form_assessment — this function
    only picks the window and phrases the bands the engine already named."""
    last = all_pmc[-1]
    ctl_now, tsb_now = last["ctl"], last["tsb"]
    ctl_series = [(r["date"], r["ctl"]) for r in all_pmc]
    form = pmc_engine.form_assessment(ctl_now, tsb_now, ctl_series)

    if tail and tail[0]["ctl"] >= 1:
        ctl_first = tail[0]["ctl"]
        pct = (ctl_now - ctl_first) / ctl_first * 100.0
        per_wk = pct / n_weeks_avail if n_weeks_avail else None
        fitness = (
            f"Fitness (CTL): {ctl_first:.0f} → {ctl_now:.0f} "
            f"({pct:+.0f}% over {n_weeks_avail} wk"
            + (f", avg {per_wk:+.1f}%/wk" if per_wk is not None else "")
            + ")"
        )
    else:
        fitness = f"Fitness (CTL): {ctl_now:.0f}"
    if n_weeks_avail < weeks:
        fitness += f" — only {n_weeks_avail} of the requested {weeks} weeks on file"

    lines = [fitness, f"Fatigue (ATL): {last['atl']:.0f}"]
    if form["form_pct"] is not None:
        lines.append(f"Form (TSB/CTL): {form['form_pct']:+.0f}% of CTL — {form['form_band']}")
    if form["ramp_pct_per_wk"] is not None:
        ramp = form["ramp_pct_per_wk"]
        # Sustainable band (4-8%/wk) is the DESIGN's own named range; the
        # engine's ramp_flag (>10%) is the one authoritative trigger — this
        # only narrates where the number sits relative to both.
        if ramp < 4:
            state = "below the typical 4–8%/wk build pace"
        elif ramp <= 8:
            state = "sustainable"
        elif not form["ramp_flag"]:
            state = "brisk, above the typical 4–8%/wk band but not flagged"
        else:
            state = "exceeds the sustainable band"
        lines.append(f"Ramp: {ramp:+.1f}%/wk of CTL — {state}")
    return lines, form


# ---------------------------------------------------------------------------
# ACWR — never rendered without its caveat
# ---------------------------------------------------------------------------

def _acwr_line(acwr: dict[str, Any]) -> str | None:
    """§3.2 #3: ACWR renders ONLY with the caveat attached, in every branch —
    this project never lets the ratio stand alone as if it were a verdict."""
    caveat = "a contested spike detector, not a verdict"
    if acwr.get("acute") is None:
        return None
    if acwr.get("acwr") is None:
        return f"ACWR: not yet reliable ({acwr['reason']}) — ACWR is {caveat}."
    val = acwr["acwr"]
    lo, hi = _ACWR_BAND
    where = "in" if lo <= val <= hi else ("above" if val > hi else "below")
    return (
        f"ACWR (EWMA 7:28): {val:.2f} — {where} the weak-evidence "
        f"{lo:.1f}–{hi:.1f} population band. ACWR is {caveat}."
    )


# ---------------------------------------------------------------------------
# monotony / strain history
# ---------------------------------------------------------------------------

def _monotony_summary(
    all_pmc: list[dict[str, Any]],
    weekly_blocks: list[list[dict[str, Any]]],
    start_pos: int,
) -> tuple[str | None, bool]:
    """Per-week monotony/strain by re-running monotony_strain on the prefix
    ending at each displayed week's last day — reusing its own trailing-week
    and percentile logic rather than maintaining a parallel history calc.
    Bounded to a 91-day prefix per week: monotony_strain itself caps its
    percentile lookback at 12 weeks, so more history than that changes
    nothing but the cost of _assert_contiguous."""
    monotony_vals: list[float] = []
    flagged = False
    strain_candidates: list[tuple[str, float, float | None]] = []
    for b, block in enumerate(weekly_blocks):
        end_idx = start_pos + (b + 1) * 7 - 1
        bound_start = max(0, end_idx - _MONOTONY_LOOKBACK_DAYS)
        window = [(r["date"], r["load"]) for r in all_pmc[bound_start:end_idx + 1]]
        ms = pmc_engine.monotony_strain(window)
        if ms["monotony"] is not None:
            monotony_vals.append(ms["monotony"])
            flagged = flagged or ms["flag"]
        if ms["strain"] is not None:
            strain_candidates.append((block[-1]["date"], ms["strain"], ms["strain_percentile"]))

    if not monotony_vals:
        return None, False

    lo, hi = min(monotony_vals), max(monotony_vals)
    span = f"{lo:.1f}" if hi - lo < 0.05 else f"{lo:.1f}–{hi:.1f}"
    line = f"Monotony {span} across the window" + (
        " — spiked above 2.0" if flagged else " — no spike"
    )
    if strain_candidates:
        peak_date, _peak_strain, peak_pctile = max(strain_candidates, key=lambda t: t[1])
        if peak_pctile is not None:
            line += (
                f" · strain peak week of {peak_date} "
                f"({_ordinal(peak_pctile)} pctile of your recent weeks)"
            )
    return line, flagged


# ---------------------------------------------------------------------------
# TID drift vs the athlete's own norm
# ---------------------------------------------------------------------------

def _tid_section(
    store: Any, end: str, weeks: int
) -> tuple[str | None, str | None, str | None]:
    end_d = _date.fromisoformat(end)
    norm_start = (end_d - timedelta(days=NORM_WEEKS * 7 - 1)).isoformat()
    norm_shares = tid.shares(tid.distribution(store.list_activities(norm_start, end)))
    if norm_shares is None:
        return None, None, None
    norm_class = tid.classify(norm_shares)

    recent_start = (end_d - timedelta(days=RECENT_WEEKS * 7 - 1)).isoformat()
    recent_shares = tid.shares(tid.distribution(store.list_activities(recent_start, end)))
    drift = tid.drift_vs_norm(recent_shares, norm_shares)

    report_start = (end_d - timedelta(days=weeks * 7 - 1)).isoformat()
    creep = tid.grey_zone_creep(tid.weekly_mid_shares(store.list_activities(report_start, end)))

    e, m, h = norm_shares
    label = _TID_LABEL.get(norm_class, norm_class)
    line = f"TID (own {NORM_WEEKS}-wk norm, {label}): {e:.0%}/{m:.0%}/{h:.0%} easy/moderate/hard"

    flag_kind: str | None = None
    if creep["creeping"]:
        line += (
            f". Grey-zone creep: mid-zone share {creep['from_share']:.0%} → "
            f"{creep['to_share']:.0%} over {creep['weeks']} wk straight"
        )
        flag_kind = "grey-zone creep"
    elif drift["drifted"] and recent_shares is not None:
        re_, rm_, rh_ = recent_shares
        line += (
            f". Recent {RECENT_WEEKS}wk: {re_:.0%}/{rm_:.0%}/{rh_:.0%} "
            "— drifted off your own norm"
        )
        flag_kind = "intensity distribution drifting from your own norm"
    else:
        line += " — on your own norm, no drift"

    note = (
        "zone splits approximated from Garmin's 5-zone buckets (Z1+Z2 easy / Z3 "
        "moderate / Z4+Z5 hard) since your LT1/LT2 boundaries aren't stored yet — "
        "drift direction is reliable even though the split itself is approximate"
    )
    return line, note, flag_kind


# ---------------------------------------------------------------------------
# personal precedent — the load-structure analogue of recovery's check
# ---------------------------------------------------------------------------

def _monotony_daily_series(daily_loads: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """One monotony value per day from its own trailing 7-day window — the
    input analytics.precedent needs to learn this athlete's own pre-episode
    monotony levels. Built by calling monotony_strain repeatedly rather than
    re-deriving mean/SD here."""
    out: list[tuple[str, float]] = []
    for i in range(6, len(daily_loads)):
        ms = pmc_engine.monotony_strain(daily_loads[i - 6:i + 1])
        if ms["monotony"] is not None:
            out.append((daily_loads[i][0], ms["monotony"]))
    return out


def _precedent_line(
    store: Any, daily_loads_full: list[tuple[str, float]]
) -> tuple[str | None, bool]:
    """The athlete's own pre-episode monotony level, mined from logged
    illness/injury (§3.2 #5) — externally-caused episodes are excluded from
    the trigger level, since their pre-episode load says nothing about this
    athlete's own tolerance (see analytics.precedent's module docstring for
    the salmonella case that motivated the exclusion)."""
    logs = store._all("SELECT date, flag, note FROM wellness_log ORDER BY date")
    episodes = precedent.episodes_from_log(logs)
    if not episodes:
        return None, False
    external = [
        r["date"] for r in logs if r.get("note") and "EXTERNAL" in str(r["note"]).upper()
    ]

    mono_series = _monotony_daily_series(daily_loads_full)
    if not mono_series:
        return None, False

    mined = precedent.mine(episodes, {"monotony": mono_series})
    levels = precedent.trigger_levels(mined, exclude=external)
    res = precedent.compare({"monotony": mono_series[-1][1]}, levels)
    if res["silent"] or not res["statements"]:
        return None, False

    n = res["n_precedents"]
    line = (
        f"Personal precedent ({n} episode{'s' if n > 1 else ''} on file): "
        + res["statements"][0]
    )
    return line, bool(res["exceeded"])


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

def _verdict_text(
    ramp_flag: bool | None, mono_flag: bool, tid_flag: str | None, precedent_exceeded: bool,
) -> str:
    issues: list[str] = []
    if ramp_flag:
        issues.append("ramp above the sustainable band")
    if mono_flag:
        issues.append("monotony spiked above 2.0 in the window")
    if tid_flag:
        issues.append(tid_flag)
    if precedent_exceeded:
        issues.append("load structure above your own pre-episode trigger level")
    if not issues:
        return "durable build, no structural flags this window."
    return "load has drift to manage: " + "; ".join(issues) + "."


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

async def run(ctx: Any, weeks: int = DEFAULT_WEEKS, anchor_date: str | None = None) -> str:
    if anchor_date is not None and not _DATE_RE.match(anchor_date):
        return (
            f"anchor_date must be YYYY-MM-DD (got {anchor_date!r}). "
            f"Today is {ctx.today()}. Example: garmin_load(weeks=8)"
        )
    if not MIN_WEEKS <= weeks <= MAX_WEEKS:
        return (
            f"weeks must be between {MIN_WEEKS} and {MAX_WEEKS} (got {weeks}). "
            f"Example: garmin_load(weeks={DEFAULT_WEEKS})"
        )

    await ctx.ensure_ready()
    end = anchor_date or ctx.today()
    store = ctx.store

    # PMC is already computed over full local history at sync time (a
    # contiguous, gap-filled rewrite — see sync.engine.recompute_derived);
    # this tool reads it rather than recomputing CTL/ATL from raw loads.
    all_pmc = store.get_pmc(end_date=end, days=FULL_HISTORY_DAYS)
    n = len(all_pmc)

    if n == 0:
        report = Report(
            title=f"Training Load — {weeks} weeks",
            date=end,
            data_as_of=ctx.data_as_of(),
            verdict="no training-load history on file yet",
            banner=ctx.banner(),
            sections=[],
            next_steps=["garmin_sync()", "garmin_brief() once a few days are in"],
        )
        return render(report, CAP)

    daily_loads_full = [(r["date"], float(r["load"])) for r in all_pmc]
    n_weeks_avail = min(weeks, n // 7)
    tail = all_pmc[-(n_weeks_avail * 7):] if n_weeks_avail else []
    weekly_blocks = [tail[i:i + 7] for i in range(0, len(tail), 7)]
    start_pos = n - len(tail)

    dose_lines, form = _fitness_lines(all_pmc, tail, n_weeks_avail, weeks)

    if weekly_blocks:
        dose_lines.append(
            "CTL weekly: " + arrow_series([b[-1]["ctl"] for b in weekly_blocks])
        )
        dose_lines.append(
            "Load weekly: "
            + arrow_series([sum(r["load"] for r in b) for b in weekly_blocks])
        )

    # Trend significance (Hamed-Rao MK + Sen slope) on the raw daily-load
    # series backs the eyeballed CTL delta above with a tested claim rather
    # than a raw percentage alone; mad_sd comes from the baseline engine so
    # the SWC reflects this athlete's own noise, not a population default.
    base90 = baselines.baseline(daily_loads_full, end, 90)
    trend = trends.analyze(
        "daily_load", daily_loads_full, end, weeks * 7,
        mad_sd=base90["mad_sd"] if base90 else None,
    )
    if not trend["suppressed"]:
        dose_lines.append(trend["sentence"])

    acwr = pmc_engine.acwr_ewma(daily_loads_full)
    acwr_line = _acwr_line(acwr)
    if acwr_line:
        dose_lines.append(acwr_line)

    mono_line, mono_flag = _monotony_summary(all_pmc, weekly_blocks, start_pos)
    if mono_line:
        dose_lines.append(mono_line)

    report_start = (_date.fromisoformat(end) - timedelta(days=weeks * 7 - 1)).isoformat()
    currency_note = f"{n}d of local history feeds CTL/ATL/ACWR"
    window_acts = store.list_activities(report_start, end)
    if window_acts:
        sources = [a.get("load_source") or "none" for a in window_acts]
        top = max(sorted(set(sources)), key=sources.count)
        currency_note += f" · load currency: {_CURRENCY_LABEL.get(top, top)}"

    sections = [
        Section(
            title=None, header=None, prose="\n".join(dose_lines),
            priority="primary", method_note=currency_note,
        )
    ]

    tid_line, tid_note, tid_flag = _tid_section(store, end, weeks)
    if tid_line:
        sections.append(
            Section(title=None, header=None, prose=tid_line,
                    priority="secondary", method_note=tid_note)
        )

    prec_line, prec_exceeded = _precedent_line(store, daily_loads_full)
    if prec_line:
        sections.append(Section(title=None, header=None, prose=prec_line, priority="secondary"))

    verdict = _verdict_text(form.get("ramp_flag"), mono_flag, tid_flag, prec_exceeded)
    provisional = n < 28
    if provisional:
        verdict = f"PROVISIONAL (n={n} days) — {verdict}"

    report = Report(
        title=f"Training Load — {weeks} weeks",
        date=end,
        data_as_of=ctx.data_as_of(),
        verdict=verdict,
        provisional=provisional,
        banner=ctx.banner(),
        sections=sections,
        next_steps=[
            "garmin_recovery(days=28) for the physiology side of this load",
            "garmin_activities() to browse sessions in this window",
        ],
    )
    return render(report, CAP)
