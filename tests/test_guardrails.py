"""CI guardrails (DESIGN §4.5) — server-surface invariants.

- Catalog test: combined tool definitions (names + descriptions + schemas)
  stay under the 3.5K-token budget.
- Breadcrumb/description validity: no tool description, server instruction,
  or tool-module source mentions a tool that is not in the v0.1 registry
  (the poisoned-breadcrumb bug class, §4.5) — Phase-2 names must not leak.
- Every declared tool name uses the garmin_ prefix and exists as a module
  entry point.
- Session-cost gate: the sum of hard caps, one call per tool at default args,
  stays ≤17K (§5 rule 8 basis).
- Description/signature consistency: every `garmin_x(arg=…)` call written into
  any tool description names a real parameter of that tool (extends breadcrumb
  validity from tool names to their arguments).
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
    "garmin_load",
    "garmin_week",
    "garmin_whats_changed",
    "garmin_reference",
}

PHASE2_NAMES = {
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


# --- session-cost gate (§5 rule 8) ------------------------------------------

def _hard_caps() -> dict[str, int]:
    """Each tool's hard cap at DEFAULT arguments — garmin_activity at
    detail='standard', reference and raw included (§5 rule 8 basis). Read from
    the tool modules so a cap change is reflected here automatically."""
    from fartlek.mcp_server.tools import (
        activities,
        activity,
        athlete,
        brief,
        fitness,
        log_tool,
        raw,
        recovery,
        reference,
        set_profile,
        sync_tool,
        week,
        whats_changed,
    )
    from fartlek.mcp_server.tools import load as load_tool

    return {
        "garmin_brief": brief.CAP,
        "garmin_activities": activities.CAP_TOKENS,
        "garmin_activity": activity.CAPS["standard"],
        "garmin_athlete": athlete.CAP_TOKENS,
        "garmin_set_profile": set_profile.CAP_TOKENS,
        "garmin_log": log_tool.CAP_TOKENS,
        "garmin_sync": sync_tool.CAP_TOKENS,
        "garmin_raw": raw.CAP,
        "garmin_recovery": recovery.CAP,
        "garmin_fitness": fitness.CAP,
        "garmin_load": load_tool.CAP,
        "garmin_week": week.CAP,
        "garmin_whats_changed": whats_changed.CAP,
        "garmin_reference": reference.CAP,
    }


def test_session_cost_under_17k():
    caps = _hard_caps()
    assert set(caps) == REGISTRY, "every registered tool must contribute a cap"
    total = sum(caps.values())
    # DESIGN §5 rule 8: the defined basis sums to ~16.1K, gated at ≤17K.
    assert total <= 17_000, f"session cost {total} exceeds the 17K guarantee"


# --- description/signature consistency --------------------------------------

_CALL = re.compile(r"\b(garmin_[a-z_]+)\(([^)]*)\)")
_ARG = re.compile(r"([A-Za-z_]\w*)\s*=")


def test_description_call_args_are_registered_params(tools):
    """Every garmin_x(arg=…) written into a description names a real parameter
    of x — a stale param reference (the sibling of the poisoned breadcrumb)
    fails the build."""
    params = {
        t.name: set((t.inputSchema.get("properties") or {}).keys()) for t in tools
    }
    for t in tools:
        for callee, arglist in _CALL.findall(t.description or ""):
            assert callee in params, f"{t.name} description calls unknown tool {callee}"
            for arg in _ARG.findall(arglist):
                assert arg in params[callee], (
                    f"{t.name} description writes {callee}({arg}=…) but {arg!r} is "
                    f"not a parameter of {callee} ({sorted(params[callee])})"
                )
