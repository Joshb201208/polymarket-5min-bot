"""Shared configuration — loads .env and exposes constants used by both agents."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


class SharedConfig:
    """Configuration shared across NBA and NHL agents."""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Trading mode
    TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")

    # Polymarket credentials
    POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
    POLYMARKET_API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
    POLYMARKET_API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
    FUNDER_ADDRESS: str = os.getenv("FUNDER_ADDRESS", "")

    # Bankroll
    STARTING_BANKROLL: float = float(os.getenv("STARTING_BANKROLL", "440.58"))
    MAX_BET_PCT: float = float(os.getenv("MAX_BET_PCT", "0.08"))
    MAX_GAME_EXPOSURE_PCT: float = float(os.getenv("MAX_GAME_EXPOSURE_PCT", "0.12"))
    MAX_TOTAL_EXPOSURE_PCT: float = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.50"))

    # Scan intervals
    SCAN_INTERVAL: int = int(os.getenv("SCAN_INTERVAL", "10"))

    # Edge thresholds
    MIN_GAME_EDGE: float = float(os.getenv("MIN_GAME_EDGE", "0.04"))

    # External APIs
    ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")
    PROXY_HOST: str = os.getenv("PROXY_HOST", "")
    PROXY_PORT: str = os.getenv("PROXY_PORT", "")
    PROXY_USER: str = os.getenv("PROXY_USER", "")
    PROXY_PASS: str = os.getenv("PROXY_PASS", "")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Paths
    PROJECT_ROOT: Path = _PROJECT_ROOT
    DATA_DIR: Path = _PROJECT_ROOT / "data"

    # API URLs
    GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"
    CLOB_API_BASE: str = "https://clob.polymarket.com"
    TELEGRAM_API_BASE: str = "https://api.telegram.org"

    @property
    def is_live(self) -> bool:
        return self.TRADING_MODE.lower() == "live"

    @property
    def is_paper(self) -> bool:
        return not self.is_live

    def ensure_data_dir(self) -> None:
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
