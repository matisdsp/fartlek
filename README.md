<!-- mcp-name: io.github.matisdsp/fartlek -->

# Fartlek

*A coach's morning report from your Garmin data, for any LLM via MCP.*

Every other Garmin MCP server hands the LLM a filing cabinet of raw JSON — one night of sleep is ~52K tokens, one activity stream ~155K. The model can't read it, so it skims and improvises. Fartlek does the synthesis server-side: computed sports-science metrics (CTL/ATL/TSB, ACWR, monotony, calibrated training load), personal baselines with significance floors, safety alerts — delivered as compact, verdict-first reports the model can actually reason about.

**The token contract (v0.1):** calling every tool in the catalog once, at default arguments, costs **under 9K tokens** — a sixth of one raw Garmin sleep payload. Excluding the `garmin_raw` escape hatch, the whole synthesis surface sums to **under 4K**. Hard caps are enforced per response by the renderer, with disclosed truncation.

> **Status: v0.1 (Phase 1).** 8 synthesis tools; the trend suite (weekly review, multi-week load, fitness/race outlook, recovery audit) ships with v0.2. Design: [`docs/DESIGN.md`](docs/DESIGN.md) · plan: [`ROADMAP.md`](ROADMAP.md).

## The tools

| Tool | What it answers | Cap |
|---|---|---|
| `garmin_brief` | "How am I today — can I train hard?" Fused GREEN/AMBER/RED verdict vs your own baselines | 600 |
| `garmin_activities` | Browse the log, get activity IDs | 1,300 |
| `garmin_activity` | One session in depth: reps, fade, comparison to your most similar past session | 1,000–4,000 |
| `garmin_athlete` | Reference card: zones, PRs, goal, data coverage | 600 |
| `garmin_set_profile` | Tell it your goal race / phase / availability (local only) | 200 |
| `garmin_log` | Log RPE, wellness, illness/injury — the athlete outranks the sensors | 120 |
| `garmin_sync` | Force refresh / deepen history backfill | 150 |
| `garmin_raw` | Bounded, compacted escape hatch to named raw sources | 5,000 |

First call on a fresh install runs the cold start automatically (~30 API calls, ≈1 minute): 180 days of history, warm CTL/ATL from day 0, then background sleep/HRV backfill.

## Quickstart

Install with either:

```bash
# uv (recommended) — runs without cloning
uvx fartlek-mcp

# or pipx
pipx install fartlek-mcp
```

Or clone and run from source (requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/matisdsp/fartlek && cd fartlek
uv sync

# One-time Garmin login (email/password + MFA if enabled).
# Credentials are never stored; OAuth tokens go to ~/.fartlek/tokens/.
uv run fartlek auth

# Optional but recommended: warm the local store now instead of on first use
uv run fartlek sync --nights 60

uv run fartlek doctor   # check everything is healthy
```

Then point your MCP client at the server.

> **Any MCP-compatible client works** — the server speaks standard JSON-RPC over stdio, so it is client-agnostic. The snippets below are just the per-client config formats; Claude Desktop, Claude Code, Cursor, Continue, Cline, Windsurf, Zed, VS Code (Copilot Chat), and Gemini CLI all work. The universal invocation is `uvx fartlek-mcp`.

**Claude Code** — from this directory, `.mcp.json` is picked up automatically. From anywhere else:

```bash
claude mcp add fartlek -- uvx fartlek-mcp
```

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fartlek": {
      "command": "uvx",
      "args": ["fartlek-mcp"]
    }
  }
}
```

**Cursor** — `.cursor/mcp.json`, same `command`/`args` block as above.

**Continue / Cline / Windsurf / Zed** — same pattern: wherever the client keeps its MCP server list, add a `fartlek` entry with `command: "uvx"`, `args: ["fartlek-mcp"]`. Most editors adopt the Claude Desktop format verbatim.

**Any other stdio MCP client** — invoke the server binary directly:

```bash
fartlek-mcp          # speaks JSON-RPC over stdin/stdout
```

Ask things like *"can I go hard today?"*, *"analyze my last run"*, *"how did I sleep this week?"* — and tell it how sessions felt: your reported RPE and illness notes gate the readiness verdict.

## Docker

Build and run locally:

```bash
docker build -t fartlek-mcp .
# Tokens and store are persisted in ./fartlek-data on the host
mkdir -p fartlek-data
docker run -i --rm -v "$PWD/fartlek-data:/data" fartlek-mcp
```

For `fartlek auth`, run it interactively once to populate the volume, then use the image as the MCP server:

```bash
docker run -it --rm -v "$PWD/fartlek-data:/data" --entrypoint fartlek fartlek-mcp auth
```

## CLI

| Command | What it does |
|---|---|
| `fartlek auth` | one-time Garmin Connect login (MFA supported), tokens stored locally |
| `fartlek sync [--nights N]` | manual sync (tier 0+1, optional N-night sleep/HRV backfill) |
| `fartlek doctor` | check tokens, Garmin connectivity, local store health |
| `fartlek accounts` | list local accounts |
| `fartlek export [dir]` | export the store (consistent SQLite snapshot + CSV per table) |
| `fartlek reset` | wipe all local tokens and data (asks confirmation) |

Environment: `GARMINTOKENS` overrides the token location, `FARTLEK_HOME` the data directory (default `~/.fartlek`).

### Releasing to PyPI (maintainers)

Releases are published via **trusted publishing** (OIDC) — no API tokens anywhere.

1. On pypi.org → Account settings → Publishing → add a GitHub publisher:
   - PyPI project name: `fartlek-mcp` · owner: `matisdsp` · repo: `fartlek`
   - Workflow: `release.yml` · environment: `pypi`
2. Bump `version` in `pyproject.toml`, commit, then tag and push:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The `release` workflow builds, runs tests, and uploads to PyPI. A published version can't be overwritten — to fix a mistake, bump to the next patch (`0.1.1`).

## Privacy

Local-first: stdio transport, your credentials and health data never leave your machine. The server only talks to Garmin's API with your own tokens, sequentially and rate-limited. `fartlek export` gives you everything; `fartlek reset` removes everything.

## License & trademark

MIT. Fartlek is an independent open-source project, **not affiliated with, endorsed by, or sponsored by Garmin Ltd.** "Garmin" is used only to describe compatibility with Garmin Connect data.
