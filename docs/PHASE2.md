# Phase 2 tracker — v0.2, the flagship release

*Working checklist. Authority stays with [`DESIGN.md`](DESIGN.md) §6 and [`../ROADMAP.md`](../ROADMAP.md); this file exists so nothing in a 4–5 week phase is quietly dropped. Update it in the same commit as the work.*

Scope decisions taken 2026-07-22: engine before tools · live Garmin probing allowed · full spec quality programme (no reduced eval harness) · docs in English, server renders English and clients translate.

---

## 1. Engine

| Item | Spec | State |
|---|---|---|
| Trend significance (Hamed–Rao MK + Sen + per-metric SWC) | §3.2 #7 | ✅ `analytics/trends.py`, cross-checked vs pymannkendall |
| Per-lap splits backfill + storage | §3.2 #12 | ✅ `activity_laps`, `SyncEngine.backfill_splits` |
| EF / decoupling / durability + HR-at-pace bands | §3.2 #12, #13 | ✅ `analytics/efficiency.py` (+ amendment: pace bands primary) |
| Sleep debt / SRI / social jetlag | §3.2 #10 | ✅ `analytics/sleep.py` |
| Overtraining convergence audit (two-sided RHR, HR-response corroboration) | §3.2 #20 | ✅ `analytics/convergence.py` — end-to-end on real data: WATCH (sleep group only) |
| Forward PMC projection + taper window | §3.2 #17 | ✅ `analytics/projection.py` — weekday-shaped pattern, basis disclosed |
| Race triangulation (Garmin / Tanda / Riegel) | §3.2 #16 | 🟡 Riegel + exponent fit done in `analytics/race.py`; Tanda and the 3-model triangulation still to write |
| **Fixed-time race model** (24h events) | amendment | ✅ `race.fixed_time_projection` + `compare_to_field` — range, explicit stoppage, population exponent labelled |
| Intensity distribution (TID) mapping + auto target | §3.2 #11 | ✅ `analytics/tid.py` — pro-rated zone mapping, own-norm target, creep detection |
| Attribution rules (closed set) | §3.2 #22 | ✅ `analytics/attribution.py` — 5 rules, silent when evidence cannot discriminate |
| Retroactive precedent mining | §3.2 #5 | ✅ `analytics/precedent.py` — cross-source merge; external episodes excludable. Athlete's own levels: weekly load 974, strain 1817, monotony 1.87 |
| Capability-gated running-tolerance / endurance-score trends | §3.2 #23 | ⬜ minor — no capability probe exists yet, permanently omitted until one does |

## 2. Tools

| Tool | Cap | State |
|---|---|---|
| `garmin_recovery` | 1,100 | ✅ |
| `garmin_load` | 1,100 | ✅ |
| `garmin_fitness` (incl. projection + taper) | 1,000 | ✅ |
| `garmin_week` | 1,200 | ✅ |
| `garmin_whats_changed` | 700 | ✅ |
| `garmin_reference` (metrics glossary) | — | ✅ |

**Engine complete enough to start tools (2026-07-22).** Remaining engine items (Tanda triangulation, capability-gated trends) are minor and not blocking.

Each tool must clear the guardrail suite and be removed from `PHASE2_NAMES` in `tests/test_guardrails.py` as it lands — that set is the progress counter.

## 3. MCP prompts & resources — *progressive enhancement, v0.2.1 candidate*

⬜ Prompts: `morning_briefing`, `weekly_review`, `post_activity_debrief`, `race_readiness`, `plan_next_week`, `injury_risk_check`, `setup_athlete` (§4.6)
⬜ Resources: `garmin://athlete/snapshot`, `garmin://reference/metrics-glossary`

## 4. Quality programme (§4.5)

**Scope revised 2026-07-23 → v0.2 ships with automated CI gates + a reduced eval harness; the heavy manual programme (30 tasks × 3 clients, transcript audits) moves to v0.2.1.** Do the ⬜ CI gates below for v0.2; the eval-harness rows marked *(v0.2.1)* are deferred.


| Gate | State |
|---|---|
| Real-tokenizer budget gate (tiktoken) — Phase 0 debt | ⬜ |
| Estimator never undercounts the tokenizer on goldens | ⬜ |
| Breadcrumb validity extended to Phase-2 tools | ✅ registry test covers all 14 tools; catalog ≤3.5K enforced |
| Attribution-language test (every "because" maps to §3.2 #22) | ⬜ |
| Description/signature consistency | ⬜ |
| Session-cost gate ≤17K | ⬜ |
| Catalog ≤3.5K tokens | ✅ exists, must keep passing |
| Eval harness ~30 multi-tool tasks, Claude Code + Desktop + Cursor | ⬜ *(v0.2.1)* |
| Token + calls-per-task regression gates | ⬜ |
| Transcript audits (every LLM-re-derived number = missing pre-computation) | ⬜ *(v0.2.1)* |
| French-language eval tasks (server renders English, client translates) | ⬜ *(v0.2.1)* |
| Engine validation vs intervals.icu golden data | ✅ decoupling validated against raw streams on 8 long runs: median diff 1.0 pt, 7/8 within 3 pts. Their derived fields (decoupling/EF) come back empty on this account, so the streams were used directly — a stronger check. NOTE: their CTL is TSS-scaled (15.6) vs ours Garmin-load-scaled (104.8); only ratios are comparable |
| Anomaly-scanner threshold tuning on real multi-month data | ✅ 75 → 27 alerts, AMBER 27 → 4, anchored by the certified salmonella positive (2026-04-19..22) |

## 5. Open questions (§7)

| # | Question | State |
|---|---|---|
| 1 | userstats RHR range on all account types | ✅ resolved — 205 days in one call, and the service serves a metricId per daily scalar over an arbitrary window (see `USERSTATS_DAILY_METRICS`) |
| 2 | Body-battery max window / chunking | ⬜ 30-day chunks work (92 days stored); max window still unprobed — see D7 |
| 3 | threshold-pace / race-prediction history availability | ⬜ |
| 4 | Anomaly-scanner false-positive rate | ✅ resolved — replay + athlete triage, see the quality table |
| 6 | `directWorkoutRpe` / `directWorkoutFeel` real shape | ⬜ |
| 7 | Enrolled Garmin Coach plans: calendar vs `get_training_plans` | ⬜ |

## 6. Defects and debts found in flight

| # | Finding | State |
|---|---|---|
| D1 | Daily wellness scalars (`steps`, `avg_stress`, `min_hr`, calories, distance, floors, intensity minutes) held 1 day each — the daily summary is only fetched for today, and the spec provided no backfill for them | ✅ fixed — userstats range call per metric, 2 → 181 days for 9 extra calls |
| D6 | Rows written by a mid-day sync stayed frozen at their mid-day values forever (2026-07-20 held 6,847 steps vs an actual 18,664) | ✅ fixed by the same range backfill, which rewrites completed days |
| D8 | HR zone boundaries and body weight fetched by tier 0 but never persisted | ✅ fixed — tier 0 persists the RUNNING zone config + seeds weight from user-settings; the 3 TID tools pro-rate via shared `_zones.resolve()` |
| D7 | `body_battery_wake` still has 1 day: it is not in userstats and the dedicated body-battery endpoint only yields high/low. Readiness fusion weights it 0.10 | ⬜ |
| D2 | `ACTIVITY_HISTORY_DAYS = 180` is not parameterisable — a long-cycle athlete cannot see their full season | ⬜ |
| D3 | First `fartlek auth` persisted `di_refresh_token: null`, so the session died after ~20h and forced a full re-login. Re-auth stored one correctly; watch whether refresh rewrites the file | ⬜ monitor |
| D4 | Steady-session EF qualifier yields too few sessions to trend on a real athlete | ✅ amended — pace bands primary |
| D5 | `digest_laps` treated lap index 0 as missing (`or` on a falsy int) | ✅ fixed with the splits commit |

## 7. Release

⬜ Bump to 0.2.0, `uv sync` to keep `uv.lock` in step, tag → PyPI via OIDC
⬜ Update `server.json` (both version fields) and publish to the MCP registry — **needs a human** for `mcp-publisher login github`
⬜ Third-party directories (Glama, mcp.so, PulseMCP) — the maintainer handles submission
