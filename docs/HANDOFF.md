# Handoff — Fartlek project state

*Last updated: 2026-07-23. Phase 2 engine, 6 tools, **5 CI gates**, and **reduced eval harness** done; version bumped to **0.2.0** locally. Only the manual release steps remain (tag/push PyPI, `mcp-publisher`).*

This document is the **entry point** for an agent (or a human) picking up the project. It states **where the project stands**, **what has been verified**, **what remains**, and **the traps that cost time**. It does not duplicate the spec: the authority remains `docs/DESIGN.md` (the what/why), `ROADMAP.md` (the phase plan), and `docs/PHASE2.md` (the item-by-item Phase 2 checklist, kept up to date in the same commit as the work).

---

## 0. TL;DR — start here

1. **Read, in order:** this file → `docs/PHASE2.md` (exact checklist of what remains) → `docs/DESIGN.md` §3.2 (the metrics catalog, contract) → `CLAUDE.md` (project discipline).
2. **Run** `uv run pytest -q` (expected: **971 pass**) and `uv run ruff check fartlek/ tests/` (clean).
3. **The engine, the 14 tools, the 5 CI gates, and the reduced eval are finished and verified on a real Garmin account.** The version is already bumped to **0.2.0** (pyproject + server.json + uv.lock). All that remains is **tagging and publishing** (§6) — human steps. The heavy eval programme (30 tasks × 3 clients, transcript audits) is deferred to **v0.2.1**.
4. **A real test account is installed** in `~/.fartlek/` (see §4). Don't break it; never commit `~/.fartlek/` or `.env`.

---

## 1. The project in three sentences

Fartlek is an MCP server that turns Garmin data into a compact coach's report. The central bet: **the synthesis happens server-side, in deterministic Python** — no server-side LLM, no raw JSON passthrough. The server delivers verdicts pre-computed against the athlete's personal baselines.

Structuring corollary: **the LLM must never have to re-derive a statistic**. If a number is recomputed on the model side, that's a design bug. And its counterpart: **fabricate nothing** — a missing metric is reported as missing, never invented, never filled in with a default disguised as a measurement.

---

## 2. Current state — verified on 2026-07-23

| Item | Status |
|---|---|
| Phase 0 (foundation) | ✅ done |
| Phase 1 (core read surface, v0.1) | ✅ done, **0.1.1 on PyPI** |
| **Phase 2 — analytics engine** | ✅ **complete** (10 modules; 2 minor non-blocking items — see §5) |
| **Phase 2 — the 6 tools** | ✅ **delivered, wired, verified over real MCP** |
| Alert detector | ✅ calibrated on 6 months of real data (75 → 27 alerts) |
| External validation (intervals.icu) | ✅ cross-checked decoupling, median gap 1 pt |
| Tests | ✅ **971 pass** (`uv run pytest -q`, ~4 s) |
| Lint | ✅ `uv run ruff check fartlek/ tests/` |
| Live PyPI version | **0.1.1** — local bumped to **0.2.0**, tag/push still to do (§6) |
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
      ↓  adapters/garmin_connect.py   (garminconnect lib; sync calls in asyncio.to_thread + fcntl lock)
      ↓  health/service.py            (field filtering; consumers ALWAYS go through here)
      ↓  sync/engine.py               (staleness, 429 backoff, resumable cursor, capability probes,
      ↓                                tier0/1/2, backfill_splits, userstats range, zones+weight)
      ↓  store/store.py               (per-account SQLite, WAL)
      ↓  analytics/*.py               (the deterministic engine — see table below)
      ↓  mcp_server/tools/_zones.py   (shared HR zone resolution for the 3 TID tools)
      ↓  render/renderer.py           (verdict grammar, token budgets, drop order, safety banner)
      ↓  mcp_server/tools/*.py        (14 tools)
      ↓  mcp_server/server.py         (FastMCP stdio)
```

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

Useful entry points: `mcp_server/context.py` (`ToolContext`, `ensure_ready()` cold start) · `render/renderer.py` (all formatting + budgets) · `mcp_server/tools/_zones.py` (the only place that resolves persisted HR zones → TID arguments).

---

## 4. Commands & test data

```bash
uv sync                          # install (dev group included)
uv run pytest -q                 # 971 tests, ~4 s
uv run ruff check fartlek/ tests/
uv run fartlek auth --replace    # Garmin login (email/password + MFA) — REQUIRES A REAL TERMINAL
uv run fartlek doctor            # health check
uv run fartlek sync              # tier0+tier1 (add --nights N for sleep/HRV backfill)
uv run fartlek-mcp               # MCP server over stdio
```

**A real test account is installed.** `~/.fartlek/` contains the Garmin tokens and the store for account `b2db9a6f-...`: **~205 days (Dec. 2025 → Jul. 2026), ~295 activities, 2,188 laps, 142 nights of sleep timeline**, persisted HR zones and weight. This is the raw material for all live verifications.
- **NEVER commit** `~/.fartlek/` or the root `.env` file (already git-ignored).
- The tokens have a `di_token` valid ~28 h and a `di_refresh_token` (see trap D3). If `fartlek doctor` says "session expired", rerun `uv run fartlek auth --replace` in a real terminal.

**intervals.icu key** (external validation): in `.env` under `INTERVALS_ICU_API_KEY`, athlete account `i649595`. ⚠️ **The API returns 403 with urllib's default User-Agent** — it needs a browser UA (the `scratchpad/icu.py` script from a previous session shows this). Their CTL is on a TSS scale (≠ our Garmin-load scale): **only the ratios and the per-session decoupling/EF are comparable.**

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

Two items, both **minor and non-blocking** for v0.2:

- ~~**Tanda + 3-model triangulation**~~ (§3.2 #16): **done 2026-07-23.** `race.tanda_marathon` implements `Pm = 17.1 + 140·e^(−0.0053K) + 0.55P` (marathon-only); PRs and Garmin race predictions are persisted at sync; `garmin_fitness._distance_section` shows the Garmin / Tanda / Riegel models together with the spread as confidence, disagreement explained (never averaged), and the Tanda sensitivity levers. Verified hermetically on a marathon goal; not yet exercised on a live *distance*-goal account (the maintainer's race is fixed-time).
- **Capability-gated trends** (running tolerance / endurance score, §3.2 #23): no capability probe exists yet for these fields; cleanly omitted until a probe is added.

**PRs are now persisted (done 2026-07-23).** Tier 0 digests Garmin's personal-record payload (`digest_personal_records`, typeId 3/4/5/6 → 5k/10k/half/marathon, seconds) into `sync_state["personal_records"]` (same sync-derived boundary as HR zones, D8 — *not* `athlete_profile`, which stays user-typed). `garmin_fitness._personal_records(store)` reads them (with any typed `pr_*` profile key as a fallback), so the Riegel distance branch is no longer dormant. This unblocks the remaining Tanda triangulation below.

---

## 6. What remains — quality & release (the path to v0.2)

**Scope decision (2026-07-23):** v0.2 ships with the **automated CI gates** + a **reduced eval harness**; the heavy programme (30 tasks × 3 clients, transcript audits, FR tasks) is deferred to **v0.2.1**. Detail in `docs/PHASE2.md` §4.

**✅ CI gates delivered** (5 checks; detail + locations in `docs/PHASE2.md` §4):
1. **Real tokenizer gate (tiktoken)** — `test_budget_gate.py` on `golden_renders.py`. Reframed after measurement: `ceil(chars/3.2)` **is not** an upper bound (it undercounts dense tables by 20–30%); no linear model can bound a BPE tokenizer. This one item carries two of the five PHASE2 rows: the gate asserts the real guarantee — **the actual tokenizer count of each golden stays under its cap** — plus a looser **estimator sanity band** (the reframed "never undercounts" row), not an impossible formula. DESIGN §4.5 + renderer docstring corrected (owner decision 2026-07-23).
2. **Attribution language** — `test_attribution_language.py`. Attribution isn't wired into a synthesis tool yet, so the render scan guards the surface preemptively.
3. **Description/signature consistency** — `test_guardrails.py::test_description_call_args_are_registered_params`.
4. **Session cost ≤17K** — `test_guardrails.py::test_session_cost_under_17k` (= 16,070).

**✅ Reduced eval harness done** (`docs/EVAL.md`): 10 tasks defined, A–F run live on the real account on 2026-07-23 (one of them in French, numbers preserved). It revealed 3 flagship consistency defects — **E1** (⚠ high HRV = false positive), **E2-B** (sleep debt `week` vs `recovery`), **E2-A** (need `athlete` mislabeled) — **all fixed** with regression tests (see PHASE2 §6). E4 (ACWR) is by-design; the HRV band transparency harmonization (E1) is deferred to v0.2.1.

**v0.2 release — version ALREADY bumped to 0.2.0** (pyproject + the 2 `server.json` fields + `uv.lock`, committed). What remains, by hand (procedure verified in Phase 1, cf. §8):
1. `git tag v0.2.0 && git push origin main v0.2.0` → the OIDC workflow publishes to PyPI.
2. `mcp-publisher login github` (**device-code OAuth — requires a human**) then `mcp-publisher publish` for the MCP registry.
3. Third-party directories (Glama, mcp.so, PulseMCP) — the maintainer submits.

---

## 7. Defects & debt — tracked in PHASE2.md §6

**Fixed along the way** (each could have produced false advice without raising an error):
- **D1/D6**: 7 daily scalars had only 1 day of history (the daily summary is only fetched for today) → backfill via `userstats-service` (1 range call per metric, see `USERSTATS_DAILY_METRICS`). Also fixes rows frozen by a mid-day sync.
- **D8**: HR zones and weight fetched by tier0 but never persisted → now stored; the 3 TID tools pro-rate via `_zones.resolve()`.
- **D4**: the spec's "steady session" qualifier only captured 21 sessions out of 201 → **amendment §3.2 #12**: pace bands become the primary measurement.
- **D5**: `digest_laps` treated lap index 0 as absent (`or` on a falsy integer).
- Two bugs in `race.py` found while building `garmin_fitness`: `fit_riegel_exponent` missing `raw_b` on degenerate returns; `fixed_time_projection` treating `stoppage=None` as 0% **and reporting it as measured**.

**Open** (non-blocking):
- **D7**: `body_battery_wake` has only 1 day of history (absent from userstats, the dedicated endpoint only returns high/low). Weighs 0.10 in the readiness fusion.
- ~~**D2**~~: **fixed** — `activity_history_days()` reads `FARTLEK_ACTIVITY_HISTORY_DAYS` (clamped 30–730, bad value falls back to the 180-day default), so a long-cycle athlete can pull a full season.
- **D3** (to watch): the first `fartlek auth` had persisted `di_refresh_token: null` → session dead at ~20 h. The re-login stored a correct one; verify that the refresh does rewrite the file over time.

**The alert scanner's calibration (§7.4) is done and worth understanding**: replay over 116 real days → 75 alerts (one every 1.5 days, unworkable). Three rules decided with the athlete: (a) only the *unfavorable* direction alerts (31% of alerts flagged an *improvement*); (b) the load baseline only uses training days; (c) sleep requires 2 consecutive short nights. Result: 75 → 27, AMBER 27 → 4. **Anchored by a certified positive**: the athlete had salmonella on 2026-04-19..22 (5 deviant markers) — `test_salmonella_episode_is_still_detected` forbids any future tightening that would mask that day.

---

## 8. Contracts not to break (design invariants)

1. **Formulas are contracts.** PMC constants, ACWR EWMA, MAD `1.4826`, Foster, Hamed-Rao, Phillips SRI: implemented as specified, tested against known values (`trends` is even cross-checked vs `pymannkendall`). Do not "improve" without updating the spec.
2. **stdout reserved for JSON-RPC.** All logging to stderr. A stray `print()` breaks the protocol.
3. **Hard token budgets**, enforced by the renderer with *announced* truncation. The **catalog** of 14 tools stays under 3,500 tokens (paid by every conversation of every client) — don't raise the ceiling, tighten the descriptions.
4. **The athlete outranks the sensors.** An illness/injury reported via `garmin_log` caps the verdict, never the reverse. Also applies to history (precedent mining).
5. **Fabricate nothing.** Missing metric = reported as missing (never a "null" line, never a default value disguised as a measurement — cf. the `stoppage` bug). Approximation allowed **if declared** (cf. the TID bucket-vs-pro-rated note).
6. **A single marker never alarms.** The overtraining audit requires ≥2 of 3 groups. Over-alerting destroys trust as much as under-alerting.
7. **Closed causality.** The only allowed "because" statements are the 5 rules in `attribution.py`; everything else is co-occurrence ("X while Y").
8. **Never commit secrets** (`~/.fartlek/`, `.env`, `garmin_tokens.json`).
9. **Commit discipline**: one coherent change = one commit, scoped imperative message, tests green beforehand. Update `docs/PHASE2.md` in the same commit as the work.

---

## 9. Athlete context (the real test account)

The athlete (the maintainer) is training for the **24 heures de Villenave d'Ornon, 2026-08-29, firm goal 200 km** — a **fixed-time** event (hence the dedicated model: Riegel/Tanda don't apply here). Facts verified in project memory (`fartlek-goal-race-24h-villenave`, `matis-personal-load-thresholds`):
- **Personal overload thresholds**, derived from 3 real episodes he categorized: weekly load **974**, strain 1817, monotony 1.87. **His predictor is weekly volume, not monotony** (his worst episode had the lowest monotony and the highest load).
- 4 episodes recorded in his `wellness_log`; the salmonella episode is marked `EXTERNAL` there to be excluded from load levels.
- Current `garmin_fitness` projection: **187–204 km** (low confidence, declared), 200 km within range. The stoppage budget is the most sensitive lever.

These facts are test context, not code invariants — but they explain many choices (the fixed-time model, the precedent comparison on weekly load, the exclusion of external episodes).

---

## 10. References

- `docs/DESIGN.md` — authoritative spec (§2 tool surface, §3 engine, §3.2 catalog + **pace-band amendment**, §4 guidance, §5 format, §6 roadmap, §7 open questions).
- `docs/PHASE2.md` — **the item-by-item checklist of what remains** (read right after this file).
- `ROADMAP.md` — phase plan.
- `CLAUDE.md` — project discipline (architecture, commands, commits).
- Project memory (automatic recall): `garmin-coach-open-source-direction`, `fartlek-goal-race-24h-villenave`, `matis-personal-load-thresholds`.
- Repo: https://github.com/matisdsp/fartlek · PyPI: https://pypi.org/project/fartlek-mcp/
- `garth` is **deprecated** (Garmin broke its login in 2026) — everything goes through `garminconnect`, whose source in `.venv/.../garminconnect/` is authoritative for endpoints.
