<!-- mcp-name: io.github.matisdsp/fartlek -->

# Fartlek

*A coach's morning report from your Garmin data, for any LLM via MCP.*

Every other Garmin MCP server hands the LLM a filing cabinet of raw JSON: one night of sleep is ~52K tokens, one activity stream ~155K. The model can't read it, so it skims and improvises. Fartlek does the synthesis server-side: computed sports-science metrics (CTL/ATL/TSB, ACWR, monotony, calibrated training load), personal baselines with significance floors, safety alerts, delivered as compact, verdict-first reports the model can actually reason about.

**The token contract:** calling every tool in the catalog once, at default arguments, costs under 17K tokens, about a third of one raw Garmin sleep payload (the sum of the caps below is 16,970; `tests/test_guardrails.py` fails above 17,000). Without `garmin_raw` and `garmin_reference` the sum is 9,970. Hard caps are enforced per response by the renderer, with disclosed truncation, and `tests/test_budget_gate.py` counts golden renders with a real tokenizer.

## The tools

Caps are in tokens, as set in the code (`CAP` constants in `fartlek/mcp_server/tools/`).

| Tool | What it answers | Cap |
|---|---|---|
| `garmin_brief` | "How am I today, can I train hard?" Fused GREEN/AMBER/RED verdict vs your own baselines | 600 |
| `garmin_activities` | Browse the log, get activity IDs | 1,300 |
| `garmin_activity` | One session in depth: reps, fade, comparison to your most similar past session (`detail` standard, splits, full) | 1,000 / 2,000 / 4,000 |
| `garmin_athlete` | Reference card: zones, PRs, goal, data coverage | 600 |
| `garmin_gear` | Shoe and bike mileage vs the retirement limits you set in Garmin Connect, and what you actually rotate | 500 |
| `garmin_set_profile` | Tell it your goal race, phase and availability (local only) | 200 |
| `garmin_log` | Log RPE, wellness, illness or injury; the athlete outranks the sensors | 120 |
| `garmin_sync` | Re-read the export, or deepen history | 150 |
| `garmin_setup` | Is the server configured, and what is missing; starts with the export | 400 |
| `garmin_recovery` | Sleep, HRV, resting HR and load structure vs baselines, plus the overtraining audit | 1,100 |
| `garmin_load` | Multi-week load: CTL/ATL/TSB, ramp, ACWR, monotony, intensity drift | 1,100 |
| `garmin_fitness` | VO2max and efficiency trends, race projection against your goal, taper guidance | 1,000 |
| `garmin_week` | One week in session-level detail, with load vs recent weeks | 1,200 |
| `garmin_whats_changed` | Only the statistically significant changes of the last N days | 700 |
| `garmin_reference` | How a number was computed, and whether to trust it | 2,000 per topic |
| `garmin_raw` | Bounded, compacted escape hatch to named raw sources | 5,000 |

## Quickstart

```bash
pipx install fartlek-mcp        # or: uv tool install fartlek-mcp
```

1. In your Garmin account, open **Data Management > Export Your Data** and request the export. Garmin
   emails you a download link. It says this usually takes about 48 hours and can take up to 30 days.
2. Import the zip. Nothing connects to Garmin and no credentials are involved:

```bash
fartlek import ~/Downloads/<your-export>.zip
```

3. Point your MCP client at the server. Claude Code:

```bash
claude mcp add fartlek -- fartlek-mcp
```

Any other MCP client: add a stdio server whose command is `fartlek-mcp`.

The report stops at the day of the export. Importing a newer export updates the store. Your own settings
and log entries are kept.

## What the export does not contain

Compared on one real account against the Connect API, over 530 activities and 356 days: activities, laps,
overnight HRV and sleep scores are identical after converting units, and daily totals match on 99 % of days
or more. A one-account check is not a guarantee for other devices. The export lacks:

| Missing | Effect |
|---|---|
| Per-minute sleep stages | Sleep regularity index unavailable |
| Garmin's nightly sleep need | Sleep debt uses 8 h, or the need you set |
| HRV status and weekly average | Not shown; Fartlek's own HRV band does not use them |
| Grade-adjusted speed per lap | Efficiency and decoupling use plain speed |
| Model names of unrenamed gear | Gear may appear under its brand only |
| Raw detail per session | `garmin_raw` and `garmin_activity detail=full` have nothing to return |

## CLI

| Command | What it does |
|---|---|
| `fartlek import <zip>` | fill the local store from a Garmin account export (`--since YYYY-MM-DD` limits how far back) |
| `fartlek accounts` | list local accounts |
| `fartlek export [dir]` | export the store (SQLite snapshot and one CSV per table) |
| `fartlek reset` | wipe all local data (asks confirmation) |

`FARTLEK_HOME` sets the data directory (default `~/.fartlek`). The `Dockerfile` builds the same server: mount
a data volume on `/data`.

## Privacy and Garmin's terms

Stdio transport, local SQLite store: your health data stays on your machine, and importing an export contacts
nobody. The code also contains a direct connection to Garmin Connect. It is off by default and unsupported: it
reads endpoints Garmin does not publish for third parties, which Garmin's
[Terms of Use](https://www.garmin.com/en-US/legal/terms-of-use/) do not allow, and Garmin can suspend accounts
for automated access.

## License & trademark

Apache 2.0. Fartlek is an independent open-source project, **not affiliated with, endorsed by, or sponsored by Garmin Ltd.** "Garmin" is used only to describe compatibility with Garmin Connect data.
