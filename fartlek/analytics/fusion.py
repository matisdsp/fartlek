"""Readiness fusion + subjective gate + acute override (DESIGN §3.2 #18-19).

CONTRACT STUB — implement me. Pure functions over store-shaped inputs.

compute_readiness(...) fuses per-marker z-scores vs personal baselines into a
GREEN / AMBER / RED verdict with a one-sentence rationale:

- Weighted z-fusion, weights: hrv .30 · sleep .25 · form .20 · rhr .15 ·
  body_battery .10. Weights renormalize over AVAILABLE markers; the result
  always lists which markers were used.
- Marker inputs (each may be None = unavailable):
  hrv: 7-day rolling mean of hrv_last_night vs the band (band from Garmin's
    baseline if present, else self-computed 60d mean ± 0.5·SD via
    baselines.baseline) → z-like position: 0 in band, negative below,
    scaled by band half-width.
  sleep: last night's score z vs 28d baseline, worsened by 14d sleep debt
    (debt_hours/5 subtracted, capped at 2).
  form: form_pct from pmc.form_assessment → 0 in productive/neutral bands,
    negative when < −40% (overload) or > +25% (detraining), scaled /15.
  rhr: −|z| vs 30d baseline (either direction is bad), from
    baselines.rhr_deviation levels: ok 0 · caution −1 · red −2.
  body_battery: z of wake value vs 30d baseline, clamped ±2.
- Fused score = Σ w_i·z_i / Σ w_i over available markers. Verdict:
  fused ≥ −0.5 → GREEN · −0.5 > fused ≥ −1.25 → AMBER · < −1.25 → RED.
- provisional=True when fewer than 3 markers available OR the sleep/HRV
  baselines have n < 14 (report n in the rationale).

apply_gates(verdict, log_entries, acute) — §3.2 #19, applied AFTER fusion:
- same-day/last-24h wellness_log flag='illness' (unresolved) → cap at RED
  ("rest pending symptoms"); flag='injury' unresolved → cap at AMBER.
- acute single-marker escalation (no streak needed): rhr_delta ≥ +7 bpm, or
  single-night HRV z ≤ −2.5 or 90d low, or sleep < 4h → at least AMBER with
  possible-illness-onset language; TWO severe acute markers → RED.
- gates only ever downgrade (GREEN→AMBER→RED), never upgrade.

Returns a dict the brief tool renders directly:
{verdict: 'GREEN'|'AMBER'|'RED', rationale: str, markers_used: [str],
 provisional: bool, provisional_n: (int, int) | None,
 gated_by: str | None, modification: str | None}
`modification` is a concrete server-computed action for AMBER/RED (§4.4),
e.g. "replace today's quality with 40 min easy below HR {easy_ceiling}".
"""
from __future__ import annotations

from typing import Any

WEIGHTS = {"hrv": 0.30, "sleep": 0.25, "form": 0.20, "rhr": 0.15, "body_battery": 0.10}


def marker_inputs(store: Any, date: str) -> dict[str, Any]:
    """Assemble raw marker inputs from the store for `date` (today).

    Returns {hrv_series, hrv_band, sleep_today, sleep_base, sleep_debt_h,
    form, rhr_dev, bb_today, bb_base, ...} — every piece None-safe. This is
    the only store-aware function; the fusion itself is pure."""
    raise NotImplementedError


def compute_readiness(inputs: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError


def apply_gates(
    readiness: dict[str, Any],
    log_entries: list[dict[str, Any]],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    raise NotImplementedError
