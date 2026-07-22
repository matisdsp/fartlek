"""The exception scanner — "anything I should know?" (DESIGN §2.4, budget 500 /
cap 700, the tightest cap in the catalog).

Scans every metric this tool has an engine for and reports ONLY the ones
`analytics.trends.analyze` calls statistically significant — never a
home-grown threshold. That single rule is why this module does almost no
arithmetic of its own: it fetches series, hands them to the engines listed in
its spec, and spends its own judgement solely on WHERE a finding belongs in
the fixed, safety-first ranking (§2.4): 1) a change that coincides with an
already-active alert (this tool's stand-in for "overtraining/illness risk" —
the full multi-marker convergence audit is `garmin_recovery`'s job, not
re-implemented here) · 2) load-structure anomalies · 3) recovery markers
moving the wrong way · 4) everything moving the right way. A metric that
clears neither the "significant" nor the "not enough data" bar is stable,
and stable metrics get one compact line, not one row each — the whole point
of an exception scanner is to be silent about what did not change.

Window sizing is the one real design decision here, so it is documented
rather than buried: `trends.analyze` needs >=21 points inside the SAME window
used for both the test and the plain-English magnitude. A literal
`since_days` window (default 7) can supply at most 7 points, so every daily
metric would be suppressed by construction — the opposite of what "catch me
up" promises. The trend test therefore runs over
max(since_days, _MIN_TREND_WINDOW) for the daily physiology series; EF and
the grey-zone check need even more history because their underlying events
(steady sessions, weekly buckets) are sparse, so they use their own fixed
windows regardless of since_days — the same pattern `garmin_recovery` uses
for its 60/90-day HRV/RHR baselines regardless of its own `days` parameter.
`since_days` still fully governs the daily-metric window once the caller
asks for _MIN_TREND_WINDOW days or more.

Engines consumed, never reimplemented: `analytics.trends` (significance),
`analytics.alerts` (the tracked-metric list and the definition of "already
alerting"), `analytics.baselines` (the 90d MAD-SD feeding every SWC),
`analytics.efficiency` (EF from splits), `analytics.tid` (grey-zone creep).
"""
from __future__ import annotations

import math
from datetime import date as _date
from datetime import timedelta
from typing import Any

from fartlek.analytics import alerts, baselines, efficiency, tid, trends
from fartlek.render.renderer import Report, Row, Section, render

CAP = 700
DEFAULT_SINCE_DAYS = 7
MIN_DAYS, MAX_DAYS = 1, 60

_MIN_TREND_WINDOW = 28   # trends.MIN_POINTS=21 can never clear a shorter window
_EF_WINDOW_DAYS = 90     # steady sessions are sparse; needs real history
_TID_LOOKBACK_DAYS = 84  # 12 weeks — tid.CREEP_WEEKS(3)+1 with margin
_BASELINE_WINDOW = 90    # the mad_sd feeding every SWC, per baselines.baseline

# Fixed ranking (§2.4) — the coaching judgement, not a display choice.
_HEALTH_RISK, _LOAD_ANOMALY, _RECOVERY_DEGRADATION, _PERFORMANCE = 1, 2, 3, 4

_LOAD_METRICS = frozenset({"daily_load", "tid_grey_zone"})

# Which direction is unwelcome, for placing a significant change in bucket 3
# vs 4. Deliberately a small LOCAL mapping rather than reaching into
# alerts._ADVERSE_DIRECTION (private, and this is a ranking/labeling choice
# for this tool — not the maths that module owns).
_ADVERSE_HIGH = {"resting_hr", "avg_stress"}
_ADVERSE_LOW = {"hrv_ln_rmssd", "sleep_score", "sleep_duration_h", "body_battery_wake"}

# Compact display labels, shared between the significant sentence (via
# trends.analyze(label=...)) and the terse "Stable: a, b, c" listing.
_LABEL = {
    "resting_hr": "resting HR",
    "hrv_last_night": "HRV",
    "sleep_score": "sleep score",
    "sleep_duration_h": "sleep duration",
    "body_battery_wake": "Body Battery",
    "avg_stress": "stress",
    "daily_load": "daily load",
    "ef": "EF (steady runs)",
    "tid_grey_zone": "TID drift",
}
_UNIT = {"resting_hr": "bpm", "sleep_duration_h": "h"}


def _series(store: Any, metric: str, end: str, days: int) -> list[tuple[str, float]]:
    try:
        return store.get_series(metric, end, days)
    except KeyError:
        return []


def _mad_sd(series: list[tuple[str, float]], end: str) -> float | None:
    base = baselines.baseline(series, end, _BASELINE_WINDOW)
    return base["mad_sd"] if base else None


def _bucket(metric_key: str, direction: str, has_alert: bool) -> int:
    """One metric's slot in the fixed ranking. `has_alert` outranks
    everything else: a trend that coincides with a live alert is this tool's
    proxy for an overtraining/illness-risk signal, without re-deriving the
    multi-marker audit that `garmin_recovery` owns."""
    if has_alert:
        return _HEALTH_RISK
    if metric_key in _LOAD_METRICS:
        return _LOAD_ANOMALY
    if metric_key == "ef":
        return _PERFORMANCE if direction == "rising" else _RECOVERY_DEGRADATION
    adverse = (
        (direction == "rising" and metric_key in _ADVERSE_HIGH)
        or (direction == "falling" and metric_key in _ADVERSE_LOW)
    )
    return _RECOVERY_DEGRADATION if adverse else _PERFORMANCE


def _scan_daily_metrics(
    store: Any, end: str, window_days: int, active_metrics: set[str]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Every alerts.tracked_metrics() column through trends.analyze.

    Returns (significant findings, stable labels, suppressed labels). HRV is
    analyzed in ln space under the metric key 'hrv_ln_rmssd' so it picks up
    trends' HRV-specific SWC exception (classical SD, not MAD-SD) — the same
    transform garmin_recovery applies before touching its own baselines.
    """
    significant: list[dict[str, Any]] = []
    stable: list[str] = []
    suppressed: list[str] = []

    for metric in alerts.tracked_metrics():
        raw = _series(store, metric, end, max(window_days, _BASELINE_WINDOW))
        key, series = metric, raw
        if metric == "hrv_last_night":
            key = "hrv_ln_rmssd"
            series = [(d, math.log(v)) for d, v in raw if v and v > 0]

        res = trends.analyze(
            key, series, end, window_days,
            mad_sd=_mad_sd(series, end), label=_LABEL[metric], unit=_UNIT.get(metric, ""),
        )
        if res["suppressed"]:
            suppressed.append(_LABEL[metric])
        elif not res["significant"]:
            stable.append(_LABEL[metric])
        else:
            bucket = _bucket(key, res["direction"], metric in active_metrics)
            significant.append({"bucket": bucket, "sentence": res["sentence"]})

    return significant, stable, suppressed


def _scan_ef(store: Any, end: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """EF over its own fixed, wider window (steady sessions are sparse — see
    module docstring). Returns (finding, stable_label, suppressed_label),
    exactly one of which is non-None."""
    start = (_date.fromisoformat(end) - timedelta(days=_EF_WINDOW_DAYS - 1)).isoformat()
    laps = store.laps_in_range(start, end, "%run%")
    series = efficiency.ef_trend_series(laps)
    res = trends.analyze(
        "ef", series, end, _EF_WINDOW_DAYS, mad_sd=_mad_sd(series, end), label=_LABEL["ef"],
    )
    if res["suppressed"]:
        return None, None, _LABEL["ef"]
    if not res["significant"]:
        return None, _LABEL["ef"], None
    return {"bucket": _bucket("ef", res["direction"], False), "sentence": res["sentence"]}, None, None


def _scan_grey_zone(store: Any, end: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Grey-zone creep (tid.grey_zone_creep) over a fixed 12-week lookback,
    independent of since_days for the same sparsity reason as EF. No
    zone_floors/lt1/lt2 are available here, so tid falls back to its
    disclosed whole-bucket approximation rather than the pro-rated one —
    an honest degradation, not a silent one."""
    start = (_date.fromisoformat(end) - timedelta(days=_TID_LOOKBACK_DAYS - 1)).isoformat()
    activities = store.list_activities(start, end)
    creep = tid.grey_zone_creep(tid.weekly_mid_shares(activities))
    label = _LABEL["tid_grey_zone"]
    if creep["reason"]:
        return None, None, label
    if not creep["creeping"]:
        return None, label, None
    sentence = (
        f"grey-zone creep: mid-zone share {creep['from_share']:.0%} -> "
        f"{creep['to_share']:.0%} over {creep['weeks']} wk "
        "(zone bands approximate — no athlete zone config used)"
    )
    return {"bucket": _bucket("tid_grey_zone", "rising", False), "sentence": sentence}, None, None


async def run(ctx: Any, since_days: int = DEFAULT_SINCE_DAYS) -> str:
    if not MIN_DAYS <= since_days <= MAX_DAYS:
        return (
            f"since_days must be between {MIN_DAYS} and {MAX_DAYS} (got {since_days}). "
            f"Today is {ctx.today()}. Example: since_days=7 (the default weekly scan)."
        )

    await ctx.ensure_ready()
    end = ctx.today()
    store = ctx.store
    window_days = max(since_days, _MIN_TREND_WINDOW)
    active_metrics = {a["metric"] for a in store.active_alerts()}

    significant, stable, suppressed = _scan_daily_metrics(store, end, window_days, active_metrics)
    n_scanned = len(alerts.tracked_metrics())

    for scan_fn in (_scan_ef, _scan_grey_zone):
        finding, stable_label, suppressed_label = scan_fn(store, end)
        n_scanned += 1
        if finding:
            significant.append(finding)
        elif stable_label:
            stable.append(stable_label)
        else:
            suppressed.append(suppressed_label)

    significant.sort(key=lambda f: f["bucket"])  # stable sort: scan order within a bucket

    n_sig, n_stable, n_suppressed = len(significant), len(stable), len(suppressed)
    if significant:
        verdict = f"{n_sig} significant change{'s' if n_sig != 1 else ''}, {n_stable} stable"
    else:
        verdict = f"Nothing notable — {n_stable} stable"
    if n_suppressed:
        verdict += f", {n_suppressed} with too little data to judge"
    verdict += "."

    sections: list[Section] = []
    if significant:
        rows = [Row([f"{i}", f["sentence"]]) for i, f in enumerate(significant, 1)]
        sections.append(Section(title=None, header=["#", "Change (safety-first order)"], rows=rows))
    if stable:
        sections.append(Section(
            title=None, header=None, prose="Stable: " + ", ".join(stable) + ".",
            priority="secondary",
        ))
    if suppressed:
        sections.append(Section(
            title=None, header=None,
            prose=f"Not enough data yet (<{trends.MIN_POINTS} points) to judge: "
                  + ", ".join(suppressed) + ".",
            priority="secondary",
        ))

    # The title must name the window the trends were actually TESTED over, not
    # only the one that was asked for. A header reading "last 7d" above a row
    # reading "over 4 wk" invites the reader to date a change to the wrong week.
    span = (f"last {since_days}d"
            if window_days == since_days
            else f"last {since_days}d asked, {window_days}d tested")
    report = Report(
        title=f"Changes — {span} ({n_scanned} metrics checked)",
        date=end,
        data_as_of=ctx.data_as_of(),
        verdict=verdict,
        banner=ctx.banner(),
        sections=sections,
        next_steps=[
            "garmin_recovery(days=14) for the physiology detail",
            "garmin_activities() to find the sessions behind a load or "
            "efficiency change",
        ],
    )
    return render(report, CAP)
