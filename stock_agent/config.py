"""Validated runtime settings for stock_agent.

How to add a setting (for peer modules):
1. Add one typed field under your area's section on ``Settings`` below.
2. If the environment variable name differs from the attribute name, add an
   entry to ``_ENV_ALIASES``; otherwise ``os.environ[FIELD_NAME]`` is read
   automatically when non-empty.
3. ``Config.FIELD_NAME`` and ``Config().FIELD_NAME`` mirror ``Settings``
   automatically — never maintain a second copy of defaults.
4. Any field whose name ends with ``_KEY``, ``_SECRET``, ``_TOKEN``, or
   ``_PASSWORD`` MUST default to ``""`` and is automatically masked in
   ``repr()``, ``redacted_summary()``, and registered for log redaction.
"""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PAPER_ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_ALPACA_BASE_URL = "https://api.alpaca.markets"
DEFAULT_ALPACA_DATA_URL = "https://data.alpaca.markets"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_DATA_DIR = _REPO_ROOT / "data" / "stock_agent"
_VPS_DATA_DIR = Path("/root/polymarket-bot/data/stock_agent")

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD")
_SECRET_EXTRA_NAMES = frozenset({"HEARTBEAT_PING_URL", "HEARTBEAT_URL"})
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})
_VALID_RESEARCH_PROVIDERS = frozenset({"perplexity", "openai", "anthropic"})

_DEFAULT_DISCORD_CHANNEL_ENV: tuple[tuple[str, str, str], ...] = (
    ("announcements", "DISCORD_CH_ANNOUNCEMENTS", "1488045682710413312"),
    ("portfolio", "DISCORD_CH_PORTFOLIO", "1488045688171266138"),
    ("thesis", "DISCORD_CH_THESIS", "1488045703929401505"),
    ("trades", "DISCORD_CH_TRADES", "1488045709113426080"),
    ("daily_pnl", "DISCORD_CH_DAILY_PNL", "1488045714905759895"),
    ("risk", "DISCORD_CH_RISK", "1488045720241049661"),
    ("market_monitor", "DISCORD_CH_MARKET", "1488045725949366435"),
    ("screener", "DISCORD_CH_SCREENER", "1488045736938442784"),
    ("analyst", "DISCORD_CH_ANALYST", "1488045742458404886"),
    ("macro", "DISCORD_CH_MACRO", "1488045747965395075"),
    ("system_logs", "DISCORD_CH_LOGS", "1488045758627315782"),
    ("trade_history", "DISCORD_CH_HISTORY", "1488045763995893760"),
    ("what_bot_did", "DISCORD_CH_WHAT_BOT_DID", "1488046586272419860"),
    ("weekly_eli5", "DISCORD_CH_WEEKLY_ELI5", "1488046604798787605"),
)

_DEFAULT_PRIORITY_SYMBOLS = ["ANET", "BK", "GS", "JPM"]

_DEFAULT_SYMBOL_CLUSTERS: dict[str, list[str]] = {
    "Semiconductors": [
        "NVDA", "AMD", "AVGO", "TSM", "ASML", "MU", "INTC", "AMAT", "LRCX",
        "KLAC", "MRVL", "QCOM", "TXN", "ON", "ARM", "MCHP", "NXPI", "ADI",
    ],
    "MegaCapTech": [
        "AAPL", "MSFT", "GOOGL", "GOOG", "META", "AMZN", "NVDA", "TSLA",
        "ORCL", "CRM", "ADBE",
    ],
    "ChinaADRs": ["BABA", "PDD", "JD", "BIDU", "NTES", "TCEHY", "NIO", "LI", "XPEV"],
    "Healthcare": [
        "LLY", "JNJ", "MRK", "PFE", "ABBV", "AMGN", "UNH", "BMY", "GILD",
        "ABT", "MDT", "TMO", "DHR", "ISRG", "REGN", "VRTX",
    ],
    "Financials": [
        "JPM", "BAC", "WFC", "GS", "MS", "C", "BRK.B", "V", "MA", "AXP",
        "BLK", "SCHW", "COF", "USB", "BK",
    ],
    "ConsumerDiscretionary": [
        "TJX", "AMZN", "HD", "LOW", "NKE", "SBUX", "MCD", "BKNG", "TSLA",
        "LULU", "ROST", "MELI",
    ],
    "Energy": ["XOM", "CVX", "COP", "OXY", "SLB", "EOG", "PXD", "MPC", "PSX"],
    "Industrials": [
        "ETN", "CAT", "DE", "HON", "UNP", "GE", "RTX", "LMT", "BA", "MMM",
        "EMR", "ITW",
    ],
}

_DEFAULT_SECTOR_MAP: dict[str, str] = {
    # Technology / Semiconductors
    "NVDA": "Technology", "AMD": "Technology", "AVGO": "Technology",
    "TSM": "Technology", "ASML": "Technology", "MU": "Technology",
    "INTC": "Technology", "AMAT": "Technology", "LRCX": "Technology",
    "KLAC": "Technology", "MRVL": "Technology", "QCOM": "Technology",
    "TXN": "Technology", "ON": "Technology", "ARM": "Technology",
    "ANET": "Technology",
    # Technology / Software & Platforms
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "GOOG": "Technology", "META": "Technology", "ORCL": "Technology",
    "CRM": "Technology", "ADBE": "Technology",
    # Consumer discretionary
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "TJX": "Consumer Discretionary", "HD": "Consumer Discretionary",
    "LOW": "Consumer Discretionary", "NKE": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary", "LULU": "Consumer Discretionary",
    "ROST": "Consumer Discretionary",
    # China ADRs
    "BABA": "Consumer Discretionary", "PDD": "Consumer Discretionary",
    "JD": "Consumer Discretionary", "MELI": "Consumer Discretionary",
    "BIDU": "Communication Services", "NTES": "Communication Services",
    # Healthcare
    "LLY": "Healthcare", "JNJ": "Healthcare", "MRK": "Healthcare",
    "PFE": "Healthcare", "ABBV": "Healthcare", "AMGN": "Healthcare",
    "UNH": "Healthcare", "BMY": "Healthcare", "GILD": "Healthcare",
    "ABT": "Healthcare", "MDT": "Healthcare", "TMO": "Healthcare",
    "DHR": "Healthcare", "ISRG": "Healthcare", "REGN": "Healthcare",
    "VRTX": "Healthcare",
    # Financials
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "MS": "Financials", "C": "Financials",
    "BRK.B": "Financials", "V": "Financials", "MA": "Financials",
    "AXP": "Financials", "BLK": "Financials", "SCHW": "Financials",
    "BK": "Financials", "COF": "Financials", "USB": "Financials",
    # Industrials
    "ETN": "Industrials", "CAT": "Industrials", "DE": "Industrials",
    "HON": "Industrials", "UNP": "Industrials", "GE": "Industrials",
    "RTX": "Industrials", "LMT": "Industrials", "BA": "Industrials",
    "MMM": "Industrials", "EMR": "Industrials", "ITW": "Industrials",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "OXY": "Energy",
    "SLB": "Energy", "EOG": "Energy",
}

_DEFAULT_CONVICTION_SIZE_MAP: dict[int, float] = {
    7: 0.05,
    8: 0.07,
    9: 0.08,
    10: 0.10,
}

# Field name -> environment variable name when they differ.
_ENV_ALIASES: dict[str, str] = {
    "MODE": "STOCK_AGENT_MODE",
    "DATA_DIR": "STOCK_DATA_DIR",
    "LOG_LEVEL": "STOCK_LOG_LEVEL",
    "HEARTBEAT_URL": "STOCK_HEARTBEAT_URL",
}


class SettingsError(ValueError):
    """Plain-language startup configuration error."""


def _default_data_dir() -> Path:
    try:
        return _VPS_DATA_DIR if _VPS_DATA_DIR.exists() else _LOCAL_DATA_DIR
    except (PermissionError, OSError):
        return _LOCAL_DATA_DIR


def _default_discord_channels() -> dict[str, str]:
    return {key: default for key, _, default in _DEFAULT_DISCORD_CHANNEL_ENV}


def _is_secret_field(name: str) -> bool:
    return name.endswith(_SECRET_SUFFIXES) or name in _SECRET_EXTRA_NAMES


def _parse_strict_bool(value: Any, env_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in _TRUE_STRINGS:
            return True
        if cleaned in _FALSE_STRINGS:
            return False
    raise SettingsError(
        f"Invalid boolean in {env_name}={value!r}: expected true/false, 1/0, yes/no, or on/off."
    )


def _pause_file_exists(data_dir: Path) -> bool:
    candidates = [data_dir / "PAUSED"]
    if data_dir.parent.name == "data":
        candidates.append(data_dir.parent / "PAUSED")
    for candidate in candidates:
        try:
            if candidate.exists():
                return True
        except OSError:
            continue
    return False


class Settings(BaseModel):
    """Single validated settings object loaded at startup."""

    model_config = ConfigDict(extra="ignore", validate_default=True)

    _BOOL_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "STOCK_AGENT_LIVE_CONFIRMED",
        "STOCK_TRADING_PAUSED",
        "RISK_PAUSE_NEW_BUYS",
        "MARGIN_ENABLED",
        "EARNINGS_CHECK_ENABLED",
        "FMP_QUOTES_ENABLED",
        "TIPRANKS_ENABLED",
        "OPTIONS_ENABLED",
        "RESEARCH_SHARE_NOTES",
        "RESEARCH_SHADOW_WEEKLY_DEEP",
        "RESEARCH_SHADOW_BUY_SECOND_OPINION",
    })

    # ── Broker, mode & safety locks (owned by security_config / broker_execution) ──
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    ALPACA_BASE_URL: str = PAPER_ALPACA_BASE_URL
    ALPACA_DATA_URL: str = DEFAULT_ALPACA_DATA_URL
    ALPACA_DATA_FEED: str = "iex"
    MODE: Literal["PAPER", "LIVE"] = "PAPER"
    STOCK_AGENT_LIVE_CONFIRMED: bool = False
    STOCK_TRADING_PAUSED: bool = False

    # ── Research pipeline & LLM routes (owned by research_pipeline) ────────────────
    PERPLEXITY_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    RESEARCH_PROVIDER_ORDER: tuple[str, ...] = ("perplexity", "openai", "anthropic")
    RESEARCH_PERPLEXITY_MODEL: str = "anthropic/claude-sonnet-5-5"
    RESEARCH_OPENAI_MODEL: str = "gpt-6-luna"
    RESEARCH_ANTHROPIC_MODEL: str = "claude-sonnet-5-5"
    RESEARCH_OPENAI_CONFIRM_MODEL: str = "gpt-5.6-luna"
    RESEARCH_ANTHROPIC_CONFIRM_MODEL: str = "claude-haiku-5-5"
    RESEARCH_PERPLEXITY_CONFIRM_MODEL: str = "openai/gpt-6-luna"
    RESEARCH_THESIS_MAX_AGE_H: int = Field(default=96, ge=1)
    RESEARCH_MAX_ADJUSTMENT: int = Field(default=1, ge=0, le=1)
    RESEARCH_WEEKLY_BUDGET_USD: float = Field(default=15.0, ge=0.0)
    RESEARCH_EXIT_RESERVE_PCT: int = Field(default=20, ge=0, lt=100)
    RESEARCH_MAX_MODEL_EXITS_PER_SESSION: int = Field(default=4, ge=1)
    RESEARCH_SHARE_NOTES: bool = False
    RESEARCH_SHADOW_WEEKLY_DEEP: bool = False
    RESEARCH_SHADOW_BUY_SECOND_OPINION: bool = False

    # ── News & market data providers (owned by news_data) ──────────────────────────
    FMP_API_KEY: str = ""
    FMP_QUOTES_ENABLED: bool = True
    FINNHUB_API_KEY: str = ""
    SEC_USER_AGENT: str = ""
    TIPRANKS_ENABLED: bool = True
    TIPRANKS_MIN_SMART_SCORE: int = Field(default=8, ge=1, le=10)
    TIPRANKS_UNIVERSE_LIMIT: int = Field(default=100, ge=1, le=10_000)
    TIPRANKS_UNIVERSE_SLICE: int = Field(default=20, ge=0, le=10_000)
    PRIORITY_SYMBOLS: list[str] = Field(default_factory=lambda: list(_DEFAULT_PRIORITY_SYMBOLS))
    MIN_MARKET_CAP: int = Field(default=10_000_000_000, ge=0)
    UNIVERSE_SIZE: int = Field(default=50, ge=1)
    DEEP_ANALYSIS_SIZE: int = Field(default=20, ge=1)

    # ── Alerts & reporting (owned by alerts_reporting) ─────────────────────────────
    DISCORD_BOT_TOKEN: str = ""
    DISCORD_CHANNELS: dict[str, str] = Field(default_factory=_default_discord_channels)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    HEARTBEAT_URL: str = ""
    HEARTBEAT_PING_URL: str = ""
    HEARTBEAT_INTERVAL_SEC: int = Field(default=300, ge=1)
    ALERT_CRITICAL_REMIND_SEC: int = Field(default=3600, ge=1)
    ALERT_WARNING_COOLDOWN_SEC: int = Field(default=14400, ge=0)
    ALERT_INFO_COOLDOWN_SEC: int = Field(default=3600, ge=0)
    ALERT_OUTBOX_MAX_ITEMS: int = Field(default=200, ge=1)

    # ── Risk limits (owned by risk_limits) ─────────────────────────────────────────
    MAX_POSITION_PCT: float = Field(default=0.15, gt=0.0, le=1.0)
    MARGIN_ENABLED: bool = True
    MAX_GROSS_EXPOSURE_PCT: float = Field(default=2.00, gt=0.0, le=2.0)
    MARGIN_MIN_EXCESS_PCT: float = Field(default=0.25, gt=0.0, lt=1.0)
    MARGIN_TRIM_EXCESS_PCT: float = Field(default=0.15, gt=0.0, lt=1.0)
    MARGIN_INTEREST_RATE_PCT: float = Field(default=6.5, ge=0.0, le=100.0)
    MAX_POSITIONS: int = Field(default=20, ge=1)
    MAX_NEW_STOCK_POSITIONS_PER_SESSION: int = Field(default=3, ge=1)
    MAX_SECTOR_PCT: float = Field(default=0.40, gt=0.0, le=1.0)
    MAX_CLUSTER_PCT: float = Field(default=0.48, gt=0.0, le=1.0)
    MIN_FREE_CASH_PCT: float = Field(default=0.02, ge=0.0, lt=1.0)
    MAX_INITIAL_STOP_RISK_PCT: float = Field(default=0.01, gt=0.0, le=1.0)
    CONVICTION_MAX_ADJUSTMENT: int = Field(default=1, ge=0, le=1)
    TOP_UP_TARGET_TOLERANCE: float = Field(default=0.90, gt=0.0, le=1.0)
    MAX_ENTRY_SIGNAL_HOURS: int = Field(default=96, ge=1)
    MAX_ENTRY_PRICE_DRIFT_PCT: float = Field(default=0.03, gt=0.0, lt=1.0)
    MAX_ENTRY_QUOTE_AGE_SECONDS: int = Field(default=60, ge=1)
    RISK_DRIFT_BUFFER_PCT: float = Field(default=0.02, ge=0.0, lt=1.0)
    RISK_TRIM_HEADROOM_PCT: float = Field(default=0.02, ge=0.0, lt=1.0)
    RISK_DRIFT_CONFIRM_SESSIONS: int = Field(default=2, ge=1)
    RISK_PAUSE_NEW_BUYS: bool = False
    DRAWDOWN_ALERT_PCT: float = Field(default=0.10, gt=0.0, lt=1.0)
    DRAWDOWN_THROTTLE_PCT: float = Field(default=0.15, gt=0.0, lt=1.0)
    DRAWDOWN_PAUSE_PCT: float = Field(default=0.20, gt=0.0, lt=1.0)
    DRAWDOWN_THROTTLE_SCALE: float = Field(default=0.50, gt=0.0, le=1.0)
    DRAWDOWN_RECOVERY_SESSIONS: int = Field(default=5, ge=1, le=10_000)
    DRAWDOWN_ROLLING_SESSIONS: int = Field(default=252, ge=2, le=10_000)
    DRAWDOWN_PAUSE_MAX_SESSIONS: int = Field(default=21, ge=1, le=10_000)
    RISK_OPTION_MAX_LOSS_PCT: float = Field(default=0.03, gt=0.0, le=1.0)
    RISK_OPTION_TOTAL_LOSS_PCT: float = Field(default=0.10, gt=0.0, le=1.0)
    RISK_OPTION_MAX_STRUCTURES: int = Field(default=6, ge=1)
    MIN_CONVICTION: int = Field(default=7, ge=1, le=10)
    CONVICTION_SIZE_MAP: dict[int, float] = Field(
        default_factory=lambda: dict(_DEFAULT_CONVICTION_SIZE_MAP)
    )
    STOP_ATR_PERIOD: int = Field(default=14, ge=1)
    STOP_ATR_MULTIPLE: float = Field(default=2.5, gt=0.0)
    STOP_LOSS_PCT_MIN: float = Field(default=0.07, gt=0.0, lt=1.0)
    STOP_LOSS_PCT_MAX: float = Field(default=0.12, gt=0.0, lt=1.0)
    STOP_LOSS_PCT: float = Field(default=0.07, gt=0.0, lt=1.0)
    SYMBOL_CLUSTERS: dict[str, list[str]] = Field(
        default_factory=lambda: copy.deepcopy(_DEFAULT_SYMBOL_CLUSTERS)
    )
    SECTOR_MAP: dict[str, str] = Field(default_factory=lambda: dict(_DEFAULT_SECTOR_MAP))

    # ── Options (owned by options) ─────────────────────────────────────────────────
    OPTIONS_MODE: Literal["defined_risk", "close_only", "off"] = "defined_risk"
    OPTIONS_ENABLED: bool = True
    OPTIONS_MAX_POSITIONS: int = Field(default=10, ge=1)
    OPTIONS_MAX_SINGLE_TRADE_PCT: float = Field(default=0.07, gt=0.0, le=1.0)
    OPTIONS_MAX_TOTAL_EXPOSURE_PCT: float = Field(default=0.15, gt=0.0, le=1.0)
    OPTIONS_AUTO_CLOSE_PROFIT_PCT: float = Field(default=0.50, gt=0.0)
    OPTIONS_AUTO_CLOSE_DTE: int = Field(default=3, ge=0)
    OPTIONS_MIN_DTE: int = Field(default=14, ge=1)
    OPTIONS_MAX_DTE: int = Field(default=45, ge=1)
    OPTIONS_MIN_PREMIUM_PCT: float = Field(default=0.005, ge=0.0)
    OPTIONS_SPREAD_WIDTH_MIN: float = Field(default=5.0, gt=0.0)
    OPTIONS_SPREAD_WIDTH_MAX: float = Field(default=10.0, gt=0.0)
    OPTIONS_MIN_RR_RATIO: float = Field(default=2.0, gt=0.0)

    # ── Schedule, paths & logging (owned by scheduler_deploy / security_config) ────
    WEEKLY_ANALYSIS_DAY: int = Field(default=6, ge=0, le=6)
    MIDWEEK_ANALYSIS_DAY: int = Field(default=2, ge=0, le=6)
    SCAN_INTERVAL_MINUTES: int = Field(default=30, ge=1)
    EARNINGS_CHECK_ENABLED: bool = True
    EARNINGS_LOOKBACK_DAYS: int = Field(default=2, ge=1)
    DATA_DIR: Path = Field(default_factory=_default_data_dir)
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("MODE", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("ALPACA_BASE_URL", "ALPACA_DATA_URL", mode="before")
    @classmethod
    def _normalize_url(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().rstrip("/")
        return value

    @field_validator("OPTIONS_MODE", mode="before")
    @classmethod
    def _normalize_options_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("PRIORITY_SYMBOLS", mode="before")
    @classmethod
    def _parse_priority_symbols(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            items = [part.strip().upper() for part in value.split(",") if part.strip()]
            return items or list(_DEFAULT_PRIORITY_SYMBOLS)
        if isinstance(value, (list, tuple)):
            return [str(part).strip().upper() for part in value if str(part).strip()]
        raise ValueError("PRIORITY_SYMBOLS must be a comma-separated string or list of symbols")

    @field_validator("RESEARCH_PROVIDER_ORDER", mode="before")
    @classmethod
    def _parse_provider_order(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            items = tuple(part.strip().lower() for part in value.split(",") if part.strip())
        elif isinstance(value, (list, tuple)):
            items = tuple(str(part).strip().lower() for part in value if str(part).strip())
        else:
            raise ValueError("RESEARCH_PROVIDER_ORDER must be comma-separated provider names")
        if not items:
            raise ValueError("RESEARCH_PROVIDER_ORDER must name at least one provider")
        unknown = [item for item in items if item not in _VALID_RESEARCH_PROVIDERS]
        if unknown:
            raise ValueError(
                f"Unknown provider(s) in RESEARCH_PROVIDER_ORDER: {', '.join(unknown)}"
            )
        return items

    @field_validator("DATA_DIR", mode="before")
    @classmethod
    def _parse_data_dir(cls, value: Any) -> Path:
        if value is None or value == "":
            return _default_data_dir()
        path = Path(value)
        return path if path.name else _default_data_dir()

    @field_validator(
        "DRAWDOWN_ROLLING_SESSIONS",
        "DRAWDOWN_PAUSE_MAX_SESSIONS",
        "DRAWDOWN_RECOVERY_SESSIONS",
        "TIPRANKS_MIN_SMART_SCORE",
        "TIPRANKS_UNIVERSE_LIMIT",
        "TIPRANKS_UNIVERSE_SLICE",
        mode="before",
    )
    @classmethod
    def _parse_strict_session_count(cls, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("must be a whole integer, not a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned and (
                cleaned.isdigit()
                or (cleaned[0] in "+-" and len(cleaned) > 1 and cleaned[1:].isdigit())
            ):
                return int(cleaned)
        raise ValueError("must be a whole integer")

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        if self.MODE == "PAPER":
            if self.ALPACA_BASE_URL != PAPER_ALPACA_BASE_URL:
                raise ValueError(
                    "STOCK_AGENT_MODE is PAPER, so ALPACA_BASE_URL must be "
                    f"{PAPER_ALPACA_BASE_URL} (got {self.ALPACA_BASE_URL!r})."
                )
            if self.STOCK_AGENT_LIVE_CONFIRMED:
                raise ValueError(
                    "STOCK_AGENT_LIVE_CONFIRMED=true cannot be set while STOCK_AGENT_MODE is PAPER."
                )
        elif self.MODE == "LIVE":
            if self.ALPACA_BASE_URL != LIVE_ALPACA_BASE_URL:
                raise ValueError(
                    "STOCK_AGENT_MODE is LIVE, so ALPACA_BASE_URL must be explicitly set to "
                    f"{LIVE_ALPACA_BASE_URL} (got {self.ALPACA_BASE_URL!r})."
                )
            if not self.STOCK_AGENT_LIVE_CONFIRMED:
                raise ValueError(
                    "STOCK_AGENT_MODE is LIVE, which requires explicit second confirmation "
                    "STOCK_AGENT_LIVE_CONFIRMED=true."
                )

        if "OPTIONS_ENABLED" in self.model_fields_set and "OPTIONS_MODE" not in self.model_fields_set:
            object.__setattr__(
                self,
                "OPTIONS_MODE",
                "defined_risk" if self.OPTIONS_ENABLED else "off",
            )
        else:
            object.__setattr__(self, "OPTIONS_ENABLED", self.OPTIONS_MODE != "off")

        if not (self.STOP_LOSS_PCT_MIN <= self.STOP_LOSS_PCT <= self.STOP_LOSS_PCT_MAX):
            raise ValueError(
                "STOP_LOSS_PCT_MIN, STOP_LOSS_PCT, and STOP_LOSS_PCT_MAX must satisfy "
                "STOP_LOSS_PCT_MIN <= STOP_LOSS_PCT <= STOP_LOSS_PCT_MAX."
            )
        if not (self.DRAWDOWN_ALERT_PCT < self.DRAWDOWN_THROTTLE_PCT < self.DRAWDOWN_PAUSE_PCT):
            raise ValueError(
                "Drawdown thresholds must satisfy "
                "DRAWDOWN_ALERT_PCT < DRAWDOWN_THROTTLE_PCT < DRAWDOWN_PAUSE_PCT."
            )
        if self.CONVICTION_SIZE_MAP and max(self.CONVICTION_SIZE_MAP.values()) > self.MAX_POSITION_PCT:
            raise ValueError(
                "CONVICTION_SIZE_MAP tiers must not exceed MAX_POSITION_PCT."
            )
        if self.MARGIN_TRIM_EXCESS_PCT >= self.MARGIN_MIN_EXCESS_PCT:
            raise ValueError("MARGIN_TRIM_EXCESS_PCT must be less than MARGIN_MIN_EXCESS_PCT.")
        return self

    @property
    def effective_max_gross_exposure_pct(self) -> float:
        return self.MAX_GROSS_EXPOSURE_PCT if self.MARGIN_ENABLED else 1.0

    @property
    def trading_base_url(self) -> str:
        """Single validated broker trading URL for both equities and options."""
        return self.ALPACA_BASE_URL

    @property
    def market_data_url(self) -> str:
        """Single validated broker market-data URL."""
        return self.ALPACA_DATA_URL

    @property
    def LIVE_TRADING_CONFIRMED(self) -> bool:
        return self.STOCK_AGENT_LIVE_CONFIRMED

    def is_trading_paused(self, data_dir: Path | None = None) -> bool:
        """True when new entries are paused by env switch or local PAUSED file."""
        if self.STOCK_TRADING_PAUSED or self.RISK_PAUSE_NEW_BUYS:
            return True
        return _pause_file_exists(data_dir if data_dir is not None else self.DATA_DIR)

    def secret_values(self) -> tuple[str, ...]:
        """Non-empty configured secret values for log redaction (never print)."""
        values: list[str] = []
        for name in type(self).model_fields:
            if _is_secret_field(name):
                val = getattr(self, name, "")
                if isinstance(val, str) and val:
                    values.append(val)
        return tuple(values)

    def redacted_summary(self) -> dict[str, Any]:
        """Safe startup summary with zero secret values."""
        return {
            "mode": self.MODE,
            "trading_base_url": self.trading_base_url,
            "market_data_url": self.market_data_url,
            "live_confirmed": self.STOCK_AGENT_LIVE_CONFIRMED,
            "options_mode": self.OPTIONS_MODE,
            "trading_paused": self.is_trading_paused(),
            "log_level": self.LOG_LEVEL,
            "data_dir": str(self.DATA_DIR),
            "providers_configured": {
                "alpaca": bool(self.ALPACA_API_KEY and self.ALPACA_SECRET_KEY),
                "perplexity": bool(self.PERPLEXITY_API_KEY),
                "openai": bool(self.OPENAI_API_KEY),
                "anthropic": bool(self.ANTHROPIC_API_KEY),
                "fmp": bool(self.FMP_API_KEY),
                "finnhub": bool(self.FINNHUB_API_KEY),
                "tipranks": bool(self.TIPRANKS_ENABLED),
                "discord": bool(self.DISCORD_BOT_TOKEN),
                "telegram": bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID),
                "heartbeat": bool(self.HEARTBEAT_PING_URL or self.HEARTBEAT_URL),
            },
            "limits": {
                "max_position_pct": self.MAX_POSITION_PCT,
                "max_sector_pct": self.MAX_SECTOR_PCT,
                "max_cluster_pct": self.MAX_CLUSTER_PCT,
                "margin_enabled": self.MARGIN_ENABLED,
                "max_gross_exposure_pct": self.effective_max_gross_exposure_pct,
                "margin_min_excess_pct": self.MARGIN_MIN_EXCESS_PCT,
                "margin_trim_excess_pct": self.MARGIN_TRIM_EXCESS_PCT,
                "margin_interest_rate_pct": self.MARGIN_INTEREST_RATE_PCT,
                "drawdown_alert_pct": self.DRAWDOWN_ALERT_PCT,
                "drawdown_throttle_pct": self.DRAWDOWN_THROTTLE_PCT,
                "drawdown_pause_pct": self.DRAWDOWN_PAUSE_PCT,
                "drawdown_recovery_sessions": self.DRAWDOWN_RECOVERY_SESSIONS,
                "drawdown_rolling_sessions": self.DRAWDOWN_ROLLING_SESSIONS,
                "drawdown_pause_max_sessions": self.DRAWDOWN_PAUSE_MAX_SESSIONS,
                "max_positions": self.MAX_POSITIONS,
                "max_new_stock_positions_per_session": self.MAX_NEW_STOCK_POSITIONS_PER_SESSION,
                "min_conviction": self.MIN_CONVICTION,
                "research_weekly_budget_usd": self.RESEARCH_WEEKLY_BUDGET_USD,
            },
        }

    def __repr__(self) -> str:
        parts: list[str] = []
        for name in type(self).model_fields:
            val = getattr(self, name)
            if _is_secret_field(name):
                shown = "'<REDACTED>'" if val else "''"
            else:
                shown = repr(val)
            parts.append(f"{name}={shown}")
        return f"Settings({', '.join(parts)})"

    __str__ = __repr__


def _format_validation_error(err: ValidationError) -> str:
    messages: list[str] = []
    for item in err.errors():
        loc = ".".join(str(part) for part in item.get("loc", ()))
        env_name = _ENV_ALIASES.get(loc, loc) if loc else "configuration"
        msg = item.get("msg", "invalid value")
        if msg.startswith("Value error, "):
            msg = msg.removeprefix("Value error, ")
        if env_name and env_name not in msg:
            messages.append(f"{env_name}: {msg}")
        else:
            messages.append(msg)
    return "Invalid configuration: " + "; ".join(messages)


def load_settings(
    environ: Mapping[str, str] | None = None,
    *,
    env_file: Path | None = None,
    load_dotenv_file: bool = False,
    require_runtime_secrets: bool = False,
    warn_retired: bool = False,
) -> Settings:
    """Load and validate ``Settings`` from ``environ`` (and optional ``.env`` file).

    Unknown extra environment variables (including legacy variables on the server)
    are ignored so start-up succeeds with today's ``.env`` unchanged.
    """
    merged: dict[str, str] = {}
    dotenv_path = env_file if env_file is not None else (_REPO_ROOT / ".env" if load_dotenv_file else None)
    if dotenv_path is not None and dotenv_path.is_file():
        for k, v in dotenv_values(dotenv_path).items():
            if k and v is not None:
                merged[k] = v

    raw_env = os.environ if environ is None else environ
    merged.update({str(k): str(v) for k, v in raw_env.items()})

    raw_values: dict[str, Any] = {}

    channels: dict[str, str] = {}
    any_channel_override = False
    for key, env_var, default_id in _DEFAULT_DISCORD_CHANNEL_ENV:
        val = merged.get(env_var, "").strip()
        if val:
            channels[key] = val
            any_channel_override = True
        else:
            channels[key] = default_id
    if any_channel_override:
        raw_values["DISCORD_CHANNELS"] = channels

    # OPTIONS_MODE takes precedence; legacy OPTIONS_ENABLED=false maps to "off".
    raw_opt_mode = merged.get("OPTIONS_MODE", "").strip()
    raw_opt_enabled = merged.get("OPTIONS_ENABLED", "").strip()
    if raw_opt_mode:
        raw_values["OPTIONS_MODE"] = raw_opt_mode
    elif raw_opt_enabled:
        enabled = _parse_strict_bool(raw_opt_enabled, "OPTIONS_ENABLED")
        raw_values["OPTIONS_MODE"] = "defined_risk" if enabled else "off"

    for field_name, field_info in Settings.model_fields.items():
        if field_name in {"DISCORD_CHANNELS", "OPTIONS_MODE", "OPTIONS_ENABLED"}:
            continue
        env_name = _ENV_ALIASES.get(field_name, field_name)
        if env_name not in merged:
            if field_name == "HEARTBEAT_URL" and "HEARTBEAT_URL" in merged:
                env_name = "HEARTBEAT_URL"
            else:
                continue

        raw_str = merged[env_name]
        if _is_secret_field(field_name) or field_info.annotation is str:
            if raw_str != "" or _is_secret_field(field_name) or field_name == "TELEGRAM_CHAT_ID":
                raw_values[field_name] = raw_str.strip() if not _is_secret_field(field_name) else raw_str
            continue

        if raw_str.strip() == "":
            continue

        if field_name in Settings._BOOL_FIELDS:
            raw_values[field_name] = _parse_strict_bool(raw_str, env_name)
        else:
            raw_values[field_name] = raw_str

    try:
        settings = Settings(**raw_values)
    except ValidationError as exc:
        raise SettingsError(_format_validation_error(exc)) from exc

    if require_runtime_secrets:
        missing: list[str] = []
        if not settings.ALPACA_API_KEY.strip():
            missing.append("ALPACA_API_KEY")
        if not settings.ALPACA_SECRET_KEY.strip():
            missing.append("ALPACA_SECRET_KEY")
        if missing:
            raise SettingsError(
                f"Missing required broker setting(s): {', '.join(missing)}. "
                "Set them in the environment or .env before starting stock_agent."
            )

    if warn_retired and "MAX_TOTAL_EXPOSURE_PCT" in merged:
        logging.getLogger(__name__).warning(
            "MAX_TOTAL_EXPOSURE_PCT is retired and ignored; use MAX_GROSS_EXPOSURE_PCT."
        )
    return settings


class _ConfigMeta(type):
    """Keeps ``Config.trading_base_url`` and ``Config.market_data_url`` synced with ``Config``."""

    @property
    def trading_base_url(cls) -> str:
        return str(getattr(cls, "ALPACA_BASE_URL", PAPER_ALPACA_BASE_URL)).rstrip("/")

    @property
    def market_data_url(cls) -> str:
        return str(getattr(cls, "ALPACA_DATA_URL", DEFAULT_ALPACA_DATA_URL)).rstrip("/")

    @property
    def LIVE_TRADING_CONFIRMED(cls) -> bool:
        return bool(getattr(cls, "STOCK_AGENT_LIVE_CONFIRMED", False))

    @property
    def effective_max_gross_exposure_pct(cls) -> float:
        return cls.MAX_GROSS_EXPOSURE_PCT if cls.MARGIN_ENABLED else 1.0


class Config(metaclass=_ConfigMeta):
    """Backward-compatible wrapper around ``Settings``.

    Supports both ``Config.X`` class-attribute access (including ``monkeypatch.setattr(Config, ...)``
    in ``FakeWorld``) and ``Config()`` / ``Config(**overrides)`` validated instance access.
    """

    def __init__(self, **overrides: Any) -> None:
        seed: dict[str, Any] = {}
        cls = type(self)
        for name in Settings.model_fields:
            val = getattr(cls, name)
            if isinstance(val, (dict, list)):
                val = copy.deepcopy(val)
            seed[name] = val
        seed.update(overrides)
        try:
            validated = Settings(**seed)
        except ValidationError as exc:
            raise SettingsError(_format_validation_error(exc)) from exc
        self._settings = validated
        for name in Settings.model_fields:
            val = getattr(validated, name)
            if isinstance(val, (dict, list)):
                val = copy.deepcopy(val)
            object.__setattr__(self, name, val)

    @property
    def trading_base_url(self) -> str:
        return str(self.ALPACA_BASE_URL).rstrip("/")

    @property
    def market_data_url(self) -> str:
        return str(self.ALPACA_DATA_URL).rstrip("/")

    @property
    def LIVE_TRADING_CONFIRMED(self) -> bool:
        return bool(self.STOCK_AGENT_LIVE_CONFIRMED)

    @property
    def effective_max_gross_exposure_pct(self) -> float:
        return self.MAX_GROSS_EXPOSURE_PCT if self.MARGIN_ENABLED else 1.0

    def is_trading_paused(self, data_dir: Path | None = None) -> bool:
        if self.STOCK_TRADING_PAUSED or self.RISK_PAUSE_NEW_BUYS:
            return True
        return _pause_file_exists(data_dir if data_dir is not None else Path(self.DATA_DIR))

    def secret_values(self) -> tuple[str, ...]:
        values: list[str] = []
        for name in Settings.model_fields:
            if _is_secret_field(name):
                val = getattr(self, name, "")
                if isinstance(val, str) and val:
                    values.append(val)
        return tuple(values)

    def redacted_summary(self) -> dict[str, Any]:
        current = Settings(**{name: getattr(self, name) for name in Settings.model_fields})
        return current.redacted_summary()

    def __repr__(self) -> str:
        parts: list[str] = []
        for name in Settings.model_fields:
            val = getattr(self, name, None)
            if _is_secret_field(name):
                shown = "'<REDACTED>'" if val else "''"
            else:
                shown = repr(val)
            parts.append(f"{name}={shown}")
        return f"Config({', '.join(parts)})"

    __str__ = __repr__


def _sync_config_class(settings: Settings) -> None:
    for name in Settings.model_fields:
        val = getattr(settings, name)
        if isinstance(val, (dict, list)):
            val = copy.deepcopy(val)
        setattr(Config, name, val)


_DEFAULT_SETTINGS = load_settings()
_sync_config_class(_DEFAULT_SETTINGS)

__all__ = [
    "Config",
    "DEFAULT_ALPACA_DATA_URL",
    "LIVE_ALPACA_BASE_URL",
    "PAPER_ALPACA_BASE_URL",
    "Settings",
    "SettingsError",
    "load_settings",
]
