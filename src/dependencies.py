"""DI wiring — composes adapters into services via FastAPI Depends().

All factories are cached per-process: adapters are stateless after `__init__`,
and recreating them on every request would re-open HTTP sessions / re-parse
tokens for nothing. When we add per-user auth, we'll move from process-level
caches to request-scoped factories.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from pathlib import Path

from pydantic import SecretStr

from src.coaching.adapters.anthropic_llm import AnthropicAdapter
from src.coaching.adapters.tools.registry import ToolRegistry
from src.coaching.ports import LLMPort
from src.coaching.service import CoachingService
from src.config import Settings, get_settings
from src.health.adapters.garmin_connect import GarminConnectAdapter
from src.health.ports import GarminPort
from src.health.service import HealthService


@lru_cache(maxsize=1)
def _build_garmin_adapter(tokenstore_str: str) -> GarminConnectAdapter:
    return GarminConnectAdapter(tokenstore=Path(tokenstore_str))


def get_garmin_port(
    settings: Annotated[Settings, Depends(get_settings)],
) -> GarminPort:
    return _build_garmin_adapter(str(settings.garmin_tokens))


def get_health_service(
    garmin: Annotated[GarminPort, Depends(get_garmin_port)],
) -> HealthService:
    return HealthService(garmin=garmin)


@lru_cache(maxsize=1)
def _build_anthropic_adapter(api_key_value: str) -> AnthropicAdapter:
    return AnthropicAdapter(api_key=SecretStr(api_key_value))


def get_llm_port(
    settings: Annotated[Settings, Depends(get_settings)],
) -> LLMPort:
    return _build_anthropic_adapter(settings.anthropic_api_key.get_secret_value())


def get_tool_registry(
    health: Annotated[HealthService, Depends(get_health_service)],
) -> ToolRegistry:
    return ToolRegistry(health=health)


def get_coaching_service(
    llm: Annotated[LLMPort, Depends(get_llm_port)],
    tools: Annotated[ToolRegistry, Depends(get_tool_registry)],
) -> CoachingService:
    return CoachingService(llm=llm, tools=tools)
