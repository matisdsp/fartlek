# Fartlek — Final Design Document

**Fartlek** — *a coach's morning report from your Garmin data, for any LLM via MCP.* PyPI package: `fartlek-mcp`. Named after the Swedish "speed play" training method. Name chosen 2026-07-20 after a conflict check: "Garmin Coach" is Garmin's own adaptive-training-plan product (trademark collision + zero SEO), and PyPI `garmin-coach-mcp` was already taken. "Garmin" appears only descriptively (nominative use); the README must carry a "not affiliated with Garmin Ltd." disclaimer. Tool names keep the `garmin_` prefix (descriptive of the data source).

**Status:** approved for implementation · **Supersedes:** the four design proposals of 2026-07-20 and the completeness review of same date (all accepted findings integrated) · **Audience:** contributors to this open-source repo
**One-line pitch:** every other Garmin MCP hands the LLM a filing cabinet; this one hands it a coach's morning report — computed, baselined against the athlete's own history, significance-tested, safety-gated, and explainable down to the formula — and lets the LLM do the only thing it does better than the server: talk to the athlete.

> **Implementation status (2026-07-20):** the garth → garminconnect auth migration referenced in Phase 0 is already done (see `src/health/adapters/garmin_connect.py` and the `ai-coach-login` CLI; tokens currently at `~/.garminconnect`). The multi-account layout and `fartlek auth/doctor` CLI contract described in §6 are the target to evolve toward.

---

## 1. Vision & design principles

### 1.1 Why synthesis beats passthrough

Real-payload sampling against a live account settles the argument arithmetically: one night of sleep is ~205 KB (~52K tokens), one activity `/details` stream is ~605 KB (~155K tokens), and a naive "look at my week" pull is ~1.1M tokens. The LLM cannot read it, so it skims and improvises — the exact failure the project owner reported. Meanwhile the 807-star incumbent (Taxuspt, 110+ passthrough tools) amputated activity details entirely rather than synthesize them, Garmin's own Connect+ AI is "hilariously bad" (TechRadar), Whoop destroyed trust by letting an LLM free-recall health data, and Athletica was rejected for opaque computed metrics. The untaken ground, confirmed by the competitive analysis, is **deterministic sports-science computation + personal baselines + full explainability + a token-budget contract**. That is this server.

The division of labor is absolute: **the server does everything the model is bad at** (aggregation, arithmetic, baseline comparison, trend extraction, anomaly scanning); **the model does everything the server is bad at** (narration, empathy, dialogue, judgment in conversation). No LLM runs server-side, ever — MCP sampling is unsupported by all three target clients (Claude Code, Claude Desktop, Cursor) and deprecated in the 2026-07-28 spec RC. The synthesis is pure Python.

One more lesson from the incumbent is taken seriously rather than sneered at: Taxuspt's 807 stars come substantially from **distribution mechanics** — one-click Desktop extension, a working MFA auth CLI, Docker — not from design quality. A better-designed server that is harder to install loses. Distribution and onboarding are therefore a first-class workstream (§6), not an afterthought.

### 1.2 The three core bets

**Bet 1 — The verdict contract.** Every response follows one grammar: *safety banner → verdict → evidence table (value + personal baseline + Δ + flag) → watch-list → `Next:` breadcrumb*. Every metric ships pre-compared to this athlete's own rolling baseline with a categorical flag and a one-sentence verdict in words — never a bare number. This converts every model-weak data-extraction task into a model-strong language-comprehension task, and it is enforced by a renderer with hard, CI-tested token budgets. Headline guarantee, published in the README and regression-gated on a precisely defined basis (§5, rule 8): **calling every tool in the catalog once — hard caps, default arguments, escape hatch included — costs under one-third of one raw Garmin sleep payload.**

**Bet 2 — A question-shaped funnel that physically enforces "where to look."** Tools named for the coach's questions, not Garmin's endpoints. One zero-argument entry point (`garmin_brief`), one exception scanner (`garmin_whats_changed`) that checks every metric every sync and returns only what tripped, period tools for week/block/fitness/recovery, and drill-downs keyed by activity IDs that only exist in prior responses. Steering is delivered where it survives: trigger-phrased descriptions, in-response `Next:` breadcrumbs with concrete arguments, and data-shaped sequencing — never reliance on a system prompt we don't control.

**Bet 3 — The athlete file grounds every verdict; the closed loop is the destination.** A local store (`garmin_set_profile`, `garmin_log`) holds what Garmin structurally lacks: the goal race, training phase, availability, session RPE, illness, injuries, life stress. Verdicts may only reference plan/goal context that actually exists **in this store, on the Garmin calendar, or in an enrolled Garmin Coach plan detected at sync** — fabricated plan-awareness ("landed as planned" with no plan on file anywhere) is banned by contract. Subjective entries gate the readiness verdict: the sensors do not outrank the athlete. Phase 3 closes the loop no competitor has: validated structured workouts pushed to the watch (`garmin_apply_plan`, dry-run-first, token-bound confirmation), with the debrief automatically scoring executed vs. prescribed — using the planned-vs-executed matching engine that ships in Phase 1 (§3.2 #15), so read-side compliance works from v0.1.

### 1.3 Non-negotiable principles

1. **Deterministic math only.** Every number traces to a formula in the metrics glossary. No hidden LLM calls.
2. **Consume, don't recompute.** Garmin ships HRV baseline bands, ACWR, VO2max, sleep scores, per-activity load and time-in-zone — and, on capable devices, running tolerance and endurance score. We surface, trend, and translate these; we compute only what Garmin lacks (true PMC, monotony/strain, EF/decoupling, durability, SRI, race triangulation, fused readiness, anomaly scanning, attribution rules).
3. **Personal baselines, honestly labeled.** Reference bands derived from the athlete's own 7/28/60/90-day distributions. Where a threshold is a population default (ACWR 0.8–1.3, monotony 2.0, decoupling 5%), the glossary says so and verdicts carry the caveat.
4. **Fetch-once → digest → cache → serve.** Garmin's documented 429 lockouts make per-question fetching non-viable. Token bombs are digested at sync time and the raw bytes discarded. Every tool call is a local SQLite read.
5. **Local-first privacy.** stdio transport, credentials and health data never leave the machine, and the athlete can wipe or export everything with one command (§3.3). The counter-position to freddy.coach ($49/yr hosted) and Connect+ ($7/mo).
6. **Confidence gates verdict strength.** Provisional baselines forbid authoritative phrasing. "PROVISIONAL (n=12 of 42 days) — leaning GREEN" is a valid verdict; a confident GREEN on thin data is not.
7. **Safety is cross-cutting, not funnel-dependent.** Any active RED/AMBER alert renders as a banner atop **every** tool response, undroppable, regardless of which tool the model calls first.
8. **Markdown is the payload.** `structured_output=False` on every tool. One authoritative, budget-counted text response; no doubled context cost from parallel `structuredContent`.

---

## 2. The tool surface

### 2.1 Layered hierarchy

```
Layer 0 — entry points (zero/near-zero knowledge required)
  garmin_brief          ← "how am I today / should I train?"
  garmin_whats_changed  ← "anything I should know? / catch me up"

Layer 1 — period synthesis
  garmin_week           ← one week, session-level detail
  garmin_load           ← multi-week dose: PMC, ramp, ACWR, TID drift
  garmin_fitness        ← outcomes: VO2max/EF/durability, race feasibility & projection
  garmin_recovery       ← physiology: sleep/HRV/RHR/stress + overtraining audit

Layer 2 — drill-down & reference
  garmin_activities     ← logbook (emits ALL activity IDs)
  garmin_activity       ← one session (by id, date, or latest-of-sport)
  garmin_athlete        ← profile card, zones, PRs, goal, plan enrollment, coverage
  garmin_reference      ← metrics glossary (Phase 2); + workout schema (Phase 3)

Layer 3 — local writes & plumbing
  garmin_set_profile    ← goal race / phase / availability / overrides (local)
  garmin_log            ← RPE, Hooper-style wellness, illness/injury notes (local)
  garmin_sync           ← force refresh / backfill (rarely needed)

Escape hatch
  garmin_raw            ← bounded, compacted, whitelisted raw views

Phase 3 (write path to the watch)
  garmin_apply_plan     ← dry-run-first structured-workout scheduling
```

Catalog counts: **13 tools at v0.1** (Phase 1 surface + placeholders resolved per §6), **14 at v0.2** (adds `garmin_reference`), **15 at v0.3** (adds `garmin_apply_plan`). Definition footprint target **≤ 3.5K tokens through v0.2, measured in CI** (lean because no output schemas are emitted).

### 2.2 Fate of the current 12 tools

| Current tool | Fate |
|---|---|
| `get_daily_health` | **dies** → digested into `garmin_brief` |
| `get_sleep` | **dies** → `garmin_brief` (last night) + `garmin_recovery` (trend); 205 KB payload digested server-side |
| `get_recent_activities` | **transforms** → `garmin_activities` (computed columns, full ID list) |
| `get_activity_details` | **transforms** → `garmin_activity` (computed digest; raw streams never forwarded) |
| `get_training_readiness` | **dies** → fused into `garmin_brief` (verified `[]` on real devices; server computes its own fusion) |
| `get_morning_readiness` | **dies** — ambiguous sibling of the above, the exact anti-pattern the research flags |
| `get_training_status` | **dies** → `garmin_load` (chronic-load-as-tunnel-midpoint approximation dies with it) |
| `get_hrv` | **dies** → `garmin_brief` row + `garmin_recovery` section |
| `get_body_battery` | **dies** (the `highest/lowest ← startBattery/endBattery` mapping bug dies with it) |
| `get_stress` | **dies** → `garmin_brief` / `garmin_recovery` |
| `get_user_profile` | **merges** → `garmin_athlete` (+ resource mirror) |
| `get_personal_records` | **merges** → `garmin_athlete` |

No raw-JSON tool survives as a peer of the synthesis tools.

### 2.3 Global tool conventions

- All read tools: `readOnlyHint=True, destructiveHint=False, openWorldHint=False`.
- `garmin_set_profile`, `garmin_log`: `readOnlyHint=False, destructiveHint=False, idempotentHint=True` (local-only writes, disclosed in descriptions).
- `garmin_sync`: `readOnlyHint=False` (network fetch), `destructiveHint=False`.
- Dates in and out: `YYYY-MM-DD`; all rendered dates carry day-of-week.
- Every response ends with a `Next:` breadcrumb naming **only declared tools with declared parameters** (CI-enforced — see §4.5).
- Token budgets per tool are hard caps enforced by the renderer (§5).
- **Parameter-count discipline:** read tools carry ≤4 parameters. The two local write tools may exceed the researched ~8-parameter guidance because every parameter is optional, they are called rarely and deliberately, and splitting them would multiply catalog tokens for no routing benefit. **Injury notes have exactly one owner: `garmin_log` (flag="injury")** — `garmin_set_profile` does not accept them.
- **RPE has two sources, one precedence rule (§3.1):** an explicit `garmin_log` entry always overrides the watch-native on-device report; tables display whichever applies, provenance-flagged.

---

### 2.4 Tool inventory

#### `garmin_brief` — budget 400 / cap 600 tokens

> **Description (LLM-facing):** "Call this FIRST for any question about how the athlete is doing **today**: readiness, whether to train, how recovery looks right now, or anything time-ambiguous about current state. Zero arguments needed. Returns a fused go/modify/rest verdict with every recovery signal compared to this athlete's personal baseline, active alerts, yesterday's session (with its activity_id), and today's scheduled workout. For multi-day questions use garmin_week, garmin_load, garmin_recovery, or garmin_fitness directly — do not call this tool first for those."

**Params:** `date: str = today` (retrospective briefs).

```markdown
# Daily Brief — Sun 2026-07-20 (data as of 07:41)

**VERDICT: GREEN — cleared for quality. Markers used: HRV, RHR, sleep, form, Body Battery.**

| Signal | Today | Your baseline | Δ | Flag |
|---|---|---|---|---|
| HRV overnight | 97 ms (7d avg in band) | band 83–106 (Garmin baseline) | in band, 9d stable | ✓ |
| Resting HR | 44 bpm | 30d median 44 (43–47) | 0 | ✓ |
| Sleep | 9h00, score 66 (Fair) | need 8h00 | +60 min | ⚠ long but light |
| Deep sleep | 11 min | typical 45–60 (n=38 nights) | −76% | ⚠ 2nd low night |
| Body Battery at wake | 99 | 30d wake avg 88 | +11 | ✓ |
| Form (TSB/CTL) | −9% | productive −10…−30% | — | ✓ |

Watch: deep sleep low 2 nights running — monitor, not a blocker. If HRV drops
below 83 tomorrow, downgrade planned intensity.
Yesterday: Run 12.0 km easy · 62:24 · HR 115 · load 41 · RPE 2/10 (id 19501244).
Today's plan: nothing on the Garmin calendar. No goal-race phase on file.

Next: garmin_activity(activity_id=19501244) · garmin_recovery(days=14) for the
deep-sleep pattern · garmin_week() for the weekly picture
```

Notes: the verdict is server-fused (§3.2 #18) because Training Readiness is verifiably `[]` on many devices; when Garmin's score exists it is shown as a cross-check. The band flag driving the verdict uses the **7-day rolling lnRMSSD vs. the band** (§3.2 #8); last night's value is displayed with its streak context, and only the acute override (§3.2 #19) acts on a single night. The verdict line always declares which markers were available (weight renormalization, §3.2 #18). A same-day `garmin_log` illness/injury entry caps the verdict (§3.2 #19). The "Today's plan" line reads the Garmin calendar **including workouts scheduled by an enrolled Garmin Coach adaptive plan**; enrollment is named when present ("Today's plan: 45 min tempo — Garmin Coach half-marathon plan, wk 4"). Yesterday's activity ID is inline in the body, not only in the breadcrumb.

#### `garmin_whats_changed` — budget 500 / cap 700 tokens

> "Call when the user asks 'anything I should know?', 'what's new?', 'catch me up', or at the start of a coaching session after days away. Scans every tracked metric over the last N days and returns ONLY statistically significant changes, ranked safety-first. Returns 'nothing notable' when nothing tripped. For today's readiness specifically, garmin_brief is the right call instead."

**Params:** `since_days: int = 7` (1–60).

**Ranking schema (fixed, this is the coaching judgment):** 1) health/overtraining/illness risk signals → 2) load anomalies (ramp, monotony, ACWR breach) → 3) recovery degradation (sleep debt, HRV/RHR drift) → 4) performance/fitness gains → 5) stable summary line. The scanned-metric set is whatever the capability map says this account produces; the header states the count.

```markdown
# Changes — last 7 days (scanned Sun 2026-07-20 · 16 metrics checked)

3 significant, 13 stable.

1. ⚠ **Deep sleep declining:** 4-night streak below your band (11–18 vs 45–60 min
   typical). Not yet in overtraining convergence (HRV, RHR normal). Pattern matches
   late bedtimes, not load (attribution rule: bedtime variance high, load normal — §3.2 #22).
2. **Grey-zone creep:** mid-zone share 9% → 19% of run time this week vs your
   12-wk norm. Three "easy" runs drifted to tempo HR.
3. **VO2max up:** 60.9 → 61.4 (precise), 3rd consecutive weekly rise — trend real
   (autocorrelation-robust test over 8 wk).

Stable: HRV, RHR, sleep duration, regularity, stress, Body Battery, weight,
monotony, ACWR, EF, cadence, respiration, weekly volume.

Next: garmin_recovery(days=14) for the sleep pattern · garmin_load(weeks=4) for
the distribution drift
```

#### `garmin_week` — budget 900 / cap 1,200 tokens

> "The coach's weekly review: one week in session-level detail. Call for 'how was my week', Sunday/Monday check-ins, or to see every session of a specific week with its activity_id. Returns load vs. recent weeks, intensity distribution, per-day session table, sleep/recovery summary, and plan compliance where a planned workout exists (Garmin calendar or enrolled Garmin Coach plan). For multi-week load trajectory use garmin_load; for fitness outcomes use garmin_fitness."

**Params:** `anchor_date: str = today` (resolves to the Mon–Sun ISO week containing it; incomplete weeks disclosed).

```markdown
# Week — Mon 2026-07-14 → Sun 2026-07-20 (complete) · phase on file: none

**VERDICT: a good, absorbable week — load +8% on a sustainable ramp, distribution
on your norm, recovery held. One fix: hard days landed back-to-back (Sat/Sun).**

| Load | This wk | Prev | 4-wk avg | Flag |
|---|---|---|---|---|
| Volume | 52 km / 5h10 | 48 km | 49 km | ✓ |
| Load (Garmin) | 412 | 380 | 372 | ✓ +8% |
| Ramp | +5.1%/wk of CTL | +4.4% | sustainable 4–8% | ✓ |
| ACWR (EWMA) | 1.12 | 1.05 | 0.8–1.3 (population band, weak signal) | ✓ Garmin: 0.9 optimal |
| Monotony | 1.6 | 1.4 | flag > 2.0 | ✓ |

Distribution (3-zone, mapped from your configured zones): 79 / 12 / 9 % by time —
consistent with your pyramidal norm. No grey-zone creep this week.
Recovery: HRV in band 7/7 · RHR flat 44 · sleep 7h21 avg vs 8h00 need →
14d debt 4h36 ⚠ rising · regularity 81/100.

| Day | Session (id) | Load | Note |
|---|---|---|---|
| Tue | Run 6×800 (19483321) | 68 | reps 5–6 faded −4% |
| Wed | Strength 30' (19488102) | 4 | — |
| Thu | Run 10.1 km easy (19492750) | 35 | clean Z1 |
| Sat | Run 12.0 km easy (19501244) | 41 | decoupling 2.1% (splits-based) |
| Sun | Run 12.4 km 6×1k (19510992) | 78 | quality |
Mon/Fri: rest.

Next: garmin_activity(activity_id=19483321) for the interval fade ·
garmin_load(weeks=8) for block context · garmin_recovery(days=14) for sleep debt
```

Compliance uses the Phase-1 matching engine (§3.2 #15); weeks with no planned workouts render no compliance section at all rather than an empty one.

#### `garmin_load` — budget 800 / cap 1,100 tokens

> "Multi-week training dose: fitness/fatigue/form (CTL/ATL/TSB) computed from full local history, ramp rate, load ratio (ACWR), monotony/strain history, and intensity-distribution drift. Call for 'is my training load right', 'am I training too much/little', ramp/taper dosing, and periodization questions. NOT for single-day readiness (garmin_brief) and NOT for the physiology side of overtraining (garmin_recovery owns that; this tool covers the load-structure side and cross-references it)."

**Params:** `weeks: int = 8` (2–52), `anchor_date: str = today`.

```markdown
# Training Load — 8 weeks to Sun 2026-07-20 (currency: Garmin load · 214d history)

**VERDICT: durable build with headroom. Ramp sustainable, variety good; one drift —
mid-zone share rising 6 weeks straight.**

Fitness (CTL): 421 → 486 (+15% in 8 wk, avg +1.9%/wk — sustainable band 4–8%/wk has headroom)
Fatigue (ATL): 528 · Form: −9% of CTL — productive zone (−10…−30%)
CTL weekly: 421→432→441→438→452→463→474→486
Load weekly: 310→342→296(deload)→405→380→412→390→412
ACWR (EWMA 7:28): 1.12 — in 0.8–1.3 population band; Garmin agrees (0.9 optimal).
Treat as spike detector, not verdict.
Monotony 1.3–1.6 all block ✓ · Strain peak wk 06-22 (84th pctile of your 90d) — no
illness followed. Personal precedent: your prior HRV suppression (mid-Jun) was
preceded by monotony >1.9 — current 1.6 is clear of your own trigger level
(1 precedent in the 60-night HRV window; deepen with garmin_sync backfill).
TID drift: mid-zone 9% → 19% over 6 wk vs your pyramidal norm — grey-zone creep.
Keep hard days hard, easy days easy (your easy ceiling: HR 148).

Next: garmin_fitness() — is the load producing adaptation ·
garmin_week(anchor_date=2026-06-22) for the strain-peak week
```

Where the device produces **running tolerance** (§3.2 #23), an impact-load-vs-capacity line renders here ("impact load Nth pctile of your tolerance — within/over capacity"); absent capability, the line is absent, never faked.

#### `garmin_fitness` — budget 700 / cap 1,000 tokens

> "Fitness outcomes and race feasibility: VO2max/threshold trends, aerobic efficiency (EF), long-run durability, race predictions triangulated from three independent models with the gap to the stored goal, and a form projection to race day with taper guidance. Call for 'am I getting fitter', 'is training working', race planning, taper timing, or goal-feasibility questions. The goal race comes from the athlete profile — set or change it with garmin_set_profile."

**Params:** `weeks: int = 12` (4–52).

```markdown
# Fitness & Race Outlook — 12 weeks to Sun 2026-07-20

**VERDICT: aerobic fitness rising; the marathon goal is volume-limited, not
speed-limited.**

VO2max (Garmin): 59.8 → 61.0, steady rise · LT: 172 bpm @ 3:58/km (−3 s/km, 12 wk)
EF (steady-run benchmark, splits-based, hot-day sessions excluded): 1.31 → 1.38
(+5% — cleanest fitness signal here)
Durability: decoupling on last three 90 min+ runs 4.2 / 3.8 / 5.1% — at the 5%
line; unproven beyond 2h (n=3 long runs — LOW confidence).

Race predictions — Marathon, goal Sun 2026-09-20, target 2:59 (62 days out):
| Model | Prediction | Basis |
|---|---|---|
| Garmin | 3:06:53 | device model |
| Tanda | 3:09:40 | 8 wk: 54 km/wk @ 5:12/km training |
| Riegel (10K 38:43, exp 1.06 fitted) | 3:04:12 | assumes marathon-ready endurance |
| Consensus | 3:04–3:10 | gap to goal 5–11 min |
Models disagree because durability is unproven — weight Tanda over Riegel here.
Levers (Tanda sensitivity): +8 km/wk ≈ −2:30 · −5 s/km training pace ≈ −1:50.
Volume is your bigger lever and also fixes the durability gap.

Projection: no scheduled workouts on the calendar — assuming your current weekly
pattern holds, CTL ≈ 540 at taper start (Sun 08-30, 3 wk out). Taper target: bring
form from −9% to +5…+25% by race day while CTL fades <10%. Detailed taper-week
dosing activates once inside 3 weeks of the goal date.

Next: garmin_load(weeks=8) for where volume can go · re-check after any tune-up race
```

The projection (§3.2 #17) runs the PMC forward from current CTL/ATL using scheduled calendar workouts where they exist, else the athlete's trailing 4-week load pattern — and always discloses which. It is arithmetic on the athlete's own numbers, never a promise.

**Degradation:** strength-dominant athletes get volume/set/load trends and no race section (one line: "race modeling not applicable to this activity mix"). **Cycling-primary athletes** get FTP trend, power-based EF (NP/avgHR where power exists), CTL/form and the projection — with one line, "run-race triangulation not applicable; cycling race models arrive with the power-depth extension (Phase 4)." **Swim sessions** feed the load ledger and volume totals but get no swim-specific fitness modeling in v1 (no CSS, no swim EF) — summary + load only, disclosed. Wellness-only users get "no fitness outcomes trackable from current activity mix" plus whatever VO2max/RHR trends exist. Where the device produces **endurance score** (§3.2 #23), its trend renders as one line in this tool.

#### `garmin_recovery` — budget 800 / cap 1,100 tokens

> "Recovery physiology over time: sleep, HRV, resting HR, stress, Body Battery vs. personal baselines, plus the multi-marker overtraining/illness audit. Call when the user asks why they feel tired, how they're sleeping, whether they're overtraining or getting sick, or when another tool flags a recovery signal. This tool OWNS overtraining questions; garmin_load covers only the load-structure side."

**Params:** `days: int = 28` (7–90), `anchor_date: str = today`.

```markdown
# Recovery — 28 days to Sun 2026-07-20

**VERDICT: coping well with building load. Overtraining audit: 1 of 10 markers off
(sleep debt) — below the ≥2-group convergence bar. No alarm.**

| Marker | State (28d) | Off baseline? |
|---|---|---|
| HRV (7d roll) vs band 83–106 | 27/28 in band, no streak below | no |
| HRV day-to-day CV | 6.2% vs 7.0% — falling under rising load | no (favorable) |
| Resting HR (deviation, either direction) | 44 ±1, flat | no |
| Sleep debt (14d vs need) | 4h36, rising | **YES** |
| Sleep regularity (SRI) | 81/100 (elite ~85) · social jetlag 1h05 | borderline |
| Deep sleep | 4-night low streak, matches late bedtimes | watch |
| Monotony / strain | max 1.9 / 84th pctile | borderline |
| Performance at fixed HR (EF) | +5% over 8 wk | no (improving) |
| Internal vs external (sRPE at fixed load) | stable across rated sessions | no |
| HR response (hard-session maxHR, HRR where recorded) | reaching usual ceiling; HRR normal | no |
| Subjective (garmin_log Hooper items) | no illness/injury notes in window | no |

This is the signature of load being absorbed, not accumulated. Highest-yield
action the data supports: fix bedtime variance before adding sleep hours.
Escalation rule on file: HRV below band ≥3 consecutive days AND/OR RHR deviating
±5 sustained AND/OR EF −5% at benchmark → RED with deload advice. Acute rule: one
severe single-day signal (RHR +7, HRV lowest-in-90d, athlete illness note)
escalates same-day.

Next: garmin_week() for the bedtime fix in context · log symptoms anytime with
garmin_log(note="...", flag="illness")
```

The audit implements §3.2 #20: RHR is monitored **two-sided** (elevation is the classic flag; a sustained *drop* below baseline alongside other symptoms is flagged as the parasympathetic pattern), and the HR-response group (suppressed hard-session max HR vs. 90d, worsening heart-rate recovery where `recoveryHeartRate` exists) corroborates but never triggers alone.

#### `garmin_activities` — budget ~40 tokens/row / cap 1,300 tokens

> "Browse the training log and get activity IDs for drill-down. One compact row per session — every row carries the activity_id that garmin_activity accepts. Filter by date range and sport. All rows in the window are listed (up to limit); truncation is disclosed with narrowing advice."

**Params:** `start_date: str = today−13d`, `end_date: str = today`, `sport: Literal["running","cycling","swimming","strength","other"] | None = None`, `limit: int = 25` (1–30; the ceiling exists so the row budget fits the cap — narrower windows, not bigger pages, are the drill-down path).

```markdown
# Activities — Mon 2026-07-07 → Sun 2026-07-20 · 11 sessions · runs 9 (98 km), strength 2

| Date | Sport | Session | id | Dist | Time | Pace | avgHR | Load | RPE |
|---|---|---|---|---|---|---|---|---|---|
| Sun 07-20 | run | 6×1k intervals | 19510992 | 12.4 | 58:03 | 4:41 | 142 | 78 | — |
| Sat 07-19 | run | easy | 19501244 | 12.0 | 62:24 | 5:12 | 115 | 41 | 2 |
| Thu 07-17 | run | easy | 19492750 | 10.1 | 55:10 | 5:28 | 112 | 35 | — |
| Wed 07-16 | strength | gym | 19488102 | — | 29:39 | — | 96 | 4 | — |
| Tue 07-15 | run | 6×800 | 19483321 | 8.4 | 42:05 | 5:01 | 141 | 68 | 7 |
| … 6 more rows, all listed … |

Next: garmin_activity(activity_id=19483321) for any session ·
garmin_activity(sport="running") for the latest run
```

The RPE column shows the athlete's `garmin_log` entry where one exists, else the watch-native on-device report converted per §3.1 (marked `w` for watch-sourced).

#### `garmin_activity` — budget 800 (standard) / caps: standard 1,000 · splits 2,000 · full 4,000

> "Deep analysis of ONE session: execution vs. structure, rep-by-rep fade for intervals, decoupling/EF for steady runs, comparison to the most similar past session, planned-vs-executed where a planned workout exists (calendar or enrolled plan). Select by activity_id (from garmin_activities/garmin_brief/garmin_week), OR by date, OR omit both for the latest activity — add sport to get the latest of that sport ('analyze my last run' = garmin_activity(sport='running')). detail='standard' is enough for coaching; 'splits' adds the full lap table; 'full' adds a 20-point downsampled HR/pace curve."

**Params:** `activity_id: int | None = None`, `date: str | None = None`, `sport: Literal[...] | None = None`, `detail: Literal["standard","splits","full"] = "standard"`.
**Resolution order:** `activity_id` wins → else `date` (no match: corrective error naming nearest activities with IDs) → else latest activity of `sport` → else latest activity. The chosen resolution is stated in the header.

```markdown
# Run — "6×800" — Tue 2026-07-15 18:04 (id 19483321 · selected: by id)

8.4 km · 42:05 · avg HR 141 / max 172 · load 68 · aerobic TE 3.8 · athlete RPE 7/10
· 21°C, 53% RH

**VERDICT: structure executed, reps 5–6 faded 3–4% at rising HR — pace slightly hot
for the day, or recoveries too short. Same session on Tue 07-01: avg rep 4:01 at
same HR → economy at speed improving.**

| Rep | Pace | avgHR | vs rep 1 |
|---|---|---|---|
| 1 | 3:58 | 158 | — |
| 2 | 3:57 | 162 | −0.4% |
| 3 | 3:59 | 164 | +0.4% |
| 4 | 4:02 | 166 | +1.7% |
| 5 | 4:06 | 167 | +3.4% |
| 6 | 4:08 | 167 | +4.2% |
Recoveries 2:00 avg, HR fell to 128–135 — good between-rep recovery. Cadence
stable 178±2; stride −3% late = fatigue, not form breakdown.
No planned workout matched to this date — no compliance score.

Ask the athlete how it felt if RPE is missing from both the log and the watch
→ garmin_log(rpe=..., activity_id=19483321)
Next: garmin_fitness() — is interval work moving your predictions ·
garmin_activity(activity_id=19483321, detail="splits") for all laps
```

Compliance sections work from Phase 1 via the matching engine (§3.2 #15). **Fallbacks (stated in rendering, not hidden):** unstructured activities without `INTERVAL_ACTIVE` typed splits fall back to manual-lap heuristics, else to a summary + splits verdict ("no interval structure detected — freeform session"). Strength activities with `UNKNOWN`/zero-weight exercise sets fall back to duration/HR/load/set-count summary instead of an empty set table. Steady runs replace the rep table with per-split aggregates, halves decoupling, and a durability line. Hot-day sessions carry an EF annotation per §3.2 #12.

#### `garmin_athlete` — budget 450 / cap 600 tokens

> "Athlete reference card: zones, thresholds, PRs, goal race and phase (from garmin_set_profile), Garmin Coach plan enrollment, personal baselines, injury notes, device data coverage. Call once when athlete context is unknown; contents change rarely. Also available as resource garmin://athlete/snapshot. To change goal/phase/overrides, use garmin_set_profile."

**Params:** none.

```markdown
# Athlete — Sun 2026-07-20

34y male · 68 kg · VO2max 61.0 · primary sport: running (31 of last 50), strength 2×/wk
**Goal (on file):** Marathon Sun 2026-09-20, target 2:59 · phase: Build wk 2 of 6
· availability 6 d/wk (set 2026-07-12 via garmin_set_profile)
**Garmin plan:** no enrolled Garmin Coach plan detected · no Garmin goals set
**Engine:** LT 172 bpm @ 3:58/km · maxHR 191 · RHR baseline 44 · run FTP 305 W
**Zones (from your Garmin config, 3-zone mapping):** easy <148 · mid 148–172 · hard >172
**PRs:** 5K 18:21 · 10K 38:43 · Half 1:26:25 · longest run 32.1 km
**Baselines (60d):** HRV band 83–106 · sleep need 8h00 · wake Body Battery 88
**Notebook (garmin_log):** L-achilles tightness 14–21 Jun (resolved)
**Data coverage:** ✓ HRV, sleep, power, dynamics, race predictions · ✗ Training
Readiness, Endurance Score (device does not produce them — this server computes its
own readiness fusion instead) · 214 days synced · load currency: Garmin activity load

Next: garmin_set_profile(...) to change goal/phase · everything above is already
inlined in other tools where relevant
```

The coverage block renders every capability probe (§3.3), including running tolerance, endurance score, and plan enrollment — never silent nulls.

#### `garmin_reference` — cap 2,000 tokens per topic · ships Phase 2

> "The server's methodology, on demand: every formula, threshold table with provenance (population default vs. personally derived), and honesty notes (ACWR criticisms, Riegel caveats, load-calibration method). Call when the user asks how a number was computed or whether to trust it. Phase 3 adds the structured-workout schema topic."

**Params:** `topic: Literal["metrics_glossary","workout_schema"]` (`workout_schema` valid from Phase 3).

This is the tool mirror of the `garmin://reference/*` resources, shipped **in the same release as the glossary itself** because Claude Desktop cannot pull resources model-side — the "explainable down to the formula" promise must be reachable on all three target clients from v0.2, not v0.3.

#### `garmin_set_profile` — budget ≤200 tokens · local write

> "Set or update athlete context the watch can't know: goal race (date/distance/target time), current training phase, weekly availability, intensity-distribution preference, LT1 override. Stored locally only; grounds the plan/goal context used by other tools. Only provided fields change. Call when the user states or changes a goal, phase, or constraint. Injuries and illness belong to garmin_log, not here."

**Params (10, all optional):** `goal_race_date: str`, `goal_distance: Literal["5k","10k","half","marathon","custom"]`, `goal_custom_km: float`, `goal_time: str "H:MM:SS"`, `phase: Literal["base","build","peak","taper","recovery","none"]`, `phase_week: int`, `phase_total_weeks: int`, `availability_days: int (1–7)`, `tid_target: Literal["polarized","pyramidal","auto"]`, `lt1_hr_override: int`.

```markdown
Profile updated: phase Build (wk 2 of 6) · goal unchanged (Marathon 2026-09-20,
2:59). These now appear in garmin_brief, garmin_week, and garmin_fitness verdicts.
```

#### `garmin_log` — budget ≤120 tokens · local write

> "Record subjective data the watch cannot capture: session RPE (1–10) and Hooper-style wellness — fatigue, soreness, stress, mood, sleep quality (each 1–7) — plus notes, especially illness or injury (set flag; resolve when healed). Stored locally; feeds sRPE load, monotony, the readiness verdict (an illness note caps today's verdict), and the overtraining audit's subjective row. Ask the athlete for RPE after discussing a session if it is missing from both the log and the watch."

**Params:** `date: str = today`, `rpe: int | None (1–10)`, `fatigue: int | None (1–7)`, `soreness: int | None (1–7)`, `stress: int | None (1–7)`, `mood: int | None (1–7)`, `sleep_quality: int | None (1–7)`, `note: str | None`, `flag: Literal["illness","injury"] | None`, `resolve_flag: bool = False` (marks the most recent open flag of that kind resolved), `activity_id: int | None`.

```markdown
Logged Sun 2026-07-20: RPE 6/10 → run 19510992 (sRPE 348 AU, alongside Garmin
load 78) · note "legs heavy late". Feeds tomorrow's readiness and the monotony series.
```

An explicit `rpe` here **overrides** any watch-native report for the same activity (§3.1).

#### `garmin_sync` — budget ≤150 tokens

> "Force a data refresh from Garmin and report freshness, or start a historical backfill (backfill_days > 0; runs with progress updates, resumable). Backfill also deepens the sleep/HRV history window, which extends personal-precedent mining. Use only if the user says data looks stale — all other tools auto-refresh when stale."

**Params:** `backfill_days: int = 0`.

#### `garmin_raw` — cap 5,000 tokens (`anthropic/maxResultSizeChars` set)

> "Bounded escape hatch to a named Garmin data source, compacted (nulls/boilerplate/IDs stripped, series downsampled) and hard-capped. Use ONLY when a synthesis tool cannot answer and the user explicitly asks for underlying values. Never a starting point."

**Params:** `source: Literal["daily_summary","sleep_detail","hrv_detail","stress_detail","body_battery","activity_summary","activity_splits","activity_zones","training_status","race_predictions","weather"]`, `date: str = today`, `activity_id: int | None = None` (required for `activity_*` and `weather`), `series: Literal["hypnogram","hr","movement","spo2","respiration","stress"] | None = None` (valid only with `sleep_detail`), `max_points: int = 50` (≤200, server-side downsampled).

Truncation is always disclosed with the exact narrower call that retrieves more (naming only declared parameters).

#### Phase 3: `garmin_apply_plan` (specified now, built later)

`garmin_apply_plan` — writes structured workouts to the Garmin calendar (synced to the watch with on-wrist guidance) and/or removes scheduled ones.
- **Params:** `add: list[WorkoutSpec] = []`, `remove: list[int] = []`, `dry_run: bool = True`, `plan_token: str | None = None`.
- **Contract:** the dry run validates specs against the athlete's zones and pace history, simulates the projected week (load, ramp, ACWR, monotony — the same forward-PMC machinery as §3.2 #17), **rejects or warns on physiologically implausible or load-spiking prescriptions**, and returns a `plan_token` (hash of the validated plan). Execution requires `dry_run=False` **plus the matching `plan_token`** — a mismatch errors with instructions to re-run the dry run. This binds what executes to what was previewed; the model cannot silently mutate the plan between preview and write.
- **Annotations:** `readOnlyHint=False, destructiveHint=True, idempotentHint=False`, `anthropic/requiresUserInteraction`. Elicitation confirmation where the client supports it; dry-run-then-confirm conversation everywhere else.
- **Compliance scoring** against pushed plans reuses the planned-vs-executed matching engine that ships in Phase 1 (§3.2 #15) — Phase 3 adds the prescription side, not the matcher.

---

## 3. Computed metrics engine

One deterministic Python engine (`src/analytics/`), pure functions over the local store, callable from tools, prompts, and resources. Cost classes: **A** = O(days) over cached scalars (microseconds); **B** = requires one cheap per-activity fetch (splits, ~3 KB) at sync; **C** = requires one `/details` stream fetch (digested once, raw discarded).

### 3.1 Canonical load currency (the substrate — get this right or everything downstream is wrong)

- **Primary daily load = Garmin `activityTrainingLoad`**, summed per local calendar day. Verified present per-activity **in the activities list payload** — 180 days of daily load history costs ~8–10 paginated list calls with zero N+1 fetches, and it is device-consistent across running, cycling, strength, and swim. This is the only scale used by PMC, ACWR, monotony, strain, ramp, the forward projection, and the Phase-3 guardrail simulation.
- **Fallback ladder for activities missing it** (each step provenance-flagged in the store and disclosed in any response whose window contains such days):
  1. Edwards TRIMP from `hrTimeInZone_1..5` (also in the list payload), **rescaled to the Garmin-load scale by a per-athlete linear regression over overlap activities** (min n=10 same-sport pairs; below that, per-sport median ratio; with zero overlap, the uncalibrated value with `load_source=trimp_uncalibrated`).
  2. No HR but an athlete RPE (log or watch-native): sRPE×min through the same calibration path.
  3. **Terminal fallback — no HR, no RPE** (manual entries, third-party syncs): the athlete's own per-sport median load-per-minute × duration, flagged `load_source=estimated`; if the athlete has no history in that sport, the day carries 0 for that activity and the disclosure line says so. Days never silently vanish from the ledger.
- **RPE sources and precedence.** Garmin's on-watch self-report (`directWorkoutRpe`/`directWorkoutFeel`, 0–100 scales) is consumed at sync: RPE converted to CR-10 by `round(x/10)` (feel kept as a 1–5 annotation), stored provenance-flagged `rpe_source=watch`. An explicit `garmin_log` entry for the same activity **overrides** it (`rpe_source=athlete`). Whichever survives feeds the sRPE ledger, the monotony subjective inputs, and the RPE columns in every table.
- **sRPE (RPE × minutes)** is additionally kept as a **parallel internal-load ledger** — never mixed into the PMC series — used for the internal-vs-external divergence signal ("RPE rising at fixed external load" early fatigue), rendered as its own row in `garmin_recovery`'s overtraining audit.
- **Never mixed scales, never seeded from incompatible units.** PMC is warmed directly from the 180-day backfill (>2× the 42-day time constant), so there is no seeding step at all and CTL is trustworthy on day 0.

### 3.2 The metric catalog

| # | Metric | Definition / formula | Inputs | Cost | Cold start |
|---|---|---|---|---|---|
| 1 | **PMC (CTL/ATL/TSB)** | `CTL += (L−CTL)(1−e^(−1/42))`; `ATL += (L−ATL)(1−e^(−1/7))`; `TSB = CTL_y − ATL_y`; missing days = 0 | daily load series (§3.1) | A | fully warm day 0 (180d backfill) |
| 2 | **Form ratio & bands (scale-invariant)** | Form% = TSB/CTL × 100. Bands: +5…+25% fresh/race-ready · −10…−30% productive · < −40% overload flag. Ramp = ΔCTL/wk expressed as %CTL/wk, sustainable band 4–8%, flag >10%. Because both are ratios, the TSS-derived interpretation zones transfer regardless of load currency | PMC | A | day 0 |
| 3 | **ACWR (EWMA 7:28)** | ratio of EWMAs. Guards: suppressed + labeled "unreliable" when history <28d or chronic <30% of the 90d median (layoff instability). Always rendered with the contested-science caveat. Ours is primary (transparent, historical); Garmin's `dailyAcuteChronicWorkloadRatio` shown as cross-check; disagreement >0.2 stated explicitly | daily load; training status | A | day 0 |
| 4 | **Monotony & Strain (Foster)** | `monotony = mean(7d load)/SD(7d load)`; `strain = weekly load × monotony`; flag monotony >2.0; strain as percentile of the athlete's own 12-wk distribution | daily load (incl. rest days = 0) | A | day 0 |
| 5 | **Personal precedent flags** | for each prior HRV-suppression streak or logged illness, record the preceding 14d monotony/ramp/strain; flag when current values exceed the athlete's own historical trigger levels. **Mining runs retroactively over the full backfilled window at Tier-2 completion** (precedent depth = HRV/sleep backfill depth, extensible via `garmin_sync(backfill_days=N)`), then builds forward. Silent until ≥1 precedent exists; renders its precedent count | PMC + alerts history + log | A | retro-mined day 0–1, then forward |
| 6 | **Baseline engine (universal)** | rolling mean/median at 7/28/60/90d, robust SD via MAD (1.4826×MAD), z-score, band position, percentile, streak counters — for every daily scalar (incl. weight, synced per §3.3 Tier 1) | daily digests | A | window-labeled (`n=` shown when < full window) |
| 7 | **Trend significance** | Hamed–Rao autocorrelation-corrected Mann–Kendall + Sen's slope; "significant" iff corrected p<0.05 AND |slope×window| > SWC. **Default SWC = 0.5 × 90d MAD-SD** (the baseline engine's robust SD) for every metric, with glossary-named exceptions: lnRMSSD 0.5×SD (HRV literature), RHR floor 2 bpm, EF 3% (measurement noise), VO2max 1.0 unit. Garmin-smoothed series (VO2max) exempt from p-language — direction+magnitude wording only. Output is always a sentence, never a p-value alone | any daily series | A | suppressed below 21 data points |
| 8 | **HRV band + streaks** | Day 1: consume Garmin's shipped baseline (`baseline.balancedLow/balancedUpper` — it IS the SWC method). Self-computed 60d lnRMSSD mean ± 0.5·SD takes over once ≥60 nights accumulate (cross-checked against Garmin's; disagreement disclosed). **Decision basis: the 7-day rolling lnRMSSD vs. the band drives the readiness-fusion input and the band flag** (Garmin's `weeklyAvg` as cross-check); single nights feed only display, streak counters, and the acute override (#19). Added value: below-band streak counter, 7d/30d CV, rising-CV-under-stable-mean early warning. Glossary caveat: abnormally *high* rMSSD vs. baseline is not automatically good — rare parasympathetic-overtraining signal, flagged to convergence (#20) as corroboration | `avgOvernightHrv` (rides in the sleep payload — no per-day HRV backfill calls), daily HRV endpoint for today | A | band available day 1 (Garmin's) |
| 9 | **RHR deviation (two-sided)** | 30d median ± MAD; **deviation in either direction is the flag**: caution ±3–5 bpm, red +5 sustained ≥2d; a −5 sustained *drop* alongside other deviant markers is flagged as the parasympathetic-OTS pattern (never alarmed alone). History via direct `connectapi('/userstats-service/wellness/daily/{displayName}', fromDate/untilDate, metricId=60)` (probe at implementation; fallback: build forward from `restingHeartRate` in daily summary/sleep payloads with disclosed window) | userstats range or daily digests | A | day 0 (range) or forward-built (labeled) |
| 10 | **Sleep debt / SRI / social jetlag** | debt = Σ max(0, `sleepNeed`−actual) over 14d (need fallback 8h, flagged "default"). **SRI computed properly**: the compact per-night sleep/wake interval timeline (onset, offset, awake windows from `sleepLevels` — ~1 KB/night) is digested into the cache before heavy series are discarded; SRI = same-state probability at 24h lag over 7d, 0–100. Social jetlag = weekend−weekday midpoint shift | `dailySleepDTO` + compact levels timeline | A | provisional until 14 nights; SRI needs 7 |
| 11 | **Intensity distribution** | The athlete's **configured Garmin zone boundaries** are fetched once (user settings); the 5 pre-bucketed `hrTimeInZone_1..5` values are mapped to the 3-zone model by whole-bucket containment, pro-rating any boundary-straddling bucket, **with the approximation disclosed** ("mapped from your configured zones"). Anchors: Garmin LT → `lt1_hr_override` → LT1 ≈ HRrest + 0.75×HRR (labeled LOW confidence). Target: `tid_target` profile setting, default **auto** = classify the athlete's own 12-wk distribution to nearest template (POL/PYR) and flag only drift from their own norm + grey-zone creep (mid-share rising ≥3 wks) — never a one-size 80/5/15 scold. Exact stream-based TID replaces the approximation for digested activities | list payload + zone config | A | day 0 (approximate), improving |
| 12 | **EF & decoupling (splits-based backbone)** | Steady-state qualifier: ≥40 min moving, ≥80% of laps with avgHR ≤ Z2 ceiling, lap-GAP CV <8%, first 10 min excluded. EF = avgGradeAdjustedSpeed (m/min) / avgHR per qualifying session (cycling: lap NP/avgHR where power present). Decoupling = (EF_half1 − EF_half2)/EF_half1 from lap halves. **Heat guard:** sessions with avg temp ≥24°C (per-activity weather) are excluded from the EF benchmark *trend* series and annotated in drill-downs ("EF suppressed — 28°C"); exclusions disclosed when they thin the trend; heat-acclimation transitions (from max-metrics/training status) noted alongside. Method labeled "splits-based"; stream-exact versions (30s rolling NGP/NP) computed for activities that get a `/details` digest and override the approximation | activity splits (~3 KB, 1 call per qualifying activity) + weather | B | history backfilled for last 8–12 wk of qualifying sessions (Tier 2) — EF trends exist within days, not months |
| 13 | **Durability** | runs ≥90 min: EF final third ÷ first third (per-km laps make thirds trivial); intra-individual trend only (no population norms). LOW-confidence label until ≥5 long sessions | splits (B); stream-exact for drill-downs (C) | B | backfilled with #12 |
| 14 | **Interval digest** | structure from typed splits (`INTERVAL_ACTIVE`); fallback ladder: manual-lap heuristic (repeating work/recover lap pattern) → summary+splits verdict, disclosed. Per-rep pace/HR, fade % (first→last), recovery HR floor where lap minima exist, cadence/stride stability; target compliance when a planned workout matches (#15) | typed splits (B) | B | on demand + new activities |
| 15 | **Planned-vs-executed matching** (Phase 1) | Garmin's workout–activity link where present; fallback: same date + same sport + duration within ±25%; no match → compliance sections state "planned workout not matched to an executed session" (and vice versa: an unexecuted planned workout renders as missed in `garmin_week`). Planned sources: Garmin calendar, enrolled Garmin Coach plan workouts, Phase-3 pushed workouts. Consumed by `garmin_activity`, `garmin_week`, and (Phase 3) the debrief scoring | calendar + activities | A | day 0 (Tier-0 calendar fetch) |
| 16 | **Race triangulation** | Garmin prediction (as-is) + Tanda `Pm = 17.1 + 140·e^(−0.0053K) + 0.55P` (K, P from `get_progress_summary_between_dates` — one call) + Riegel `T2 = T1(D2/D1)^b`, b fitted to the athlete's PR set (bounds 1.03–1.12, default 1.06, fit quality disclosed). Spread = confidence; disagreement explained, never averaged. Run-only in v1 (see `garmin_fitness` degradation) | progress summary, PRs, race predictions | A | day 0 |
| 17 | **Forward PMC projection & taper window** | project CTL/ATL/TSB forward from current values using scheduled calendar workouts where present, else the trailing 4-wk daily-load pattern (basis always disclosed); render race-day form % vs. the +5…+25% fresh band and CTL fade. Inside 3 weeks of a stored goal race, emit taper guidance (bring TSB into the fresh band while CTL fade <10%). Same machinery powers the Phase-3 dry-run simulation | PMC + calendar + profile | A | day 0 once a goal is on file |
| 18 | **Readiness fusion** | weighted z-fusion vs. personal baselines: HRV 7d-roll band position .30 · sleep (score+debt) .25 · form ratio .20 · RHR delta .15 · Body Battery at wake .10. **Weights renormalized over available markers; every verdict declares which markers were used.** Primary when the device lacks Training Readiness (verified real case); otherwise shown alongside Garmin's with discrepancy notes | daily digests + PMC + log | A | provisional-labeled until sleep baselines warm |
| 19 | **Subjective gate & acute override** | Same-day/last-24h `garmin_log`: `flag="illness"` → verdict capped at RED ("rest pending symptoms"); `flag="injury"` unresolved → capped at AMBER with modification; RPE/fatigue/soreness ≥ threshold → annotation. **Acute single-marker escalation** (no waiting for 3-day streaks): RHR ≥ +7 bpm, or single-night HRV z ≤ −2.5 / 90-day low, or sleep <4h → at least AMBER with possible-illness-onset language the same morning; two severe acute markers → RED | log + daily digests | A | day 0 |
| 20 | **Overtraining convergence** | RED alarm iff ≥2 of 3 primary marker groups persistently deviant ≥3d — autonomic (HRV 7d-roll below band, RHR deviating either direction, HRV-CV rising) · sleep (debt >5h/14d, deep-sleep streak, SRI <75) · load (monotony >2, strain >90th pctile, ramp >10%CTL/wk, form < −40%) — OR the acute override (#19). A fourth, **corroborating-only** group — HR response (hard-session maxHR declining vs 90d "can't get HR up", worsening `recoveryHeartRate` where present) — strengthens an alarm and feeds WATCH items but never counts as one of the two triggering groups. Single markers = "watch", never alarms | all above | A | day 0 |
| 21 | **Anomaly scan → alerts table** | every tracked scalar with \|z\|>2 vs 90d or ≥3-day out-of-band streak → materialized `alerts(date, metric, severity RED/AMBER/WATCH, message, resolved)` rows, ranked by the §2.4 `whats_changed` schema. RED/AMBER rows render as the global banner (§4.4) | baseline engine | A | day 0 |
| 22 | **Attribution rules (deterministic, closed set)** | the only cross-domain "because" statements verdicts may make, each a fixed rule over computed inputs, listed in the glossary: (a) deep-sleep decline + bedtime-variance high (SRI/jetlag) + load normal → "matches late bedtimes, not load"; (b) deep-sleep decline + load ramp/strain elevated + bedtime stable → "matches load, not schedule"; (c) lagged cross-correlation of daily load → next-day HRV over 90d (reported only when |r|>0.3, labeled correlation-not-causation); (d) sleep-debt ↔ next-day-HRV association, same guard; (e) hot-day EF suppression (#12). Anything outside this set is phrased as co-occurrence ("X while Y"), never causation — unspecified attribution language is a renderer contract violation | daily digests + load + sleep timeline | A | rules (a)/(b) day 0; (c)/(d) need 60d |
| 23 | **Garmin-computed depth metrics (capability-gated consume-and-trend)** | per principle 2: **running tolerance** (Garmin's impact-load-vs-capacity injury guard) — current vs. capacity + 4-wk trend, rendered in `garmin_load` and as a WATCH input to #21 when over capacity; **endurance score** — trend line in `garmin_fitness`. Both probed at first sync (§3.3); absent capability → absent line, recorded in the coverage block, never fabricated | tolerance/endurance endpoints | A | day 0 where device supports |
| 24 | **Enum phrase table** | static translation of Garmin feedback enums (`NEGATIVE_LONG_BUT_LIGHT` → "long but light — duration fine, depth poor"; `AEROBIC_HIGH_SHORTAGE` → "not enough high-aerobic work this month"; …). Unknown keys pass through humanized (`SNAKE_CASE` → sentence case) so new enums never break rendering | — | — | — |

#### Amendment (2026-07-22, Phase 2) — HR-at-pace bands are the primary efficiency measure

Metric #12's steady-state session qualifier (≥40 min, lap-pace CV <8%, ≥80% of laps under the Z2 ceiling) turns out to be too restrictive to carry a trend on a real high-variety athlete. Measured on the maintainer's account, 201 backfilled runs over six months yielded **21 qualifying steady sessions in total and only 20 inside the 180-day window** — below the 21-point floor metric #7 requires before it will claim anything. The measure was, in practice, permanently suppressed.

The same laps analysed by **pace band** — every lap whose grade-adjusted pace falls in a requested window, regardless of the session it belongs to — yielded **1,348 qualifying laps across 201 sessions**. Same evidence, two orders of magnitude more of it, because a band does not require the *session* to have been steady, only the *lap*.

So the engine computes both, and the ordering is:

1. **Primary: HR-at-pace over a band** (`analytics/efficiency.hr_at_pace`). Duration-weighted, grade-adjusted where the device provides GAP. Rejects laps whose HR does not belong to their pace — marked interval recoveries, laps following one >15% faster (HR lags effort by a minute or more), laps under 400 m — and reports every rejection count so responses disclose coverage rather than implying it.
2. **Secondary: steady-session EF** (unchanged §3.2 #12) where sessions qualify — a cleaner but rarer signal, and the one comparable to outside sources.

Both feed metric #7 through the same SWC machinery. The heat guard governs both: laps at or above 24 °C are flagged and excluded from trend series, never deleted. This matters more than it looks — on the sampled account **96% of July laps were run at ≥24 °C versus 11% in March**, and the measured heat penalty (≈1–2% EF) fully accounts for the apparent summer regression. Without the guard the engine would have reported a fitness loss during a genuine improvement, violating principle "never fabricate" in the most damaging direction.

Storage consequence: `activity_laps` (per-lap digest, ~1 KB/session on the wire) joins the table list below. Laps are already a digest — the per-second streams remain discarded.

### 3.3 Store, sync, rate limits, cold start

**Store:** SQLite at `~/.fartlek/<garmin-user-id>/store.db` — **keyed per Garmin account**, so multiple accounts on one machine never share a store or a baseline. Tables: `days` (~25 digested scalars/day, incl. weight), `activities` (summary digest + computed metrics + RPE with source), `sleep_timeline` (compact per-night intervals for SRI), `activity_laps` (per-lap digest: distance/time/HR/speed + grade-adjusted speed + temperature — the substrate for HR-at-pace), `activity_digests` (EF/decoupling/interval results — raw streams discarded after digest), `baselines`, `pmc`, `alerts`, `wellness_log`, `athlete_profile`, `plan_calendar` (scheduled/enrolled-plan workouts + match results), `capability_map`, `sync_state`.

**Lifecycle (table stakes for the local-first position):** `fartlek accounts` lists stored accounts; `fartlek switch <account>` selects; `fartlek export` dumps the store (SQLite copy + CSV per table); `fartlek reset [--account]` wipes tokens and data after confirmation. All documented in the README next to the privacy claim.

**Concurrency (multiple stdio clients are the norm):** WAL mode, `busy_timeout=5000`, and an advisory `sync.lock` file (stale after 10 min) so Claude Desktop + Claude Code instances never double-fetch or corrupt sync state. All sync operations idempotent.

**Timezone/day-boundary rules (fixed):** all daily bucketing uses Garmin's `calendarDate` (already athlete-local); sleep belongs to its wake-date per Garmin's convention; "today" = the server machine's local date unless a `date` param is given; every response header carries the data-as-of sync timestamp. Travel across timezones is a documented limitation, not silent corruption. Multi-device maps (`latestTrainingStatusData` keyed by deviceId) resolve via `get_primary_training_device`, else most-recently-synced; the choice is recorded in the capability map.

**Capability probes:** at first sync, each load-bearing unofficial field or endpoint (`hrTimeInZone_1..5`, `activityTrainingLoad`, typed splits, `avgGradeAdjustedSpeed`, `sleepNeed`, HRV baseline, Training Readiness, Endurance Score, running tolerance, `directWorkoutRpe`, enrolled training plans, user goals) is probed and recorded in `capability_map` with its concrete fallback engaged (per §3.2). `garmin_athlete` renders the result as the data-coverage block — never silent nulls.

**Sync policy:** Garmin is hit only by the sync process. Staleness check on **every tool invocation**: last sync >6h → background refresh thread, response served from cache with its data-as-of stamp; for `garmin_brief`, if today's wellness rows are absent and last sync >30 min, a bounded 3-call inline refresh (daily summary, sleep, HRV) runs first. Sequential fetching, ≤1 request/2s during backfill, exponential backoff on 429 (60s → ×2 → cap 15 min), cursor-resumable across sessions.

**Cold start (honest accounting):**
- **Tier 0 — first minute, ~17 calls:** profile + user settings (zone config), PRs, race predictions, training status, LT latest, today's daily summary/sleep/HRV, last activities page, scheduled workouts (this + next month), **enrolled training plans + user-set goals** (2 calls — a coach that ignores an existing Garmin Coach plan gives incoherent advice), devices. Every tool answers immediately, provisional-labeled.
- **Tier 1 — first hour, ~16–21 calls:** activities-by-date 180d (paginated, ~8–10 — this alone fully warms the PMC), RHR range (1), **weight/body-composition range (1)**, body-battery range chunked (2–3), weekly stress 52w (1), maxmet history (2–3), LT/FTP ranges (2–3), progress summary (1).
- **Tier 2 — background, resumable, ~120–220 calls over day 0–1:** sleep DTO + compact timeline backfill 60 nights (60 calls; bytes are cheap, only ~1–2 KB/night is kept — HRV rides in the same payload, so **HRV history depth = sleep backfill depth**, extensible via `garmin_sync(backfill_days=N)`); splits for qualifying steady/long/interval sessions of the last 8–12 weeks (~30–50); `/details` for runs ≥90 min in the last 8 weeks (~8–16). **On completion, precedent mining (§3.2 #5) runs retroactively over the full backfilled window.** Runs inside the `garmin_sync` request context when user-triggered (progress notifications require a progressToken — lifespan hooks cannot emit them), else as a throttled background thread with log notifications.
- **Steady state:** ~8–12 calls/day incremental + 1–2 splits digests per new qualifying activity.

---

## 4. LLM guidance

### 4.1 Server `instructions` (verbatim, ~150 words)

> "Garmin coaching server. Routing: questions about **today** — readiness, whether to train, current state — start with `garmin_brief` (zero arguments). 'Anything new / catch me up' → `garmin_whats_changed`. One week in detail → `garmin_week`. Multi-week load/dose → `garmin_load`. Fitness trends and races → `garmin_fitness`. Sleep/HRV/overtraining physiology → `garmin_recovery`. One session → `garmin_activity` (by id, date, or latest-of-sport). How a number was computed → `garmin_reference`. Log athlete-reported RPE, illness, injuries with `garmin_log`; goals and phases with `garmin_set_profile`. Never start with `garmin_raw`. All numbers are pre-computed against this athlete's personal baselines: do not re-derive statistics or aggregates — but athlete-reported state (illness, pain, exhaustion) always outranks a sensor-based GREEN; if the user reports feeling unwell, advise caution regardless of the verdict, and log it. Dates include weekdays; trust them."

The "do not recompute" rule is explicitly narrowed to arithmetic; judgment and athlete-report overrides belong to the model.

### 4.2 Trigger-phrase ownership map (single owner per phrase, encoded in descriptions)

| Phrase family | Owner | Boundary note in sibling descriptions |
|---|---|---|
| today / readiness / "should I train" | `garmin_brief` | all period tools say "not for single-day readiness" |
| "anything new / what changed" | `garmin_whats_changed` | brief says "for changes over a window, whats_changed" |
| "how was my week" | `garmin_week` | load says "session-level week → garmin_week" |
| "training load / too much / ramp / taper dose" | `garmin_load` | — |
| "am I getting fitter / race / goal / taper timing" | `garmin_fitness` | — |
| "overtraining / sick / tired / sleep / HRV" | `garmin_recovery` | load: "physiology side of overtraining → garmin_recovery" |
| "list / find sessions" | `garmin_activities` | — |
| "analyze my (last) run/ride/workout" | `garmin_activity` | 1 call via sport selector |
| "how is X computed / can I trust X" | `garmin_reference` | — |

### 4.3 In-response steering

Every response ends with a `Next:` breadcrumb (≤35 tokens) naming 1–3 concrete calls **with concrete arguments**, chosen by the engine from what actually tripped. Breadcrumbs never reference slash commands or resource URIs — model-facing channels contain only model-callable actions; user-only surfaces are referenced as "suggest the user run /…" inside prompt-recipe text only. Errors use the same channel: `"date must be YYYY-MM-DD (got 'yesterday'). Today is Sun 2026-07-20. Example: garmin_brief(date='2026-07-19')"` · `"No activity on Fri 07-18. Nearest: garmin_activity(activity_id=19492750) (Thu 07-17, run), garmin_activity(activity_id=19501244) (Sat 07-19, run)"` · `"Garmin session expired — the user must re-run `fartlek auth`. Retrying will not help."` Domain errors return `isError: true` tool results.

### 4.4 Red-flag surfacing (the safety invariant)

**Any active RED or AMBER alert renders as line 1 of EVERY tool response** — activity analyses, logbooks, trends, raw views included — in a fixed one-line format, undroppable by the budget renderer:

```
⚠ ACTIVE (since Thu 07-17): HRV below band 3 days + RHR +5 — see garmin_recovery()
```

WATCH-severity items appear only in `garmin_brief` and `garmin_whats_changed`. Alert states live in the materialized `alerts` table; resolution (metric back in band ≥2 days, or injury marked resolved via `garmin_log`) clears the banner. Because MCP is pull-only, the design acknowledges the limit honestly: alerts surface on the *first* tool call of any conversation, whatever it is — that is the strongest guarantee a server can make, and the banner invariant delivers it.

AMBER/RED verdicts always carry a concrete server-computed modification in the response itself (e.g., "replace today's quality with 40 min easy below HR 148; reassess tomorrow") — never a bare color.

### 4.5 CI guardrails for guidance quality

- **Breadcrumb validity test:** every breadcrumb and truncation notice the renderer can emit is parsed against the tool registry — phantom tools or parameters fail the build (the poisoned-breadcrumb bug class is structurally eliminated).
- **Budget test, real tokenizer:** dense numeric content — exactly this server's output — tokenizes at ~2.6 chars/token, ~20–30% worse than prose, confirmed on the golden corpus (`week` real 475 vs estimate 386; `activity.full` real 432 vs estimate 292). So `ceil(chars/3.2)` is *not* a conservative upper bound: it undercounts tables and over-counts prose, and no single linear char-divisor can bound a BPE tokenizer (adversarial punctuation is ~1 token/char). The gate therefore asserts the guarantee that matters rather than a formula that cannot hold: **every golden render's real-tokenizer count fits its tool's cap** (`tests/test_budget_gate.py` over `tests/golden_renders.py`). The runtime renderer keeps the cheap `ceil(chars/3.2)` estimate for drop-ordering — safe because every cap carries large real headroom (worst observed utilisation ~58%) — and a second, looser check keeps that estimate within a documented sanity band of the real count so a renderer change cannot make it wildly misleading.
- **Session-cost gate:** the sum of all hard caps, one call per tool at default arguments (basis defined in §5 rule 8), is asserted ≤17K.
- **Catalog test:** combined tool definitions ≤3.5K tokens, measured.
- **Description/signature consistency test:** each tool's description is checked against its declared parameters (names and formats mentioned in prose must exist in the schema).
- **Attribution-language test:** rendered "because"-statements must map to a rule in §3.2 #22; anything else fails the build.

### 4.6 MCP prompts & resources (progressive enhancement; everything mirrored by tools)

**Prompts** (slash commands in Claude Code, "+"-menu in Desktop, supported in Cursor) — each embeds the relevant pre-computed digest inline plus a short coaching-doctrine frame (Friel/Seiler-grounded review order), so one command = data + methodology:
`morning_briefing` · `weekly_review` · `post_activity_debrief(activity_id)` (completion: last 10 activities) · `race_readiness` · `plan_next_week` · `injury_risk_check` · `setup_athlete` (elicitation on Claude Code/Cursor; on Desktop degrades to a rendered questionnaire the model asks conversationally, answers persisted via `garmin_set_profile`).

**Resources:** `garmin://athlete/snapshot` (mirror of `garmin_athlete`, priority 1.0) · `garmin://reference/metrics-glossary` (every formula, threshold table with its provenance label — *population default* vs *personally derived* — the SWC exception table, the attribution rule set, and honesty notes: ACWR criticisms, Riegel caveats, high-rMSSD caveat, load-calibration method; costs zero runtime tokens until pulled) · Phase 3 adds `garmin://reference/workout-schema`. The `garmin_reference` tool mirrors these **in the same release** each ships, because Claude Desktop cannot pull resources model-side (§2.4).

**Degradation:** no correctness depends on prompts, resources, elicitation, or `instructions` injection. Tool descriptions + breadcrumbs + data-shaped sequencing carry the full load on a tools-only client.

---

## 5. Response format conventions (the house style)

Every synthesis tool response follows this exact structure, enforced by one shared renderer:

```
[⚠ ACTIVE banner — only if a RED/AMBER alert exists]        ← undroppable
# Title — Ddd YYYY-MM-DD (data as of HH:MM)                  ← undroppable
**VERDICT: …**                                               ← undroppable
[evidence table(s)]
[watch-list, ≤3 items]
[detail sections]
Next: tool(args) · tool(args)                                ← undroppable
```

**Rules:**
1. **Markdown only.** `structured_output=False` on every tool; the Markdown text is the sole, authoritative, budget-counted payload. No JSON, no `{value, unit}` objects, no nulls (absent → omitted, or `no data (device)` when absence is signal), no UUIDs except activity IDs (which are load-bearing arguments).
2. **Every headline metric:** `value (unit) — personal baseline (window, n= if <full) — Δ — categorical flag`, plus one server-computed verdict sentence per section. Numbers pre-formatted (`3:58/km`, `7h29`, `44 bpm`, `+1.9%/wk`).
3. **Dates:** always `Ddd YYYY-MM-DD` or `Ddd MM-DD` in tables.
4. **Series:** ≤12 points, inline arrow line (`310→342→296→405`) or a shape verdict ("rising 4 straight weeks"). Never raw per-second/per-epoch data.
5. **Verdict strength gated by confidence:** any verdict resting on provisional baselines must be phrased provisionally — `PROVISIONAL (n=12 of 42 days) — leaning GREEN` — and authoritative GREEN/RED phrasing is forbidden until the underlying baselines pass their minimum windows.
6. **Method notes:** contested or approximated figures carry a short parenthetical (`splits-based`, `population band`, `mapped from configured zones`, `hot-day sessions excluded`); full formulas live in the glossary (`garmin_reference`).
7. **Section-drop priority (per response, in drop order):** ① optional detail rows beyond the 6 most recent → ② secondary tables → ③ watch-list items beyond 3 → ④ method parentheticals. **Never dropped:** banner, title, verdict, alert lines, activity IDs already rendered, breadcrumb. Every drop is disclosed: `(5 more rows — garmin_activities(start_date="2026-07-07") for all)`.
8. **Token budgets (targets/hard caps; CI counts with a real tokenizer, runtime renderer estimates with `ceil(chars/3.2)` — see §4.5):**

| Tool | Target | Cap | | Tool | Target | Cap |
|---|---|---|---|---|---|---|
| brief | 400 | 600 | | activities | 600 | 1,300 |
| whats_changed | 500 | 700 | | activity (standard) | 800 | 1,000 |
| week | 900 | 1,200 | | activity (splits/full) | — | 2,000/4,000 |
| load | 800 | 1,100 | | athlete | 450 | 600 |
| fitness | 700 | 1,000 | | reference (per topic) | — | 2,000 |
| recovery | 800 | 1,100 | | set_profile / log / sync | — | 200/120/150 |
| | | | | raw | — | 5,000 |

**Session-cost guarantee, defined basis:** the regression gate sums **hard caps** (not targets), **one call per tool at default arguments** — `garmin_activity` at `detail="standard"`, `garmin_reference` and `garmin_raw` included. That sum is **~16.1K tokens** for the full v0.2 catalog, gated at ≤17K — under one-third of one raw sleep payload (52K). The absolute worst case (swapping in `detail="full"`) is ~19.1K, still far under the payload and documented, not gated. Excluding the escape hatch and glossary — the tools a normal coaching session actually uses — the synthesis surface sums to ~9.1K. All per-tool caps sit far below Claude Code's 25K truncation ceiling, leaving headroom for stacked servers.

---

## 6. Implementation roadmap

Each phase ships a working, useful server. Estimates assume one experienced developer.

**Phase 0 — Foundation (2 weeks).**
Auth via garth/garminconnect migration (see the distribution workstream below for the CLI contract), per-account SQLite store with WAL + sync lock + lifecycle commands, sync engine (staleness checks, 429 backoff, resumable cursor, capability probes incl. plans/goals/tolerance/native-RPE), Tier 0+1 cold start, daily-load ledger + calibration + terminal fallback, PMC/form-ratio/ACWR/monotony, baseline engine, alerts table, planned-vs-executed matcher (§3.2 #15), the shared renderer with budgets/drop-order/banner, CI guardrails (§4.5) including the real-tokenizer budget gate. *Nothing user-visible yet; this is half the project's real effort and is treated as such.*

**Phase 1 — Core read surface (3–4 weeks). Shippable v0.1.**
Tools: `garmin_brief`, `garmin_activities`, `garmin_activity` (standard + splits, interval fallback ladder, strength fallback, compliance via the Phase-0 matcher), `garmin_athlete`, `garmin_set_profile`, `garmin_log` (full Hooper set, RPE precedence), `garmin_sync`, `garmin_raw`. Readiness fusion with subjective gate + acute override; enum phrase table; corrective errors. README publishes the token-budget contract with its defined basis.

**Distribution & onboarding (parallel workstream, lands with v0.1).** The incumbent's 807 stars are distribution, not design — this workstream is not optional polish. Deliverables: `fartlek auth` CLI with the full garth MFA flow (TOTP + SMS paths, clear error taxonomy); token storage at `~/.fartlek/<account>/tokens/` with `0600` permissions; session-expiry behavior = the fixed re-auth error contract of §4.3 (never a silent retry loop); `fartlek doctor` (probe auth, connectivity, store health); install paths: `uvx`/`pipx` one-liner, Docker image, and a one-click **`.mcpb`/`.dxt` Desktop extension**; copy-paste client config snippets for Claude Code, Claude Desktop, and Cursor in the README; `accounts/switch/export/reset` lifecycle commands (§3.3).

**Phase 2 — Trend suite & engine completion (4–5 weeks). Shippable v0.2 — the flagship release.**
Tools: `garmin_whats_changed`, `garmin_week`, `garmin_load`, `garmin_fitness` (incl. projection + taper window), `garmin_recovery`, **`garmin_reference` (metrics_glossary topic — ships with the glossary itself so explainability reaches Desktop)**. Engine: Tier-2 backfill (sleep timeline/SRI, splits-based EF/decoupling/durability history, retroactive precedent mining), TID mapping + auto target, race triangulation, trend significance (Hamed–Rao + per-metric SWC), overtraining convergence with the two-sided RHR and HR-response corroboration group, attribution rules, capability-gated running-tolerance/endurance-score trends, forward PMC projection. MCP prompts + resources + glossary. Evaluation harness: ~30 multi-tool coaching tasks across Claude Code/Desktop/Cursor with token and calls-per-task regression gates and transcript audits (every LLM-re-derived number = a missing pre-computation). Engine validation against golden datasets (cross-check PMC/EF/decoupling outputs vs. intervals.icu exports for the same activities). Anomaly-scanner threshold tuning on the maintainer's own multi-month account (§7 open question 4) before release.

**Phase 3 — The closed loop (3 weeks). Shippable v0.3.**
`garmin_apply_plan` (dry-run + `plan_token` binding, guardrail simulation reusing §3.2 #17, sanity validation against zones/history), `garmin_reference` workout_schema topic + workout-schema resource, prescription-side compliance scoring in the debrief (the matcher itself shipped in Phase 0), `setup_athlete` elicitation flow. This is the most brittle, ToS-exposed surface — it ships last, alone, behind trust earned by the read side.

**Phase 4 — Depth extensions (ongoing).**
Cycling power depth (stream-NP EF, power TID, cycling race models), swim-specific modeling (CSS), menstrual-cycle-aware baselines (clinician-reviewed framing before shipping), body-composition trend verdicts (the raw weight scalar syncs from Phase 0), Body Battery drain/charge event attribution, hill score, lifestyle-logging HRV-confounder annotation, gear/shoe-mileage flags inside verdicts, hosted streamable-HTTP mode, MCP Apps dashboard (watch item pending client support).

Total to v0.3: ~3–3.5 months solo — consistent with the panel's feasibility assessments, with the distribution workstream absorbed in parallel during Phase 1.

---

## 7. Open questions & explicitly deferred

**Deferred by decision (with reasons):**
1. **Endpoint-parity passthrough** — the incumbent owns coverage; coverage is the wrong game. `garmin_raw` is the only raw surface and it is deliberately incapable of emitting streams above 200 downsampled points.
2. **MCP sampling / any server-side LLM** — unsupported by all target clients, deprecated in the spec RC, and antithetical to the trust position.
3. **structuredContent output schemas** — doubles context cost on clients that inject both; machine consumers can import the Python engine directly.
4. **Full auto-periodization engine** — Athletica proves opaque machine plans get rejected even when athletes PR. The server proposes; the plan stays a human–LLM conversation, with `garmin_apply_plan` executing one approved week at a time.
5. **Multi-source aggregation** (Strava/Oura/MFP) — Garmin-deep beats N-shallow; freddy.coach's game, not ours. Service-layer separation keeps the door open.
6. **Resource subscriptions, protocol tasks, SSE/WebSocket, roots, completions-as-dependency** — unsupported or churn-zone; harmless completions ship where cheap.
7. **Individually fitted Banister time constants, FOR/NFOR diagnosis, injury prediction** — no ground truth available; we ship population constants with disclosed limitations, multi-marker convergence rules, and refuse single-metric verdicts. Medical language is capability-bounded: convergence flags + "consider a professional", never diagnosis.
8. **Body Battery drain/charge events & hill score** — narrative garnish with low verdict value relative to catalog cost; Phase 4 candidates, not v1 surface.
9. **Lifestyle logging (alcohol/caffeine/late-meal tags)** — real HRV confounders, but the endpoint depends on manual athlete entry that is sparse on most accounts; Phase 4 probes it and, where tags exist, annotates single-night HRV anomalies ("tagged: alcohol") rather than adjusting baselines.

**Genuinely open (to resolve during implementation):**
1. Does the userstats RHR range endpoint accept arbitrary spans on all account types? (Fallback specified in §3.2 #9 either way.)
2. Body-battery range-call maximum window — probe and chunk accordingly.
3. Threshold-pace/race-prediction history endpoints' real availability per device — where absent, history builds forward from sync snapshots with the truncated window labeled (never fabricated 8-week deltas).
4. Alert false-positive rate of the anomaly scanner on real multi-month data — tune the |z|>2 and streak thresholds against the maintainer's own account before v0.2; over-firing recreates the Whoop trust failure from the opposite direction.
5. Whether `anthropic/requiresUserInteraction` and elicitation client-version gates hold as documented at Phase 3 time — re-verify against current client releases before building the write path's consent flow.
6. Real-world shape and coverage of `directWorkoutRpe`/`directWorkoutFeel` across device generations (the 0–100 → CR-10 conversion in §3.1 assumes the documented scale; verify against sampled accounts).
7. Enrolled-plan payloads: whether Garmin Coach adaptive-plan workouts appear on the standard calendar endpoint or require `get_training_plans` traversal — the Tier-0 probe answers this per account.

---

*Every example figure in this document traces to the live sampled account (HRV 97 / band 83–106, RHR 44, sleep score 66 FAIR with deep 11 min, wake Body Battery 99, ACWR 0.9 OPTIMAL, VO2max 61.0, marathon prediction 3:06:53, load balance 1807/662/277, RPE/feel fields) or is direct arithmetic on those figures (the CTL projection). No invented endpoints, no invented fields. That discipline — every number real, every formula printed, every threshold labeled — is the product.*