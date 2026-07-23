"""Training intensity distribution (DESIGN.md §3.2 #11).

Maps Garmin's five HR-zone buckets onto the three-zone model coaches reason
in — easy / moderate / hard, split at the first and second lactate thresholds
— and answers one question: **is this athlete drifting away from their own
norm?**

Deliberately NOT a scold against 80/20. Polarised and pyramidal are both
defensible, the evidence does not crown one, and an ultra athlete in a base
block should not be told off for running 95% easy. The target defaults to the
athlete's own 12-week distribution, so what gets flagged is *drift*, plus the
one pattern that is bad under every model: grey-zone creep, where easy runs
quietly become moderate ones.

Zone mapping is approximate and says so. Garmin reports time per zone, not the
HR trace, so when a zone straddles a threshold the time inside it is
pro-rated by the HR width on each side — an assumption of uniform distribution
within the zone. `method` reports which path was taken so the renderer can
disclose it.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

# Grey-zone creep: mid-share rising this many consecutive weeks (§3.2 #11).
CREEP_WEEKS = 3
CREEP_MIN_RISE = 0.02          # 2 percentage points per week, cumulative
DRIFT_FLAG = 0.07              # 7 points off the athlete's own norm
_EPS = 1e-9


def lt1_estimate(resting_hr: float, max_hr: float) -> float:
    """LT1 ≈ HRrest + 0.75 × HR reserve — the §3.2 #11 fallback anchor.

    LOW confidence by construction: it is a population formula, and callers
    must label it as such. A device-reported threshold or an athlete override
    always wins.
    """
    return resting_hr + 0.75 * (max_hr - resting_hr)


def zone_mapping_kwargs(
    zone_config: dict[str, Any] | None,
    *,
    resting_hr: float | None = None,
    lt1_override: float | None = None,
) -> dict[str, Any]:
    """Turn a stored HR-zone config (from the biometric endpoint, digested by
    sync.engine.digest_hr_zones) into the kwargs `distribution`/`map_to_three_zones`
    need for pro-rated mapping. Empty dict when the config cannot anchor both
    thresholds — the caller then gets the whole-bucket fallback and discloses it.

    LT2 is Garmin's lactate-threshold HR; LT1 is the athlete override if set,
    else the population estimate (§3.2 #11 anchor order). Returns {} rather
    than guessing when max_hr or a resting HR for the estimate is missing.
    """
    if not zone_config:
        return {}
    floors = zone_config.get("zone_floors")
    lthr = zone_config.get("lthr")
    max_hr = zone_config.get("max_hr")
    if not floors or len(floors) < 5 or not lthr or not max_hr:
        return {}

    lt2 = float(lthr)
    if lt1_override is not None:
        lt1 = float(lt1_override)
    else:
        rest = resting_hr if resting_hr is not None else zone_config.get("resting_hr")
        if rest is None:
            return {}
        lt1 = lt1_estimate(float(rest), float(max_hr))
    if not lt1 < lt2:
        return {}
    return {"zone_floors": [float(f) for f in floors[:5]],
            "max_hr": float(max_hr), "lt1": lt1, "lt2": lt2}


def _overlap(lo: float, hi: float, a: float, b: float) -> float:
    """Width of [lo,hi) ∩ [a,b), zero when disjoint."""
    return max(0.0, min(hi, b) - max(lo, a))


def map_to_three_zones(
    zone_seconds: list[float],
    *,
    zone_floors: list[float] | None = None,
    max_hr: float | None = None,
    lt1: float | None = None,
    lt2: float | None = None,
) -> dict[str, Any]:
    """Five zone-second buckets → {easy, moderate, hard, method, total}.

    With `zone_floors` (5 lower bounds), `max_hr`, `lt1` and `lt2`, each zone's
    seconds are split across the three bands in proportion to how much of its
    HR width falls on each side of a threshold — the pro-rating §3.2 #11
    requires, since a zone straddling LT1 is neither wholly easy nor wholly
    moderate.

    Without the boundaries it falls back to whole-bucket containment
    (Z1+Z2 easy, Z3 moderate, Z4+Z5 hard) and reports
    method='buckets_approximate'.
    """
    secs = [float(s or 0.0) for s in (list(zone_seconds) + [0.0] * 5)[:5]]
    total = sum(secs)
    if total <= _EPS:
        return {"easy": 0.0, "moderate": 0.0, "hard": 0.0, "total": 0.0,
                "method": "no_data"}

    usable = (zone_floors and len(zone_floors) >= 5 and max_hr
              and lt1 is not None and lt2 is not None and lt1 < lt2)
    if not usable:
        return {"easy": secs[0] + secs[1], "moderate": secs[2],
                "hard": secs[3] + secs[4], "total": total,
                "method": "buckets_approximate"}

    edges = [float(f) for f in zone_floors[:5]] + [float(max_hr)]
    easy = moderate = hard = 0.0
    for i, s in enumerate(secs):
        lo, hi = edges[i], edges[i + 1]
        width = hi - lo
        if width <= _EPS or s <= _EPS:
            # Degenerate zone: fall back to containment for this bucket alone.
            if s > _EPS:
                mid = lo
                if mid < lt1:
                    easy += s
                elif mid < lt2:
                    moderate += s
                else:
                    hard += s
            continue
        easy += s * _overlap(lo, hi, -1e9, lt1) / width
        moderate += s * _overlap(lo, hi, lt1, lt2) / width
        hard += s * _overlap(lo, hi, lt2, 1e9) / width

    return {"easy": easy, "moderate": moderate, "hard": hard, "total": total,
            "method": "prorated"}


def shares(mapped: dict[str, Any]) -> tuple[float, float, float] | None:
    """(easy, moderate, hard) as fractions summing to 1, None with no data."""
    total = mapped.get("total") or 0.0
    if total <= _EPS:
        return None
    return (mapped["easy"] / total, mapped["moderate"] / total, mapped["hard"] / total)


def distribution(
    activities: list[dict[str, Any]], **mapping: Any
) -> dict[str, Any]:
    """Aggregate zone time across activities, then map once.

    Summing seconds before mapping (rather than mapping each session and
    averaging) keeps long sessions weighted by their actual duration.
    """
    totals = [0.0] * 5
    n = 0
    for act in activities:
        vals = [act.get(f"hr_z{i}_s") for i in range(1, 6)]
        if not any(v for v in vals):
            continue
        n += 1
        for i, v in enumerate(vals):
            totals[i] += float(v or 0.0)
    out = map_to_three_zones(totals, **mapping)
    out["n_activities"] = n
    return out


def classify(share: tuple[float, float, float] | None) -> str:
    """Nearest template for an (easy, moderate, hard) split.

    'polarized' — little middle, meaningful hard · 'pyramidal' — moderate
    exceeds hard · 'threshold' — the middle dominates · 'base' — almost
    entirely easy, the normal state of an ultra base block, and NOT a fault.
    """
    if share is None:
        return "unknown"
    easy, mod, hard = share
    if easy >= 0.90 and hard < 0.05:
        return "base"
    if mod < 0.10 and hard >= 0.10:
        return "polarized"
    if mod >= 0.25:
        return "threshold"
    return "pyramidal" if mod >= hard else "polarized"


def weekly_mid_shares(
    activities: list[dict[str, Any]], **mapping: Any
) -> list[tuple[str, float]]:
    """[(ISO week, moderate share)] ascending — the grey-zone creep input.

    Activities must carry `date`. Weeks with no zone data are skipped rather
    than reported as zero moderate.
    """
    from datetime import date as _date

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for act in activities:
        d = act.get("date")
        if not d:
            continue
        iso = _date.fromisoformat(str(d)).isocalendar()
        buckets[f"{iso.year}-W{iso.week:02d}"].append(act)

    out: list[tuple[str, float]] = []
    for week in sorted(buckets):
        sh = shares(distribution(buckets[week], **mapping))
        if sh is not None:
            out.append((week, sh[1]))
    return out


def grey_zone_creep(mid_shares: list[tuple[str, float]]) -> dict[str, Any]:
    """Detect the moderate share rising for CREEP_WEEKS consecutive weeks.

    The one pattern that is unwelcome under every model: easy runs drifting to
    tempo. Returns {creeping, weeks, rise, from_share, to_share}.
    """
    if len(mid_shares) < CREEP_WEEKS + 1:
        return {"creeping": False, "weeks": 0, "rise": 0.0,
                "reason": f"need {CREEP_WEEKS + 1} weeks of data"}

    run = 0
    for i in range(len(mid_shares) - 1, 0, -1):
        if mid_shares[i][1] > mid_shares[i - 1][1]:
            run += 1
        else:
            break
    if run < CREEP_WEEKS:
        return {"creeping": False, "weeks": run, "rise": 0.0, "reason": None}

    start, end = mid_shares[-1 - run][1], mid_shares[-1][1]
    rise = end - start
    return {"creeping": rise >= CREEP_MIN_RISE, "weeks": run, "rise": rise,
            "from_share": start, "to_share": end, "reason": None}


def drift_vs_norm(
    recent: tuple[float, float, float] | None,
    norm: tuple[float, float, float] | None,
) -> dict[str, Any]:
    """Compare a recent split to the athlete's OWN 12-week norm.

    Never against a population template: an ultra athlete running 95% easy is
    following their plan, not making a mistake.
    """
    if recent is None or norm is None:
        return {"drifted": False, "deltas": None, "reason": "insufficient data"}
    deltas = tuple(r - n for r, n in zip(recent, norm, strict=True))
    worst = max(abs(d) for d in deltas)
    return {"drifted": worst >= DRIFT_FLAG, "deltas": deltas,
            "worst": worst, "reason": None}
