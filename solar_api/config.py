"""Configuration and date helpers for the APsystems client."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


def load_env_file(path: Path) -> None:
    """Load dotenv settings without overriding values already in the environment."""
    load_dotenv(dotenv_path=path, override=False, encoding="utf-8")


def get_today(timezone: str | None = None) -> str:
    """Return today's date in a timezone, defaulting to the machine's local zone."""
    if timezone:
        try:
            today = datetime.now(ZoneInfo(timezone)).date()
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown APPSYSTEMS_TIMEZONE: {timezone}") from error
    else:
        today = datetime.now().astimezone().date()
    return today.isoformat()
