"""garmin_reference — the server's methodology, on demand (DESIGN §2.4 cap 2,000/topic, §4.6).

Mirrors the garmin://reference/metrics-glossary resource in the SAME release, because Claude
Desktop cannot pull resources model-side — on that client this tool IS the glossary, not a
convenience copy of it. Two response shapes share one cap: an INDEX (metric name + one-liner)
when no metric is given, and a full ENTRY (formula, inputs, threshold provenance, honesty
caveat) when one is named. The full glossary cannot fit in 2,000 tokens at once — that is a
fact about the content, not a renderer limitation — so drill-down is per metric by design.

Every threshold below is generated from the analytics engine's own constants rather than
retyped by hand, so this file cannot silently drift from the code it describes: the whole
point of a glossary a model can actually trust is that it is wrong in exactly the same way the
engine is wrong, never a different way. Where a value is genuinely a matter of prose (a bare
formula shape, a design rationale) it is written directly, but every NUMBER a coach could act
on is imported.

Every threshold also carries an explicit provenance label — "population default" vs.
"personally derived" — because conflating the two is exactly the false authority this project
exists to avoid (§3.2's hard invariant, restated in the module docstrings of convergence.py,
baselines.py, precedent.py and race.py). An entry with no honesty caveat worth stating does not
exist in this catalog: every metric here has at least one real limitation, and hiding that
would be worse than not shipping the glossary at all.

`topic="workout_schema"` is declared in the DESIGN surface for Phase 3 and correctly rejected
here — Phase 2 ships the glossary only.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from fartlek.analytics import alerts as alerts_engine
from fartlek.analytics import attribution as attribution_engine
from fartlek.analytics import baselines as baselines_engine
from fartlek.analytics import convergence as convergence_engine
from fartlek.analytics import efficiency as efficiency_engine
from fartlek.analytics import fusion as fusion_engine
from fartlek.analytics import load as load_engine
from fartlek.analytics import pmc as pmc_engine
from fartlek.analytics import precedent as precedent_engine
from fartlek.analytics import race as race_engine
from fartlek.analytics import sleep as sleep_engine
from fartlek.analytics import tid as tid_engine
from fartlek.analytics import trends as trends_engine
from fartlek.render.renderer import Report, Row, Section, render

CAP = 2000  # DESIGN §2.4: "cap 2,000 tokens per topic"
TOPICS = ("metrics_glossary",)  # "workout_schema" is declared for Phase 3, not valid yet


@dataclass(frozen=True)
class Entry:
    title: str            # display heading for the drill-down
    summary: str           # one-liner for the index table
    formula: str           # the method, stated plainly
    inputs: str            # what feeds it
    thresholds: tuple[str, ...]   # each already carries its provenance label
    caveat: str            # the honesty note — every entry has one, see module docstring
    used_by: str           # which shipped surface(s) render this today
    tool_hint: str | None = None  # a concrete shipped-tool call, for the breadcrumb


# --- reusable honesty-note text (shared verbatim between the index summary and the ------
# --- owning entry, so the two can never say something different about the same fact) ---

_ACWR_CAVEAT = (
    "ACWR is contested in the sports-science literature and is used here ONLY as a spike "
    "detector, never a standalone verdict — a high ACWR flags 'load rose fast', not 'injury "
    "imminent', and it never counts by itself toward an overtraining alarm."
)
_RIEGEL_CAVEAT = (
    "Riegel's power law is a DISTANCE-race model; extrapolating it to a FIXED-TIME event "
    "(6h/12h/24h) is meaningless, so fixed-time projections use a SEPARATE model with a "
    "population exponent band, NEVER personally fitted from training runs — a fitted exponent "
    "measures pacing discipline on sub-maximal efforts, not the athlete's physiological limit."
)
_HIGH_RMSSD_CAVEAT = (
    "An abnormally HIGH rMSSD vs. this athlete's own baseline is not automatically good — it is "
    "a rare parasympathetic-overtraining signal, and it is fed to the overtraining convergence "
    "audit as corroboration only, never credited to readiness."
)
_HEAT_GUARD_CAVEAT = (
    f"Sessions/laps at or above {efficiency_engine.HOT_TEMP_C:g}°C are FLAGGED and excluded from "
    "the trend series, never deleted — on the account this was tuned against, 96% of one "
    "month's laps ran at or above the heat threshold vs. 11% two months earlier, and the "
    "measured heat penalty alone accounted for what would otherwise have read as a summer "
    "fitness regression."
)
_TREND_CAVEAT = (
    "A trend is reported as significant only when BOTH gates clear: a Hamed-Rao "
    "autocorrelation-corrected p-value AND a magnitude beyond the metric's smallest-worthwhile "
    "change. Either alone is not enough, and the result is always rendered as a sentence, never "
    "a bare p-value."
)
_LOAD_CAL_CAVEAT = (
    "Every non-Garmin-native load carries its provenance flag (trimp_calibrated / "
    "trimp_uncalibrated / srpe_calibrated / srpe_uncalibrated / estimated / none), and any "
    "response whose window contains such a day discloses it — a load number is never presented "
    "as Garmin-native when it wasn't."
)
_EF_AMENDMENT_CAVEAT = (
    "Amendment (2026-07-22): the steady-session qualifier proved too restrictive to ever carry "
    "a trend for a high-variety athlete — one real account produced only 20 qualifying sessions "
    "in 180 days, under the trend-significance floor. The same laps, read by pace band "
    "regardless of whether their SESSION was steady, yielded two orders of magnitude more "
    "evidence (1,348 qualifying laps vs. 21 qualifying sessions), so HR-at-pace is now PRIMARY "
    "and steady-session EF is secondary."
)

# --- precomputed text fragments pulled straight from engine constants, so no number below ---
# --- is retyped by hand -----------------------------------------------------------------

_ADVERSE_TXT = ", ".join(f"{k} ({v})" for k, v in alerts_engine._ADVERSE_DIRECTION.items())
_TRAINING_DAYS_TXT = ", ".join(sorted(alerts_engine._TRAINING_DAYS_ONLY))
_MIN_SEVERE_TXT = ", ".join(
    f"{k}={v}d" for k, v in sorted(alerts_engine._MIN_SEVERE_STREAK.items())
)
_SWC_EXC_TXT = ", ".join(
    f"{name} {kind} {val:g}" for name, (kind, val) in trends_engine._SWC_EXCEPTIONS.items()
)
_WEIGHTS_TXT = ", ".join(f"{k} {v:.0%}" for k, v in fusion_engine.WEIGHTS.items())
_RHR_RESOLUTION_FLOOR = alerts_engine._RESOLUTION_FLOOR["resting_hr"]


_ENTRIES: dict[str, Entry] = {
    "pmc": Entry(
        title="PMC — CTL / ATL / TSB, Form%, Ramp",
        summary="Fitness/fatigue/form (CTL/ATL/TSB) and weekly ramp rate",
        formula=(
            f"CTL += (load−CTL)·(1−e^(−1/{pmc_engine.K_CTL})); "
            f"ATL += (load−ATL)·(1−e^(−1/{pmc_engine.K_ATL})); "
            "TSB = CTL_yesterday − ATL_yesterday. Form% = TSB/CTL×100. "
            "Ramp% = (CTL_today − CTL_7d_ago)/CTL_today×100 (%CTL/week)."
        ),
        inputs="daily load series, every calendar day, missing days = 0 (see load_calibration)",
        thresholds=(
            f"CTL time constant {pmc_engine.K_CTL}d, ATL time constant {pmc_engine.K_ATL}d "
            "— population default (fixed PMC protocol constants, never personally fitted)",
            "form bands: +5…+25% fresh/race-ready · −10…+5 neutral · −30…−10 productive · "
            "−40…−30 deep · <−40 overload · >+25 transition/detraining risk — population "
            "default (fixed bands, not personalized); ramp flag >10% CTL/week — population "
            "default",
            "CTL/ATL/TSB themselves are personally derived — the athlete's own load history run "
            "through the fixed constants above, never a population fitness number",
        ),
        caveat=(
            "PMC is warmed directly from the 180-day backfill rather than seeded from zero, so "
            "CTL is trustworthy from day 0 — a new account is not a low-confidence account here."
        ),
        used_by="garmin_recovery (form/ramp rows), garmin_brief (form marker)",
        tool_hint="garmin_recovery(days=28) to see this metric live",
    ),
    "acwr": Entry(
        title="ACWR (Acute:Chronic Workload Ratio)",
        summary="Acute:chronic load-spike ratio (contested — spike detector, not a verdict)",
        formula=(
            "EWMA(7):EWMA(28) ratio of daily load; each EWMA seeded at the series' first value, "
            "alpha = 2/(N+1) for its own window."
        ),
        inputs="daily load series; Garmin's own dailyAcuteChronicWorkloadRatio as a cross-check",
        thresholds=(
            "suppressed ('unreliable') below 28 days of history, when chronic load is ~0, or "
            "when chronic is under 30% of its trailing 90-day median (layoff instability) — "
            "population default (fixed guard, not personalized)",
            "disagreement with Garmin's own ACWR beyond 0.2 is stated explicitly, never averaged",
            "the acute/chronic EWMA values themselves are personally derived from this athlete's "
            "own load history — only the guard thresholds above are population defaults",
        ),
        caveat=_ACWR_CAVEAT,
        used_by=(
            "not yet surfaced by a shipped tool this phase — computed as groundwork for the "
            "load-spike surface"
        ),
        tool_hint=None,
    ),
    "monotony_strain": Entry(
        title="Monotony & Strain (Foster)",
        summary="Training monotony and strain (Foster)",
        formula=(
            "monotony = mean(7d load) / population-SD(7d load); strain = weekly_load × "
            "monotony; strain_percentile = share of the athlete's own trailing ≤12 weekly "
            "strains at or below the current one."
        ),
        inputs="trailing 7 daily loads (rest days count as 0); up to 12 trailing weekly strains",
        thresholds=(
            f"monotony flag > {convergence_engine.MONOTONY_FLAG:g} — population default (Foster "
            "monotony literature)",
            f"strain flag > {convergence_engine.STRAIN_PCTILE_FLAG:g}th percentile — personally "
            "derived (the percentile is computed against this athlete's OWN trailing weekly-"
            "strain distribution, never a population table)",
        ),
        caveat=(
            "A degenerate zero-variance week (SD~0) only flags when that week actually had "
            "training — an all-rest week is rest, not monotonous."
        ),
        used_by="garmin_recovery (Monotony / strain row)",
        tool_hint="garmin_recovery(days=28) to see this metric live",
    ),
    "baseline_engine": Entry(
        title="Baseline Engine (rolling median / MAD / z-score)",
        summary="The universal rolling-baseline machinery behind every 'off baseline' claim",
        formula=(
            "rolling mean/median at 7/28/60/90d windows; robust SD = "
            f"{baselines_engine.MAD_SCALE:g}×MAD (median absolute deviation), floored so it is "
            "always a safe z-score divisor; band position (|z|≤1 in band, 1–2 high/low, >2 very "
            "high/low); calendar-gap-breaking streak counts."
        ),
        inputs="any daily scalar series (HRV, RHR, sleep, deep sleep, Body Battery, weight, …)",
        thresholds=(
            f"MAD→SD scale factor {baselines_engine.MAD_SCALE:g} — population default (the "
            "standard normal-consistency constant for MAD, not athlete-specific)",
            "the resulting median/mean/band itself is personally derived — always this "
            "athlete's own rolling history, never a population norm",
        ),
        caveat=(
            "Every 'off baseline' claim in this server compares an athlete to their OWN recent "
            "history, not to population norms — the scale factor above is the only population "
            "constant anywhere in the computation."
        ),
        used_by="garmin_recovery, garmin_brief, garmin_athlete (every baseline comparison)",
        tool_hint="garmin_athlete() to see personal baselines on file",
    ),
    "trend_significance": Entry(
        title="Trend Significance (Hamed-Rao MK + Sen's slope + SWC)",
        summary="Whether a trend is real: corrected p-value AND smallest-worthwhile-change",
        formula=(
            "Hamed–Rao autocorrelation-corrected Mann–Kendall test for direction; Sen's slope "
            "(median of all pairwise slopes, real calendar-day spacing) for magnitude."
        ),
        inputs="any daily series over its analysis window (gaps allowed)",
        thresholds=(
            f"suppressed below {trends_engine.MIN_POINTS} data points — population default "
            "(statistical floor)",
            f"statistical gate: corrected p < {trends_engine.P_THRESHOLD:g} — population default",
            f"practical gate: |Sen slope × window| > SWC, default "
            f"{trends_engine._DEFAULT_SWC_FRACTION:g}× the {trends_engine._SWC_WINDOW}d MAD-SD "
            "— population default fraction applied to this athlete's own personally derived "
            "MAD-SD",
            f"named SWC exceptions (population default, not personalized): {_SWC_EXC_TXT}",
        ),
        caveat=_TREND_CAVEAT,
        used_by=(
            "not yet surfaced by a shipped tool this phase — the dual-gate logic every trend "
            "line in the server is built to use"
        ),
        tool_hint=None,
    ),
    "hrv_band": Entry(
        title="HRV Band & Streaks",
        summary="HRV band position, streaks, and the high-rMSSD caveat",
        formula=(
            "Day 1: consumes Garmin's shipped baseline (balancedLow/balancedUpper). From ≥60 "
            "nights: self-computed 60d mean ln(rMSSD) ± 0.5×SD, cross-checked against Garmin's "
            "own band. Decision basis is the 7-day rolling mean vs. the band; single nights "
            "feed only display, streak counters, and the acute override."
        ),
        inputs="hrv_last_night (ms, worked in ln space), Garmin's shipped baseline on day 1",
        thresholds=(
            "band half-width 0.5× the 60d SD — population default fraction (HRV literature) "
            "applied to this athlete's own personally derived 60-night SD",
            f"HRV coefficient-of-variation rise flag: {convergence_engine.HRV_CV_RISE_FLAG:.0%} "
            "above the athlete's own trailing CV — personally derived (compares the athlete to "
            "their own history, not a population CV)",
        ),
        caveat=_HIGH_RMSSD_CAVEAT,
        used_by="garmin_recovery (HRV rows), garmin_athlete (HRV band line), garmin_brief",
        tool_hint="garmin_recovery(days=28) to see this metric live",
    ),
    "rhr_deviation": Entry(
        title="Resting HR Deviation (two-sided)",
        summary="Two-sided resting-HR deviation (elevation AND a sustained drop both flag)",
        formula=(
            "delta = today's RHR − 30-day median (today excluded from the median). Deviation "
            "in EITHER direction is the flag — elevation is the classic overreaching sign, but "
            "a sustained drop alongside other deviant markers is the parasympathetic pattern."
        ),
        inputs="resting_hr series (30d trailing median, today's value)",
        thresholds=(
            f"caution band ±{baselines_engine._RHR_CAUTION:g} bpm, severe "
            f"±{baselines_engine._RHR_SEVERE:g} bpm sustained ≥{baselines_engine._RHR_SUSTAINED_DAYS}d, "
            f"minimum {baselines_engine._RHR_MIN_N}d of history required — population default "
            "(fixed bpm thresholds, not personalized)",
            "the 30-day median itself is personally derived — the bpm thresholds above are "
            "applied to THIS athlete's own baseline, never a population RHR",
        ),
        caveat=(
            "A sustained DROP in RHR is never alarmed on its own — it feeds the overtraining "
            "convergence audit as one input among several, because a naive 'high is bad' test "
            "misses the parasympathetic-overtraining pattern entirely."
        ),
        used_by="garmin_recovery (Resting HR row), garmin_brief, garmin_athlete",
        tool_hint="garmin_recovery(days=28) to see this metric live",
    ),
    "sleep_debt_sri": Entry(
        title="Sleep Debt, Regularity (SRI) & Social Jetlag",
        summary="Sleep debt, regularity (SRI), and social jetlag — three separate measures",
        formula=(
            "debt = Σ max(0, need − actual) over the trailing window; surplus nights do NOT "
            "offset deficits. SRI (Phillips et al. 2017) = −100 + 200×(same-state agreement "
            "rate at 24h lag). Social jetlag = weekend mid-sleep − weekday mid-sleep."
        ),
        inputs="sleep duration/need (days table), compact per-night sleep/wake timeline (SRI)",
        thresholds=(
            f"debt window {sleep_engine.DEBT_WINDOW_DAYS}d, sleep-need fallback "
            f"{sleep_engine.DEFAULT_SLEEP_NEED_H:g}h when the device reports none — population "
            "default; every debt figure discloses whether the need came from the device or "
            "this fallback",
            f"SRI suppressed below {sleep_engine.SRI_MIN_DAY_PAIRS} comparable 24h transitions "
            "— population default (statistical floor, stops one odd night swinging the figure)",
            f"convergence flags: debt > {convergence_engine.SLEEP_DEBT_H_14D:g}h/14d, SRI < "
            f"{convergence_engine.SRI_FLOOR:g}, deep-sleep low streak ≥ "
            f"{convergence_engine.DEEP_SLEEP_STREAK_DAYS}d — population default",
            "the debt/regularity/deep-sleep NUMBERS themselves are personally derived (this "
            "athlete's own nights, need, and rolling deep-sleep baseline) — only the day-count "
            "windows and flags above are population defaults",
        ),
        caveat=(
            "Debt, regularity, and jetlag are deliberately NOT blended into one 'sleep score' — "
            "an athlete can sleep 8h every night and still score poorly on regularity by moving "
            "those 8h around, and duration cannot stand in for consistency."
        ),
        used_by="garmin_recovery (sleep rows), garmin_brief (sleep marker)",
        tool_hint="garmin_recovery(days=28) to see this metric live",
    ),
    "intensity_distribution": Entry(
        title="Training Intensity Distribution (TID)",
        summary="Easy/moderate/hard time-in-zone mapping and drift from the athlete's own norm",
        formula=(
            "Garmin's 5 HR-zone-second buckets are mapped to easy/moderate/hard by pro-rating "
            "any zone that straddles LT1/LT2 by HR width (falls back to whole-bucket "
            "containment when zone boundaries aren't known). The target defaults to the "
            "athlete's own trailing 12-week split; only DRIFT from that own norm is flagged."
        ),
        inputs="hrTimeInZone_1..5 per activity, configured zone floors, LT1/LT2 anchors",
        thresholds=(
            "LT1 fallback ≈ HRrest + 0.75×HR-reserve — population default formula, LOW "
            "confidence by construction; a device-reported threshold or athlete override wins",
            f"drift flag: {tid_engine.DRIFT_FLAG:.0%} off the athlete's own 12-week norm — "
            "personally derived (never against a population 80/20 template)",
            f"grey-zone creep: moderate share rising {tid_engine.CREEP_WEEKS} consecutive weeks "
            f"by ≥{tid_engine.CREEP_MIN_RISE:.0%}/week — population default (the one pattern "
            "flagged under every training-distribution model)",
        ),
        caveat=(
            "Deliberately not a scold against any one distribution model — polarised, "
            "pyramidal, and an almost-all-easy base block are all defensible; only drift from "
            "the athlete's OWN norm and grey-zone creep are flagged."
        ),
        used_by=(
            "not yet surfaced by a shipped tool this phase — feeds the intensity-distribution "
            "surface"
        ),
        tool_hint=None,
    ),
    "efficiency": Entry(
        title="Aerobic Efficiency — EF, Decoupling, HR-at-Pace, Durability",
        summary="Aerobic efficiency: HR-at-pace (primary), steady EF, decoupling, durability",
        formula=(
            "PRIMARY: HR-at-pace over a requested pace band — duration-weighted avg HR and EF "
            "(speed/HR) across every qualifying LAP in the band, regardless of session "
            "(Amendment below). SECONDARY: steady-session EF (avg grade-adjusted speed ÷ avg "
            "HR) where a session qualifies as steady. Decoupling = (EF_half1 − EF_half2) / "
            "EF_half1. Durability (runs ≥90 min) = EF_final_third ÷ EF_first_third."
        ),
        inputs="per-lap splits digest (distance, time, HR, grade-adjusted speed, temperature)",
        thresholds=(
            f"HR-at-pace lap qualifiers: ≥{efficiency_engine.MIN_LAP_DISTANCE_M:g}m, drop laps "
            f"following one >{efficiency_engine.PREV_LAP_SPEED_RATIO:g}× faster (HR still "
            "elevated from the previous rep), drop marked interval recoveries — population "
            "default (fixed methodology, applied uniformly)",
            f"steady-session qualifier (secondary EF): ≥{efficiency_engine.STEADY_MIN_MOVING_S // 60}"
            f"min moving after a {efficiency_engine.WARMUP_EXCLUDE_S // 60}min warm-up exclusion, "
            f"lap-pace CV <{efficiency_engine.STEADY_LAP_GAP_CV_MAX:.0%}, "
            f"≥{efficiency_engine.STEADY_MIN_EASY_LAP_SHARE:.0%} of laps at/below the Z2 ceiling "
            "— population default",
            f"heat guard: {efficiency_engine.HOT_TEMP_C:g}°C — population default temperature "
            "threshold (see the caveat below)",
            f"durability requires ≥{efficiency_engine.LONG_RUN_MIN_S // 60}min moving — intra-"
            "individual trend only, no population norms, LOW-confidence label until ≥5 long "
            "sessions on file",
            "the EF/pace/HR numbers themselves are personally derived (this athlete's own "
            "laps) — the qualifier constants above (durations, ratios, temperature) are "
            "population defaults applied uniformly to every athlete",
        ),
        caveat=f"{_EF_AMENDMENT_CAVEAT} {_HEAT_GUARD_CAVEAT}",
        used_by=(
            "not yet surfaced by a shipped tool this phase — feeds the aerobic-efficiency and "
            "race-projection surfaces"
        ),
        tool_hint="garmin_activity(detail=\"splits\") for one session's laps",
    ),
    "race_projection": Entry(
        title="Race Time/Distance Projection",
        summary="Race projection: Garmin/Tanda/Riegel triangulation, plus the fixed-time model",
        formula=(
            "DISTANCE races: Garmin's own prediction (as-is) + Tanda's regression + Riegel's "
            "power law T2 = T1×(D2/D1)^b, triangulated (spread = confidence, never averaged). "
            "FIXED-TIME events (6h/12h/24h): a SEPARATE model, D2 = D1×(T2/T1)^(1/b) from one "
            "long reference effort, reported as a RANGE across an exponent band, with stoppage "
            "time modelled explicitly."
        ),
        inputs="PRs, progress-summary inputs, Garmin race predictions, one long reference effort",
        thresholds=(
            f"Riegel exponent default {race_engine.RIEGEL_DEFAULT_B:g}, fit bounds "
            f"{race_engine.RIEGEL_BOUNDS} — population default UNLESS fitted from the "
            "athlete's own PRs (then personally derived, fit quality disclosed good/weak); "
            "fitting is restricted to MAXIMAL performances — sub-maximal runs measure pacing "
            "discipline, not the athlete's limit",
            f"fixed-time exponent band {race_engine.FIXED_TIME_EXPONENT_BAND} — population "
            "default, NEVER personally fitted",
            f"reference effort must be ≥{race_engine.MIN_REFERENCE_HOURS:g}h; confidence drops "
            f"to LOW beyond {race_engine.MAX_EXTRAPOLATION_RATIO:g}× the reference duration, or "
            "when the reference was sub-maximal — population default guard",
        ),
        caveat=_RIEGEL_CAVEAT,
        used_by=(
            "not yet surfaced by a shipped tool this phase — a fixed-time exponent fitted from "
            "training runs on one real account came out at 0.99 (better than linear, "
            "physiologically impossible), which is why fitting is restricted to maximal efforts"
        ),
        tool_hint=None,
    ),
    "readiness_fusion": Entry(
        title="Readiness Fusion",
        summary="Today's fused readiness verdict (weighted z-score across available markers)",
        formula=(
            f"weighted z-score fusion vs. personal baselines: {_WEIGHTS_TXT}. Weights "
            "renormalize over whichever markers are available; every verdict states which "
            "markers were used."
        ),
        inputs="HRV 7d-roll band position, sleep score+debt, form ratio, RHR delta, Body Battery",
        thresholds=(
            f"GREEN ≥ {fusion_engine.GREEN_FLOOR:g} · AMBER ≥ {fusion_engine.AMBER_FLOOR:g} · "
            "else RED — population default fixed cut-points",
            f"provisional below {fusion_engine.MIN_MARKERS} available markers, or a baseline "
            f"with fewer than {fusion_engine.BASELINE_WARM_N} nights/days — population default "
            "(never a confident GREEN on no data)",
            f"acute override (bypasses the weighting): RHR ≥+{fusion_engine.ACUTE_RHR_DELTA:g}"
            f"bpm overnight, single-night HRV z ≤{fusion_engine.ACUTE_HRV_Z:g} or a 90d low, "
            f"sleep <{fusion_engine.ACUTE_SLEEP_H:g}h — population default; two flags together "
            "escalate straight to RED",
            "the z-scores fed into this weighting are personally derived (each marker's own "
            "baseline-engine comparison) — the weights and cut-points above are the only "
            "population defaults",
        ),
        caveat=(
            "Above-band HRV never earns readiness credit — see hrv_band's caveat: an "
            "abnormally high reading is not automatically treated as good."
        ),
        used_by="garmin_brief (the fused VERDICT)",
        tool_hint="garmin_brief() to see this metric live",
    ),
    "subjective_gate": Entry(
        title="Subjective Gate & Acute Override",
        summary="How a logged illness/injury or an acute marker caps the readiness verdict",
        formula=(
            "Applied AFTER the readiness fusion, and only ever downgrades a verdict, never "
            "upgrades it: a same-day illness log caps at RED, an unresolved injury caps at "
            "AMBER, and the athlete's own report always outranks a calm sensor reading."
        ),
        inputs="garmin_log entries (flag=illness/injury), the acute markers from readiness_fusion",
        thresholds=(
            "illness → RED ('rest pending symptoms'); unresolved injury → AMBER — population "
            "default (fixed rule, not tunable)",
            "two severe acute markers together → RED even with no illness logged — population "
            "default",
            "the acute markers themselves (RHR delta, HRV z, sleep hours) are computed against "
            "personally derived baselines — see rhr_deviation, hrv_band, sleep_debt_sri",
        ),
        caveat=(
            "This is the one place population thresholds are deliberately overridden by the "
            "athlete rather than the reverse: a logged illness caps the verdict however GREEN "
            "the sensors look."
        ),
        used_by="garmin_brief, garmin_recovery (verdict caps)",
        tool_hint="garmin_brief() to see this metric live",
    ),
    "overtraining_convergence": Entry(
        title="Overtraining Convergence",
        summary="The RED overtraining alarm rule — no single marker ever alarms alone",
        formula=(
            "RED requires at least MIN_TRIGGERING_GROUPS of the independent marker groups "
            "(autonomic, sleep, load) persistently deviant for several days. A 4th group (HR "
            "response) is corroborating-only: it can strengthen an alarm but never counts "
            "toward the triggering total."
        ),
        inputs="the autonomic/sleep/load marker groups (see their own entries) + subjective_gate",
        thresholds=(
            f"{convergence_engine.MIN_TRIGGERING_GROUPS} of "
            f"{len(convergence_engine.TRIGGERING_GROUPS)} groups, persistent ≥"
            f"{convergence_engine.PERSISTENCE_DAYS} days — population default (the central "
            "safety rule: physiological series are noisy enough that any ONE marker crosses a "
            "threshold regularly in a healthy athlete)",
            "each contributing marker is itself compared to a personally derived baseline (see "
            "hrv_band, rhr_deviation, sleep_debt_sri, monotony_strain) — only the grouping RULE "
            "above is a population default",
        ),
        caveat=(
            "A single deviant marker group renders as WATCH, explicitly labelled 'not an "
            "alarm' — the server is designed to be ignorable on any one signal, because a "
            "server that shouts on every crossing gets ignored within a fortnight."
        ),
        used_by="garmin_recovery (the overtraining VERDICT)",
        tool_hint="garmin_recovery(days=28) to see this metric live",
    ),
    "anomaly_alerts": Entry(
        title="Anomaly Scan → Alerts",
        summary="The anomaly scanner behind the ⚠ ACTIVE banner on every response",
        formula=(
            "Per tracked metric: robust z vs. a 90-day baseline. Trips on |z|>2 today OR a "
            "≥3-day out-of-band streak (|z|>1, calendar-gap-breaking). At most one alert per "
            "metric, the most severe applicable."
        ),
        inputs=(
            "resting_hr, hrv_last_night, sleep_score, sleep_duration_h, body_battery_wake, "
            "avg_stress, daily_load"
        ),
        thresholds=(
            f"MAD scale {alerts_engine._MAD_SCALE:g}, 90d window, per-metric resolution floors "
            f"(e.g. {_RHR_RESOLUTION_FLOOR:g} bpm for resting HR) so a degenerate zero-MAD "
            "window can't turn a 1-unit wiggle into an alert — population default",
            f"ADVERSE DIRECTION ONLY (the other side never alerts): {_ADVERSE_TXT} — population "
            "default, tuned after 31% of alerts fired on IMPROVEMENTS before this rule shipped",
            f"{_TRAINING_DAYS_TXT}'s 90d baseline excludes rest days (0-load days) — population "
            "default; otherwise every long run tripped the scanner by construction",
            f"sleep needs {_MIN_SEVERE_TXT} consecutive severe days before alerting (isolated "
            "short nights are this athlete's norm) — population default",
            "the 90-day median/MAD comparison itself is personally derived (this athlete's own "
            "history) — only the z-thresholds, direction rules, and streak requirements above "
            "are population defaults",
        ),
        caveat=(
            "Tuned against the maintainer's own 6-month account, not a theoretical spec — see "
            "the alerts.py module docstring for the exact before/after numbers behind each rule "
            "above."
        ),
        used_by="every tool's ⚠ ACTIVE banner (RED/AMBER); WATCH items in garmin_brief/garmin_recovery",
        tool_hint="garmin_brief() — the banner surfaces here first",
    ),
    "personal_precedent": Entry(
        title="Personal Precedent",
        summary="Comparing today's load to this athlete's own pre-episode levels",
        formula=(
            "For every prior illness/injury/HRV-suppression episode, records the preceding "
            "fortnight's load conditions. Current conditions compare against the MEDIAN peak "
            "across the athlete's own episodes — never a population trigger level."
        ),
        inputs="garmin_log illness/injury entries, HRV-suppression streaks, weekly load history",
        thresholds=(
            f"lookback {precedent_engine.LOOKBACK_DAYS}d, episodes closer than "
            f"{precedent_engine.MIN_EPISODE_GAP_DAYS}d merged into one, HRV episodes need "
            f"{precedent_engine.HRV_SUPPRESSED_DAYS} consecutive suppressed days — population "
            "default (fixed windowing, not personalized)",
            "the trigger level itself is personally derived — the median peak across THIS "
            "athlete's own prior episodes; silent (nothing rendered) until at least one exists",
        ),
        caveat=(
            "Externally-caused episodes (food poisoning, a crash) must be excluded from LOAD "
            "trigger levels via a garmin_log note — one real account's food-poisoning episode "
            "had unremarkable preceding load, and including it dragged the trigger level down "
            "until ordinary training read as 'above your own pre-episode level', a false alarm "
            "manufactured by an illness that had nothing to do with load."
        ),
        used_by="garmin_recovery (Personal precedent line)",
        tool_hint="garmin_recovery(days=28) to see this metric live",
    ),
    "attribution_rules": Entry(
        title="Attribution Rules (the closed 'because' set)",
        summary="The closed set of 'because' statements this server is allowed to make",
        formula=(
            "A CLOSED set of five rules is the only way this server ever says 'because'. "
            "Everything else is phrased as co-occurrence ('X while Y'), never causation."
        ),
        inputs=(
            "deep-sleep trend, bedtime variance, ramp/strain, 90d load/HRV series, sleep-debt/"
            "HRV series, hot-lap share"
        ),
        thresholds=(
            f"(a)/(b) bedtime variance ≥{attribution_engine.BEDTIME_VARIANCE_HIGH_H:g}h SD vs. "
            f"ramp >{attribution_engine.RAMP_ELEVATED_PCT:g}%/wk or strain >"
            f"{attribution_engine.STRAIN_ELEVATED_PCTILE:g}th pctile discriminate schedule vs. "
            "load — population default; BOTH or NEITHER abnormal → no attribution at all",
            f"(c)/(d) lagged load→HRV and debt→HRV associations need ≥"
            f"{attribution_engine.MIN_CORRELATION_DAYS}d of pairs and |r|>"
            f"{attribution_engine.MIN_ABS_R:g} — population default; reported as correlation, "
            "never mechanism",
            "(e) hot-day EF suppression — see efficiency's heat guard; this one IS personally "
            "derived (the athlete's own hot-vs-cool EF gap, not a population penalty)",
        ),
        caveat=(
            "An LLM handed a pile of correlated numbers will invent explanations. This module "
            "exists so the server can say 'because' in exactly five situations and nowhere "
            "else — an unlisted causal claim is a bug, not a stylistic choice."
        ),
        used_by=(
            "not yet surfaced by a shipped tool this phase — the causal-language rule any "
            "future 'because' statement must cite"
        ),
        tool_hint=None,
    ),
    "load_calibration": Entry(
        title="Load Currency & Calibration Ladder",
        summary="How a day's training load is derived when Garmin doesn't supply it directly",
        formula=(
            "Primary load = Garmin's own activityTrainingLoad, used unchanged. Activities "
            "missing it fall through a provenance-flagged ladder: Edwards TRIMP (Σ minutes-in-"
            "zone × zone) rescaled by a per-athlete linear regression over overlap activities → "
            "athlete-reported RPE×minutes (sRPE) through its OWN dedicated calibration → per-"
            "sport median load-per-minute → 0 ('none'). Days never silently vanish."
        ),
        inputs="activityTrainingLoad where present; hrTimeInZone_1..5; athlete RPE; sport history",
        thresholds=(
            f"regression requires ≥{load_engine.MIN_REGRESSION_PAIRS} same-sport overlap pairs "
            "— below that, median ratio; below any overlap, the value ships uncalibrated and "
            "flagged as such — population default STRUCTURE, but the fitted regression factor "
            "itself is personally derived once enough overlap exists",
            "TRIMP and sRPE are calibrated SEPARATELY (their unit scales differ systematically) "
            "— one factor is never reused for both",
        ),
        caveat=_LOAD_CAL_CAVEAT,
        used_by="garmin_athlete (load currency line), garmin_activity (load provenance)",
        tool_hint="garmin_athlete() for the load-currency line on file",
    ),
}

_HONESTY_NOTES = (
    _ACWR_CAVEAT,
    _RIEGEL_CAVEAT,
    _HIGH_RMSSD_CAVEAT,
    _LOAD_CAL_CAVEAT,
    _HEAT_GUARD_CAVEAT,
    _TREND_CAVEAT,
)


def _normalize(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def _unknown_metric_error(original: str, normalized: str) -> str:
    """Never a bare failure (§4.3): name the nearest matches, falling back to the full
    valid-name list when nothing is close enough to guess at."""
    names = sorted(_ENTRIES)
    close = difflib.get_close_matches(normalized, names, n=3, cutoff=0.4)
    if close:
        listing = f"Nearest: {', '.join(close)}"
        example = close[0]
    else:
        listing = f"Valid names: {', '.join(names)}"
        example = names[0]
    return (
        f"Unknown metric {original!r}. {listing}. "
        f"Example: garmin_reference(metric={example!r})"
    )


def _render_index(ctx: Any) -> str:
    rows = [Row([key, _ENTRIES[key].summary]) for key in sorted(_ENTRIES)]
    honesty = "\n".join(f"- {note}" for note in _HONESTY_NOTES)
    report = Report(
        title="Reference — Metrics Glossary",
        date=ctx.today(),
        data_as_of=ctx.data_as_of(),
        verdict=(
            f"{len(_ENTRIES)} metrics documented — every threshold below is labelled "
            "'population default' or 'personally derived'."
        ),
        banner=ctx.banner(),
        sections=[
            Section(title=None, header=["Metric", "Answers"], rows=rows, priority="primary"),
            Section(title="Honesty notes", header=None, prose=honesty, priority="secondary"),
        ],
        next_steps=[
            "Call again with metric=\"<name above>\" for the formula and provenance",
            "garmin_recovery(days=28) for the physiology audit this glossary explains",
        ],
    )
    return render(report, CAP)


def _render_entry(ctx: Any, entry: Entry) -> str:
    lines = [
        f"**Formula:** {entry.formula}",
        f"**Inputs:** {entry.inputs}",
        "**Thresholds & provenance:**",
    ]
    lines.extend(f"- {t}" for t in entry.thresholds)
    lines.append(f"**Honesty caveat:** {entry.caveat}")
    lines.append(f"**Rendered by:** {entry.used_by}")

    next_steps = ["Call again with no metric for the full index"]
    if entry.tool_hint:
        next_steps.append(entry.tool_hint)

    report = Report(
        title=f"Reference — {entry.title}",
        date=ctx.today(),
        data_as_of=ctx.data_as_of(),
        verdict=f"{entry.title}: formula, inputs, and threshold provenance below.",
        banner=ctx.banner(),
        sections=[Section(title=None, header=None, prose="\n".join(lines))],
        next_steps=next_steps,
    )
    return render(report, CAP)


async def run(ctx: Any, topic: str = "metrics_glossary", metric: str | None = None) -> str:
    if topic not in TOPICS:
        return (
            f"topic must be 'metrics_glossary' (got {topic!r}) — 'workout_schema' ships in a "
            "later phase and is not valid yet. Example: garmin_reference()"
        )

    key: str | None = None
    if metric is not None:
        key = _normalize(metric)
        if key not in _ENTRIES:
            return _unknown_metric_error(metric, key)

    await ctx.ensure_ready()
    if key is None:
        return _render_index(ctx)
    return _render_entry(ctx, _ENTRIES[key])
