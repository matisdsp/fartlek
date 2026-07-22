# Fartlek — Garmin MCP server

*A coach's morning report from your Garmin data, for any LLM via MCP.* Open-source project. The authoritative spec is `docs/DESIGN.md`; the phase plan is `ROADMAP.md`.

**Picking up the project? Start with [`docs/HANDOFF.md`](docs/HANDOFF.md)** — current state, verified facts, invariants, and the traps that cost time. Phases 0 and 1 are done (v0.1.1 on PyPI); Phase 2 is next.

## Git discipline — commit as you go

**Commit incrementally, as soon as a coherent change is done and verified.** Do not accumulate a large uncommitted working tree.

- One logical change = one commit (a module, a new tool, a fix — not "misc").
- Verify before committing (run the relevant tests; for the MCP server, the standalone JSON-RPC check below).
- Concise imperative messages, scoped: `analytics: add PMC engine`, `store: fix WAL busy timeout`.
- Never commit secrets or tokens (`~/.fartlek/`, `garmin_tokens.json`).

## Architecture

- `fartlek/health/` — Garmin data access. `ports.py` (GarminPort protocol) → `adapters/garmin_connect.py` (`garminconnect` lib, sync calls in `asyncio.to_thread` behind a lock + cross-process fcntl token lock) → `service.py` (field filtering). Consumers go through HealthService, never the adapter.
- `fartlek/mcp_server/server.py` — FastMCP stdio server. stdout is reserved for JSON-RPC; log to stderr only.
- `fartlek/cli.py` — `fartlek auth/doctor/accounts/reset`. The only place credentials are typed.
- `fartlek/paths.py` — filesystem layout (`~/.fartlek/`, override `FARTLEK_HOME`; tokens override `GARMINTOKENS`).
- Phase 0 adds: `fartlek/store/` (per-account SQLite), `fartlek/sync/` (fetch + digestion engine), `fartlek/analytics/` (deterministic metrics), `fartlek/render/` (budgeted verdict renderer). Specs: DESIGN.md §3 and §5.

## Commands

```bash
uv sync                        # install (dev group included)
uv run fartlek auth            # one-time Garmin login (email/password + MFA)
uv run fartlek doctor          # health check
uv run fartlek-mcp             # run MCP server (stdio) — Claude Code auto-starts it via .mcp.json
uv run pytest                  # tests
uv run ruff check fartlek/     # lint
```

Standalone MCP smoke check:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | (cat; sleep 2) | uv run fartlek-mcp 2>/dev/null
```

## Notes

- `garth` is deprecated (Garmin broke its login in 2026) — use `garminconnect` for all Garmin API access; its source in `.venv/.../garminconnect/` is the ground truth for endpoints.
- Python ≥ 3.12, managed by `uv`. PyPI name: `fartlek-mcp`.
- The design doc's formulas (PMC constants, ACWR EWMA, MAD scaling 1.4826, Foster monotony) are contracts — implement exactly, test against known values.
- Live testing against the maintainer's real Garmin account is possible: tokens in the session scratchpad or via `GARMINTOKENS`. Be polite with call volume (sequential, backoff on 429).
