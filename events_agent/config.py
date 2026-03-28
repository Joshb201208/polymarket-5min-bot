"""Configuration — loads .env and exposes all events-specific constants."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


class EventsConfig:
    """Central configuration for the Events agent."""

    # Telegram (shared with NBA agent)
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Trading mode
    TRADING_MODE: str = os.getenv("EVENTS_TRADING_MODE", "live")

    # Polymarket credentials (live mode only — shared with NBA)
    POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
    POLYMARKET_API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
    POLYMARKET_API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
    FUNDER_ADDRESS: str = os.getenv("FUNDER_ADDRESS", "")

    # Bankroll (shared)
    STARTING_BANKROLL: float = float(os.getenv("STARTING_BANKROLL", "500"))
    MAX_BET_PCT: float = float(os.getenv("EVENTS_MAX_BET_PCT", "0.02"))
    MAX_TOTAL_EXPOSURE_PCT: float = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.50"))

    # Scan interval (minutes)
    SCAN_INTERVAL: int = int(os.getenv("EVENTS_SCAN_INTERVAL_MINUTES", "45"))

    # Edge thresholds
    MIN_EDGE: float = float(os.getenv("EVENTS_MIN_EDGE", "0.05"))

    # Liquidity floor
    MIN_LIQUIDITY: float = float(os.getenv("EVENTS_MIN_LIQUIDITY", "10000"))

    # Early exit thresholds (legacy, used as fallback)
    TAKE_PROFIT: float = float(os.getenv("EVENTS_TAKE_PROFIT", "0.30"))
    STOP_LOSS: float = float(os.getenv("EVENTS_STOP_LOSS", "0.25"))

    # Smart exit engine parameters
    TRAILING_STOP_DRAWDOWN: float = float(os.getenv("EVENTS_TRAILING_STOP_DRAWDOWN", "0.40"))
    HARD_STOP_LOSS: float = float(os.getenv("EVENTS_HARD_STOP_LOSS", "0.30"))
    TIME_EXIT_DAYS: int = int(os.getenv("EVENTS_TIME_EXIT_DAYS", "30"))
    MIN_EXIT_LIQUIDITY: float = float(os.getenv("EVENTS_MIN_EXIT_LIQUIDITY", "3000"))

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
