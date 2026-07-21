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
- [ ] CI guardrails incl. real-tokenizer budget regression gate *(basic CI — ruff + 244 tests — in place; tokenizer budget gate lands with the Phase-1 tools that have golden renders)*

## Phase 1 — Core read surface (3–4 weeks) · **ships v0.1**

- [x] Tools: `garmin_brief`, `garmin_activities`, `garmin_activity`, `garmin_athlete`, `garmin_set_profile`, `garmin_log`, `garmin_sync`, `garmin_raw`
- [x] Readiness fusion with subjective gate + acute override; corrective error messages
- [x] README publishes the token-budget contract

### Distribution workstream (parallel, lands with v0.1) — not optional polish

- [x] `fartlek auth` (full MFA flow, error taxonomy) + `fartlek doctor` + `accounts/switch/export/reset`
- [x] Install paths: `uvx`/`pipx` one-liner, Docker image (`.mcpb`/`.dxt` Desktop extension packaging TBD)
- [x] Client config snippets: Claude Code, Claude Desktop, Cursor
- [ ] Open-sourcing basics: MIT LICENSE, English README with "not affiliated with Garmin Ltd." disclaimer, PyPI `fartlek-mcp` publish, MCP registry submissions

## Phase 2 — Trend suite & engine completion (4–5 weeks) · **ships v0.2, the flagship**

- [ ] Tools: `garmin_whats_changed`, `garmin_week`, `garmin_load`, `garmin_fitness` (incl. race projection + taper window), `garmin_recovery`, `garmin_reference` (metrics glossary)
- [ ] Engine: Tier-2 history backfill, EF/decoupling/durability, sleep timeline + SRI, TID mapping, race triangulation, trend significance (per-metric SWC), overtraining convergence audit, attribution rules
- [ ] MCP prompts + resources (progressive enhancement)
- [ ] Evaluation harness: ~30 multi-tool coaching tasks across clients, token/calls regression gates; engine validation vs. intervals.icu golden data
- [ ] Anomaly-scanner threshold tuning on real multi-month data

## Phase 3 — The closed loop (3 weeks) · **ships v0.3**

- [ ] `garmin_apply_plan`: dry-run-first structured workouts pushed to the watch, guardrail simulation, token-bound confirmation
- [ ] `garmin_reference` workout-schema topic; prescription-side compliance in the debrief
- [ ] `setup_athlete` elicitation flow

## Phase 4 — Depth extensions (ongoing)

Cycling power depth · swim CSS · menstrual-cycle-aware baselines (clinician-reviewed) · body-composition verdicts · Body Battery event attribution · hosted streamable-HTTP mode · MCP Apps dashboard

---

**Total to v0.3: ~3–3.5 months solo.** Each phase ships a working, useful server.
