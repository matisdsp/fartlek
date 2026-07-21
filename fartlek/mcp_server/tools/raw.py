"""garmin_raw — bounded, compacted escape hatch to named Garmin sources.

DESIGN §2.4: one live GET per call, then aggressive compaction — nulls,
empties, and boilerplate keys stripped; floats rounded to 3 significant
places; any list longer than max_points even-stride downsampled keeping
first/last, with a '(downsampled N→M)' disclosure naming the exact narrower
call that retrieves more. `series` (sleep_detail only) returns exactly one
sleep series. The 5,000-token cap is enforced by re-downsampling (halving
max_points), each step disclosed; a payload that cannot shrink returns a
corrective error, never a truncated blob.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from fartlek.health.exceptions import GarminAuthError
from fartlek.render.renderer import Report, Section, format_date, render

CAP = 5000
MAX_POINTS_CEILING = 200

_ACTIVITY_SOURCES = {
    "activity_summary": "/activity-service/activity/{id}",
    "activity_splits": "/activity-service/activity/{id}/splits",
    "activity_zones": "/activity-service/activity/{id}/hrTimeInZones",
    "weather": "/activity-service/activity/{id}/weather",
}

# series name -> key in the dailySleepData payload (DESIGN §2.4)
_SLEEP_SERIES = {
    "hypnogram": "sleepLevels",
    "hr": "sleepHeartRate",
    "movement": "sleepMovement",
    "spo2": "wellnessEpochSPO2DataDTOList",
    "respiration": "wellnessEpochRespirationDataDTOList",
    "stress": "sleepStress",
}

_DROP_KEYS = {
    "userProfilePK",
    "userProfilePk",
    "userProfileId",
    "ownerId",
    "rule",
    "accessControlRuleDTO",
    "sleepQualityTypePK",
    "sleepResultTypePK",
}


def _is_boilerplate(key: str) -> bool:
    low = key.lower()
    return key in _DROP_KEYS or low.startswith("privacy") or low.endswith("uuid")


def _round_float(x: float) -> float | int:
    """3 significant places, never truncating integer digits; integral → int."""
    if x == 0:
        return 0
    if math.isnan(x) or math.isinf(x):
        return x
    digits = max(0, 2 - int(math.floor(math.log10(abs(x)))))
    r = round(x, digits)
    return int(r) if r == int(r) else r


def _downsample(lst: list, m: int) -> tuple[list, bool]:
    """Even-stride pick of ≤m items, always keeping first and last."""
    n = len(lst)
    if n <= m:
        return lst, False
    if m <= 1:
        return [lst[-1]], True
    idx = dict.fromkeys(round(i * (n - 1) / (m - 1)) for i in range(m))
    return [lst[i] for i in idx], True


def _compact(node: Any, date: str, points: int,
             notices: list[tuple[str, int, int]], path: str = "payload") -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if _is_boilerplate(k):
                continue
            if k == "calendarDate" and v == date:  # duplicate of the header date
                continue
            cv = _compact(v, date, points, notices, path=k)
            if cv is None or cv == "" or cv == [] or cv == {}:
                continue
            out[k] = cv
        return out
    if isinstance(node, list):
        kept, did = _downsample(node, points)
        if did:
            notices.append((path, len(node), len(kept)))
        out_l = []
        for v in kept:
            cv = _compact(v, date, points, notices, path=path)
            if cv is None or cv == "" or cv == [] or cv == {}:
                continue
            out_l.append(cv)
        return out_l
    if isinstance(node, bool):
        return node
    if isinstance(node, float):
        return _round_float(node)
    return node


def _error(ctx, msg: str) -> str:
    banner = ctx.banner()
    return f"{banner}\n\n{msg}" if banner else msg


def _narrower_hint(source: str, series: str | None, activity_id: int | None,
                   points: int, notice_paths: list[str]) -> str:
    if source == "sleep_detail" and series is None:
        rev = {v: k for k, v in _SLEEP_SERIES.items()}
        for p in notice_paths:
            if p in rev:
                return (
                    f"more points for one series: garmin_raw(source='sleep_detail', "
                    f"series='{rev[p]}', max_points={MAX_POINTS_CEILING})"
                )
    if points < MAX_POINTS_CEILING:
        args = [f"source='{source}'"]
        if activity_id is not None:
            args.append(f"activity_id={activity_id}")
        if series is not None:
            args.append(f"series='{series}'")
        args.append(f"max_points={MAX_POINTS_CEILING}")
        return f"more points: garmin_raw({', '.join(args)})"
    return f"already at the max_points={MAX_POINTS_CEILING} ceiling"


def _format_notices(notices: list[tuple[str, int, int]], hint: str) -> str:
    uniq = list(dict.fromkeys(notices))
    shown = "; ".join(f"{p} downsampled {n}→{m}" for p, n, m in uniq[:6])
    if len(uniq) > 6:
        shown += f"; +{len(uniq) - 6} more series"
    return f"({shown} — {hint})"


async def run(
    ctx,
    source: str,
    date: str | None = None,
    activity_id: int | None = None,
    series: str | None = None,
    max_points: int = 50,
) -> str:
    await ctx.ensure_ready()
    today = ctx.today()

    # --- argument validation (corrective, §4.3) ---
    if series is not None and source != "sleep_detail":
        return _error(ctx, (
            f"series is only valid with source='sleep_detail' (valid series: "
            f"{', '.join(_SLEEP_SERIES)}). For {source}, call "
            f"garmin_raw(source='{source}') without series."
        ))
    if source in _ACTIVITY_SOURCES and activity_id is None:
        return _error(ctx, (
            f"activity_id is required for source='{source}' — find IDs with "
            f"garmin_activities(), then garmin_raw(source='{source}', "
            f"activity_id=<id>)."
        ))
    date = date or today
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return _error(ctx, (
            f"Invalid date '{date}' — use YYYY-MM-DD (today is {today}). "
            f"Example: garmin_raw(source='daily_summary', date='{today}')."
        ))

    clamp_note = None
    points = max(1, min(MAX_POINTS_CEILING, max_points))
    if points != max_points:
        clamp_note = f"(max_points clamped to {points}; range is 1–{MAX_POINTS_CEILING})"

    # --- source registry → one live GET ---
    if source in _ACTIVITY_SOURCES:
        path, params = _ACTIVITY_SOURCES[source].format(id=activity_id), {}
    elif source == "daily_summary":
        path = f"/usersummary-service/usersummary/daily/{ctx.display_name}"
        params = {"calendarDate": date}
    elif source == "sleep_detail":
        path = f"/wellness-service/wellness/dailySleepData/{ctx.display_name}"
        params = {"date": date, "nonSleepBufferMinutes": 60}
    elif source == "hrv_detail":
        path, params = f"/hrv-service/hrv/{date}", {}
    elif source == "stress_detail":
        path, params = f"/wellness-service/wellness/dailyStress/{date}", {}
    elif source == "body_battery":
        path = "/wellness-service/wellness/bodyBattery/reports/daily"
        params = {"startDate": date, "endDate": date}
    elif source == "training_status":
        path, params = f"/metrics-service/metrics/trainingstatus/aggregated/{date}", {}
    elif source == "race_predictions":
        path = f"/metrics-service/metrics/racepredictions/latest/{ctx.display_name}"
        params = {}
    else:
        return _error(ctx, (
            f"Unknown source '{source}' — valid sources: daily_summary, sleep_detail, "
            f"hrv_detail, stress_detail, body_battery, activity_summary, activity_splits, "
            f"activity_zones, training_status, race_predictions, weather."
        ))

    try:
        payload = await ctx.fetch_raw(path, **params)
    except GarminAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 — every live-fetch failure becomes corrective text
        if "429" in str(exc) or "Too Many" in str(exc):
            hint = "rate-limited by Garmin — wait a minute, then retry."
        else:
            hint = "try garmin_sync() to refresh the connection, then retry."
        return _error(ctx, (
            f"Garmin fetch failed for {source} ({type(exc).__name__}). {hint}"
        ))

    if not payload:
        if source in _ACTIVITY_SOURCES:
            return _error(ctx, (
                f"Garmin returned no {source} data for activity_id={activity_id} — "
                f"verify the id with garmin_activities()."
            ))
        return _error(ctx, (
            f"Garmin returned no {source} data for {format_date(date)} — the watch may "
            f"not have synced yet; try garmin_sync() or an earlier date (today is {today})."
        ))

    # --- series extraction (sleep_detail only) ---
    if series is not None:
        key = _SLEEP_SERIES[series]
        if not isinstance(payload, dict) or not payload.get(key):
            available = [
                name for name, k in _SLEEP_SERIES.items()
                if isinstance(payload, dict) and payload.get(k)
            ]
            avail_txt = (
                f"available this night: {', '.join(available)}" if available
                else "no series present this night"
            )
            return _error(ctx, (
                f"series '{series}' ({key}) is not present in the sleep_detail payload "
                f"for {format_date(date)} — {avail_txt}. "
                f"garmin_raw(source='sleep_detail') shows the full compacted payload."
            ))
        payload = {key: payload[key]}

    # --- compaction + cap enforcement by re-downsampling ---
    step_disclosures: list[str] = []
    while True:
        notices: list[tuple[str, int, int]] = []
        compacted = _compact(payload, date, points, notices)
        if compacted == {} or compacted == []:
            return _error(ctx, (
                f"The {source} payload for {format_date(date)} compacted to nothing "
                f"(only nulls/boilerplate) — try garmin_sync() or an earlier date "
                f"(today is {today})."
            ))
        json_text = json.dumps(
            compacted, separators=(",", ":"), ensure_ascii=False, default=str
        )

        prose_lines = [f"```json\n{json_text}\n```"]
        if notices:
            hint = _narrower_hint(source, series, activity_id, points,
                                  [p for p, _, _ in notices])
            prose_lines.append(_format_notices(notices, hint))
        if clamp_note:
            prose_lines.append(clamp_note)
        prose_lines.extend(step_disclosures)

        bits = [f"activity_id={activity_id}" if activity_id is not None else f"date={date}"]
        if series is not None:
            bits.append(f"series='{series}'")
        bits.append(f"max_points={points}")
        report = Report(
            title=f"Raw: {source}",
            date=date,
            data_as_of=ctx.data_as_of(),
            verdict=f"compacted raw {source} · {' · '.join(bits)} — escape-hatch view",
            banner=ctx.banner(),
            sections=[Section(title=None, header=None, prose="\n".join(prose_lines))],
            next_steps=[f"{_next_step(source, activity_id)} for the interpreted view"],
        )
        out = render(report, CAP)
        if json_text[:60] in out:
            return out
        if points <= 2:
            if source == "sleep_detail" and series is None:
                fix = ("request one series: garmin_raw(source='sleep_detail', "
                       "series='hr'|'hypnogram'|'movement'|'spo2'|'respiration'|'stress')")
            else:
                fix = f"{_next_step(source, activity_id)} gives the interpreted view"
            return _error(ctx, (
                f"The {source} payload exceeds the {CAP:,}-token cap even after "
                f"downsampling to {points} points — {fix}."
            ))
        step_disclosures.append(
            f"(re-downsampled to max_points={points // 2} to fit the {CAP:,}-token cap)"
        )
        points //= 2


def _next_step(source: str, activity_id: int | None) -> str:
    if source in _ACTIVITY_SOURCES:
        return f"garmin_activity(activity_id={activity_id})"
    if source in ("training_status", "race_predictions"):
        return "garmin_athlete()"
    return "garmin_brief()"
