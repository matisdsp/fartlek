"""Filesystem layout — single source of truth for where Fartlek keeps state.

~/.fartlek/                     (override: FARTLEK_HOME)
├── tokens/garmin_tokens.json   Garmin OAuth tokens (override: GARMINTOKENS)
└── <garmin-user-id>/store.db   per-account SQLite store (created at first sync)
"""
from __future__ import annotations

import os
from pathlib import Path


def fartlek_home() -> Path:
    return Path(os.environ.get("FARTLEK_HOME") or (Path.home() / ".fartlek")).expanduser()


def default_tokenstore() -> Path:
    return Path(os.environ.get("GARMINTOKENS") or (fartlek_home() / "tokens")).expanduser()


def account_dir(garmin_user_id: str) -> Path:
    return fartlek_home() / garmin_user_id


def store_path(garmin_user_id: str) -> Path:
    return account_dir(garmin_user_id) / "store.db"
