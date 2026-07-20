# Fartlek

*A coach's morning report from your Garmin data, for any LLM via MCP.*

Every other Garmin MCP server hands the LLM a filing cabinet of raw JSON — one night of sleep is ~52K tokens, one activity stream ~155K. The model can't read it, so it skims and improvises. Fartlek is an MCP server that does the synthesis server-side: computed sports-science metrics (PMC, ACWR, monotony, aerobic decoupling…), personal baselines, significance-tested trends, safety flags — delivered as compact, verdict-first reports the model can actually reason about.

> **Status: pre-release (Phase 0 — foundation).** The full design is in [`docs/DESIGN.md`](docs/DESIGN.md), the phase plan in [`ROADMAP.md`](ROADMAP.md). The current interim surface exposes 12 field-filtered Garmin tools; the synthesis tool surface (`garmin_brief` & co.) ships with v0.1.

## Quickstart

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo> && cd fartlek
uv sync

# One-time Garmin login (email/password + MFA if enabled).
# Credentials are never stored; OAuth tokens go to ~/.fartlek/tokens/.
uv run fartlek auth

# Check everything is healthy
uv run fartlek doctor
```

Then point any MCP client at the server. From this directory, Claude Code picks up `.mcp.json` automatically; for other clients:

```json
{
  "mcpServers": {
    "fartlek": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/fartlek", "fartlek-mcp"]
    }
  }
}
```

Ask things like *"how did I sleep?"*, *"analyze my last run"*, *"can I go hard today or should I recover?"*.

## CLI

| Command | What it does |
|---|---|
| `fartlek auth` | one-time Garmin Connect login (MFA supported), tokens stored locally |
| `fartlek doctor` | check tokens, Garmin connectivity, local store health |
| `fartlek accounts` | list local accounts |
| `fartlek reset` | wipe all local tokens and data (asks confirmation) |

Environment: `GARMINTOKENS` overrides the token location, `FARTLEK_HOME` the data directory (default `~/.fartlek`).

## Privacy

Local-first: stdio transport, your credentials and health data never leave your machine. The server only talks to Garmin's API with your own tokens.

## License & trademark

MIT. Fartlek is an independent open-source project, **not affiliated with, endorsed by, or sponsored by Garmin Ltd.** "Garmin" is used only to describe compatibility with Garmin Connect data.
