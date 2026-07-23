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
| Race triangulation (Garmin / Tanda / Riegel) | §3.2 #16 | ✅ **complete** — `race.tanda_marathon` (hand-tested), PRs + Garmin predictions persisted at sync, `fitness._distance_section` shows all three models with the spread as confidence, disagreement explained, never averaged; Tanda levers rendered |
| **Fixed-time race model** (24h events) | amendment | ✅ `race.fixed_time_projection` + `compare_to_field` — range, explicit stoppage, population exponent labelled |
| Intensity distribution (TID) mapping + auto target | §3.2 #11 | ✅ `analytics/tid.py` — pro-rated zone mapping, own-norm target, creep detection |
| Attribution rules (closed set) | §3.2 #22 | ✅ `analytics/attribution.py` — 5 rules, silent when evidence cannot discriminate |
| Retroactive precedent mining | §3.2 #5 | ✅ `analytics/precedent.py` — cross-source merge; external episodes excludable. Athlete's own levels: weekly load 974, strain 1817, monotony 1.87 |
| Capability-gated running-tolerance / endurance-score trends | §3.2 #23 | ✅ **done** — Tier-1 probes both (custom availability check for endurance's 200+all-null shell), persisted to `days` columns via an `ALTER TABLE` migration, endurance trend in `garmin_fitness`, tolerance line + over-capacity WATCH alert (via `alerts.tolerance_alert`) in the load/brief/week path. **Capability-absent on the maintainer's device (FR735XT/255) → fixture-tested only; running-tolerance digest shape is UNVERIFIED (defensive: unknown shape → absent line, never faked)** |

## 2. Tools

| Tool | Cap | State |
|---|---|---|
| `garmin_recovery` | 1,100 | ✅ |
| `garmin_load` | 1,100 | ✅ |
| `garmin_fitness` (incl. projection + taper) | 1,000 | ✅ |
| `garmin_week` | 1,200 | ✅ |
| `garmin_whats_changed` | 700 | ✅ |
| `garmin_reference` (metrics glossary) | — | ✅ |

**Engine complete (2026-07-23).** Race triangulation and the capability-gated running-tolerance / endurance-score trends both landed 2026-07-23 — every §3.2 engine item is now done. The running-tolerance digest is the one piece shipped on an unverified response shape (no supporting device to test against), written defensively so an unknown shape omits the line rather than faking it.

Each tool must clear the guardrail suite and be removed from `PHASE2_NAMES` in `tests/test_guardrails.py` as it lands — that set is the progress counter.

## 3. MCP prompts & resources — *progressive enhancement (landed 2026-07-23)*

✅ Prompts (7, `fartlek/mcp_server/prompts.py`, registered in `server.py`): `morning_briefing`, `weekly_review`, `post_activity_debrief(activity_id)`, `race_readiness`, `plan_next_week`, `injury_risk_check`, `setup_athlete` — each a data+methodology conversation starter (directs the tool call, frames the Seiler/Friel review order, restates the honesty rules). Verified over JSON-RPC (`prompts/list` → 7).
✅ Resources (2): `garmin://athlete/snapshot` (mirrors `garmin_athlete`), `garmin://reference/metrics-glossary` (mirrors `garmin_reference`). `resources/list` → 2. Tests in `test_prompts.py` (registration, phantom-tool guard on prompt content, resource-mirror wiring).

## 4. Quality programme (§4.5)

**Scope revised 2026-07-23 → v0.2 ships with automated CI gates + a reduced eval harness; the heavy manual programme (30 tasks × 3 clients, transcript audits) moves to v0.2.1.** Do the ⬜ CI gates below for v0.2; the eval-harness rows marked *(v0.2.1)* are deferred.


| Gate | State |
|---|---|
| Real-tokenizer budget gate (tiktoken) — Phase 0 debt | ✅ `test_budget_gate.py` over `golden_renders.py`: 16 goldens, every real tiktoken count ≤ cap (worst util 58%) |
| ~~Estimator never undercounts the tokenizer on goldens~~ → **refuted & reframed** | ✅ the estimator *does* undercount dense tables 20–30% (no linear char-divisor can bound BPE); gate now asserts **real renders fit real caps** + an estimator sanity band. DESIGN §4.5 + renderer docstring corrected. Decision 2026-07-23 (owner) |
| Breadcrumb validity extended to Phase-2 tools | ✅ registry test covers all 14 tools; catalog ≤3.5K enforced |
| Attribution-language test (every "because" maps to §3.2 #22) | ✅ `test_attribution_language.py` — engine phrasings closed, glossary/engine consistency, no bare "because" in any render (attribution not yet wired into a synthesis tool, so this future-proofs the render surface) |
| Description/signature consistency | ✅ `test_guardrails.py::test_description_call_args_are_registered_params` — every `garmin_x(arg=…)` in a description names a real param of x |
| Session-cost gate ≤17K | ✅ `test_guardrails.py::test_session_cost_under_17k` — sum of hard caps at default args = 16,070 (basis §5 rule 8) |
| Catalog ≤3.5K tokens | ✅ exists, must keep passing |
| **Reduced eval harness (v0.2)** — ~10 tasks, Claude Code only | ✅ `docs/EVAL.md` — 10-task set defined, tasks A–F run live on the real account 2026-07-23; criteria 1–5 pass; surfaced 4 cross-tool coherence findings (E1–E4) |
| Eval harness ~30 multi-tool tasks, Claude Code + Desktop + Cursor | ⬜ *(v0.2.1)* |
| Token + calls-per-task regression gates | ⬜ *(v0.2.1)* |
| Transcript audits (every LLM-re-derived number = missing pre-computation) | ⬜ *(v0.2.1)* |
| French-language eval tasks (server renders English, client translates) | ✅ v0.2 reduced: task D (`garmin_load`) answered in French, all numbers preserved (`docs/EVAL.md`). Full FR set → *(v0.2.1)* |
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
| D2 | `ACTIVITY_HISTORY_DAYS = 180` is not parameterisable — a long-cycle athlete cannot see their full season | ✅ fixed — `activity_history_days()` reads `FARTLEK_ACTIVITY_HISTORY_DAYS` (clamped 30–730, bad value → default 180); `test_activity_history_days_override` |
| D3 | First `fartlek auth` persisted `di_refresh_token: null`, so the session died after ~20h and forced a full re-login. Re-auth stored one correctly; watch whether refresh rewrites the file | ⬜ monitor |
| D4 | Steady-session EF qualifier yields too few sessions to trend on a real athlete | ✅ amended — pace bands primary |
| D5 | `digest_laps` treated lap index 0 as missing (`or` on a falsy int) | ✅ fixed with the splits commit |
| E1 | `brief` flagged an above-band HRV roll as ⚠ — a favorable-direction false positive (fusion never credits high HRV §3.2 #8; the alert scanner already tuned this out). Investigation corrected the eval's guess: both tools use a **60d** band, not different windows; the split is brief two-sided vs `recovery` floor-only | ✅ fixed — `brief` renders above-band as ✓ (display-only). **Band transparency added**: `recovery` now shows a two-sided band + bounds (audit stays one-sided) and `week` prints its bounds. **Shared resolver DONE (2026-07-23):** `baselines.hrv_band`/`hrv_roll`/`hrv_position` — the canonical 60d **lnRMSSD mean ± 0.5·MAD-SD** (§3.2 #8) — now backs all four sites (brief, recovery, week, **fusion**), replacing the three divergent variants. Verified on the real account: band 85–95 ms, verdict unchanged (the log bounds land within ~1 ms of the old raw ones, so no reclassification). E1 fully closed. Tests: the four above + `test_hrv_band_is_60d_lnrmssd_mean_half_mad` et al. |
| E2-B | `week` 14d sleep-debt disagreed with `garmin_recovery` (20.9h vs 32.8h) run the same day — `_recovery` anchored the trailing 14d/7d windows at the future ISO-week Sunday, not today | ✅ fixed — trailing windows clamped to `min(end, today)`; `test_sleep_debt_anchors_at_today_not_the_future_week_end` |
| E2-A | `athlete` showed sleep need as the latest single night under a "Baselines (60d)" header (mislabel); the 8h00-vs-8.8h gap is a point value vs a 14-night mean of the one `sleep_need_h` column | ✅ fixed — `athlete` uses the 60d median; `test_sleep_need_is_a_60d_baseline_not_the_latest_night` |

## 7. Release

✅ Bumped to 0.2.0 — `pyproject.toml`, both `server.json` version fields, and `uv.lock` (`uv sync`), committed
⬜ `git tag v0.2.0 && git push origin main v0.2.0` → PyPI via OIDC — **human step** (irreversible publish)
⬜ Publish to the MCP registry — **needs a human** for `mcp-publisher login github` (device-code OAuth) then `mcp-publisher publish`
⬜ Third-party directories (Glama, mcp.so, PulseMCP) — the maintainer handles submission
