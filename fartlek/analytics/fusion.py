"""Readiness fusion + subjective gate + acute override (DESIGN §3.2 #18-19).

Pure functions over store-shaped inputs.

compute_readiness(...) fuses per-marker z-scores vs personal baselines into a
GREEN / AMBER / RED verdict with a one-sentence rationale:

- Weighted z-fusion, weights: hrv .30 · sleep .25 · form .20 · rhr .15 ·
  body_battery .10. Weights renormalize over AVAILABLE markers; the result
  always lists which markers were used.
- Marker inputs (each may be None = unavailable):
  hrv: 7-day rolling mean of hrv_last_night vs the band. The band is
    SELF-COMPUTED ONLY (documented decision): Garmin's shipped baseline is
    not persisted in the days schema, so the band is the 60d mean ± 0.5·SD
    (robust SD = mad_sd from baselines.baseline, raw-ms space) once n ≥ 14
    nights exist; fewer nights → marker unavailable. z-like position: 0 in
    band, negative below scaled by band half-width; ABOVE band contributes 0
    (abnormally high rMSSD is not automatically good — §3.2 #8; it feeds
    convergence, not readiness credit).
  sleep: last night's score z vs 28d baseline, worsened by 14d sleep debt
    (debt_hours/5 subtracted, capped at 2).
  form: form_pct from pmc.form_assessment → 0 in productive/neutral bands,
    negative when < −40% (overload) or > +25% (detraining), scaled /15.
  rhr: −|z| vs 30d baseline (either direction is bad), from
    baselines.rhr_deviation levels: ok 0 · caution −1 · red −2
    (parasympathetic_watch maps to −1: two-sided caution weight, but a
    convergence-only signal never drives RED by itself).
  body_battery: z of wake value vs 30d baseline, clamped ±2. When today's
    wake value is absent, body_battery_high substitutes (same fallback for
    the baseline series) and the marker is disclosed as "Body Battery (high)".
- Fused score = Σ w_i·z_i / Σ w_i over available markers. Verdict:
  fused ≥ −0.5 → GREEN · −0.5 > fused ≥ −1.25 → AMBER · < −1.25 → RED.
  Zero markers available → AMBER + provisional (never a confident GREEN on
  no data).
- provisional=True when fewer than 3 markers available OR the sleep/HRV
  baselines have n < 14 (n reported in the rationale). provisional_n is
  (available_markers, 3) in the first case, (min_deficient_n, 14) in the
  second (marker count takes precedence when both apply).

apply_gates(verdict, log_entries, inputs) — §3.2 #19, applied AFTER fusion:
- entries with flag='illness' not resolved → cap at RED ("rest pending
  symptoms"); flag='injury' not resolved → cap at AMBER. The CALLER selects
  the time window (same-day logs_for(date) + unresolved_injuries()).
- acute single-marker escalation (no streak needed): rhr_dev delta ≥ +7 bpm,
  or single-night HRV z ≤ −2.5 or a 90d low, or sleep < 4h → at least AMBER
  with possible-illness-onset language; TWO severe acute markers → RED.
  (HRV z ≤ −2.5 and 90d-low together still count as ONE acute marker.)
- gates only ever downgrade (GREEN→AMBER→RED), never upgrade.

Returns a dict the brief tool renders directly:
{verdict: 'GREEN'|'AMBER'|'RED', rationale: str, markers_used: [str],
 provisional: bool, provisional_n: (int, int) | None,
 gated_by: str | None, modification: str | None}
plus diagnostic extras: score (fused float | None) and zs (per-marker z map,
canonical keys). `modification` is a concrete server-computed action for
AMBER/RED (§4.4), e.g. "replace today's quality with 40 min easy below HR
{easy_ceiling}"; easy_ceiling = profile lt1_hr_override when set, else
80% of the 90d max observed daily max_hr, else omitted from the text.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import timedelta
from statistics import fmean
from typing import Any

from fartlek.analytics import baselines as baselines_mod
from fartlek.analytics import pmc as pmc_mod

WEIGHTS = {"hrv": 0.30, "sleep": 0.25, "form": 0.20, "rhr": 0.15, "body_battery": 0.10}

_ORDER = ("hrv", "sleep", "form", "rhr", "body_battery")
_DISPLAY = {
    "hrv": "HRV",
    "sleep": "sleep",
    "form": "form",
    "rhr": "RHR",
    "body_battery": "Body Battery",
}
_VERDICTS = ("GREEN", "AMBER", "RED")
_SEV = {"GREEN": 0, "AMBER": 1, "RED": 2}
_RHR_Z = {"ok": 0.0, "caution": -1.0, "red": -2.0, "parasympathetic_watch": -1.0}

GREEN_FLOOR = -0.5
AMBER_FLOOR = -1.25
MIN_MARKERS = 3
BASELINE_WARM_N = 14
DEBT_DIVISOR = 5.0
DEBT_PENALTY_CAP = 2.0
BB_CLAMP = 2.0
ACUTE_RHR_DELTA = 7.0
ACUTE_HRV_Z = -2.5
ACUTE_SLEEP_H = 4.0
DEFAULT_SLEEP_NEED_H = 8.0
EASY_CEILING_FRACTION = 0.80


def marker_inputs(store: Any, date: str) -> dict[str, Any]:
    """Assemble raw marker inputs from the store for `date` (today).

    The only store-aware function; the fusion itself is pure. Every piece is
    None-safe (empty store → all-None inputs, debt 0.0). Returned keys:
    date · hrv_series (60d) · hrv_band {low, high, n}|None (self-computed
    60d mean ± 0.5·mad_sd, n≥14) · hrv_roll7 · hrv_last_night ·
    hrv_last_night_z (vs 60d baseline) · hrv_90d_low (bool, needs ≥14 prior
    nights) · sleep_today (score) · sleep_base (28d baseline dict) ·
    sleep_debt_h (Σ max(0, need|8 − duration) over last 14d) ·
    sleep_duration_h · form (pmc.form_assessment dict|None) · rhr_dev
    (baselines.rhr_deviation dict) · bb_today · bb_base (30d) · bb_source
    ('wake'|'high') · easy_ceiling (int|None).
    """
    day = store.get_day(date) or {}
    out: dict[str, Any] = {"date": date}

    # --- HRV ---
    hrv_series = store.get_series("hrv_last_night", date, 60)
    out["hrv_series"] = hrv_series
    b60 = baselines_mod.baseline(hrv_series, date, 60)
    band = None
    if b60 is not None and b60["n"] >= BASELINE_WARM_N:
        half = 0.5 * b60["mad_sd"]
        band = {"low": b60["mean"] - half, "high": b60["mean"] + half, "n": b60["n"]}
    out["hrv_band"] = band
    start7 = (_date.fromisoformat(date) - timedelta(days=6)).isoformat()
    last7 = [v for d, v in hrv_series if d >= start7]
    out["hrv_roll7"] = fmean(last7) if last7 else None
    last_night = day.get("hrv_last_night")
    out["hrv_last_night"] = last_night
    out["hrv_last_night_z"] = (
        baselines_mod.zscore(float(last_night), b60)
        if (last_night is not None and b60 is not None)
        else None
    )
    prior90 = [v for d, v in store.get_series("hrv_last_night", date, 90) if d != date]
    out["hrv_90d_low"] = bool(
        last_night is not None
        and len(prior90) >= BASELINE_WARM_N
        and float(last_night) < min(prior90)
    )

    # --- sleep ---
    out["sleep_today"] = day.get("sleep_score")
    out["sleep_base"] = baselines_mod.baseline(
        store.get_series("sleep_score", date, 28), date, 28
    )
    durations = dict(store.get_series("sleep_duration_h", date, 14))
    needs = dict(store.get_series("sleep_need_h", date, 14))
    out["sleep_debt_h"] = round(
        sum(max(0.0, needs.get(d, DEFAULT_SLEEP_NEED_H) - v) for d, v in durations.items()), 2
    )
    out["sleep_duration_h"] = day.get("sleep_duration_h")

    # --- form ---
    pmc_rows = store.get_pmc(date, 60)
    if pmc_rows:
        last = pmc_rows[-1]
        ctl_series = [(r["date"], r["ctl"]) for r in pmc_rows]
        out["form"] = pmc_mod.form_assessment(last["ctl"], last["tsb"], ctl_series)
    else:
        out["form"] = None

    # --- rhr ---
    out["rhr_dev"] = baselines_mod.rhr_deviation(
        store.get_series("resting_hr", date, 120), date
    )

    # --- body battery (wake, high fallback) ---
    bb_today = day.get("body_battery_wake")
    bb_source = "wake"
    if bb_today is None:
        bb_today = day.get("body_battery_high")
        bb_source = "high"
    bb_metric = "body_battery_wake" if bb_source == "wake" else "body_battery_high"
    out["bb_today"] = bb_today
    out["bb_base"] = baselines_mod.baseline(store.get_series(bb_metric, date, 30), date, 30)
    out["bb_source"] = bb_source

    # --- easy ceiling for modifications (§4.4) ---
    ceiling: int | None = None
    lt1 = (store.get_profile() or {}).get("lt1_hr_override")
    if lt1:
        try:
            ceiling = int(float(lt1))
        except (TypeError, ValueError):
            ceiling = None
    if ceiling is None:
        # Activity max HR, not the days column: after a cold start the days
        # table only has a few (possibly easy) days, and 80% of an easy-day
        # max is dangerously low advice. Activities carry real session maxima.
        start = (_date.fromisoformat(date) - timedelta(days=90)).isoformat()
        session_max = [
            a["max_hr"]
            for a in store.list_activities(start, date)
            if a.get("max_hr") is not None
        ]
        if not session_max:
            series = store.get_series("max_hr", date, 90)
            session_max = [v for _, v in series]
        if session_max:
            ceiling = round(EASY_CEILING_FRACTION * max(session_max))
    out["easy_ceiling"] = ceiling
    return out


def _marker_zs(inputs: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    """(z per available marker, phrase per negative marker)."""
    zs: dict[str, float] = {}
    phrases: dict[str, str] = {}

    band, roll = inputs.get("hrv_band"), inputs.get("hrv_roll7")
    if band is not None and roll is not None:
        half = max((band["high"] - band["low"]) / 2.0, 1e-9)
        if roll < band["low"]:
            zs["hrv"] = (roll - band["low"]) / half
            phrases["hrv"] = (
                f"HRV 7d avg {roll:.0f} ms below band {band['low']:.0f}-{band['high']:.0f}"
            )
        else:  # in band or above band — above is never credited (§3.2 #8)
            zs["hrv"] = 0.0

    score, base = inputs.get("sleep_today"), inputs.get("sleep_base")
    if score is not None and base is not None:
        z_raw = baselines_mod.zscore(float(score), base)
        debt = max(float(inputs.get("sleep_debt_h") or 0.0), 0.0)
        z = z_raw - min(debt / DEBT_DIVISOR, DEBT_PENALTY_CAP)
        zs["sleep"] = z
        if z <= -0.5:
            phrase = f"sleep score {score:.0f} ({z_raw:+.1f}σ vs 28d)"
            if debt >= 0.5:
                phrase += f" with {debt:.1f}h 14d debt"
            phrases["sleep"] = phrase

    form = inputs.get("form")
    if form is not None and form.get("form_pct") is not None:
        fp = float(form["form_pct"])
        if fp < -40:
            zs["form"] = (fp + 40.0) / 15.0
            phrases["form"] = f"form {fp:+.0f}% (overload)"
        elif fp > 25:
            zs["form"] = (25.0 - fp) / 15.0
            phrases["form"] = f"form {fp:+.0f}% (detraining risk)"
        else:
            zs["form"] = 0.0

    rd = inputs.get("rhr_dev")
    if rd is not None and rd.get("level") in _RHR_Z:
        z = _RHR_Z[rd["level"]]
        zs["rhr"] = z
        if z < 0 and rd.get("delta") is not None:
            phrases["rhr"] = f"RHR {rd['delta']:+.0f} bpm vs 30d median"

    bb, bbase = inputs.get("bb_today"), inputs.get("bb_base")
    if bb is not None and bbase is not None:
        z = min(BB_CLAMP, max(-BB_CLAMP, baselines_mod.zscore(float(bb), bbase)))
        zs["body_battery"] = z
        if z <= -0.5:
            phrases["body_battery"] = f"Body Battery {bb:.0f} at wake ({z:+.1f}σ low)"

    return zs, phrases


def _modification(verdict: str, inputs: dict[str, Any], *, illness: bool = False) -> str | None:
    """Concrete server-computed action for AMBER/RED (§4.4); None for GREEN."""
    if verdict == "GREEN":
        return None
    if verdict == "RED":
        if illness:
            return "rest today — no training pending symptoms; reassess tomorrow"
        return "rest today (no training; walking is fine); reassess tomorrow morning"
    ceiling = inputs.get("easy_ceiling")
    if ceiling:
        return f"replace today's quality with 40 min easy below HR {ceiling}; reassess tomorrow"
    return "replace today's quality with 40 min easy at conversational effort; reassess tomorrow"


def compute_readiness(inputs: dict[str, Any]) -> dict[str, Any]:
    zs, phrases = _marker_zs(inputs)

    if zs:
        total_w = sum(WEIGHTS[m] for m in zs)
        score = sum(WEIGHTS[m] * z for m, z in zs.items()) / total_w
        if score >= GREEN_FLOOR:
            verdict = "GREEN"
        elif score >= AMBER_FLOOR:
            verdict = "AMBER"
        else:
            verdict = "RED"
    else:
        score = None
        verdict = "AMBER"  # zero data never earns a confident GREEN

    markers_used = []
    for m in _ORDER:
        if m in zs:
            if m == "body_battery" and inputs.get("bb_source") == "high":
                markers_used.append("Body Battery (high)")
            else:
                markers_used.append(_DISPLAY[m])

    # Provisional: marker count first, then baseline warmth (sleep/HRV n<14).
    warm_ns = []
    if "sleep" in zs and inputs.get("sleep_base"):
        warm_ns.append(inputs["sleep_base"]["n"])
    if "hrv" in zs and inputs.get("hrv_band"):
        warm_ns.append(inputs["hrv_band"]["n"])
    deficient = [n for n in warm_ns if n < BASELINE_WARM_N]
    provisional_n: tuple[int, int] | None = None
    if len(zs) < MIN_MARKERS:
        provisional_n = (len(zs), MIN_MARKERS)
        suffix = f" (provisional: {len(zs)}/{MIN_MARKERS} markers)"
    elif deficient:
        provisional_n = (min(deficient), BASELINE_WARM_N)
        suffix = f" (baselines warming, n={provisional_n[0]}/{BASELINE_WARM_N})"
    else:
        suffix = ""

    neg = [phrases[m] for m in _ORDER if m in phrases and zs.get(m, 0.0) <= -0.5]
    if score is None:
        rationale = "no readiness markers available yet — store still warming"
    elif neg:
        rationale = f"fused {score:+.2f}: " + "; ".join(neg[:3])
    else:
        rationale = f"fused {score:+.2f}: all {len(zs)} markers at or near personal baselines"
    rationale += suffix

    return {
        "verdict": verdict,
        "rationale": rationale,
        "markers_used": markers_used,
        "provisional": provisional_n is not None,
        "provisional_n": provisional_n,
        "gated_by": None,
        "modification": _modification(verdict, inputs),
        "score": score,
        "zs": zs,
    }


def _acute_flags(inputs: dict[str, Any]) -> list[str]:
    """Severe acute markers (§3.2 #19); HRV z-collapse and 90d-low are ONE flag."""
    flags: list[str] = []
    delta = (inputs.get("rhr_dev") or {}).get("delta")
    if delta is not None and delta >= ACUTE_RHR_DELTA:
        flags.append(f"RHR {delta:+.0f} bpm overnight")
    z = inputs.get("hrv_last_night_z")
    if z is not None and z <= ACUTE_HRV_Z:
        flags.append(f"overnight HRV {z:.1f}σ below baseline")
    elif inputs.get("hrv_90d_low"):
        flags.append("overnight HRV at a 90d low")
    dur = inputs.get("sleep_duration_h")
    if dur is not None and float(dur) < ACUTE_SLEEP_H:
        flags.append(f"only {float(dur):.1f}h sleep")
    return flags


def apply_gates(
    readiness: dict[str, Any],
    log_entries: list[dict[str, Any]],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    out = dict(readiness)
    illness = any(
        e.get("flag") == "illness" and not e.get("resolved") for e in log_entries
    )
    injury = any(
        e.get("flag") == "injury" and not e.get("resolved") for e in log_entries
    )
    acute = _acute_flags(inputs)

    # (target severity, gated_by, rationale sentence, illness wording) —
    # ordered by priority; max() keeps the first maximal entry.
    candidates: list[tuple[int, str, str, bool]] = []
    if illness:
        candidates.append((2, "illness log", "illness logged — rest pending symptoms", True))
    if len(acute) >= 2:
        candidates.append(
            (2, "acute override",
             "two acute red flags (" + " + ".join(acute) + ") — possible illness onset", False)
        )
    if injury:
        candidates.append((1, "injury log", "unresolved injury on file", False))
    if len(acute) == 1:
        candidates.append(
            (1, "acute override", f"acute red flag: {acute[0]} — possible illness onset", False)
        )
    if not candidates:
        return out

    pre = _SEV[out["verdict"]]
    target, name, sentence, ill_wording = max(candidates, key=lambda c: c[0])
    final = max(pre, target)  # gates only ever downgrade, never upgrade
    out["verdict"] = _VERDICTS[final]
    if target >= pre:  # the gate determines (or co-determines) the verdict
        out["gated_by"] = name
        out["rationale"] = f"{out['rationale']}; {sentence}"
    out["modification"] = _modification(
        out["verdict"], inputs, illness=ill_wording and final == 2
    )
    return out
