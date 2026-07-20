"""garmin_athlete — the athlete reference card (DESIGN §2.4, cap 600 tokens).

Renders what the store actually holds and omits what it doesn't — never
silent nulls (§3.3): profile lines from athlete_profile (garmin_set_profile
writes), physiology from days/activities (weight, VO2max, primary sport,
RHR 28d baseline, HRV 60d band, sleep need, wake Body Battery), the injury
notebook from wellness_log, and a data-coverage block rendering EVERY
capability_map row as ✓/✗ (training_readiness=False carries the §2.4
readiness-fusion phrasing) plus days-synced count and load currency.

Garmin PR/zone payloads are capability probes only in Phase 0/1 — nothing
lands in athlete_profile from sync — so PRs/zones surface solely through the
coverage block until a later phase persists them.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any

from fartlek.analytics import baselines as baselines_mod
from fartlek.analytics.matcher import sport_family
from fartlek.render.renderer import Report, Section, format_date, render

CAP_TOKENS = 600

_HRV_BAND_MIN_N = 14
_RESOLVED_LOOKBACK_DAYS = 45

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_GOAL_LABEL = {"5k": "5K", "10k": "10K", "half": "Half", "marathon": "Marathon"}

_CURRENCY_LABEL = {
    "garmin": "Garmin activity load",
    "trimp_calibrated": "calibrated TRIMP",
    "trimp_uncalibrated": "uncalibrated TRIMP",
    "srpe_calibrated": "calibrated sRPE",
    "srpe_uncalibrated": "uncalibrated sRPE",
    "estimated": "duration-estimated",
    "none": "none",
}

_CAP_LABEL = {
    "profile": "profile",
    "user_settings": "user settings",
    "personal_records": "personal records",
    "race_predictions": "race predictions",
    "training_status": "training status",
    "training_readiness": "Training Readiness",
    "endurance_score": "Endurance Score",
    "running_tolerance": "running tolerance",
    "daily_summary": "daily summary",
    "sleep": "sleep",
    "sleepNeed": "sleep need",
    "hrv": "HRV",
    "hrv_baseline": "HRV baseline",
    "activities": "activities",
    "activityTrainingLoad": "activity training load",
    "hrTimeInZone": "HR time-in-zone",
    "directWorkoutRpe": "watch RPE",
    "avgGradeAdjustedSpeed": "grade-adjusted pace",
    "calendar_month_0": "calendar (this month)",
    "calendar_month_1": "calendar (next month)",
    "training_plans": "Garmin Coach plans",
    "goals": "Garmin goals",
    "devices": "devices",
    "rhr_range": "RHR history",
    "weight_range": "weight history",
    "weekly_stress": "weekly stress",
    "maxmet_history": "VO2max history",
    "maxmet_recent": "VO2max recent",
    "progress_summary": "progress summary",
}


def _short(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{_WEEKDAYS[dt.weekday()]} {date_str[5:]}"


def _cap_label(key: str) -> str:
    return _CAP_LABEL.get(key, key.replace("_", " "))


def _baseline(store: Any, metric: str, today: str, window: int) -> dict[str, Any] | None:
    """Cached baseline row when present, else computed from the days series."""
    cached = store.get_baseline(metric, today, window)
    if cached is not None:
        return cached
    return baselines_mod.baseline(store.get_series(metric, today, window), today, window)


def _latest(store: Any, metric: str, today: str, days: int = 365) -> float | None:
    series = store.get_series(metric, today, days)
    return series[-1][1] if series else None


def _fmt_hours(hours: float) -> str:
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h}h{m:02d}"


def _trim_goal_time(t: str) -> str:
    parts = t.split(":")
    if len(parts) == 3 and parts[2] == "00":
        return ":".join(parts[:2])
    return t


def _identity_line(store: Any, today: str) -> str | None:
    pieces: list[str] = []
    weight_g = _latest(store, "weight_g", today)
    if weight_g:
        pieces.append(f"{weight_g / 1000:.0f} kg")
    acts = store.list_activities("0000-01-01", today)
    recent = acts[-50:]
    vo2 = next((a["vo2max"] for a in reversed(recent) if a.get("vo2max") is not None), None)
    if vo2 is not None:
        pieces.append(f"VO2max {vo2:.1f}")
    if recent:
        counts: dict[str, int] = {}
        for a in recent:
            fam = sport_family(a["sport"])
            counts[fam] = counts.get(fam, 0) + 1
        top = max(sorted(counts), key=lambda f: counts[f])
        pieces.append(f"primary sport: {top} ({counts[top]} of last {len(recent)})")
    return " · ".join(pieces) if pieces else None


def _goal_line(profile: dict[str, str]) -> tuple[str, str | None]:
    """Returns (line, goal_summary_for_verdict or None)."""
    goal_bits = None
    distance = profile.get("goal_distance")
    if distance == "custom" and profile.get("goal_custom_km"):
        label = f"{float(profile['goal_custom_km']):g} km"
    else:
        label = _GOAL_LABEL.get(distance or "", None)
    date = profile.get("goal_race_date")
    time = profile.get("goal_time")
    if label or date or time:
        goal_bits = label or "race"
        if date:
            goal_bits += f" {format_date(date)}"
        if time:
            goal_bits += f", target {_trim_goal_time(time)}"

    parts: list[str] = []
    if goal_bits:
        parts.append(goal_bits)
    phase = profile.get("phase")
    if phase and phase != "none":
        p = f"phase: {phase.capitalize()}"
        if profile.get("phase_week") and profile.get("phase_total_weeks"):
            p += f" wk {profile['phase_week']} of {profile['phase_total_weeks']}"
        parts.append(p)
    if profile.get("availability_days"):
        parts.append(f"availability {profile['availability_days']} d/wk")

    if not parts:
        return (
            "**Goal (on file):** none — garmin_set_profile(...) records goal race, "
            "phase, and availability",
            None,
        )
    line = "**Goal (on file):** " + " · ".join(parts)
    set_date = next(
        (profile[k] for k in ("set_date", "updated_at", "profile_updated") if profile.get(k)),
        None,
    )
    if set_date:
        line += f" (set {set_date[:10]} via garmin_set_profile)"
    return line, goal_bits


def _plan_line(caps: dict[str, dict[str, Any]]) -> str | None:
    parts: list[str] = []
    if "training_plans" in caps:
        parts.append(
            "Garmin Coach plan enrolled"
            if caps["training_plans"]["available"]
            else "no enrolled Garmin Coach plan detected"
        )
    if "goals" in caps:
        parts.append(
            "Garmin goals set" if caps["goals"]["available"] else "no Garmin goals set"
        )
    return "**Garmin plan:** " + " · ".join(parts) if parts else None


def _engine_line(store: Any, profile: dict[str, str], today: str) -> str | None:
    pieces: list[str] = []
    if profile.get("lt1_hr_override"):
        pieces.append(f"LT1 {profile['lt1_hr_override']} bpm (athlete override)")
    rhr = _baseline(store, "resting_hr", today, 28)
    if rhr is not None:
        pieces.append(f"RHR baseline {rhr['median']:.0f}")
    return "**Engine:** " + " · ".join(pieces) if pieces else None


def _baselines_line(store: Any, today: str) -> str | None:
    pieces: list[str] = []
    hrv = _baseline(store, "hrv_last_night", today, 60)
    if hrv is not None and hrv["n"] >= _HRV_BAND_MIN_N:
        lo, hi = hrv["median"] - hrv["mad_sd"], hrv["median"] + hrv["mad_sd"]
        pieces.append(f"HRV band {lo:.0f}–{hi:.0f}")
    need = _latest(store, "sleep_need_h", today, 90)
    if need:
        pieces.append(f"sleep need {_fmt_hours(need)}")
    bb = _baseline(store, "body_battery_wake", today, 60)
    if bb is not None:
        pieces.append(f"wake Body Battery {bb['median']:.0f}")
    return "**Baselines (60d):** " + " · ".join(pieces) if pieces else None


def _notebook_line(store: Any, today: str) -> str | None:
    items: list[str] = []
    for entry in store.unresolved_injuries():
        note = entry.get("note") or "injury"
        items.append(f"{note} (since {_short(entry['date'])})")
    start = _date.fromisoformat(today)
    for back in range(_RESOLVED_LOOKBACK_DAYS + 1):
        d = (start - timedelta(days=back)).isoformat()
        for entry in store.logs_for(d):
            if entry.get("flag") == "injury" and entry.get("resolved"):
                note = entry.get("note") or "injury"
                items.append(f"{note} (logged {_short(entry['date'])}, resolved)")
    return "**Notebook (garmin_log):** " + " · ".join(items) if items else None


def _coverage_line(store: Any, today: str) -> str:
    caps = store.get_capabilities()
    parts: list[str] = []
    if not caps:
        parts.append("no capability probes recorded yet — garmin_sync() runs them")
    else:
        ok = [_cap_label(k) for k, v in caps.items() if v["available"]]
        missing = [_cap_label(k) for k, v in caps.items() if not v["available"]]
        if ok:
            parts.append("✓ " + ", ".join(ok))
        if missing:
            miss = "✗ " + ", ".join(missing)
            if "training_readiness" in caps and not caps["training_readiness"]["available"]:
                miss += (
                    " (device does not produce them — this server computes its own "
                    "readiness fusion instead)"
                )
            parts.append(miss)
    n_days = len(store.get_series("daily_load", today, 100_000))
    parts.append(f"{n_days} days synced")
    acts = store.list_activities("0000-01-01", today)[-50:]
    if acts:
        sources = [a.get("load_source") or "none" for a in acts]
        top = max(sorted(set(sources)), key=sources.count)
        parts.append(f"load currency: {_CURRENCY_LABEL.get(top, top)}")
    return "**Data coverage:** " + " · ".join(parts)


async def run(ctx: Any) -> str:
    await ctx.ensure_ready()
    today = ctx.today()
    store = ctx.store
    profile = store.get_profile()
    caps = store.get_capabilities()

    goal_line, goal_summary = _goal_line(profile)
    lines = [
        _identity_line(store, today),
        goal_line,
        _plan_line(caps),
        _engine_line(store, profile, today),
        _baselines_line(store, today),
        _notebook_line(store, today),
    ]
    profile_prose = "\n".join(line for line in lines if line)

    if goal_summary:
        verdict = f"reference card — goal on file: {goal_summary}"
    else:
        verdict = "reference card — no goal on file; garmin_set_profile(...) sets it"

    report = Report(
        title="Athlete",
        date=today,
        data_as_of=ctx.data_as_of(),
        verdict=verdict,
        banner=ctx.banner(),
        sections=[
            Section(title=None, header=None, prose=profile_prose),
            Section(title=None, header=None, prose=_coverage_line(store, today)),
        ],
        next_steps=[
            "garmin_set_profile(...) to change goal/phase",
            "everything above is already inlined in other tools where relevant",
        ],
    )
    return render(report, CAP_TOKENS)
