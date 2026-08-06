# Fartlek — Roadmap

> Condensed from [`docs/DESIGN.md`](docs/DESIGN.md) §6 (the authoritative spec — every item below is specified there).
> Fartlek: *a coach's morning report from your Garmin data, for any LLM via MCP.*

## Done — 2026-07-20

- [x] Migrate Garmin access from deprecated `garth` to `garminconnect` (multi-strategy login, MFA)
- [x] Integrated login CLI (`ai-coach-login` — becomes `fartlek auth` in Phase 0)
- [x] Adapter hardened after adversarial review (self-heal on re-login, cross-process token lock, connect-failure backoff)
- [x] Full design document (`docs/DESIGN.md`) — synthesis layer, 15-tool surface, metrics engine, token-budget contract
- [x] Name check + decision: **Fartlek** (PyPI `fartlek-mcp` free; "Garmin Coach" is Garmin's own product)

## Phase 0 — Foundation (~2 weeks) · nothing user-visible, half the real effort

- [x] Rename: package `ai-coach` → `fartlek`, CLI → `fartlek auth`, repo restructure around the MCP server (FastAPI dropped, recoverable from git history)
- [x] Per-account SQLite store (`~/.fartlek/<account>/store.db`, WAL, sync lock, lifecycle commands)
- [x] Sync engine: staleness checks, 429 backoff, resumable cursor, capability probes (plans, goals, running tolerance, native RPE)
- [x] Cold start Tier 0+1; daily-load ledger with calibration + terminal fallback
- [x] Core metrics: PMC (CTL/ATL/TSB), form ratio, ACWR, monotony/strain; baseline engine; alerts table
- [x] Planned-vs-executed workout matcher
- [x] Shared response renderer: verdict grammar, token budgets, drop order, safety banner
- [x] CI guardrails incl. real-tokenizer budget regression gate *(5 gates delivered with Phase 2: tiktoken budget gate over 16 golden renders, attribution language, description/signature consistency, session cost ≤17K, catalog ≤3.5K)*

## Phase 1 — Core read surface (3–4 weeks) · **ships v0.1**

- [x] Tools: `garmin_brief`, `garmin_activities`, `garmin_activity`, `garmin_athlete`, `garmin_set_profile`, `garmin_log`, `garmin_sync`, `garmin_raw`
- [x] Readiness fusion with subjective gate + acute override; corrective error messages
- [x] README publishes the token-budget contract

### Distribution workstream (parallel, lands with v0.1) — not optional polish

- [x] `fartlek auth` (full MFA flow, error taxonomy) + `fartlek doctor` + `accounts/switch/export/reset`
- [x] Install paths: `uvx`/`pipx` one-liner, Docker image (`.mcpb`/`.dxt` Desktop extension packaging TBD)
- [x] Client config snippets: Claude Code, Claude Desktop, Cursor
- [x] Open-sourcing basics: Apache 2.0 LICENSE (relicensed from MIT 2026-07-27), English README with "not affiliated with Garmin Ltd." disclaimer, PyPI `fartlek-mcp` publish (v0.2.2 live), official MCP registry entry `io.github.matisdsp/fartlek` (0.2.2, `isLatest`) — third-party directories (Glama, mcp.so, PulseMCP) still open

## Phase 2 — Trend suite & engine completion (4–5 weeks) · **ships v0.2, the flagship** — done, v0.2.2 live

- [x] Tools: `garmin_whats_changed`, `garmin_week`, `garmin_load`, `garmin_fitness` (incl. race projection + taper window), `garmin_recovery`, `garmin_reference` (metrics glossary)
- [x] Engine: Tier-2 history backfill, EF/decoupling/durability, sleep timeline + SRI, TID mapping, race triangulation, trend significance (per-metric SWC), overtraining convergence audit, attribution rules
- [x] MCP prompts + resources (progressive enhancement) — 7 prompts + 2 resources
- [x] Engine validation vs. intervals.icu golden data (decoupling cross-checked on 8 long runs, median gap 1 pt)
- [x] Anomaly-scanner threshold tuning on real multi-month data (75 → 27 alerts over 116 days)
- [ ] Evaluation harness: ~30 multi-tool coaching tasks across clients, token/calls regression gates, transcript audits *(deferred to a later v0.2.x; a reduced 10-task harness shipped — see `docs/EVAL.md`)*

## Phase 3 — ~~The closed loop~~ **cancelled 2026-07-28 · Fartlek stays read-only**

The write path is dropped, permanently. Rationale in [`docs/DESIGN.md`](docs/DESIGN.md) §2.4, in short: Garmin's Terms of Use already cover the read path we ship, Garmin *does* sanction third-party writes through the Connect Developer Program's Training API, and taking that sanctioned route would force a cloud-to-cloud integration — surrendering the local-first property the project exists to defend. Reading your own data locally is defensible; routing around a write API that exists is not.

- [x] ~~`garmin_apply_plan`~~ — dropped (spec kept in DESIGN §2.4 for the record)
- [x] ~~`garmin_reference` workout-schema topic; prescription-side compliance~~ — dropped with it
- [x] First-run onboarding — `garmin_setup`, state-aware `instructions`, day-1-vs-expired errors (v0.2.3)
- [ ] `setup_athlete` elicitation flow

## v0.3 — Read-side depth & the quality programme

- [x] **Gear** — `garmin_gear` (mileage vs the athlete's Garmin-set retirement limits, replacement verdict, 90-day rotation), the pair worn inlined in `garmin_activity`, a `gear` source in `garmin_raw`; `gear`/`activity_gear` tables, tiered sync with bounded background attribution *(promoted from Phase 4)*
- [ ] Eval harness: ~30 multi-tool tasks across Claude Code / Desktop / Cursor
- [ ] Token + calls-per-task regression gates
- [ ] Transcript audits — every number the LLM re-derives is a missing server-side pre-computation
- [ ] Depth items promoted from Phase 4 as they earn it

## Phase 4 — Depth extensions (ongoing)

Cycling power depth · swim CSS · menstrual-cycle-aware baselines (clinician-reviewed) · body-composition verdicts · Body Battery event attribution · hosted streamable-HTTP mode · MCP Apps dashboard

---

**Each phase ships a working, useful server.** The original plan put the closed loop at v0.3; that phase is cancelled, so v0.3 is a read-side release built on the quality programme deferred from v0.2.
