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
| Race triangulation (Garmin / Tanda / Riegel) | §3.2 #16 | ⬜ |
| **Fixed-time race model** (24h events — Riegel/Tanda structurally inapplicable) | amendment needed | ⬜ next |
| Intensity distribution (TID) mapping + auto target | §3.2 #11 | ⬜ |
| Attribution rules (closed set) | §3.2 #22 | ⬜ |
| Retroactive precedent mining | §3.2 #5 | ⬜ |
| Capability-gated running-tolerance / endurance-score trends | §3.2 #23 | ⬜ |

## 2. Tools

| Tool | Cap | State |
|---|---|---|
| `garmin_recovery` | 1,100 | ⬜ |
| `garmin_load` | 1,100 | ⬜ |
| `garmin_fitness` (incl. projection + taper) | 1,000 | ⬜ |
| `garmin_week` | 1,200 | ⬜ |
| `garmin_whats_changed` | 700 | ⬜ |
| `garmin_reference` (metrics glossary) | — | ⬜ |

Each tool must clear the guardrail suite and be removed from `PHASE2_NAMES` in `tests/test_guardrails.py` as it lands — that set is the progress counter.

## 3. MCP prompts & resources

⬜ Prompts: `morning_briefing`, `weekly_review`, `post_activity_debrief`, `race_readiness`, `plan_next_week`, `injury_risk_check`, `setup_athlete` (§4.6)
⬜ Resources: `garmin://athlete/snapshot`, `garmin://reference/metrics-glossary`

## 4. Quality programme (§4.5) — full spec scope

| Gate | State |
|---|---|
| Real-tokenizer budget gate (tiktoken) — Phase 0 debt | ⬜ |
| Estimator never undercounts the tokenizer on goldens | ⬜ |
| Breadcrumb validity extended to Phase-2 tools | ⬜ (registry test exists) |
| Attribution-language test (every "because" maps to §3.2 #22) | ⬜ |
| Description/signature consistency | ⬜ |
| Session-cost gate ≤17K | ⬜ |
| Catalog ≤3.5K tokens | ✅ exists, must keep passing |
| Eval harness ~30 multi-tool tasks, Claude Code + Desktop + Cursor | ⬜ |
| Token + calls-per-task regression gates | ⬜ |
| Transcript audits (every LLM-re-derived number = missing pre-computation) | ⬜ |
| French-language eval tasks (server renders English, client translates) | ⬜ |
| Engine validation vs intervals.icu golden data | ⬜ **blocked**: needs a user decision on creating the account |
| Anomaly-scanner threshold tuning on real multi-month data | ⬜ needs a triage pass with the athlete |

## 5. Open questions (§7)

| # | Question | State |
|---|---|---|
| 1 | userstats RHR range on all account types | ✅ resolved — 205 days in one call, and the service serves a metricId per daily scalar over an arbitrary window (see `USERSTATS_DAILY_METRICS`) |
| 2 | Body-battery max window / chunking | ⬜ 30-day chunks work (92 days stored); max window still unprobed — see D7 |
| 3 | threshold-pace / race-prediction history availability | ⬜ |
| 4 | Anomaly-scanner false-positive rate | ⬜ |
| 6 | `directWorkoutRpe` / `directWorkoutFeel` real shape | ⬜ |
| 7 | Enrolled Garmin Coach plans: calendar vs `get_training_plans` | ⬜ |

## 6. Defects and debts found in flight

| # | Finding | State |
|---|---|---|
| D1 | Daily wellness scalars (`steps`, `avg_stress`, `min_hr`, calories, distance, floors, intensity minutes) held 1 day each — the daily summary is only fetched for today, and the spec provided no backfill for them | ✅ fixed — userstats range call per metric, 2 → 181 days for 9 extra calls |
| D6 | Rows written by a mid-day sync stayed frozen at their mid-day values forever (2026-07-20 held 6,847 steps vs an actual 18,664) | ✅ fixed by the same range backfill, which rewrites completed days |
| D7 | `body_battery_wake` still has 1 day: it is not in userstats and the dedicated body-battery endpoint only yields high/low. Readiness fusion weights it 0.10 | ⬜ |
| D2 | `ACTIVITY_HISTORY_DAYS = 180` is not parameterisable — a long-cycle athlete cannot see their full season | ⬜ |
| D3 | First `fartlek auth` persisted `di_refresh_token: null`, so the session died after ~20h and forced a full re-login. Re-auth stored one correctly; watch whether refresh rewrites the file | ⬜ monitor |
| D4 | Steady-session EF qualifier yields too few sessions to trend on a real athlete | ✅ amended — pace bands primary |
| D5 | `digest_laps` treated lap index 0 as missing (`or` on a falsy int) | ✅ fixed with the splits commit |

## 7. Release

⬜ Bump to 0.2.0, `uv sync` to keep `uv.lock` in step, tag → PyPI via OIDC
⬜ Update `server.json` (both version fields) and publish to the MCP registry — **needs a human** for `mcp-publisher login github`
⬜ Third-party directories (Glama, mcp.so, PulseMCP) — the maintainer handles submission
