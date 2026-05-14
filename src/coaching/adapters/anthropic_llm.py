"""Anthropic adapter — implements LLMPort with the Claude SDK.

Uses claude-opus-4-7 with adaptive thinking + high effort. Preserves the
raw assistant content blocks across turns (required so thinking blocks +
tool_use IDs round-trip correctly).
"""
from __future__ import annotations

import logging
from typing import Any

import anthropic
from pydantic import SecretStr

from src.coaching.domain import (
    ContentPart,
    LLMResponse,
    Message,
    TextPart,
    ToolResultPart,
    ToolUsePart,
)
from src.coaching.exceptions import LLMError

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"
MAX_TOKENS = 8192


class AnthropicAdapter:
    def __init__(self, api_key: SecretStr):
        self._client = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value())

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        api_messages = [self._to_api_message(m) for m in messages]
        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=system,
                tools=tools,
                messages=api_messages,
            )
        except anthropic.APIError as exc:
            log.exception("Anthropic API error")
            raise LLMError(f"Claude API error: {exc}") from exc

        text_chunks: list[str] = []
        parts: list[ContentPart] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_chunks.append(block.text)
                parts.append(TextPart(type="text", text=block.text))
            elif block_type == "tool_use":
                parts.append(
                    ToolUsePart(
                        type="tool_use",
                        id=block.id,
                        name=block.name,
                        input=dict(block.input) if block.input else {},
                    )
                )
            # thinking blocks: kept inside raw_assistant_content, not surfaced as parts

        return LLMResponse(
            text="\n".join(text_chunks),
            content_parts=parts,
            stop_reason=response.stop_reason or "end_turn",
            raw_assistant_content=response.content,
        )

    @staticmethod
    def _to_api_message(m: Message) -> dict[str, Any]:
        if isinstance(m.content, str):
            return {"role": m.role, "content": m.content}

        # content is either: raw Anthropic blocks (assistant turn we got back from the API)
        # or a list of our ContentPart dataclasses (user turn carrying tool results)
        if m.content and not isinstance(m.content[0], (TextPart, ToolUsePart, ToolResultPart)):
            return {"role": m.role, "content": m.content}

        serialized = []
        for part in m.content:
            if isinstance(part, TextPart):
                serialized.append({"type": "text", "text": part.text})
            elif isinstance(part, ToolUsePart):
                serialized.append({
                    "type": "tool_use",
                    "id": part.id,
                    "name": part.name,
                    "input": part.input,
                })
            elif isinstance(part, ToolResultPart):
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": part.tool_use_id,
                    "content": part.content,
                }
                if part.is_error:
                    block["is_error"] = True
                serialized.append(block)
        return {"role": m.role, "content": serialized}
