# AI Coach — Garmin MCP server

MCP server exposing Garmin Connect data to any LLM (Claude Code, Claude Desktop, Cursor…), with an intelligent synthesis layer as its core value. Being refocused/open-sourced around `src/mcp_server/` — the FastAPI chat (`src/coaching/`, option B) is secondary.

## Git discipline — commit as you go

**Commit incrementally, as soon as a coherent change is done and verified.** Do not accumulate a large uncommitted working tree.

- One logical change = one commit (a migration, a new tool, a fix — not "misc").
- Verify before committing (run the relevant test/smoke check; for the MCP server, the standalone JSON-RPC check below).
- Concise imperative messages, scoped: `mcp: add get_weekly_report tool`, `health: fix HRV baseline fallback`.
- Never commit secrets or tokens (`.env`, `~/.garminconnect/`, `garmin_tokens.json`).

## Architecture

Modular monolith, DDD-lite, one folder per bounded context:

- `src/health/` — Garmin data. `ports.py` (GarminPort protocol) → `adapters/garmin_connect.py` (`garminconnect` lib, sync calls wrapped in `asyncio.to_thread` behind a lock) → `service.py` (HealthService: filters raw Garmin JSON down to coaching-relevant fields). All consumers go through HealthService, never the adapter.
- `src/mcp_server/server.py` — FastMCP stdio server exposing the tools. stdout is reserved for JSON-RPC; log to stderr only.
- `src/coaching/` — FastAPI chat + Anthropic adapter (option B, needs `ANTHROPIC_API_KEY`).
- `src/login.py` — `ai-coach-login` CLI: the only place Garmin credentials are typed; stores tokens at `~/.garminconnect/` (override: `GARMINTOKENS`).

## Commands

```bash
uv sync                        # install
uv run ai-coach-login          # one-time Garmin login (email/password + MFA)
uv run ai-coach-mcp            # run MCP server (stdio) — Claude Code auto-starts it via .mcp.json
uv run uvicorn src.main:app --reload --port 8000   # option B web UI
```

Standalone MCP smoke check:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | (cat; sleep 2) | uv run ai-coach-mcp 2>/dev/null
```

## Notes

- `garth` is deprecated (Garmin broke its login in 2026) — use `garminconnect` for all Garmin API access; its source in `.venv/.../garminconnect/` is the ground truth for endpoints.
- Python ≥ 3.12, managed by `uv` (`pyproject.toml` + `uv.lock`).
