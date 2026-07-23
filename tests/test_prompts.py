"""MCP prompts & resources (DESIGN §4.6) — registration, content safety, wiring.

Prompts are progressive enhancement, but a prompt that named a phantom tool
would be a routing trap just like a poisoned breadcrumb, so the same registry
check applies. Resources must mirror their tools verbatim.
"""
from __future__ import annotations

import asyncio
import re

from fartlek.mcp_server import prompts, server
from fartlek.mcp_server.tools import athlete, reference
from tests.test_guardrails import DATA_ENUMS, REGISTRY


def _prompt_names() -> set[str]:
    return {p.name for p in asyncio.run(server.mcp.list_prompts())}


def _resource_uris() -> set[str]:
    return {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}


def test_all_seven_prompts_registered():
    assert _prompt_names() == set(prompts.PROMPTS)


def test_two_resources_registered():
    assert _resource_uris() == {
        "garmin://athlete/snapshot", "garmin://reference/metrics-glossary"}


def test_every_prompt_renders_non_empty():
    for name, (builder, _desc) in prompts.PROMPTS.items():
        text = builder("12345") if name == "post_activity_debrief" else builder()
        assert text.strip(), f"prompt {name} rendered empty"


def test_post_activity_debrief_embeds_its_argument():
    assert "activity_id=98765" in prompts.post_activity_debrief("98765")


def test_prompt_content_names_only_registered_tools():
    """No prompt may steer the model to a tool that does not exist."""
    for name, (builder, _desc) in prompts.PROMPTS.items():
        text = builder("1") if name == "post_activity_debrief" else builder()
        for tool in set(re.findall(r"\bgarmin_[a-z_]+\b", text)) - DATA_ENUMS:
            assert tool in REGISTRY, f"prompt {name} names phantom tool {tool}"


async def test_resources_mirror_their_tools_verbatim(monkeypatch):
    """The two resources return exactly what their mirror tool renders."""
    async def fake_athlete(ctx, **kw):
        return "# Athlete snapshot"

    async def fake_reference(ctx, **kw):
        return "# Metrics glossary"

    monkeypatch.setattr(athlete, "run", fake_athlete)
    monkeypatch.setattr(reference, "run", fake_reference)
    assert await server.athlete_snapshot() == "# Athlete snapshot"
    assert await server.metrics_glossary() == "# Metrics glossary"
