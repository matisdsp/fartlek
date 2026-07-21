"""garmin_activity — deep analysis of ONE session (DESIGN §2.4).

Caps: standard 1,000 · splits 2,000 · full 4,000 tokens.

Resolution order: activity_id → date (no match → corrective error naming the
two nearest activities with real IDs) → latest of sport → latest overall; the
chosen resolution is stated in the header ('selected: by id' / 'by date' /
'latest run' / 'latest activity').

standard — store-only, no live fetch: header, stats line (km, duration,
avgHR/maxHR, load with load_source parenthetical when not garmin-native,
aerobic TE, RPE with provenance), VERDICT comparing to the most similar past
session (same sport family, duration within ±25%, most recent), HR-zone
distribution when present, plan compliance via the Phase-0 matcher result in
plan_calendar, and an RPE nudge with a garmin_log breadcrumb when RPE is
missing.

splits — adds the lap analysis fetched live from
/activity-service/activity/{id}/splits: typed INTERVAL_ACTIVE laps → rep
table + recovery HR-floor line; else manual-lap heuristic (alternating
fast/slow clusters); else per-split aggregate table (≤12 rows, "no interval
structure detected — freeform session"). Fetch failure degrades gracefully.

full — splits + a 20-point downsampled HR/pace curve from
/activity-service/activity/{id}/details (metricDescriptors →
directHeartRate/directSpeed indices into activityDetailMetrics).

Strength activities render a duration/HR/load/set-count summary; exercise-set
detail is not synced in v0.1 and that is disclosed, never hidden.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any

from fartlek.analytics.matcher import sport_family
from fartlek.health.exceptions import GarminAuthError
from fartlek.render.renderer import (
    Report,
    Row,
    Section,
    arrow_series,
    format_date,
    render,
)

CAPS = {"standard": 1000, "splits": 2000, "full": 4000}

_FAMILY_TITLE = {
    "running": "Run",
    "cycling": "Ride",
    "swimming": "Swim",
    "strength": "Strength",
    "walking": "Walk",
    "hiking": "Hike",
}
_FAMILY_NOUN = {
    "running": "run",
    "cycling": "ride",
    "swimming": "swim",
    "strength": "strength session",
}
_LOAD_SOURCE_LABEL = {
    "trimp_calibrated": "TRIMP-derived",
    "trimp_uncalibrated": "TRIMP-derived, uncalibrated",
    "srpe_calibrated": "sRPE-derived",
    "srpe_uncalibrated": "sRPE-derived, uncalibrated",
    "estimated": "estimated",
}
_CORE_FAMILIES = {"running", "cycling", "swimming", "strength"}
_RECOVERY_TYPES = {"INTERVAL_RECOVERY", "INTERVAL_REST", "RECOVERY", "REST"}
_WIDE_START, _WIDE_END = "1900-01-01", "2100-12-31"


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def _dur(seconds: float | None) -> str:
    """'XhMM' for ≥1h (unambiguous vs mm:ss), else mm:ss (§2.3)."""
    if seconds is None:
        return "—"
    s = int(round(float(seconds)))
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _pace_s(speed_ms: float | None) -> float | None:
    """m/s → seconds per km."""
    if speed_ms is None or speed_ms <= 0:
        return None
    return 1000.0 / float(speed_ms)


def _pace_str(speed_ms: float | None) -> str | None:
    sec = _pace_s(speed_ms)
    if sec is None:
        return None
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def _pct(delta_fraction: float) -> str:
    """+1.7% / −0.4% with a typographic minus (house style)."""
    return f"{delta_fraction * 100:+.1f}%".replace("-", "−")


def _dshort(date: str) -> str:
    """'2026-07-15' → 'Wed 07-15'."""
    return format_date(date).replace(f" {date}", f" {date[5:]}")


def _start_hhmm(start_local: str | None) -> str | None:
    if not start_local:
        return None
    try:
        return datetime.fromisoformat(start_local.replace(" ", "T")).strftime("%H:%M")
    except ValueError:
        return None


def _extra(act: dict[str, Any]) -> dict[str, Any]:
    raw = act.get("extra_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

class _Resolved:
    def __init__(self, act: dict[str, Any], how: str):
        self.act = act
        self.how = how


def _latest(acts: list[dict[str, Any]]) -> dict[str, Any]:
    return max(acts, key=lambda a: (a["date"], a.get("start_local") or "", a["activity_id"]))


def _ref(a: dict[str, Any]) -> str:
    """'garmin_activity(activity_id=N) (Ddd MM-DD, run)' — corrective-error form."""
    noun = _FAMILY_NOUN.get(sport_family(a.get("sport") or ""), a.get("sport") or "activity")
    return f"garmin_activity(activity_id={a['activity_id']}) ({_dshort(a['date'])}, {noun})"


def _resolve(
    ctx, activity_id: int | None, date: str | None, sport: str | None
) -> _Resolved | str:
    """Resolve to one activity or return a corrective error string."""
    store = ctx.store
    if activity_id is not None:
        act = store.get_activity(activity_id)
        if act is not None:
            return _Resolved(act, "by id")
        acts = store.list_activities(_WIDE_START, _WIDE_END)
        hint = f" Latest: {_ref(_latest(acts))}." if acts else ""
        return (
            f"No activity with id {activity_id}. Browse ids with garmin_activities().{hint}"
        )

    acts = store.list_activities(_WIDE_START, _WIDE_END)
    if not acts:
        return (
            "No activities in the local store yet. Run garmin_sync() to fetch from "
            "Garmin, then garmin_activities() to browse."
        )

    if date is not None:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return (
                f"date must be YYYY-MM-DD (got '{date}'). Today is "
                f"{format_date(ctx.today())}. Example: garmin_activity(date='{ctx.today()}')"
            )
        on_date = [a for a in acts if a["date"] == date]
        if on_date:
            return _Resolved(_latest(on_date), "by date")
        target = datetime.strptime(date, "%Y-%m-%d")
        nearest = sorted(
            acts,
            key=lambda a: (
                abs((datetime.strptime(a["date"], "%Y-%m-%d") - target).days),
                a["date"],
            ),
        )[:2]
        return (
            f"No activity on {_dshort(date)}. Nearest: "
            + ", ".join(_ref(a) for a in nearest)
        )

    if sport is not None:
        if sport == "other":
            of_sport = [a for a in acts if sport_family(a["sport"]) not in _CORE_FAMILIES]
        else:
            of_sport = [a for a in acts if sport_family(a["sport"]) == sport]
        if of_sport:
            noun = _FAMILY_NOUN.get(sport, "activity")
            return _Resolved(_latest(of_sport), f"latest {noun}")
        return (
            f"No {sport} activities in the store. Latest overall: "
            f"{_ref(_latest(acts))} · garmin_activities() to browse."
        )

    return _Resolved(_latest(acts), "latest activity")


# ---------------------------------------------------------------------------
# standard sections
# ---------------------------------------------------------------------------

def _stats_line(act: dict[str, Any]) -> str:
    parts: list[str] = []
    strength = sport_family(act.get("sport") or "") == "strength"
    dist = act.get("distance_m")
    if not strength and dist:
        parts.append(f"{dist / 1000:.1f} km")
    if act.get("duration_s") is not None:
        parts.append(_dur(act["duration_s"]))
    if act.get("avg_hr") is not None:
        hr = f"avg HR {act['avg_hr']}"
        if act.get("max_hr") is not None:
            hr += f" / max {act['max_hr']}"
        parts.append(hr)
    load, source = act.get("load"), act.get("load_source") or "garmin"
    if load is not None and source != "none":
        label = _LOAD_SOURCE_LABEL.get(source)
        parts.append(f"load {round(load)} ({label})" if label else f"load {round(load)}")
    if not strength and act.get("aerobic_te") is not None:
        parts.append(f"aerobic TE {act['aerobic_te']:.1f}")
    if strength:
        sets = _extra(act).get("totalSets")
        if sets is None:
            sets = _extra(act).get("total_sets")
        if sets is not None:
            parts.append(f"{sets} sets")
    if act.get("rpe") is not None:
        prov = {"athlete": "athlete RPE", "watch": "watch RPE"}.get(
            act.get("rpe_source") or "", "RPE"
        )
        parts.append(f"{prov} {act['rpe']}/10")
    if strength:
        parts.append("set detail not synced in v0.1")
    return " · ".join(parts)


def _most_similar(store, act: dict[str, Any]) -> dict[str, Any] | None:
    """Most recent past activity of the same sport family with duration
    within ±25% of this one."""
    fam = sport_family(act.get("sport") or "")
    dur = act.get("duration_s")
    if dur is None or dur <= 0:
        return None
    cands = [
        a
        for a in store.list_activities(_WIDE_START, act["date"])
        if a["activity_id"] != act["activity_id"]
        and sport_family(a.get("sport") or "") == fam
        and a.get("duration_s") is not None
        and abs(a["duration_s"] - dur) <= 0.25 * dur
    ]
    if not cands:
        return None
    return _latest(cands)


def _comparison_verdict(act: dict[str, Any], comp: dict[str, Any] | None) -> str:
    if comp is None:
        return "no comparable past session in the log — nothing to benchmark against yet."
    when = _dshort(comp["date"])
    p_sel, p_cmp = _pace_s(act.get("avg_speed")), _pace_s(comp.get("avg_speed"))
    hr_sel, hr_cmp = act.get("avg_hr"), comp.get("avg_hr")
    if p_sel and p_cmp and hr_sel is not None and hr_cmp is not None:
        pace_delta = (p_sel - p_cmp) / p_cmp
        hr_delta = hr_sel - hr_cmp
        head = (
            f"vs {when} ({_dur(comp.get('duration_s'))} at "
            f"{_pace_str(comp.get('avg_speed'))}/km, HR {hr_cmp}): today "
            f"{_pace_str(act.get('avg_speed'))}/km at HR {hr_sel}"
        )
        if abs(pace_delta) <= 0.02 and hr_delta <= -3:
            return f"{head} — same pace at lower HR, aerobic economy improving."
        if abs(pace_delta) <= 0.02 and hr_delta >= 3:
            return f"{head} — same pace at higher HR, this one cost more."
        if pace_delta <= -0.02 and abs(hr_delta) < 3:
            return f"{head} — faster at similar HR, fitness moving the right way."
        return f"{head} — comparable effort ({_pct(pace_delta)} pace, {hr_delta:+d} bpm)."
    l_sel, l_cmp = act.get("load"), comp.get("load")
    if l_sel is not None and l_cmp is not None:
        shape = "a higher" if l_sel > l_cmp else "a lower" if l_sel < l_cmp else "the same"
        return (
            f"vs {when} ({_dur(comp.get('duration_s'))}): load {round(l_sel)} vs "
            f"{round(l_cmp)} — {shape} dose than the most similar past session."
        )
    return f"closest comparable session: {when}, {_dur(comp.get('duration_s'))}."


def _zones_line(act: dict[str, Any]) -> str | None:
    zones = [act.get(f"hr_z{i}_s") for i in range(1, 6)]
    if not any(z is not None and z > 0 for z in zones):
        return None
    cells = [f"Z{i} {_dur(z) if z is not None else '—'}" for i, z in enumerate(zones, 1)]
    return "HR zones: " + " · ".join(cells)


def _compliance_line(store, act: dict[str, Any]) -> str:
    entries = store.plan_entries(act["date"], act["date"])
    matched = next(
        (e for e in entries if e.get("matched_activity_id") == act["activity_id"]), None
    )
    if matched is not None:
        return f"planned workout matched ({matched.get('match_method') or 'unknown'})"
    return "No planned workout matched to this date — no compliance score."


# ---------------------------------------------------------------------------
# splits detail
# ---------------------------------------------------------------------------

def _laps(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("lapDTOs") or payload.get("splits") or []
    if not isinstance(payload, list):
        return []
    return [lap for lap in payload if isinstance(lap, dict)]


def _lap_type(lap: dict[str, Any]) -> str:
    v = lap.get("intensityType") or lap.get("type") or ""
    return str(v).upper()


def _lap_duration(lap: dict[str, Any]) -> float | None:
    for key in ("duration", "movingDuration"):
        if lap.get(key) is not None:
            return float(lap[key])
    return None


def _lap_speed(lap: dict[str, Any]) -> float | None:
    if lap.get("averageSpeed"):
        return float(lap["averageSpeed"])
    dist, dur = lap.get("distance"), _lap_duration(lap)
    if dist and dur:
        return float(dist) / dur
    return None


def _lap_hr(lap: dict[str, Any]) -> int | None:
    for key in ("averageHR", "averageHr", "avgHr"):
        if lap.get(key) is not None:
            return int(round(float(lap[key])))
    return None


def _lap_hr_floor(lap: dict[str, Any]) -> int | None:
    for key in ("minHR", "minHr"):
        if lap.get(key) is not None:
            return int(round(float(lap[key])))
    return _lap_hr(lap)


def _rep_section(reps: list[dict[str, Any]], method_note: str | None) -> Section:
    rows: list[Row] = []
    base = _pace_s(_lap_speed(reps[0])) if reps else None
    for i, lap in enumerate(reps, 1):
        pace_s = _pace_s(_lap_speed(lap))
        vs = "—"
        if i > 1 and base and pace_s:
            vs = _pct((pace_s - base) / base)
        hr = _lap_hr(lap)
        rows.append(
            Row([str(i), _pace_str(_lap_speed(lap)) or "—", str(hr) if hr else "—", vs])
        )
    return Section(
        title=None,
        header=["Rep", "Pace", "avgHR", "vs rep 1"],
        rows=rows,
        priority="primary",
        method_note=method_note,
    )


def _recovery_line(recs: list[dict[str, Any]]) -> str | None:
    if not recs:
        return None
    durs = [d for d in (_lap_duration(r) for r in recs) if d is not None]
    floors = [f for f in (_lap_hr_floor(r) for r in recs) if f is not None]
    parts = []
    if durs:
        parts.append(f"Recoveries {_dur(statistics.fmean(durs))} avg")
    if floors:
        lo, hi = min(floors), max(floors)
        span = str(lo) if lo == hi else f"{lo}–{hi}"
        parts.append(f"HR fell to {span} between reps")
    return ", ".join(parts) + "." if parts else None


def _manual_clusters(
    laps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Alternating fast/slow lap detection: split lap speeds into two clusters
    around the mean; accept when strictly alternating and the cluster means
    are separated by more than one stddev of all lap speeds."""
    timed = [(lap, _lap_speed(lap)) for lap in laps]
    timed = [(lap, s) for lap, s in timed if s is not None]
    if len(timed) < 4:
        return None
    speeds = [s for _, s in timed]
    mean, sd = statistics.fmean(speeds), statistics.pstdev(speeds)
    if sd == 0:
        return None
    classes = [s > mean for _, s in timed]
    if not all(classes[i] != classes[i + 1] for i in range(len(classes) - 1)):
        return None
    fast = [lap for (lap, _), c in zip(timed, classes, strict=True) if c]
    slow = [lap for (lap, _), c in zip(timed, classes, strict=True) if not c]
    if len(fast) < 2 or not slow:
        return None
    fast_mean = statistics.fmean(_lap_speed(lap) for lap in fast)  # type: ignore[misc]
    slow_mean = statistics.fmean(_lap_speed(lap) for lap in slow)  # type: ignore[misc]
    if fast_mean - slow_mean <= sd:
        return None
    return fast, slow


def _splits_sections(payload: Any, activity_id: int) -> list[Section]:
    laps = _laps(payload)
    if not laps:
        return [Section(None, None, prose="no splits recorded for this session.")]

    active = [lap for lap in laps if _lap_type(lap) == "INTERVAL_ACTIVE"]
    if active:
        recs = [lap for lap in laps if _lap_type(lap) in _RECOVERY_TYPES]
        sections = [_rep_section(active, None)]
        rec_line = _recovery_line(recs)
        if rec_line:
            sections.append(Section(None, None, prose=rec_line))
        return sections

    clusters = _manual_clusters(laps)
    if clusters is not None:
        fast, slow = clusters
        sections = [_rep_section(fast, "manual laps — interval structure inferred")]
        rec_line = _recovery_line(slow)
        if rec_line:
            sections.append(Section(None, None, prose=rec_line))
        return sections

    rows = []
    for i, lap in enumerate(laps[:12], 1):
        dist = lap.get("distance")
        rows.append(
            Row(
                [
                    str(i),
                    f"{dist / 1000:.2f}" if dist else "—",
                    _dur(_lap_duration(lap)),
                    _pace_str(_lap_speed(lap)) or "—",
                    str(_lap_hr(lap) or "—"),
                ]
            )
        )
    sections = [
        Section(None, None, prose="no interval structure detected — freeform session."),
        Section(
            title=None,
            header=["Split", "km", "Time", "Pace", "avgHR"],
            rows=rows,
            priority="primary",
            overflow_hint=(
                f"garmin_raw(source='activity_splits', activity_id={activity_id}) for all"
            ),
        ),
    ]
    if len(laps) > 12:
        sections.append(
            Section(
                None,
                None,
                prose=(
                    f"({len(laps) - 12} more splits — garmin_raw("
                    f"source='activity_splits', activity_id={activity_id}) for all)"
                ),
            )
        )
    return sections


# ---------------------------------------------------------------------------
# full detail (HR/pace curve)
# ---------------------------------------------------------------------------

def _downsample(values: list[float], n: int) -> list[float]:
    if len(values) <= n:
        return values
    idx = [round(i * (len(values) - 1) / (n - 1)) for i in range(n)]
    return [values[i] for i in dict.fromkeys(idx)]


def _curve_section(payload: Any) -> Section:
    descs = payload.get("metricDescriptors") if isinstance(payload, dict) else None
    metrics = payload.get("activityDetailMetrics") if isinstance(payload, dict) else None
    if not isinstance(descs, list) or not isinstance(metrics, list):
        return Section(None, None, prose="HR/pace streams not present in details payload.")
    index = {
        d.get("key"): d.get("metricsIndex")
        for d in descs
        if isinstance(d, dict) and d.get("metricsIndex") is not None
    }

    def series(key: str) -> list[float]:
        i = index.get(key)
        if i is None:
            return []
        out = []
        for m in metrics:
            vals = m.get("metrics") if isinstance(m, dict) else None
            if isinstance(vals, list) and i < len(vals) and vals[i] is not None:
                out.append(float(vals[i]))
        return out

    lines = []
    hr = series("directHeartRate")
    if hr:
        lines.append(f"HR (bpm): {arrow_series(hr, max_points=20)}")
    speed = [s for s in series("directSpeed") if s > 0]
    if speed:
        paces = [_pace_str(s) for s in _downsample(speed, 20)]
        lines.append("Pace (/km): " + "→".join(p for p in paces if p))
    if not lines:
        return Section(None, None, prose="HR/pace streams not present in details payload.")
    return Section(
        title="Curve (20-pt downsample)",
        header=None,
        prose="\n".join(lines),
        priority="secondary",
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

async def run(
    ctx,
    activity_id: int | None = None,
    date: str | None = None,
    sport: str | None = None,
    detail: str = "standard",
) -> str:
    await ctx.ensure_ready()
    if detail not in CAPS:
        return (
            f"detail must be one of 'standard', 'splits', 'full' (got '{detail}'). "
            f"Example: garmin_activity(detail='splits')"
        )

    resolved = _resolve(ctx, activity_id, date, sport)
    if isinstance(resolved, str):
        banner = ctx.banner()
        return f"{banner}\n\n{resolved}" if banner else resolved
    act, how = resolved.act, resolved.how
    act_id = act["activity_id"]

    fam = sport_family(act.get("sport") or "")
    sport_title = _FAMILY_TITLE.get(fam) or (act.get("sport") or "Activity").replace(
        "_", " "
    ).title()
    head_bits = []
    hhmm = _start_hhmm(act.get("start_local"))
    if hhmm:
        head_bits.append(hhmm)
    head_bits += [f"id {act_id}", f"selected: {how}"]
    name = act.get("name")
    title = f'{sport_title} — "{name}"' if name else sport_title
    title += f" ({' · '.join(head_bits)})"

    sections: list[Section] = [Section(None, None, prose=_stats_line(act))]

    if detail in ("splits", "full"):
        try:
            payload = await ctx.fetch_raw(f"/activity-service/activity/{act_id}/splits")
        except GarminAuthError:
            raise
        except Exception:
            payload = None
        if payload is None:
            sections.append(
                Section(
                    None, None,
                    prose=(
                        "splits unavailable (live fetch failed — try garmin_sync() "
                        "or retry in a minute if rate-limited)."
                    ),
                )
            )
        else:
            sections.extend(_splits_sections(payload, act_id))

    if detail == "full":
        try:
            details = await ctx.fetch_raw(f"/activity-service/activity/{act_id}/details")
        except GarminAuthError:
            raise
        except Exception:
            details = None
        if details is None:
            sections.append(
                Section(
                    None, None,
                    prose=(
                        "HR/pace curve unavailable (live fetch failed — try garmin_sync() "
                        "or retry in a minute if rate-limited)."
                    ),
                )
            )
        else:
            sections.append(_curve_section(details))

    zones = _zones_line(act)
    if zones:
        sections.append(Section(None, None, prose=zones, priority="secondary"))
    sections.append(Section(None, None, prose=_compliance_line(ctx.store, act)))
    if act.get("rpe") is None:
        sections.append(
            Section(
                None, None,
                prose=(
                    "Ask the athlete how it felt if RPE is missing from both the log "
                    f"and the watch\n→ garmin_log(rpe=..., activity_id={act_id})"
                ),
            )
        )

    if detail == "standard":
        next_steps = [
            f'garmin_activity(activity_id={act_id}, detail="splits") for all laps',
            "garmin_activities() to browse the log",
        ]
    elif detail == "splits":
        next_steps = [
            f'garmin_activity(activity_id={act_id}, detail="full") for the HR/pace curve',
            "garmin_activities() to browse the log",
        ]
    else:
        next_steps = [
            "garmin_activities() to browse the log",
            "garmin_brief() for today's readiness",
        ]

    report = Report(
        title=title,
        date=act["date"],
        data_as_of=ctx.data_as_of(),
        verdict=_comparison_verdict(act, _most_similar(ctx.store, act)),
        banner=ctx.banner(),
        sections=sections,
        next_steps=next_steps,
    )
    return render(report, CAPS[detail])
