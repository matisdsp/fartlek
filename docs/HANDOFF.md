# Handoff — Fartlek project state

*Last updated: 2026-07-27. **v0.2.2 is live on PyPI and on the official MCP registry** (`isLatest`, both verified 2026-07-27). The engine is **fully complete** (race Garmin/Tanda/Riegel triangulation + capability-gated running-tolerance/endurance trends), 7 MCP prompts + 2 resources, E1/E2/D2/D3/D7 fixed — **no open defects**. 1010 tests green. main is at the `v0.2.2` tag: **Phase 2 is closed**, and the next work is Phase 3 (the write path) plus the deferred heavy eval programme.*

This document is the **entry point** for an agent (or a human) picking up the project. It states **where the project stands**, **what has been verified**, **what remains**, and **the traps that cost time**. It does not duplicate the spec: the authority remains `docs/DESIGN.md` (the what/why), `ROADMAP.md` (the phase plan), and `docs/PHASE2.md` (the item-by-item Phase 2 checklist, kept up to date in the same commit as the work).

---

## 0. TL;DR — start here

1. **Read, in order:** this file → `docs/PHASE2.md` (exact checklist of what remains) → `docs/DESIGN.md` §3.2 (the metrics catalog, contract) → `CLAUDE.md` (project discipline).
2. **Run** `uv run pytest -q` (expected: **1010 pass**) and `uv run ruff check fartlek/ tests/` (clean).
3. **The engine is fully complete** (every §3.2 item, incl. the race triangulation and capability-gated trends), plus 14 tools, 7 prompts + 2 resources, 5 CI gates, and the reduced eval — all verified on a real Garmin account. **v0.2.2 is live on PyPI and the MCP registry.** The heavy eval programme (30 tasks × 3 clients, transcript audits) is still deferred to a later v0.2.x.
4. **A real test account is installed** in `~/.fartlek/` (see §4). Don't break it; never commit `~/.fartlek/` or `.env`.

---

## 1. The project in three sentences

Fartlek is an MCP server that turns Garmin data into a compact coach's report. The central bet: **the synthesis happens server-side, in deterministic Python** — no server-side LLM, no raw JSON passthrough. The server delivers verdicts pre-computed against the athlete's personal baselines.

Structuring corollary: **the LLM must never have to re-derive a statistic**. If a number is recomputed on the model side, that's a design bug. And its counterpart: **fabricate nothing** — a missing metric is reported as missing, never invented, never filled in with a default disguised as a measurement.

---

## 2. Current state — verified on 2026-07-27

| Item | Status |
|---|---|
| Phase 0 (foundation) | ✅ done |
| Phase 1 (core read surface, v0.1) | ✅ done, **0.1.1 on PyPI** |
| **Phase 2 — analytics engine** | ✅ **fully complete** — every §3.2 item done, incl. race triangulation & capability-gated trends (2026-07-23) |
| **Phase 2 — the 6 tools** | ✅ **delivered, wired, verified over real MCP** |
| Alert detector | ✅ calibrated on 6 months of real data (75 → 27 alerts) |
| External validation (intervals.icu) | ✅ cross-checked decoupling, median gap 1 pt |
| Tests | ✅ **1010 pass** (`uv run pytest -q`, ~5 s) |
| Lint | ✅ `uv run ruff check fartlek/ tests/` |
| Live version | **0.2.2** on PyPI **and** the official MCP registry (`isLatest: true`, verified 2026-07-27) |
| Quality programme / CI gates | ✅ **5 gates delivered** (§6); reduced eval done (`docs/EVAL.md`) |
| MCP prompts & resources | ✅ 7 prompts + 2 resources (`prompts.py`, `server.py`); verified over JSON-RPC |

**14 tools exposed** in total: 8 from Phase 1 (`garmin_brief`, `garmin_activities`, `garmin_activity`, `garmin_athlete`, `garmin_set_profile`, `garmin_log`, `garmin_sync`, `garmin_raw`) + 6 from Phase 2 (`garmin_recovery`, `garmin_load`, `garmin_fitness`, `garmin_week`, `garmin_whats_changed`, `garmin_reference`).

Verifications actually performed (not assumed):
- All 14 tools appear in `tools/list` via the real MCP protocol (smoke check §4).
- `garmin_recovery`, `garmin_fitness`, `garmin_load`, `garmin_week` rendered on the real account and manually reviewed.
- The tool catalog fits under the 3,500-token ceiling (gate `test_catalog_under_budget`).

---

## 3. Architecture — the data path

```
Garmin Connect API
      ↓  adapters/garmin_connect.py   (garminconnect lib; connect_sync/fetch_sync, fcntl token lock)
      ↓  sync/engine.py               (staleness, 429 backoff, resumable cursor, capability probes,
      ↓                                tier0/1/2, daily_catchup, backfill_splits, backfill_gear,
      ↓                                userstats range, zones+weight, gear locker+attribution)
      ↓  store/store.py               (per-account SQLite, WAL)
      ↓  analytics/*.py               (the deterministic engine — see table below)
      ↓  mcp_server/tools/_zones.py   (shared HR zone resolution for the 3 TID tools)
      ↓  render/renderer.py           (verdict grammar, token budgets, drop order, safety banner)
      ↓  mcp_server/tools/*.py        (16 tools)
      ↓  mcp_server/server.py         (FastMCP stdio)
```

> **`health/service.py` and `health/ports.py` are dead** (verified 2026-08-06: no consumer anywhere in `fartlek/` or `tests/`). `HealthService`, the `GarminPort` protocol, and the adapter's whole `async get_*` surface are pre-store leftovers — the live path is `connect_sync`/`fetch_sync` → `SyncEngine`. An earlier version of this diagram claimed consumers *always* go through `HealthService`; they never do. Add new Garmin access to the sync engine, not to the port, or delete the three files.

### The `analytics/` engine — who computes what

| Module | Role | Spec |
|---|---|---|
| `pmc.py` | CTL/ATL/TSB, form bands, ACWR EWMA, monotony/strain; `advance()` shared with the projection | §3.2 #1-4 |
| `baselines.py` | rolling mean/median/MAD-SD, z, band position, streak; **RHR deviation two-sided** | §3.2 #6, #9 |
| `trends.py` | **significance**: Hamed-Rao MK + Sen + SWC per metric. Cross-checked vs `pymannkendall` | §3.2 #7 |
| `efficiency.py` | EF/decoupling/durability **per lap**; **HR-at-pace per band = primary measurement** (amendment) | §3.2 #12, #13 |
| `sleep.py` | sleep debt, SRI (Phillips), social jet lag | §3.2 #10 |
| `tid.py` | **pro-rated** 3-zone intensity distribution; classify (incl. `base`); grey-zone creep; `zone_mapping_kwargs` | §3.2 #11 |
| `convergence.py` | overtraining audit: ≥2 of 3 groups to alarm; corroborating hr_response | §3.2 #20 |
| `projection.py` | forward PMC (day-of-week pattern) + taper window | §3.2 #17 |
| `race.py` | Riegel + exponent fit + **fixed-time model** (range, stoppages, `compare_to_field`) | §3.2 #16 + amendment |
| `attribution.py` | the **only 5** allowed "because" statements; silent if the evidence doesn't settle it | §3.2 #22 |
| `precedent.py` | personal precedents; multi-source fusion; exclusion of external episodes | §3.2 #5 |
| `matcher.py` | planned-vs-executed (Phase 1) | §3.2 #15 |
| `alerts.py` | anomaly scan → `alerts` table; **calibrated on real data** (see §7) | §3.2 #21 |
| `fusion.py` | readiness fusion (core of `garmin_brief`) | §3.2 #18 |
| `load.py` | daily load curve + calibration + fallback | §3.1 |
| `gear.py` | shoe/bike wear vs the athlete's own limit (ok/watch/due/**unknown**) + 90d rotation share | §3.2 #25 |

Useful entry points: `mcp_server/context.py` (`ToolContext`, `ensure_ready()` cold start) · `render/renderer.py` (all formatting + budgets) · `mcp_server/tools/_zones.py` (the only place that resolves persisted HR zones → TID arguments).

---

## 4. Commands & test data

```bash
uv sync                          # install (dev group included)
uv run pytest -q                 # 1010 tests, ~5 s
uv run ruff check fartlek/ tests/
uv run fartlek auth --replace    # Garmin login (email/password + MFA) — REQUIRES A REAL TERMINAL
uv run fartlek doctor            # health check
uv run fartlek sync              # tier0+tier1 (add --nights N for sleep/HRV backfill)
uv run fartlek-mcp               # MCP server over stdio
```

**A real test account is installed.** `~/.fartlek/` contains the Garmin tokens and the store for account `b2db9a6f-...`: **~205 days (Dec. 2025 → Jul. 2026), ~295 activities, 2,188 laps, 142 nights of sleep timeline**, persisted HR zones and weight. This is the raw material for all live verifications.
- **NEVER commit** `~/.fartlek/` or the root `.env` file (already git-ignored).
- The tokens have a `di_token` valid ~28 h and a `di_refresh_token` (see trap D3). If `fartlek doctor` says "session expired", rerun `uv run fartlek auth --replace` in a real terminal.

**intervals.icu key** (external validation): in `.env` under `INTERVALS_ICU_API_KEY` (athlete id in `.env` too). ⚠️ **The API returns 403 with urllib's default User-Agent** — it needs a browser UA (the `scratchpad/icu.py` script from a previous session shows this). Their CTL is on a TSS scale (≠ our Garmin-load scale): **only the ratios and the per-session decoupling/EF are comparable.**

Standalone MCP smoke check (calling a tool):

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"garmin_recovery","arguments":{"days":28}}}' \
  | (cat; sleep 25) | uv run fartlek-mcp 2>/dev/null
```

**Live testing**: sequential, polite Garmin calls (≤1 req/2s during backfill, backoff on 429). Garmin is contacted **only by the sync process**.

---

## 5. What remains — engine

**Nothing.** Every §3.2 item is implemented. The two items that were open at v0.2.0 both landed on 2026-07-23; they are kept here as implementation notes, because each carries a verification gap worth knowing before touching that code:

- ~~**Tanda + 3-model triangulation**~~ (§3.2 #16): **done 2026-07-23.** `race.tanda_marathon` implements `Pm = 17.1 + 140·e^(−0.0053K) + 0.55P` (marathon-only); PRs and Garmin race predictions are persisted at sync; `garmin_fitness._distance_section` shows the Garmin / Tanda / Riegel models together with the spread as confidence, disagreement explained (never averaged), and the Tanda sensitivity levers. Verified hermetically on a marathon goal; not yet exercised on a live *distance*-goal account (the maintainer's race is fixed-time).
- ~~**Capability-gated trends**~~ (running tolerance / endurance score, §3.2 #23): **done 2026-07-23.** Tier 1 probes both (endurance needs a custom availability check — the endpoint returns 200 + an all-null shell on unsupported devices), persists them to new `days` columns (`endurance_score`, `running_tolerance_pct`) via an idempotent `ALTER TABLE` migration, trends endurance in `garmin_fitness` and impact-load-vs-capacity in `garmin_load`, with an over-capacity WATCH alert feeding brief/week. Capability-absent on the maintainer's device → fixture-tested only; the running-tolerance response shape is **unverified** and its digest is defensive (unknown shape → no line, never a fabricated number).

**PRs are now persisted (done 2026-07-23).** Tier 0 digests Garmin's personal-record payload (`digest_personal_records`, typeId 3/4/5/6 → 5k/10k/half/marathon, seconds) into `sync_state["personal_records"]` (same sync-derived boundary as HR zones, D8 — *not* `athlete_profile`, which stays user-typed). `garmin_fitness._personal_records(store)` reads them (with any typed `pr_*` profile key as a fallback), so the Riegel distance branch is no longer dormant. This unblocks the remaining Tanda triangulation below.

---

## 6. What remains — quality, release, and the road to v0.3

**Scope decision (2026-07-23):** v0.2 ships with the **automated CI gates** + a **reduced eval harness**; the heavy programme (30 tasks × 3 clients, transcript audits, FR tasks) is deferred to **a later v0.2.x** — it did not land in 0.2.1 or 0.2.2 and is still open. Detail in `docs/PHASE2.md` §4.

**✅ CI gates delivered** (5 checks; detail + locations in `docs/PHASE2.md` §4):
1. **Real tokenizer gate (tiktoken)** — `test_budget_gate.py` on `golden_renders.py`. Reframed after measurement: `ceil(chars/3.2)` **is not** an upper bound (it undercounts dense tables by 20–30%); no linear model can bound a BPE tokenizer. This one item carries two of the five PHASE2 rows: the gate asserts the real guarantee — **the actual tokenizer count of each golden stays under its cap** — plus a looser **estimator sanity band** (the reframed "never undercounts" row), not an impossible formula. DESIGN §4.5 + renderer docstring corrected (owner decision 2026-07-23).
2. **Attribution language** — `test_attribution_language.py`. Attribution isn't wired into a synthesis tool yet, so the render scan guards the surface preemptively.
3. **Description/signature consistency** — `test_guardrails.py::test_description_call_args_are_registered_params`.
4. **Session cost ≤17K** — `test_guardrails.py::test_session_cost_under_17k` (= 16,070).

**✅ Reduced eval harness done** (`docs/EVAL.md`): 10 tasks defined, A–F run live on the real account on 2026-07-23 (one of them in French, numbers preserved). It revealed 3 flagship consistency defects — **E1** (⚠ high HRV = false positive), **E2-B** (sleep debt `week` vs `recovery`), **E2-A** (need `athlete` mislabeled) — **all fixed** with regression tests (see PHASE2 §6). E4 (ACWR) is by-design; the HRV band transparency harmonization (E1) shipped in v0.2.2 (one canonical resolver across brief/recovery/week/fusion).

**v0.2 released.** v0.2.0, v0.2.1 and v0.2.2 are all on PyPI (OIDC tag-push workflow) and on the official MCP registry (`mcp-publisher`, **0.2.2 `isLatest`** — published 2026-07-24, re-verified 2026-07-27). Still open:
- **Third-party directories** (checked 2026-07-24: fartlek listed on none of the three). **Glama** and **mcp.so** require a manual submission (Glama: "Add MCP Server" button on their servers page, GitHub-repo based, automated checks; mcp.so: submit form / GitHub issue). **PulseMCP** auto-crawls the official MCP registry, so it may pick fartlek up on its own; a manual claim at `pulsemcp.com/submit` speeds it up. The maintainer handles all three (account-holder actions).

### Phase 3 is cancelled — Fartlek is read-only, permanently (2026-07-28)

**Do not build the write path.** `garmin_apply_plan` was fully specified and then dropped by owner decision; the spec and the full rationale are kept in DESIGN §2.4. The short version, because an agent picking this up will otherwise "helpfully" resurrect it:

- Garmin's Terms of Use prohibit *"any process, whether automated or manual, that accesses, copies, or scrapes content from the Site through any means not purposely made available"* — which already describes the **read** path this project ships. The question was never "may we add writes".
- Garmin **does** sanction third-party writes, via the Connect Developer Program's Training API ("publish workouts and training plans to the Garmin Connect calendar"). So there is no "no official route" defence.
- That route is cloud-to-cloud: it needs a hosted component and Garmin's approval, which would break the local-first promise the README makes. The sanctioned path costs the property the project exists to defend.

Reading your own data locally, at polite volume, without redistribution, is defensible. Writing into Garmin's systems through undocumented endpoints while a sanctioned write API exists is not. **The README now states this to users before they connect an account** (§ "How Fartlek reaches your data") — it was undisclosed from v0.1 to v0.2.2, which was the real gap.

Two open questions died with it: DESIGN §7 Q5 (elicitation/`requiresUserInteraction` gates, which existed for the write consent flow) is moot, and PHASE2 §5 Q7 (Garmin Coach workouts on the calendar vs `get_training_plans`) now only affects read-side compliance display, not a prescription side.

### Next up — v0.3, a read-side release

0. **Gear shipped (2026-08-06)** — `garmin_gear`, the pair worn inlined in `garmin_activity`, a `gear` source in `garmin_raw`; `gear`/`activity_gear` tables and a tiered sync (locker in tier 0, odometers in tier 1, per-session attribution in tier 2 + the background `daily_catchup`). Promoted out of the Phase-4 depth list. Two traps it cost: the gear service keys on the numeric `userProfilePk`, not `displayName`; and the session-cost gate (§5 rule 8) is now at **16,970/17,000** — the tool was sized down to fit rather than the ceiling raised, so the next tool needs a real decision.
1. **Publish v0.2.3** — `garmin_setup`, the state-aware handshake, the day-1-vs-expired error split, the curated sign-in output, and the README disclosure. All on main, none of it on PyPI yet.
2. **The deferred quality programme** (PHASE2 §4): ~30-task eval across Claude Code/Desktop/Cursor, token and calls-per-task regression gates, transcript audits. The live runs keep proving its worth — the last one found four defects, two of which were server bugs and not model hallucinations.
3. **PHASE2 §5 Q6** — the real shape of `directWorkoutRpe`/`directWorkoutFeel`, still unanswered, still a live probe away.
4. **`setup_athlete` elicitation** — the one Phase-3 item that survives, now much lower stakes since no credentials or writes ride on it.

---

## 7. Defects & debt — tracked in PHASE2.md §6

**Fixed along the way** (each could have produced false advice without raising an error):
- **D1/D6**: 7 daily scalars had only 1 day of history (the daily summary is only fetched for today) → backfill via `userstats-service` (1 range call per metric, see `USERSTATS_DAILY_METRICS`). Also fixes rows frozen by a mid-day sync.
- **D9** (2026-08-18): three wellness families only accrued *forward* — HRV came from `/hrv-service/hrv/{today}` alone, the daily-summary-only columns (`max_hr`, `max_stress`, `spo2_avg`) were D6's residue and froze mid-day, and body battery walked a hard-coded 90d that ignored a widened history window. Fixed with `backfill_hrv()` / `backfill_daily_summary()` (capped, resumable, no cursor — the `backfill_splits` pattern) run as their own steps of `fartlek sync --nights N`, plus a tier-1 re-ask of **yesterday** that thaws the frozen row. **Trap**: that re-ask must stay *before* tier 1's range calls — userstats owns the 9 metrics it serves and has to land last.
- **D8**: HR zones and weight fetched by tier0 but never persisted → now stored; the 3 TID tools pro-rate via `_zones.resolve()`.
- **D4**: the spec's "steady session" qualifier only captured 21 sessions out of 201 → **amendment §3.2 #12**: pace bands become the primary measurement.
- **D5**: `digest_laps` treated lap index 0 as absent (`or` on a falsy integer).
- Two bugs in `race.py` found while building `garmin_fitness`: `fit_riegel_exponent` missing `raw_b` on degenerate returns; `fixed_time_projection` treating `stoppage=None` as 0% **and reporting it as measured**.

**Open** (non-blocking):
- ~~**D7**~~: **fixed 2026-07-24** — the wake value is now *derived*: the sparse `bodyBatteryValuesArray` sample nearest the day's stored `sleep_end_ts` (≤60 min gap, else missing; calibrated: median 5.6 min on 87 real days). Tier1 backfills it, never overwriting Garmin's own scalar. Real-account coverage went 2 → 86/94 days, so the fusion's 30d baseline (weight 0.10) actually forms. **Trap**: the array is *event-driven*, not periodic (~6 points/day) — don't assume a dense timeline. Note also that the CLI's `fartlek sync` re-runs tier1 backfills on every invocation (self-healing), while the MCP server only runs tier0+tier1 inline on a cold store.
- ~~**D2**~~: **fixed** — `activity_history_days()` reads `FARTLEK_ACTIVITY_HISTORY_DAYS` (clamped 30–730, bad value falls back to the 180-day default), so a long-cycle athlete can pull a full season.
- ~~**D3**~~: **verified 2026-07-24** — the token file holds a non-null `di_refresh_token` and its mtime (2026-07-23 22:29) postdates the re-login by ~32 h, so the refresh does rewrite the file. Closed.

**The alert scanner's calibration (§7.4) is done and worth understanding**: replay over 116 real days → 75 alerts (one every 1.5 days, unworkable). Three rules decided with the athlete: (a) only the *unfavorable* direction alerts (31% of alerts flagged an *improvement*); (b) the load baseline only uses training days; (c) sleep requires 2 consecutive short nights. Result: 75 → 27, AMBER 27 → 4. **Anchored by a certified positive**: the athlete had a documented multi-day illness episode (5 deviant markers) — `test_certified_illness_episode_is_still_detected` forbids any future tightening that would mask that day.

---

## 8. Contracts not to break (design invariants)

1. **Formulas are contracts.** PMC constants, ACWR EWMA, MAD `1.4826`, Foster, Hamed-Rao, Phillips SRI: implemented as specified, tested against known values (`trends` is even cross-checked vs `pymannkendall`). Do not "improve" without updating the spec.
2. **stdout reserved for JSON-RPC.** All logging to stderr. A stray `print()` breaks the protocol.
3. **Hard token budgets**, enforced by the renderer with *announced* truncation. The **catalog** (15 tools) is gated at 5,000 tokens — it is paid by every conversation of every client, so tighten descriptions first and treat the ceiling as a last resort. It was raised from 3,500 on 2026-07-27 when the 14-tool catalog hit 3,496 (four tokens of headroom) and `garmin_setup` could not fit; the alternative was amputating an existing tool's parameters to pay for a new one. Rationale in DESIGN §2.1. The **published** guarantee is the ≤17K session cost, not this number.
4. **The athlete outranks the sensors.** An illness/injury reported via `garmin_log` caps the verdict, never the reverse. Also applies to history (precedent mining).
5. **Fabricate nothing.** Missing metric = reported as missing (never a "null" line, never a default value disguised as a measurement — cf. the `stoppage` bug). Approximation allowed **if declared** (cf. the TID bucket-vs-pro-rated note).
6. **A single marker never alarms.** The overtraining audit requires ≥2 of 3 groups. Over-alerting destroys trust as much as under-alerting.
7. **Closed causality.** The only allowed "because" statements are the 5 rules in `attribution.py`; everything else is co-occurrence ("X while Y").
8. **Never commit secrets** (`~/.fartlek/`, `.env`, `garmin_tokens.json`).
9. **Commit discipline**: one coherent change = one commit, scoped imperative message, tests green beforehand. Update `docs/PHASE2.md` in the same commit as the work.

---

## 9. Athlete context (the real test account)

The athlete (the maintainer) is training for a **fixed-time 24-hour event** (hence the dedicated model: Riegel/Tanda don't apply here). The identifying specifics — race, date, goal distance, personal threshold values — live in the maintainer's private notes, outside this repo. What the code relies on:
- **Personal overload thresholds**, derived from 3 real episodes he categorized. **His predictor is weekly volume, not monotony** (his worst episode had the lowest monotony and the highest load).
- 4 episodes recorded in his `wellness_log`; the illness episode is marked `EXTERNAL` there to be excluded from load levels.
- Current `garmin_fitness` projection: a declared low-confidence range that brackets the goal. The stoppage budget is the most sensitive lever.

These facts are test context, not code invariants — but they explain many choices (the fixed-time model, the precedent comparison on weekly load, the exclusion of external episodes).

---

## 10. References

- `docs/DESIGN.md` — authoritative spec (§2 tool surface, §3 engine, §3.2 catalog + **pace-band amendment**, §4 guidance, §5 format, §6 roadmap, §7 open questions).
- `docs/PHASE2.md` — **the item-by-item checklist of what remains** (read right after this file).
- `ROADMAP.md` — phase plan.
- `CLAUDE.md` — project discipline (architecture, commands, commits).
- Project memory (automatic recall): `garmin-coach-open-source-direction`, plus private athlete-context notes kept outside the repo.
- Repo: https://github.com/matisdsp/fartlek · PyPI: https://pypi.org/project/fartlek-mcp/
- `garth` is **deprecated** (Garmin broke its login in 2026) — everything goes through `garminconnect`, whose source in `.venv/.../garminconnect/` is authoritative for endpoints.
