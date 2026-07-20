"""garmin_log — local write of subjective data (DESIGN §2.4, cap 120).

Stores RPE / Hooper wellness / notes / illness+injury flags in wellness_log.
An explicit rpe always attaches to an activity (given id, or the log date's
single activity) and overrides the watch report (rpe_source='athlete', §3.1).
Returns a short plain string, banner-prefixed when an alert is active (§4.4).

Store-API notes (store.py is frozen for this phase):
- wellness_log has no `mood` column — a provided mood is folded into the
  stored note as "[mood N/7]" and echoed in the response.
- No public resolve/open-flag readers beyond unresolved_injuries(); this
  module uses the Store's internal query/upsert helpers (_all/_upsert) on
  wellness_log rather than emulating resolution with an extra log row, so
  resolved=1 is real and the brief's live log reads stay correct.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from typing import Any

from fartlek.render.renderer import estimate_tokens, format_date

CAP_TOKENS = 120

_SPORT_LABEL = {
    "running": "run",
    "treadmill_running": "run",
    "trail_running": "run",
    "cycling": "ride",
    "swimming": "swim",
    "lap_swimming": "swim",
    "strength_training": "strength",
}
_HOOPER = ("fatigue", "soreness", "stress", "sleep_quality")


def _finish(banner: str | None, body: str, cap: int = CAP_TOKENS) -> str:
    text = f"{banner}\n\n{body}" if banner else body
    if estimate_tokens(text) > cap:
        text = text[: int(cap * 3.2) - 2].rstrip() + " …"
    return text


def _short(date: str) -> str:
    """'2026-07-17' → 'Fri 07-17' (table/error form, §5 rule 3)."""
    return f"{format_date(date)[:3]} {date[5:]}"


def _sport(activity: dict[str, Any]) -> str:
    return _SPORT_LABEL.get(str(activity.get("sport") or ""), str(activity.get("sport") or "session"))


def _trunc(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _open_flags(store, kind: str | None = None) -> list[dict[str, Any]]:
    """Unresolved flag rows, oldest first."""
    return store.open_flags(kind)


def _mark_resolved(store, row_id: int) -> None:
    store.resolve_log(row_id)


async def run(
    ctx,
    *,
    date: str | None = None,
    rpe: int | None = None,
    fatigue: int | None = None,
    soreness: int | None = None,
    stress: int | None = None,
    mood: int | None = None,
    sleep_quality: int | None = None,
    note: str | None = None,
    flag: str | None = None,
    resolve_flag: bool = False,
    activity_id: int | None = None,
) -> str:
    await ctx.ensure_ready()
    banner = ctx.banner()
    today = ctx.today()

    log_date = date or today
    try:
        _date.fromisoformat(log_date)
    except ValueError:
        return _finish(
            banner,
            f"date must be YYYY-MM-DD (got '{log_date}'). Today is {format_date(today)}. "
            "Example: garmin_log(date='2026-07-19', rpe=6)",
        )

    # --- range re-checks (schema-enforced upstream, corrective here) ---
    if rpe is not None and not 1 <= rpe <= 10:
        return _finish(banner, f"rpe must be 1-10 (got {rpe}). Example: garmin_log(rpe=6)")
    wellness = {
        "fatigue": fatigue,
        "soreness": soreness,
        "stress": stress,
        "mood": mood,
        "sleep_quality": sleep_quality,
    }
    for name, value in wellness.items():
        if value is not None and not 1 <= value <= 7:
            return _finish(
                banner, f"{name} must be 1-7 (got {value}). Example: garmin_log({name}=4)"
            )

    if flag is not None and flag not in ("illness", "injury"):
        return _finish(banner, f"flag must be 'illness' or 'injury' (got '{flag}').")
    if resolve_flag and flag is None:
        return _finish(
            banner,
            "resolve_flag needs the flag kind to close. "
            "Example: garmin_log(flag='injury', resolve_flag=True)",
        )

    has_payload = any(
        v is not None for v in (rpe, fatigue, soreness, stress, mood, sleep_quality, note)
    ) or (flag is not None and not resolve_flag)
    if not has_payload and not resolve_flag:
        return _finish(
            banner,
            "Nothing to log — provide rpe, wellness scores (fatigue/soreness/stress/mood/"
            "sleep_quality, each 1-7), a note, or a flag. Example: garmin_log(rpe=6)",
        )

    # --- resolve path ---
    resolved_msg = None
    if resolve_flag:
        open_rows = _open_flags(ctx.store, flag)
        if not open_rows:
            others = _open_flags(ctx.store)
            listing = (
                ", ".join(
                    f"{r['flag']} since {_short(r['date'])}"
                    + (f" (\"{_trunc(r['note'], 30)}\")" if r.get("note") else "")
                    for r in others
                )
                or "none"
            )
            return _finish(
                banner, f"No open {flag} flag to resolve. Open flags: {listing}."
            )
        newest = open_rows[-1]
        _mark_resolved(ctx.store, int(newest["id"]))
        opened = _short(str(newest["date"]))
        note_bit = f", \"{_trunc(newest['note'], 30)}\"" if newest.get("note") else ""
        resolved_msg = f"Resolved {flag} flag (open since {opened}{note_bit})"

    # --- rpe → activity wiring ---
    attached: dict[str, Any] | None = None
    if rpe is not None:
        if activity_id is not None:
            attached = ctx.store.get_activity(activity_id)
            if attached is None:
                all_acts = ctx.store.list_activities("0000-01-01", "9999-12-31")
                if not all_acts:
                    return _finish(
                        banner,
                        f"No activity {activity_id} — the store has no activities yet. "
                        "Run garmin_sync() first.",
                    )
                nearest = sorted(all_acts, key=lambda a: abs(int(a["activity_id"]) - activity_id))[:2]
                options = ", ".join(
                    f"garmin_activity(activity_id={a['activity_id']}) "
                    f"({_short(str(a['date']))}, {_sport(a)})"
                    for a in nearest
                )
                return _finish(
                    banner, f"No activity {activity_id} in the store. Nearest: {options}"
                )
        else:
            day_acts = ctx.store.list_activities(log_date, log_date)
            if len(day_acts) == 1:
                attached = day_acts[0]
                activity_id = int(attached["activity_id"])
            elif not day_acts:
                return _finish(
                    banner,
                    f"No activity on {_short(log_date)} to attach rpe to — pass "
                    "activity_id (find it with garmin_activities()), or log wellness without rpe.",
                )
            else:
                options = ", ".join(
                    f"garmin_log(rpe={rpe}, activity_id={a['activity_id']}) ({_sport(a)})"
                    for a in day_acts
                )
                return _finish(
                    banner,
                    f"{len(day_acts)} activities on {_short(log_date)} — specify one: {options}",
                )

    # --- persist ---
    stored_note = note
    if mood is not None:
        stored_note = (f"{note} " if note else "") + f"[mood {mood}/7]"
    if has_payload:
        row: dict[str, Any] = {
            "date": log_date,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        for key, value in (
            ("rpe", rpe),
            ("fatigue", fatigue),
            ("soreness", soreness),
            ("stress", stress),
            ("sleep_quality", sleep_quality),
            ("note", stored_note),
            ("activity_id", activity_id if attached else None),
        ):
            if value is not None:
                row[key] = value
        if flag is not None and not resolve_flag:
            row["flag"] = flag
        ctx.store.add_log(row)

    if attached is not None:
        ctx.store.upsert_activity(
            {"activity_id": int(attached["activity_id"]), "rpe": rpe, "rpe_source": "athlete"}
        )
        ctx.store.recompute_daily_loads()

    # --- response (§2.4 example house style) ---
    parts: list[str] = []
    if attached is not None:
        piece = f"RPE {rpe}/10 → {_sport(attached)} {attached['activity_id']}"
        duration_s = attached.get("duration_s")
        inner: list[str] = []
        if duration_s:
            inner.append(f"sRPE {round(rpe * float(duration_s) / 60)} AU")
        if attached.get("load") is not None:
            prefix = "alongside " if inner else ""
            inner.append(f"{prefix}Garmin load {round(float(attached['load']))}")
        if inner:
            piece += f" ({', '.join(inner)})"
        parts.append(piece)
    hooper_bits = [f"{k.replace('_', ' ')} {v}/7" for k, v in wellness.items() if v is not None]
    parts.extend(hooper_bits)
    if note:
        parts.append(f'note "{_trunc(note, 60)}"')
    if flag is not None and not resolve_flag:
        parts.append(
            f"{flag} flag opened (garmin_log(flag='{flag}', resolve_flag=True) to close)"
        )

    if parts:
        body = f"Logged {format_date(log_date)}: " + " · ".join(parts) + "."
        if resolved_msg:
            body = f"{resolved_msg}. {body}"
        body += " Feeds tomorrow's readiness and the monotony series."
    else:
        body = f"{resolved_msg}."
    return _finish(banner, body)
