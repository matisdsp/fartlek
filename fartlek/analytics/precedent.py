"""Personal precedent flags (DESIGN.md §3.2 #5).

Population thresholds say what breaks *people*. This module asks a narrower and
far more useful question: **what preceded trouble for THIS athlete?**

For every prior episode — a logged illness or injury, or a stretch of
suppressed HRV — it records the load conditions of the preceding fortnight.
Once at least one episode exists, current conditions can be compared against
the athlete's own historical trigger levels rather than against a textbook.

Two properties make this honest rather than superstitious:

- **Silent until it has evidence.** With zero episodes nothing is emitted at
  all, and every output carries `n_precedents` so a claim resting on a single
  episode can never masquerade as a pattern.
- **Co-occurrence, not causation.** "Your last illness followed a fortnight at
  monotony 2.1" is a statement about sequence. It deliberately does not say
  the monotony caused it — that would need a controlled experiment, and the
  attribution module owns which causal claims are permitted at all.

Mining runs retroactively over the full backfilled window at Tier-2
completion, then extends forward as new episodes occur.

**Known limitation, found on real data (2026-07-22).** Not every episode is
about training. The maintainer's April episode was food poisoning: monotony in
the preceding fortnight was 1.13, i.e. unremarkable, because the cause had
nothing to do with load. Feeding such an episode into load trigger levels
drags them *down*, so ordinary training later reads as "above your own
pre-episode level" — a false alarm manufactured by an illness the athlete
caught from a meal.

There is no way to infer cause from the data, so `trigger_levels` accepts
`exclude`: episodes the athlete knows were externally caused are left out of
LOAD trigger levels. They remain precedents for everything else. Callers that
cannot ask the athlete should prefer reporting the episode count over reading
much into the level.
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import fmean, median
from typing import Any

LOOKBACK_DAYS = 14        # the fortnight before an episode
MIN_EPISODE_GAP_DAYS = 21  # closer episodes are treated as one
HRV_SUPPRESSED_DAYS = 3    # consecutive days below band to count as an episode
EXCEEDANCE_MARGIN = 0.0    # current must strictly exceed the historical level


def find_hrv_episodes(
    below_band_days: list[str], min_days: int = HRV_SUPPRESSED_DAYS
) -> list[str]:
    """Start dates of HRV-suppression episodes from the dates below band.

    Consecutive calendar dates form one episode; episodes closer together than
    MIN_EPISODE_GAP_DAYS are merged, because the tail of a bad fortnight is
    not a second independent event.
    """
    if not below_band_days:
        return []
    days = sorted({date.fromisoformat(d) for d in below_band_days})

    runs: list[list[date]] = [[days[0]]]
    for d in days[1:]:
        if d - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])

    starts = [run[0] for run in runs if len(run) >= min_days]
    merged: list[date] = []
    for s in starts:
        if merged and (s - merged[-1]).days < MIN_EPISODE_GAP_DAYS:
            continue
        merged.append(s)
    return [d.isoformat() for d in merged]


def episodes_from_log(log_rows: list[dict[str, Any]]) -> list[str]:
    """Episode start dates from garmin_log illness/injury entries.

    The athlete's own report is the strongest evidence available — stronger
    than any sensor — so a logged illness is a precedent even when the sensors
    that day looked unremarkable.
    """
    days = sorted({
        str(r["date"]) for r in log_rows
        if r.get("flag") in ("illness", "injury") and r.get("date")
    })
    merged: list[str] = []
    for d in days:
        if merged and (date.fromisoformat(d)
                       - date.fromisoformat(merged[-1])).days < MIN_EPISODE_GAP_DAYS:
            continue
        merged.append(d)
    return merged


def merge_episodes(*sources: list[str]) -> list[str]:
    """Merge episode dates from every source into distinct events.

    Essential, not tidying: on the maintainer's account the HRV series dipped
    on 2026-04-18 and the athlete logged illness on 04-19 — one bout of
    salmonella, detected by sensor a day before it was reported. Counting it
    twice would double-weight a single event when trigger levels are computed.
    """
    days = sorted({d for src in sources for d in src if d})
    merged: list[str] = []
    for d in days:
        if merged and (date.fromisoformat(d)
                       - date.fromisoformat(merged[-1])).days < MIN_EPISODE_GAP_DAYS:
            continue
        merged.append(d)
    return merged


def _window_stats(
    series: list[tuple[str, float]], end_date: str, days: int = LOOKBACK_DAYS
) -> dict[str, float] | None:
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=days)
    vals = [v for d, v in series
            if start_d <= date.fromisoformat(d) < end_d]
    if not vals:
        return None
    return {"mean": fmean(vals), "max": max(vals), "n": len(vals)}


def mine(
    episode_dates: list[str],
    metrics: dict[str, list[tuple[str, float]]],
    *,
    lookback: int = LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """For each episode, the preceding fortnight's level per metric.

    `metrics` maps a name (monotony, ramp_pct, strain_pctile, daily_load…) to
    its daily series. Episodes with no usable history are dropped rather than
    recorded with blanks.
    """
    out: list[dict[str, Any]] = []
    for ep in sorted(episode_dates):
        stats = {}
        for name, series in metrics.items():
            s = _window_stats(series, ep, lookback)
            if s is not None:
                stats[name] = s
        if stats:
            out.append({"episode": ep, "lookback_days": lookback, "metrics": stats})
    return out


def trigger_levels(
    precedents: list[dict[str, Any]], *, exclude: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """{metric: {level, n, episodes}} — the athlete's own historical trigger
    level per metric, taken as the MEDIAN peak across episodes.

    Median rather than minimum: one unusually calm fortnight before an illness
    (which happens — people catch things at rest) would otherwise drag the
    trigger level down until everything looks alarming.

    `exclude` drops episodes with a known external cause (food poisoning, a
    crash, a family illness). Their pre-episode load says nothing about this
    athlete's tolerance, and including them manufactures false alarms — see
    the module docstring for the case that revealed it.
    """
    skip = set(exclude or ())
    by_metric: dict[str, list[float]] = {}
    episodes: dict[str, list[str]] = {}
    for p in precedents:
        if p["episode"] in skip:
            continue
        for name, stats in p["metrics"].items():
            by_metric.setdefault(name, []).append(stats["max"])
            episodes.setdefault(name, []).append(p["episode"])
    return {
        name: {"level": median(vals), "n": len(vals), "episodes": episodes[name]}
        for name, vals in by_metric.items()
    }


def compare(
    current: dict[str, float], levels: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Current values against the athlete's own trigger levels.

    Returns {silent, n_precedents, exceeded, statements}. `silent` is True
    when there is no precedent to compare against — the correct state for most
    athletes most of the time, and the one that must not be dressed up as
    reassurance.
    """
    if not levels:
        return {"silent": True, "n_precedents": 0, "exceeded": [], "statements": [],
                "reason": "no prior episode on record"}

    exceeded, statements = [], []
    n = max(v["n"] for v in levels.values())
    for name, value in current.items():
        lvl = levels.get(name)
        if lvl is None or value is None:
            continue
        if value > lvl["level"] + EXCEEDANCE_MARGIN:
            exceeded.append(name)
            statements.append(
                f"{name} {value:.3g} is above your own pre-episode level "
                f"({lvl['level']:.3g}, from {lvl['n']} episode"
                f"{'s' if lvl['n'] > 1 else ''})"
            )
        else:
            statements.append(
                f"{name} {value:.3g} is clear of your own pre-episode level "
                f"({lvl['level']:.3g})"
            )
    return {"silent": False, "n_precedents": n, "exceeded": exceeded,
            "statements": statements, "reason": None}
