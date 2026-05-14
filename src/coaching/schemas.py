"""API edge — request/response models for the chat endpoint.

These are intentionally permissive: `content` is Any because the frontend
roundtrips the raw Anthropic blocks (tool_use, tool_result, thinking) it
received on the previous turn. The service layer handles them via the
domain.Message shape.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class APIMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[Any]


class ChatRequest(BaseModel):
    messages: list[APIMessage] = Field(..., min_length=1)


class ChatResponse(BaseModel):
    messages: list[APIMessage]
    response_text: str
