"""App configuration — env-driven settings.

Loaded once at startup, injected via Depends(get_settings).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    anthropic_api_key: SecretStr
    # GARMINTOKENS is the single canonical env var (same one the
    # garminconnect library and the login CLI honor)
    garmin_tokens: Path = Field(
        default=Path.home() / ".garminconnect",
        validation_alias=AliasChoices("GARMINTOKENS"),
    )
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
