"""garmin_fitness — fitness outcomes and race feasibility (DESIGN §2.4, budget 700 / cap 1000).

Owns the "am I getting fitter, and will it be enough on race day" question.
Two halves, both selection-and-phrasing over engine output:

1. **Outcomes** — VO2max, HR-at-pace over the athlete's own pace band, steady
   -session EF, long-run durability. Per the §3.2 amendment (2026-07-22) the
   pace band is the PRIMARY efficiency measure and steady-session EF the
   secondary one: the steady qualifier is so restrictive that on real data it
   yielded 20 usable sessions in 180 days, below the 21-point floor
   `trends.analyze` needs, i.e. permanently suppressed. Same laps read by band
   gave two orders of magnitude more evidence.

2. **Race** — and this is where the tool branches, because the spec's
   triangulation answers "how long will D take?" while a fixed-time event asks
   "how far in T?". Riegel extrapolated to 24h from a 10K PR is meaningless and
   Tanda is fitted on marathons, so emitting either for a 24h goal would
   fabricate. A fixed-time goal therefore routes to `race.fixed_time_projection`
   and renders a DISTANCE RANGE with its assumptions and confidence, never a
   single number.

Tanda and the full three-model consensus are not implemented in
`analytics.race` yet; nothing here invents them. Where the inputs a model needs
are absent (no maximal performance on file, no long run to anchor a fixed-time
projection) the section says what is missing instead of guessing.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date as _date
from datetime import timedelta
from typing import Any

from fartlek.analytics import efficiency, projection, race, trends
from fartlek.analytics.matcher import sport_family
from fartlek.render.renderer import Report, Row, Section, format_date, render

CAP = 1000
DEFAULT_WEEKS = 12
MIN_WEEKS, MAX_WEEKS = 4, 52

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# "24h", "24 h", "12hr", "6 hours", "24-hour" — the free-text forms an athlete
# actually types into garmin_set_profile(goal_distance=...).
_FIXED_TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-?\s*(?:h|hr|hrs|hour|hours)\b", re.I)
_HMS_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*$")

_DISTANCE_M = {"5k": 5000.0, "10k": 10000.0, "half": 21097.5, "marathon": 42195.0}
_DISTANCE_LABEL = {"5k": "5K", "10k": "10K", "half": "Half marathon",
                   "marathon": "Marathon"}

# A band needs enough laps for its quartiles to mean anything; below this the
# "your usual pace" claim is really "one session's pace".
MIN_BAND_LAPS = 8
# §3.2 #13: durability stays LOW-confidence until the athlete has this many
# long runs on file.
DURABILITY_CONFIDENT_N = 5


# --- small formatters -------------------------------------------------------

def _pace(s_per_km: float) -> str:
    total = int(round(s_per_km))
    return f"{total // 60}:{total % 60:02d}"


def _hms(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_hms(value: str | None) -> float | None:
    """'2:59:00' / '38:43' → seconds. Same shape garmin_set_profile accepts for
    goal_time, so a PR reads the way a goal does."""
    if not value:
        return None
    m = _HMS_RE.match(str(value))
    if not m:
        return None
    hours = float(m.group(1) or 0)
    return hours * 3600 + float(m.group(2)) * 60 + float(m.group(3))


# --- goal resolution --------------------------------------------------------

def _goal(profile: dict[str, str]) -> dict[str, Any]:
    """The goal race as the store holds it, classified by KIND.

    Kind drives which race model may run at all, so it is resolved once here
    rather than being re-sniffed at every use. `goal_distance` is free text
    (garmin_set_profile does not enumerate it), which is exactly how a
    fixed-time event gets recorded: 'goal_distance=24h'.
    """
    raw = str(profile.get("goal_distance")
              or profile.get("goal_race_distance") or "").strip()
    declared = str(profile.get("goal_race_type") or "").strip().lower()
    date_str = profile.get("goal_race_date")
    if date_str and not _DATE_RE.match(str(date_str)):
        date_str = None

    fixed = _FIXED_TIME_RE.match(raw)
    hours: float | None = None
    if fixed:
        hours = float(fixed.group(1))
    elif profile.get("goal_hours"):
        try:
            hours = float(str(profile["goal_hours"]))
        except ValueError:
            hours = None

    target_km = None
    for key in ("goal_target_km", "goal_custom_km"):
        if profile.get(key):
            try:
                target_km = float(str(profile[key]))
                break
            except ValueError:
                pass

    if fixed or (declared == "fixed_time" and hours):
        label = raw or f"{hours:g}h"
        return {"kind": "fixed_time", "label": label, "hours": hours,
                "date": date_str, "target_km": target_km}

    if raw:
        key = raw.lower()
        if key == "custom" and target_km:
            metres, label = target_km * 1000.0, f"{target_km:g} km"
        else:
            metres = _DISTANCE_M.get(key)
            label = _DISTANCE_LABEL.get(key, raw)
        if metres:
            return {"kind": "distance", "label": label, "distance_m": metres,
                    "date": date_str, "goal_time": profile.get("goal_time")}
    if date_str:
        return {"kind": "unknown", "label": "race", "date": date_str}
    return {"kind": "none"}


def _personal_records(store: Any) -> list[tuple[float, float]]:
    """[(metres, seconds)] of MAXIMAL efforts for 5k / 10k / half / marathon.

    Prefers Garmin's persisted PRs (sync-derived, tier 0) and falls back to a
    user-entered profile pr_* key. Only MAXIMAL efforts belong here:
    `race.fit_riegel_exponent` fits pacing discipline rather than physiology
    when fed training runs (its docstring documents the 0.99 exponent that
    produced), so the Riegel line is omitted rather than anchored on a non-PR.
    """
    sync_prs = store.get_personal_records() or {}
    profile = store.get_profile()
    out: list[tuple[float, float]] = []
    for key, metres in _DISTANCE_M.items():
        record = sync_prs.get(key)
        if record and record.get("seconds"):
            secs: float | None = float(record["seconds"])
        else:
            secs = _parse_hms(profile.get(f"pr_{key}"))
        if secs:
            out.append((metres, secs))
    return sorted(out)


# --- fitness outcomes -------------------------------------------------------

def _finding(res: dict[str, Any], label: str) -> str:
    """The trend sentence with its leading label removed — the table's first
    column already names the signal, and paying tokens twice for it is the
    kind of duplication the 1000-token cap cannot afford."""
    sentence = res["sentence"]
    return sentence[len(label):].lstrip(" :") if sentence.startswith(label) else sentence


def _vo2max_row(store: Any, start: str, end: str, window_days: int) -> Row | None:
    """VO2max is per-activity, not a days column — one reading per date, latest
    wins when a day carries two runs."""
    by_date: dict[str, float] = {}
    for act in store.list_activities(start, end):
        if act.get("vo2max"):
            by_date[act["date"]] = float(act["vo2max"])
    if not by_date:
        return None
    series = sorted(by_date.items())
    res = trends.analyze("vo2max", series, end, window_days, unit="")
    if res["suppressed"]:
        return Row(["VO2max", f"{series[-1][1]:.1f} latest — {res['reason']}, "
                              "no trend claimed"])
    return Row(["VO2max", f"{series[-1][1]:.1f} · " + _finding(res, "VO2max")])


def _pace_band(laps: list[dict[str, Any]]) -> tuple[float, float] | None:
    """The athlete's middle-half pace band, in s/km.

    Derived from the whole window at once and then held fixed across the
    period buckets: a band recomputed per month would move with the athlete's
    fitness and hide the very change it is meant to measure.
    """
    paces = sorted(
        p for lap in laps
        if lap.get("avg_hr")
        and (lap.get("distance_m") or 0) >= efficiency.MIN_LAP_DISTANCE_M
        and (p := efficiency.lap_pace_s_per_km(lap)) is not None
        and 150.0 <= p <= 900.0
    )
    if len(paces) < MIN_BAND_LAPS:
        return None
    lo, hi = paces[len(paces) // 4], paces[(3 * len(paces)) // 4]
    if hi - lo < 20.0:  # degenerate band: widen to ±10 s/km so laps qualify
        mid = (lo + hi) / 2.0
        lo, hi = mid - 10.0, mid + 10.0
    return lo, hi


def _band_rows(laps: list[dict[str, Any]]) -> tuple[list[Row], dict[str, Any]]:
    """HR-at-pace now, plus its month-over-month move where enough laps exist.

    The move is stated as arithmetic ('146 → 141 bpm'), never as a significance
    claim: monthly buckets can never reach trends.MIN_POINTS, and calling four
    points a trend would be the kind of over-claim §3.2 #7 exists to prevent.
    """
    band = _pace_band(laps)
    if band is None:
        return [], {}
    lo, hi = band
    overall = efficiency.hr_at_pace(laps, lo, hi, exclude_hot=True)
    if not overall["n_laps"]:
        return [], {}

    label = f"HR at {_pace(lo)}–{_pace(hi)}/km"
    cell = (f"{overall['avg_hr']:.0f} bpm over {overall['n_laps']} laps / "
            f"{overall['n_sessions']} sessions")
    buckets = [b for b in efficiency.hr_at_pace_by_period(
        laps, lo, hi, period="month", exclude_hot=True).values()
        if b["n_laps"] >= efficiency.MIN_LAPS_FOR_BAND]
    move = {}
    if len(buckets) >= 2:
        first, last = buckets[0], buckets[-1]
        delta = last["avg_hr"] - first["avg_hr"]
        cell += f" · {first['avg_hr']:.0f} → {last['avg_hr']:.0f} bpm"
        move = {"delta_hr": delta, "n_laps": overall["n_laps"]}
    return [Row([label, cell])], move


def _ef_row(laps: list[dict[str, Any]], end: str, window_days: int) -> Row | None:
    """Steady-session EF — the secondary measure, rendered only when the
    significance machinery will actually speak."""
    series = efficiency.ef_trend_series(laps)
    if not series:
        return None
    res = trends.analyze("ef", series, end, window_days, label="Steady-run EF")
    if res["suppressed"]:
        return None
    return Row(["EF (steady runs, splits-based)", _finding(res, "Steady-run EF")])


def _long_runs(laps: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    by_activity: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    dates: dict[Any, str] = {}
    for lap in laps:
        by_activity[lap["activity_id"]].append(lap)
        if lap.get("date"):
            dates[lap["activity_id"]] = str(lap["date"])
    out = []
    for aid, session in by_activity.items():
        res = efficiency.session_efficiency(session)
        if res["moving_s"] >= efficiency.LONG_RUN_MIN_S and aid in dates:
            out.append((dates[aid], res))
    return sorted(out)


def _durability_row(sessions: list[tuple[str, dict[str, Any]]]) -> Row | None:
    values = [(d, r["decoupling"]) for d, r in sessions if r["decoupling"] is not None]
    if not values:
        return None
    recent = values[-3:]
    cell = " / ".join(f"{v:+.1%}" for _, v in reversed(recent))
    n = len(values)
    if n < DURABILITY_CONFIDENT_N:
        cell += f" (n={n} long run{'s' if n > 1 else ''} — LOW confidence)"
    return Row(["Decoupling, last 90 min+ runs", cell])


# --- race sections ----------------------------------------------------------

def _reference_effort(store: Any, end: str, lookback_days: int) -> dict[str, Any] | None:
    """The longest run available as the anchor for a fixed-time projection.

    Deliberately searched over a wider window than the report's own: the
    biggest effort on file is the evidence, and refusing to use a 78 km run
    because it fell four weeks outside a 12-week view would be pedantry. Its
    date renders, so the athlete can judge how current it is.
    """
    start = (_date.fromisoformat(end) - timedelta(days=lookback_days)).isoformat()
    best = None
    for act in store.list_activities(start, end):
        if sport_family(act.get("sport") or "") != "running":
            continue
        moving = float(act.get("moving_s") or act.get("duration_s") or 0.0)
        dist = float(act.get("distance_m") or 0.0)
        if moving / 3600.0 < race.MIN_REFERENCE_HOURS or dist <= 0:
            continue
        if best is None or dist > best["distance_m"]:
            best = {"activity_id": act["activity_id"], "date": act["date"],
                    "distance_m": dist, "moving_s": moving,
                    "duration_s": float(act.get("duration_s") or moving)}
    return best


def _stoppage(store: Any, ref: dict[str, Any]) -> float | None:
    """Stoppage from laps when they exist, else from the activity's own
    elapsed-vs-moving split. On a fixed-time event this is not a rounding
    detail — it is hours of standing still."""
    laps = store.get_activity_laps(ref["activity_id"])
    if laps:
        value = race.stoppage_ratio(laps)
        if value is not None:
            return value
    if ref["duration_s"] > 0:
        return max(0.0, 1.0 - ref["moving_s"] / ref["duration_s"])
    return None


def _fixed_time_section(
    store: Any, goal: dict[str, Any], end: str, window_days: int
) -> tuple[Section | None, str]:
    """Distance RANGE for a fixed-time goal, with the model's own assumptions.

    Returns (section, verdict clause). Never a single number: the exponent band
    is a population default and the reference is normally sub-maximal, so a
    point estimate would claim a precision the inputs do not carry.
    """
    hours = goal.get("hours")
    head = goal["label"] + (f" — {format_date(goal['date'])}" if goal.get("date") else "")
    if not hours:
        return (Section(title=None, header=None, prose=(
            f"**{head}** — fixed-time goal recorded without a duration, so no "
            "projection is possible. Example: "
            "garmin_set_profile(goal_distance='24h')"), priority="primary"),
            "fixed-time goal on file without a duration")

    ref = _reference_effort(store, end, max(window_days, 365))
    if ref is None:
        return (Section(title=None, header=None, prose=(
            f"**{head}** — no run of ≥{race.MIN_REFERENCE_HOURS:g}h on file, and a "
            f"{hours:g}h projection extrapolated from anything shorter is fiction. "
            "One long run anchors it."), priority="primary"),
            f"{goal['label']} distance not projectable — no long-run anchor on file")

    stop = _stoppage(store, ref)
    proj = race.fixed_time_projection(
        reference_distance_m=ref["distance_m"], reference_moving_s=ref["moving_s"],
        target_hours=hours, stoppage=stop,
    )
    if proj.get("error"):
        return (None, f"{goal['label']} projection unavailable: {proj['error']}")

    lo_km, hi_km = proj["low_m"] / 1000.0, proj["high_m"] / 1000.0
    lines = [f"**{head}** — projected distance **{lo_km:.0f}–{hi_km:.0f} km** "
             f"({proj['confidence']} confidence; a range, not a number)."]
    lines.append("Assumptions: " + " · ".join(proj["assumptions"])
                 + f" · anchor run {format_date(ref['date'])}.")

    clause = f"{goal['label']} projects {lo_km:.0f}–{hi_km:.0f} km"
    target = goal.get("target_km")
    if target:
        if target > hi_km:
            gap = f"target {target:g} km sits {target - hi_km:.0f} km above the range"
        elif target < lo_km:
            gap = f"target {target:g} km sits below the range — it is in reach"
        else:
            gap = f"target {target:g} km sits inside the range"
        lines.append(gap[0].upper() + gap[1:] + ".")
        clause += f", {gap}"
    return Section(title=None, header=None, prose="\n\n".join(lines),
                   priority="primary"), clause


def _distance_key(target_m: float) -> str | None:
    """The 5k/10k/half/marathon key for a target distance, or None if it is not
    one of the four Garmin/Tanda standard distances."""
    for key, metres in _DISTANCE_M.items():
        if abs(target_m - metres) < 1.0:
            return key
    return None


def _tanda_inputs(store: Any, end: str, weeks: int = 8) -> tuple[float, float] | None:
    """(mean weekly km, mean training pace s/km) over `weeks` of RUN activities
    ending at `end` — the two inputs Tanda regresses on. None with no mileage."""
    start = (_date.fromisoformat(end) - timedelta(days=weeks * 7 - 1)).isoformat()
    dist_m = dur_s = 0.0
    for a in store.list_activities(start, end):
        if sport_family(a.get("sport") or "") != "running":
            continue
        d, t = a.get("distance_m") or 0.0, a.get("duration_s") or 0.0
        if d > 0 and t > 0:
            dist_m += d
            dur_s += t
    if dist_m <= 0 or dur_s <= 0:
        return None
    return (dist_m / 1000.0) / weeks, dur_s / (dist_m / 1000.0)


def _distance_section(
    store: Any, goal: dict[str, Any], end: str
) -> tuple[Section, str]:
    """Triangulated race read for a distance goal: Garmin / Tanda / Riegel shown
    together, the spread as the confidence — disagreement is explained, never
    averaged (§3.2 #16). Tanda is marathon-specific; each model appears only
    when its inputs exist, and nothing is fabricated to force a consensus.
    """
    head = goal["label"] + (f" — {format_date(goal['date'])}" if goal.get("date") else "")
    target_m = goal["distance_m"]
    key = _distance_key(target_m)

    table = ["| Model | Predicted | Basis |", "|---|---|---|"]
    predictions: list[float] = []
    have_riegel = False
    tanda: dict[str, Any] | None = None

    # Garmin's own model, surfaced as-is
    preds = store.get_race_predictions() or {}
    if key and preds.get(key):
        table.append(f"| Garmin | {_hms(preds[key])} | device model |")
        predictions.append(preds[key])

    # Tanda — marathon only (it is a marathon regression)
    if key == "marathon":
        ti = _tanda_inputs(store, end)
        if ti:
            wk_km, pace = ti
            tanda = race.tanda_marathon(wk_km, pace)
            domain = "" if tanda["in_domain"] else ", outside Tanda's 30–160 km/wk range"
            table.append(f"| Tanda | {_hms(tanda['seconds'])} | "
                         f"8 wk: {wk_km:.0f} km/wk @ {_hms(pace)}/km training{domain} |")
            predictions.append(tanda["seconds"])

    # Riegel — from maximal efforts (PRs)
    prs = _personal_records(store)
    if prs:
        fit = race.fit_riegel_exponent(prs)
        d1, t1 = min(prs, key=lambda p: abs(p[0] - target_m))
        predicted = race.riegel_time(t1, d1, target_m, fit["b"])
        if fit["quality"] == "default":
            rb = f"exp {fit['b']:.3g} (population default, one PR)"
        else:
            rb = f"exp {fit['b']:.3g} fitted on {fit['n']} PRs"
            rb += ", clamped to 1.03-1.12" if fit["clamped"] else ""
        table.append(f"| Riegel ({d1 / 1000:.3g}k in {_hms(t1)}) | {_hms(predicted)} | {rb} |")
        predictions.append(predicted)
        have_riegel = True

    if not predictions:
        return (Section(title=None, header=None, prose=(
            f"**{head}** — no PR, no device prediction, and not enough run "
            "volume to model this race, so no time is offered. A training run "
            "is not a maximal effort and modelling one would overstate you."),
            priority="primary"),
            f"{goal['label']} goal on file, nothing to predict from")

    lo, hi = min(predictions), max(predictions)
    n = len(predictions)
    lines = [f"**{head}**", "\n".join(table)]
    if n >= 2:
        lines.append(f"{n} models span {_hms(lo)}–{_hms(hi)} (spread {_hms(hi - lo)}) — "
                     "the range is the confidence, not averaged.")
        if tanda is not None and have_riegel:
            lines.append("Riegel extrapolates PR speed and assumes marathon-ready "
                         "endurance; Tanda reads your actual 8-week volume — the gap "
                         "between them is unproven durability, not measurement error.")
    clause = f"{goal['label']}: {n} model{'s' if n > 1 else ''} {_hms(lo)}–{_hms(hi)}"

    goal_secs = _parse_hms(goal.get("goal_time"))
    if goal_secs:
        if goal_secs < lo:
            rel = f"{_hms(lo - goal_secs)} faster than every model"
        elif goal_secs > hi:
            rel = f"{_hms(goal_secs - hi)} slower than every model"
        else:
            rel = "inside the model range"
        lines.append(f"Target {_hms(goal_secs)}: {rel}.")
        clause += f", target {'in range' if lo <= goal_secs <= hi else 'outside'}"

    if tanda is not None:
        km_lever = abs(tanda["seconds_per_km_per_week"]) * 5
        pace_lever = tanda["seconds_per_training_pace_s"] * 5
        lines.append(f"Tanda levers: +5 km/wk ≈ −{_hms(km_lever)}, "
                     f"−5 s/km training pace ≈ −{_hms(pace_lever)}.")

    return Section(title=None, header=None, prose="\n\n".join(lines),
                   priority="primary"), clause


def _projection_line(store: Any, goal: dict[str, Any], end: str) -> str | None:
    """Forward PMC to race day plus taper state — arithmetic on the athlete's
    own numbers, with the basis disclosed (§3.2 #17), never a promise."""
    if not goal.get("date"):
        return None
    pmc_rows = store.get_pmc(end_date=end, days=1)
    if not pmc_rows:
        return None
    last = pmc_rows[-1]
    loads = store.get_series("daily_load", end, 28)
    proj = projection.project_to_race(
        ctl=float(last["ctl"]), atl=float(last["atl"]), today=end,
        race_date=goal["date"], daily_loads=loads,
    )
    if proj.get("error"):
        return None
    basis = {"pattern": "your trailing 4 weeks replayed by weekday",
             "scheduled": "workouts scheduled on your calendar",
             "mixed": "scheduled workouts where they exist, your weekday pattern elsewhere",
             "none": "no load pattern on file — assumes nothing further is run"}[proj["basis"]]
    form = proj["form_race_pct"]
    line = (f"Projection ({proj['days_out']} d out): CTL {proj['ctl_now']:.0f} → "
            f"{proj['ctl_race']:.0f}")
    if form is not None:
        line += f", form {form:+.0f}% of CTL on race day"
    line += f" — basis: {basis}."

    taper = projection.taper_guidance(proj)
    if not taper["active"]:
        line += f" {taper['reason'].capitalize()}."
    else:
        line += (f" Taper: {taper['verdict'].replace('_', ' ')}, CTL fade "
                 f"{taper['ctl_fade_pct']:.0f}%.")
        if taper["actions"]:
            line += " " + taper["actions"][0] + "."
    return line


# --- entry point ------------------------------------------------------------

async def run(ctx: Any, weeks: int = DEFAULT_WEEKS,
              anchor_date: str | None = None) -> str:
    if anchor_date is not None and not _DATE_RE.match(anchor_date):
        return (f"anchor_date must be YYYY-MM-DD (got {anchor_date!r}). "
                f"Today is {ctx.today()}. Example: garmin_fitness(weeks={DEFAULT_WEEKS})")
    if not MIN_WEEKS <= weeks <= MAX_WEEKS:
        return (f"weeks must be between {MIN_WEEKS} and {MAX_WEEKS} (got {weeks}). "
                f"Today is {ctx.today()}. "
                f"Example: garmin_fitness(weeks={DEFAULT_WEEKS})")

    await ctx.ensure_ready()
    end = anchor_date or ctx.today()
    store = ctx.store
    window_days = weeks * 7
    start = (_date.fromisoformat(end) - timedelta(days=window_days - 1)).isoformat()

    laps = store.laps_in_range(start, end, "%run%")
    rows: list[Row] = []
    vo2 = _vo2max_row(store, start, end, window_days)
    if vo2:
        rows.append(vo2)
    band_rows, move = _band_rows(laps)
    rows.extend(band_rows)
    ef = _ef_row(laps, end, window_days)
    if ef:
        rows.append(ef)
    long_runs = _long_runs(laps)
    dur = _durability_row(long_runs)
    if dur:
        rows.append(dur)

    # Fitness lead: the band comes first because it is the primary measure
    # (§3.2 amendment) and because HR falling at an unchanged pace is the one
    # statement about fitness that is not confounded by effort choice.
    if move.get("delta_hr") is not None and move["delta_hr"] <= -1.0:
        lead = "aerobic fitness rising — same pace at lower HR"
    elif move.get("delta_hr") is not None and move["delta_hr"] >= 1.0:
        lead = "HR at your usual pace has drifted up"
    elif rows:
        lead = "fitness signals steady"
    else:
        lead = "no fitness outcomes trackable from the current activity mix"

    profile = store.get_profile()
    goal = _goal(profile)
    sections: list[Section] = []
    if rows:
        sections.append(Section(
            title=None, header=["Signal", f"Read over {weeks} wk"], rows=rows,
            method_note="HR-at-pace is the primary efficiency measure; laps at "
                        "≥24 °C are excluded, not deleted",
        ))

    if goal["kind"] == "fixed_time":
        section, clause = _fixed_time_section(store, goal, end, window_days)
    elif goal["kind"] == "distance":
        section, clause = _distance_section(store, goal, end)
    elif goal["kind"] == "unknown":
        section, clause = None, (
            "a goal date is on file without a distance — "
            "garmin_set_profile(goal_distance='marathon') completes it")
    else:
        section, clause = None, (
            "no goal race on file — set one with garmin_set_profile("
            f"goal_race_date='{end}', goal_distance='marathon')")
    if section:
        sections.append(section)

    proj_line = _projection_line(store, goal, end)
    if proj_line:
        sections.append(Section(title=None, header=None, prose=proj_line,
                                priority="secondary"))

    watch: list[str] = []
    if long_runs and len(long_runs) < DURABILITY_CONFIDENT_N:
        watch.append(f"durability rests on {len(long_runs)} long run"
                     f"{'s' if len(long_runs) > 1 else ''} — treat it as LOW confidence")
    if goal["kind"] == "fixed_time" and goal.get("hours"):
        watch.append("stoppage time is the lever the projection is most "
                     "sensitive to on a fixed-time event")

    report = Report(
        title=f"Fitness & Race Outlook — {weeks} weeks",
        date=end,
        data_as_of=ctx.data_as_of(),
        verdict=f"{lead}. {clause}.",
        banner=ctx.banner(),
        sections=sections,
        watch_list=watch,
        next_steps=[
            f"garmin_activities(start_date={start}) for the sessions behind this",
            "garmin_set_profile(goal_race_date=...) to change the goal",
        ],
    )
    return render(report, CAP)
