"""LLMPort — domain-owned interface for completions with tool use.

Swap providers by writing a new adapter; the service doesn't change.
"""
from __future__ import annotations

from typing import Any, Protocol

from src.coaching.domain import LLMResponse, Message


class LLMPort(Protocol):
    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> LLMResponse: ...
