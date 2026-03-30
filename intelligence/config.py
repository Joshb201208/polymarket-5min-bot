"""Intelligence module configuration — loads from env vars."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _bool_env(key: str, default: str = "true") -> bool:
    return os.getenv(key, default).lower() == "true"


# Master enable/disable for each module (module-level for easy import)
INTELLIGENCE_MODULES = {
    "x_scanner": _bool_env("X_SCANNER_ENABLED"),
    "orderbook": _bool_env("ORDERBOOK_INTEL_ENABLED"),
    "metaculus": _bool_env("METACULUS_ENABLED"),
    "google_trends": _bool_env("GOOGLE_TRENDS_ENABLED"),
    "congress": _bool_env("CONGRESS_TRACKER_ENABLED"),
    "cross_market": _bool_env("CROSS_MARKET_ENABLED"),
    "whale_tracker": _bool_env("WHALE_TRACKER_ENABLED"),
    "reference_price": _bool_env("REFERENCE_PRICE_ENABLED"),
}

# Composite Scoring
COMPOSITE_MIN_SCORE = float(os.getenv("COMPOSITE_MIN_SCORE", "0.4"))
COMPOSITE_DIRECTION_BONUS = float(os.getenv("COMPOSITE_DIRECTION_BONUS", "0.2"))

# Signal source weights (sum to 1.0; x_scanner zeroed, weight redistributed)
SOURCE_WEIGHTS = {
    "reference_price": 0.25,
    "metaculus": 0.20,
    "x_scanner": 0.00,
    "orderbook": 0.15,
    "whale_tracker": 0.15,
    "google_trends": 0.10,
    "congress": 0.07,
    "cross_market": 0.08,
}


class IntelligenceConfig:
    """Central configuration for all intelligence modules."""

    # Master enable/disable for each module
    MODULES_ENABLED: dict = {
        "x_scanner": _bool_env("X_SCANNER_ENABLED"),
        "orderbook": _bool_env("ORDERBOOK_INTEL_ENABLED"),
        "metaculus": _bool_env("METACULUS_ENABLED"),
        "google_trends": _bool_env("GOOGLE_TRENDS_ENABLED"),
        "congress": _bool_env("CONGRESS_TRACKER_ENABLED"),
        "cross_market": _bool_env("CROSS_MARKET_ENABLED"),
        "whale_tracker": _bool_env("WHALE_TRACKER_ENABLED"),
        "reference_price": _bool_env("REFERENCE_PRICE_ENABLED"),
    }

    # X/Twitter Scanner
    TWITTER_BEARER_TOKEN: str = os.getenv("TWITTER_BEARER_TOKEN", "")
    X_MIN_FOLLOWERS: int = int(os.getenv("X_MIN_FOLLOWERS", "1000"))
    X_SENTIMENT_VELOCITY_THRESHOLD: float = float(
        os.getenv("X_SENTIMENT_VELOCITY_THRESHOLD", "0.3")
    )

    # Orderbook Intelligence
    ORDERBOOK_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    WHALE_TRADE_THRESHOLD: float = float(os.getenv("ORDERBOOK_WHALE_THRESHOLD", "5000"))
    IMBALANCE_RATIO: float = float(os.getenv("ORDERBOOK_IMBALANCE_RATIO", "3.0"))
    SHARP_MOVE_PCT: float = float(os.getenv("ORDERBOOK_SHARP_MOVE_PCT", "0.03"))
    SPREAD_OPPORTUNITY_THRESHOLD: float = float(
        os.getenv("ORDERBOOK_SPREAD_THRESHOLD", "0.05")
    )
    VOLUME_SPIKE_MULTIPLIER: float = float(
        os.getenv("ORDERBOOK_VOLUME_SPIKE_MULTIPLIER", "3.0")
    )

    # Metaculus
    METACULUS_BASE_URL: str = "https://www.metaculus.com/api2"
    METACULUS_DIVERGENCE_THRESHOLD: float = float(
        os.getenv("METACULUS_DIVERGENCE_THRESHOLD", "0.05")
    )
    METACULUS_FUZZY_THRESHOLD: int = int(os.getenv("METACULUS_FUZZY_THRESHOLD", "40"))

    # Google Trends
    GOOGLE_TRENDS_VELOCITY_THRESHOLD: float = float(
        os.getenv("GOOGLE_TRENDS_VELOCITY_THRESHOLD", "2.0")
    )
    GOOGLE_TRENDS_MAX_QUERIES: int = int(
        os.getenv("GOOGLE_TRENDS_MAX_QUERIES", "10")
    )

    # Congress/Government Tracker
    CONGRESS_API_KEY: str = os.getenv("CONGRESS_API_KEY", "")
    CONGRESS_API_BASE: str = "https://api.congress.gov/v3"
    FEDERAL_REGISTER_API: str = "https://www.federalregister.gov/api/v1"

    # Cross-Market Arbitrage
    CROSS_MARKET_DIVERGENCE_THRESHOLD: float = float(
        os.getenv("CROSS_MARKET_DIVERGENCE_THRESHOLD", "0.03")
    )

    # Whale Tracker
    POLYGONSCAN_API_KEY: str = os.getenv("POLYGONSCAN_API_KEY", "")
    POLYGONSCAN_API: str = "https://api.polygonscan.com/api"
    CTF_CONTRACT: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    WHALE_MIN_TRADE_SIZE: float = float(os.getenv("WHALE_MIN_TRADE_SIZE", "5000"))
    WHALE_CONSENSUS_THRESHOLD: int = int(os.getenv("WHALE_CONSENSUS_THRESHOLD", "2"))

    # Scan timeout (seconds) for each module
    MODULE_TIMEOUT: int = int(os.getenv("INTELLIGENCE_MODULE_TIMEOUT", "30"))

    # Paths
    PROJECT_ROOT: Path = _PROJECT_ROOT
    DATA_DIR: Path = _PROJECT_ROOT / "data"

    # API URLs (shared)
    GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"

    def is_enabled(self, module_name: str) -> bool:
        """Check if a module is enabled."""
        return self.MODULES_ENABLED.get(module_name, False)

    def ensure_data_dir(self) -> None:
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
