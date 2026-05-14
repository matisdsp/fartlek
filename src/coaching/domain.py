"""Coaching domain — conversation entities.

Kept provider-agnostic on purpose: an LLMPort adapter translates this shape
to/from the Anthropic SDK format, so switching providers later only touches
the adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant"]


@dataclass(slots=True)
class TextPart:
    type: Literal["text"]
    text: str


@dataclass(slots=True)
class ToolUsePart:
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class ToolResultPart:
    type: Literal["tool_result"]
    tool_use_id: str
    content: str
    is_error: bool = False


ContentPart = TextPart | ToolUsePart | ToolResultPart


@dataclass(slots=True)
class Message:
    """A turn in the conversation.

    `content` is either a plain string (simple user message) or a list of
    typed parts (assistant turns that mix text and tool_use blocks, or user
    turns that carry tool_result blocks).
    """
    role: Role
    content: str | list[ContentPart]


@dataclass(slots=True)
class LLMResponse:
    """What an LLMPort.complete() returns."""
    text: str
    content_parts: list[ContentPart]
    stop_reason: str
    raw_assistant_content: Any  # provider-specific blob to pass back unchanged on next turn


@dataclass(slots=True)
class Conversation:
    messages: list[Message] = field(default_factory=list)

    def add_user_text(self, text: str) -> None:
        self.messages.append(Message(role="user", content=text))

    def add_assistant_raw(self, raw_content: Any) -> None:
        """Append assistant's raw response content (preserves provider-specific blocks)."""
        self.messages.append(Message(role="assistant", content=raw_content))

    def add_tool_results(self, results: list[ToolResultPart]) -> None:
        self.messages.append(Message(role="user", content=list(results)))
