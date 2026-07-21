"""garmin_set_profile — local write of athlete context (DESIGN §2.4, cap 200).

Only provided fields change. Each write group (goal / phase / availability)
also stamps '<group>_set' = today so garmin_athlete can render provenance
("set 2026-07-12 via garmin_set_profile"). Returns a short plain string —
banner-prefixed when a RED/AMBER alert is active (§4.4) — never a full
Report skeleton.
"""
from __future__ import annotations

import re
from datetime import date as _date
from typing import Any

from fartlek.render.renderer import estimate_tokens, format_date

CAP_TOKENS = 200

_GOAL_TIME_RE = re.compile(r"^\d{1,2}:[0-5]\d:[0-5]\d$")
_GOAL_KEYS = ("goal_race_date", "goal_distance", "goal_custom_km", "goal_time")
_PHASE_KEYS = ("phase", "phase_week", "phase_total_weeks")
_DISTANCE_LABEL = {"5k": "5K", "10k": "10K", "half": "Half", "marathon": "Marathon"}


def _finish(banner: str | None, body: str, cap: int = CAP_TOKENS) -> str:
    text = f"{banner}\n\n{body}" if banner else body
    if estimate_tokens(text) > cap:
        text = text[: int(cap * 3.2) - 2].rstrip() + " …"
    return text


def _goal_summary(profile: dict[str, Any]) -> str:
    dist = profile.get("goal_distance")
    if dist == "custom" and profile.get("goal_custom_km"):
        label = f"{float(profile['goal_custom_km']):g} km"
    else:
        label = _DISTANCE_LABEL.get(str(dist or ""), str(dist)) if dist else "race"
    parts = [label]
    if profile.get("goal_race_date"):
        parts.append(format_date(str(profile["goal_race_date"])))
    summary = " ".join(parts)
    if profile.get("goal_time"):
        summary += f", {profile['goal_time']}"
    return summary


async def run(
    ctx,
    *,
    goal_race_date: str | None = None,
    goal_distance: str | None = None,
    goal_custom_km: float | None = None,
    goal_time: str | None = None,
    phase: str | None = None,
    phase_week: int | None = None,
    phase_total_weeks: int | None = None,
    availability_days: int | None = None,
    tid_target: str | None = None,
    lt1_hr_override: int | None = None,
) -> str:
    await ctx.ensure_ready()
    banner = ctx.banner()
    today = ctx.today()

    provided: dict[str, Any] = {
        k: v
        for k, v in {
            "goal_race_date": goal_race_date,
            "goal_distance": goal_distance,
            "goal_custom_km": goal_custom_km,
            "goal_time": goal_time,
            "phase": phase,
            "phase_week": phase_week,
            "phase_total_weeks": phase_total_weeks,
            "availability_days": availability_days,
            "tid_target": tid_target,
            "lt1_hr_override": lt1_hr_override,
        }.items()
        if v is not None
    }
    if not provided:
        return _finish(
            banner,
            "Nothing to update — provide at least one field. "
            "Example: garmin_set_profile(phase='build', phase_week=2, phase_total_weeks=6)",
        )

    # --- validation (corrective errors, §4.3) ---
    if goal_race_date is not None:
        try:
            d = _date.fromisoformat(goal_race_date)
        except ValueError:
            return _finish(
                banner,
                f"goal_race_date must be YYYY-MM-DD (got '{goal_race_date}'). "
                f"Today is {format_date(today)}. "
                "Example: garmin_set_profile(goal_race_date='2026-09-20')",
            )
        if d < _date.fromisoformat(today):
            return _finish(
                banner,
                f"goal_race_date {goal_race_date} is in the past — today is "
                f"{format_date(today)}. Use today's date or a future one.",
            )
    if goal_time is not None and not _GOAL_TIME_RE.match(goal_time):
        return _finish(
            banner,
            f"goal_time must be H:MM:SS (got '{goal_time}'). "
            "Example: garmin_set_profile(goal_time='2:59:00')",
        )

    profile = ctx.store.get_profile()
    effective_distance = goal_distance or profile.get("goal_distance")
    if goal_custom_km is not None and effective_distance != "custom":
        return _finish(
            banner,
            "goal_custom_km applies only with goal_distance='custom' "
            f"(goal_distance is {effective_distance or 'not set'}). "
            "Example: garmin_set_profile(goal_distance='custom', goal_custom_km=25.0)",
        )
    if (
        goal_distance == "custom"
        and goal_custom_km is None
        and "goal_custom_km" not in profile
    ):
        return _finish(
            banner,
            "goal_distance='custom' needs goal_custom_km. "
            "Example: garmin_set_profile(goal_distance='custom', goal_custom_km=25.0)",
        )
    if availability_days is not None and not 1 <= availability_days <= 7:
        return _finish(
            banner,
            f"availability_days must be 1-7 (got {availability_days}). "
            "Example: garmin_set_profile(availability_days=6)",
        )
    if phase_week is not None and phase_week < 1:
        return _finish(
            banner,
            f"phase_week must be ≥1 (got {phase_week}). "
            "Example: garmin_set_profile(phase='build', phase_week=2, phase_total_weeks=6)",
        )
    if phase_total_weeks is not None and phase_total_weeks < 1:
        return _finish(
            banner,
            f"phase_total_weeks must be ≥1 (got {phase_total_weeks}). "
            "Example: garmin_set_profile(phase='build', phase_week=2, phase_total_weeks=6)",
        )
    if lt1_hr_override is not None and not 80 <= lt1_hr_override <= 220:
        return _finish(
            banner,
            f"lt1_hr_override must be a plausible HR in bpm, 80-220 (got {lt1_hr_override}). "
            "Example: garmin_set_profile(lt1_hr_override=155)",
        )

    # --- write only the provided fields, plus set-date stamps ---
    for key, value in provided.items():
        ctx.store.set_profile(key, str(value))
    if any(k in provided for k in _GOAL_KEYS):
        ctx.store.set_profile("goal_set", today)
    if any(k in provided for k in _PHASE_KEYS):
        ctx.store.set_profile("phase_set", today)
    if "availability_days" in provided:
        ctx.store.set_profile("availability_set", today)

    merged = {**profile, **{k: str(v) for k, v in provided.items()}}

    # --- response (§2.4 example house style) ---
    bits: list[str] = []
    if any(k in provided for k in _GOAL_KEYS):
        bits.append("goal " + _goal_summary(merged))
    if any(k in provided for k in _PHASE_KEYS):
        ph = str(merged.get("phase", "none"))
        label = f"phase {ph.capitalize()}" if ph != "none" else "phase none"
        wk, total = merged.get("phase_week"), merged.get("phase_total_weeks")
        if ph != "none" and wk and total:
            label += f" (wk {wk} of {total})"
        elif ph != "none" and wk:
            label += f" (wk {wk})"
        bits.append(label)
    if "availability_days" in provided:
        bits.append(f"availability {availability_days} d/wk")
    if "tid_target" in provided:
        bits.append(f"TID target {tid_target}")
    if "lt1_hr_override" in provided:
        bits.append(f"LT1 override {lt1_hr_override} bpm")
    if not any(k in provided for k in _GOAL_KEYS) and (
        profile.get("goal_race_date") or profile.get("goal_distance")
    ):
        bits.append(f"goal unchanged ({_goal_summary(profile)})")

    body = (
        "Profile updated: "
        + " · ".join(bits)
        + ". These now appear in garmin_brief verdicts."
    )
    return _finish(banner, body)
