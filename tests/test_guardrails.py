"""CI guardrails (DESIGN §4.5) — server-surface invariants.

- Catalog test: combined tool definitions (names + descriptions + schemas)
  stay under the 3.5K-token budget.
- Breadcrumb/description validity: no tool description, server instruction,
  or tool-module source mentions a tool that is not in the v0.1 registry
  (the poisoned-breadcrumb bug class, §4.5) — Phase-2 names must not leak.
- Every declared tool name uses the garmin_ prefix and exists as a module
  entry point.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fartlek.mcp_server import server
from fartlek.render.renderer import estimate_tokens

REGISTRY = {
    "garmin_brief",
    "garmin_activities",
    "garmin_activity",
    "garmin_athlete",
    "garmin_set_profile",
    "garmin_log",
    "garmin_sync",
    "garmin_raw",
    "garmin_recovery",
    "garmin_fitness",
}

PHASE2_NAMES = {
    "garmin_week",
    "garmin_load",
    "garmin_whats_changed",
    "garmin_reference",
    "garmin_apply_plan",
}

TOOLS_DIR = Path(server.__file__).parent / "tools"


@pytest.fixture(scope="module")
def tools():
    import asyncio

    return asyncio.run(server.mcp.list_tools())


def test_registry_matches_declared_tools(tools):
    assert {t.name for t in tools} == REGISTRY


def test_catalog_under_budget(tools):
    blob = "\n".join(
        f"{t.name}\n{t.description}\n{json.dumps(t.inputSchema)}" for t in tools
    )
    assert estimate_tokens(blob) <= 3500, f"catalog is {estimate_tokens(blob)} tokens"


def test_no_phase2_tool_mentioned_anywhere(tools):
    surfaces = [server.INSTRUCTIONS] + [t.description or "" for t in tools]
    surfaces += [p.read_text() for p in TOOLS_DIR.glob("*.py")]
    for text in surfaces:
        leaked = [name for name in PHASE2_NAMES if name in text]
        assert not leaked, f"phase-2 tool name(s) leaked: {leaked}"


# garmin_-prefixed identifiers that are data values, not tool names
DATA_ENUMS = {"garmin_coach"}  # plan_calendar.source enum


def test_tool_mentions_are_registered(tools):
    """Every garmin_* token in descriptions/instructions/tool sources is a
    declared tool — no phantom names of any kind."""
    surfaces = [server.INSTRUCTIONS] + [t.description or "" for t in tools]
    surfaces += [p.read_text() for p in TOOLS_DIR.glob("*.py")]
    for text in surfaces:
        for name in set(re.findall(r"\bgarmin_[a-z_]+\b", text)) - DATA_ENUMS:
            assert name in REGISTRY, f"phantom tool mentioned: {name}"


def test_descriptions_have_trigger_and_boundary(tools):
    by_name = {t.name: t.description or "" for t in tools}
    # The entry point must self-describe as first call; the escape hatch must
    # warn against starting with it (§4.2 trigger ownership).
    assert "FIRST" in by_name["garmin_brief"]
    assert "Never a starting point" in by_name["garmin_raw"]
