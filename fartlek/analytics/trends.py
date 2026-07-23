"""Trend significance — Hamed-Rao corrected Mann-Kendall + Sen's slope
(DESIGN.md §3.2 #7).

A trend is reported as real only when it clears BOTH gates:

  1. statistical: autocorrelation-corrected Mann-Kendall p < 0.05
  2. practical:   |Sen slope x window| > the metric's smallest worthwhile
                  change (SWC)

Gate 2 exists because daily physiological series are long enough that trivial
drifts reach significance. Gate 1 is corrected because those same series are
autocorrelated, which inflates plain Mann-Kendall's confidence: Hamed & Rao
(1998) rescale Var(S) by the autocorrelation of the detrended ranks.

SWC (§3.2 #7): default 0.5 x the 90-day MAD-SD from the baseline engine, with
four named exceptions in _SWC_EXCEPTIONS. Every result reports which basis it
used, so a threshold is never silently population-derived when the athlete has
their own history.

Garmin-smoothed series (VO2max) are exempt from p-language: Garmin already
filters them, so a p-value computed on the smoothed output overstates
confidence. They report direction and magnitude only.

Below MIN_POINTS observations nothing is claimed at all (suppressed).

Series are [(date, value)] ascending, gaps allowed. Sen's slope uses real
calendar-day spacing, so it is gap-correct and always expressed per day. The
autocorrelation correction treats the observations as evenly spaced — the
standard approximation, and the reason gappy series stay conservative rather
than over-confident (documented limitation, not silent corruption).

Nothing here formats for display: `sentence` is the canonical one-line
statement of the finding (§3.2 #7 "output is always a sentence, never a
p-value alone"); tools place it, the renderer budgets it.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import median, pstdev
from typing import Any

MIN_POINTS = 21  # below this, no claim is made at all
P_THRESHOLD = 0.05
_DEFAULT_SWC_FRACTION = 0.5  # x 90d MAD-SD
_SWC_WINDOW = 90
_ACF_Z = 1.959963984540054  # two-sided 95% normal quantile, for the ACF band
_EPS = 1e-12

# Named SWC exceptions (§3.2 #7). Kinds:
#   'sd_fraction' — fraction x the classical SD of the window (HRV literature
#                   works in SD, not MAD-SD)
#   'floor'       — the default MAD-SD SWC, but never below this absolute value
#   'relative'    — fraction x |median| of the window (measurement noise scales
#                   with the level)
#   'absolute'    — fixed value in the metric's own unit
_SWC_EXCEPTIONS: dict[str, tuple[str, float]] = {
    "hrv_ln_rmssd": ("sd_fraction", 0.5),   # HRV literature
    "resting_hr": ("floor", 2.0),           # bpm
    "ef": ("relative", 0.03),               # 3% — measurement noise
    "vo2max": ("absolute", 1.0),            # 1.0 unit
}

# Garmin-smoothed series: direction + magnitude wording only, never p-language.
_SMOOTHED_METRICS = frozenset({"vo2max", "endurance_score"})

# Metrics whose rise is unwelcome — drives the wording, never the arithmetic.
_LABELS: dict[str, str] = {
    "hrv_ln_rmssd": "HRV (ln rMSSD)",
    "resting_hr": "resting HR",
    "ef": "efficiency factor",
    "vo2max": "VO2max",
    "endurance_score": "Endurance Score",
    "running_tolerance_pct": "running tolerance",
    "sleep_score": "sleep score",
    "sleep_duration_h": "sleep duration",
    "daily_load": "daily load",
}


def _window_points(
    series: list[tuple[str, float]], end_date: str, window_days: int
) -> list[tuple[date, float]]:
    end_d = date.fromisoformat(end_date)
    start_d = end_d - timedelta(days=window_days - 1)
    return sorted(
        (date.fromisoformat(d), float(v))
        for d, v in series
        if start_d <= date.fromisoformat(d) <= end_d
    )


def sens_slope(points: list[tuple[date, float]]) -> float:
    """Median of all pairwise slopes, in units per DAY (real calendar spacing,
    so gaps do not distort it). Fewer than 2 points → 0.0."""
    slopes = [
        (yj - yi) / (dj - di).days
        for i, (di, yi) in enumerate(points)
        for dj, yj in points[i + 1:]
        if (dj - di).days
    ]
    return median(slopes) if slopes else 0.0


def _mann_kendall_s(values: list[float]) -> float:
    """S = sum of sign(x_j - x_i) over all i < j."""
    s = 0.0
    for i, xi in enumerate(values):
        for xj in values[i + 1:]:
            if xj > xi:
                s += 1
            elif xj < xi:
                s -= 1
    return s


def _var_s(values: list[float]) -> float:
    """Var(S) with the standard tie correction:
    [n(n-1)(2n+5) - sum t(t-1)(2t+5)] / 18."""
    n = len(values)
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    ties = sum(t * (t - 1) * (2 * t + 5) for t in counts.values() if t > 1)
    return (n * (n - 1) * (2 * n + 5) - ties) / 18.0


def _rank(values: list[float]) -> list[float]:
    """Ranks 1..n, ties averaged."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def _autocorrelation(ranks: list[float], lag: int) -> float:
    """ACF at `lag` using the standard biased estimator (every autocovariance
    divided by n, not n-lag). The biased form is what Hamed & Rao's correction
    is calibrated on; the unbiased one inflates high-lag terms, which are
    exactly the terms the correction weights most heavily."""
    n = len(ranks)
    mean = sum(ranks) / n
    dev = [r - mean for r in ranks]
    acov0 = sum(d * d for d in dev) / n
    if acov0 < _EPS:
        return 0.0
    acov = sum(dev[t] * dev[t + lag] for t in range(n - lag)) / n
    return acov / acov0


def hamed_rao_factor(points: list[tuple[date, float]], slope_per_day: float) -> float:
    """Var(S) inflation factor n/n_s* from Hamed & Rao (1998).

    Computed on the ranks of the SEN-DETRENDED series (removing the trend
    first is what makes the remaining autocorrelation the noise structure
    rather than the trend itself), keeping only lag autocorrelations that
    clear the 95% white-noise bounds — the paper's own pre-whitening filter,
    without which the factor is dominated by sampling noise.

    Returns 1.0 (no correction) when the factor is degenerate (n < 4, or a
    non-positive factor from strong negative autocorrelation), so the
    correction can never manufacture significance.
    """
    n = len(points)
    if n < 4:
        return 1.0
    t0 = points[0][0]
    detrended = [v - slope_per_day * (d - t0).days for d, v in points]
    ranks = _rank(detrended)

    # 95% white-noise band for the ACF: lags inside it are indistinguishable
    # from noise and contribute nothing (the paper's pre-whitening filter).
    margin = _ACF_Z / math.sqrt(n)
    total = 0.0
    for lag in range(1, n):
        rho = _autocorrelation(ranks, lag)
        if abs(rho) <= margin:
            continue
        total += (n - lag) * (n - lag - 1) * (n - lag - 2) * rho

    factor = 1.0 + (2.0 / (n * (n - 1) * (n - 2))) * total
    return factor if factor > _EPS else 1.0


def _two_sided_p(s: float, var_s: float) -> float:
    """Normal-approximation p-value with the standard continuity correction."""
    if var_s <= _EPS:
        return 1.0
    if s > 0:
        z = (s - 1) / math.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        return 1.0
    return math.erfc(abs(z) / math.sqrt(2))


def swc_for(
    metric: str, points: list[tuple[date, float]], mad_sd: float | None
) -> tuple[float, str]:
    """(swc, basis) for `metric` — §3.2 #7's default with its named exceptions.

    `mad_sd` is the 90-day robust SD from the baseline engine; None (or a
    metric with no baseline yet) falls back to the window's own MAD-SD so a
    threshold always exists, with the basis naming the fallback.
    """
    values = [v for _, v in points]
    kind_frac = _SWC_EXCEPTIONS.get(metric)

    if kind_frac and kind_frac[0] == "absolute":
        return kind_frac[1], f"{metric} fixed {kind_frac[1]:g}"
    if kind_frac and kind_frac[0] == "relative":
        level = abs(median(values)) if values else 0.0
        return kind_frac[1] * level, f"{kind_frac[1]:.0%} of level ({level:.3g})"
    if kind_frac and kind_frac[0] == "sd_fraction":
        sd = pstdev(values) if len(values) > 1 else 0.0
        return kind_frac[1] * sd, f"{kind_frac[1]:g}x SD ({sd:.3g})"

    if mad_sd is not None:
        swc, basis = _DEFAULT_SWC_FRACTION * mad_sd, f"0.5x {_SWC_WINDOW}d MAD-SD"
    else:
        med = median(values) if values else 0.0
        mad = median([abs(v - med) for v in values]) if values else 0.0
        swc = _DEFAULT_SWC_FRACTION * 1.4826 * mad
        basis = "0.5x window MAD-SD (no 90d baseline yet)"

    if kind_frac and kind_frac[0] == "floor" and swc < kind_frac[1]:
        return kind_frac[1], f"floor {kind_frac[1]:g}"
    return swc, basis


def _describe(
    metric: str, label: str, unit: str, change: float, window_days: int,
    direction: str, significant: bool, p_value: float | None, swc: float,
) -> str:
    span = f"{window_days // 7} wk" if window_days % 7 == 0 else f"{window_days} d"
    mag = f"{abs(change):.3g}{unit}"

    if direction == "flat":
        if abs(change) < _EPS:
            return f"{label} flat over {span} (no drift; SWC {swc:.3g}{unit})"
        drift = "up" if change > 0 else "down"
        # Keep the direction: "flat" means "not worth acting on", not "identical".
        return (
            f"{label} flat over {span} (drifted {drift} {mag}, under the "
            f"{swc:.3g}{unit} smallest worthwhile change)"
        )

    moved = "up" if direction == "rising" else "down"
    if metric in _SMOOTHED_METRICS:
        # Garmin already smooths this series — magnitude and direction only.
        return f"{label} {moved} {mag} over {span} (Garmin-smoothed series — no significance test)"
    if significant:
        return f"{label} {moved} {mag} over {span} (significant, p={p_value:.3g})"
    if p_value is not None and p_value >= P_THRESHOLD:
        return f"{label} {moved} {mag} over {span} — not significant (p={p_value:.3g})"
    return (
        f"{label} {moved} {mag} over {span} — below the {swc:.3g}{unit} "
        "smallest worthwhile change"
    )


def analyze(
    metric: str,
    series: list[tuple[str, float]],
    end_date: str,
    window_days: int,
    *,
    mad_sd: float | None = None,
    label: str | None = None,
    unit: str = "",
) -> dict[str, Any]:
    """Trend verdict for `metric` over the window ending at `end_date`.

    `series` is [(date, value)] ascending (gaps fine); `mad_sd` is the metric's
    90-day MAD-SD from the baseline engine (drives the default SWC).

    Returns a dict that always carries `sentence` — the canonical statement of
    what was found, including when nothing was:

        {suppressed, reason, n, window_days, first, last, change,
         slope_per_day, s, tau, p_value, autocorr_factor, swc, swc_basis,
         direction, significant, smoothed, sentence}

    `significant` is True only when p < 0.05 AND |change| > SWC. For smoothed
    metrics p_value is None and `significant` reports the practical gate alone.
    """
    label = label or _LABELS.get(metric, metric.replace("_", " "))
    points = _window_points(series, end_date, window_days)
    n = len(points)
    base = {
        "metric": metric, "n": n, "window_days": window_days,
        "smoothed": metric in _SMOOTHED_METRICS,
    }

    if n < MIN_POINTS:
        return {
            **base, "suppressed": True,
            "reason": f"n={n} < {MIN_POINTS} points",
            "first": points[0][1] if points else None,
            "last": points[-1][1] if points else None,
            "change": None, "slope_per_day": None, "s": None, "tau": None,
            "p_value": None, "autocorr_factor": None, "swc": None,
            "swc_basis": None, "direction": "flat", "significant": False,
            "sentence": f"{label}: not enough data to judge a trend "
                        f"({n} of {MIN_POINTS} points needed)",
        }

    values = [v for _, v in points]
    slope_per_day = sens_slope(points)
    change = slope_per_day * window_days
    swc, swc_basis = swc_for(metric, points, mad_sd)

    s = _mann_kendall_s(values)
    factor = hamed_rao_factor(points, slope_per_day)
    p_value = None if metric in _SMOOTHED_METRICS else _two_sided_p(s, _var_s(values) * factor)

    practical = abs(change) > swc
    statistical = True if p_value is None else p_value < P_THRESHOLD
    significant = practical and statistical
    direction = "flat" if not practical else ("rising" if change > 0 else "falling")

    denom = n * (n - 1) / 2.0
    return {
        **base, "suppressed": False, "reason": None,
        "first": values[0], "last": values[-1],
        "change": change, "slope_per_day": slope_per_day,
        "s": s, "tau": (s / denom) if denom else 0.0,
        "p_value": p_value, "autocorr_factor": factor,
        "swc": swc, "swc_basis": swc_basis,
        "direction": direction, "significant": significant,
        "sentence": _describe(
            metric, label, unit, change, window_days, direction,
            significant, p_value, swc,
        ),
    }
