"""Shared configuration for cross-agent coordination."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


class SharedConfig:
    """Configuration shared across all agents."""

    # Bankroll limits (shared)
    STARTING_BANKROLL: float = float(os.getenv("STARTING_BANKROLL", "500"))
    MAX_TOTAL_EXPOSURE_PCT: float = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.50"))

    # Telegram (shared)
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_API_BASE: str = "https://api.telegram.org"

    # Paths
    PROJECT_ROOT: Path = _PROJECT_ROOT
    DATA_DIR: Path = _PROJECT_ROOT / "data"
