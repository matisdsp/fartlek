"""FastAPI router for the coaching context — POST /api/chat."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from src.coaching.domain import Message
from src.coaching.exceptions import LLMError
from src.coaching.schemas import APIMessage, ChatRequest, ChatResponse
from src.coaching.service import CoachingService
from src.dependencies import get_coaching_service
from src.health.exceptions import GarminAuthError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["coaching"])


def _to_domain(api_msg: APIMessage) -> Message:
    return Message(role=api_msg.role, content=api_msg.content)


def _last_assistant_text(messages: list[Message]) -> str:
    for msg in reversed(messages):
        if msg.role != "assistant":
            continue
        if isinstance(msg.content, str):
            return msg.content
        # raw Anthropic content blocks — pull text blocks out
        chunks: list[str] = []
        for block in msg.content:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
                if text:
                    chunks.append(text)
        if chunks:
            return "\n".join(chunks)
    return ""


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    service: Annotated[CoachingService, Depends(get_coaching_service)],
) -> ChatResponse:
    incoming = [_to_domain(m) for m in body.messages]

    try:
        conv = await service.handle_messages(incoming)
    except GarminAuthError as exc:
        log.error("Garmin auth failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Garmin authentication unavailable: {exc}",
        ) from exc
    except LLMError as exc:
        log.error("LLM error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM provider error: {exc}",
        ) from exc

    return ChatResponse(
        messages=[APIMessage(role=m.role, content=m.content if isinstance(m.content, (str, list)) else str(m.content)) for m in conv.messages],
        response_text=_last_assistant_text(conv.messages),
    )
