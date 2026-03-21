"""
config.py - Configuration and environment variable management for Polymarket trading bot.

Loads all settings from environment variables (via .env file) and provides
typed, validated configuration objects for all modules.
"""

import os
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("LOG_FILE", "polymarket_bot.log")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: parse boolean env vars
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        logger.warning("Could not parse %s as float; using default %s", key, default)
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        logger.warning("Could not parse %s as int; using default %s", key, default)
        return default


def _env_list(key: str, default: List[str]) -> List[str]:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    return [item.strip().upper() for item in val.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Polymarket Credentials
# ---------------------------------------------------------------------------

@dataclass
class PolymarketCredentials:
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str
    funder_address: str
    signature_type: int = 2  # 0=EOA, 1=Magic, 2=Gnosis

    @property
    def is_configured(self) -> bool:
        """Returns True if all required credentials are present."""
        return bool(
            self.private_key
            and self.api_key
            and self.api_secret
            and self.api_passphrase
        )


# ---------------------------------------------------------------------------
# Risk Parameters
# ---------------------------------------------------------------------------

@dataclass
class RiskConfig:
    # Maximum single position as fraction of total balance
    max_position_pct: float = 0.03          # 3%
    # Maximum daily loss before bot stops trading
    daily_loss_limit_pct: float = 0.10      # 10%
    # Maximum drawdown from peak before bot stops
    max_drawdown_pct: float = 0.20          # 20%
    # Minimum edge required to place a trade
    min_edge_threshold: float = 0.02        # 2 cents edge minimum
    # Kelly fraction (< 1.0 for safety)
    kelly_fraction: float = 0.50            # half-Kelly
    # Minimum position in USD
    min_position_usd: float = 1.0
    # Maximum concurrent open positions
    max_concurrent_positions: int = 3
    # Number of consecutive losses before circuit breaker triggers
    circuit_breaker_losses: int = 5
    # Circuit breaker pause in minutes
    circuit_breaker_pause_minutes: int = 15


# ---------------------------------------------------------------------------
# Strategy Parameters
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    # Crypto assets to trade
    assets: List[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    # Primary strategy: "latency_arb" or "signal_based"
    primary_strategy: str = "latency_arb"
    # Lookback window for signal-based strategy in minutes
    signal_lookback_minutes: int = 15
    # Price change threshold that triggers latency arb signal (fraction)
    latency_arb_threshold: float = 0.001   # 0.1%
    # Lookback window for latency arb momentum in seconds
    latency_arb_lookback_seconds: int = 30
    # Minimum combined signal confidence for signal-based strategy
    signal_confidence_threshold: float = 0.60
    # RSI period
    rsi_period: int = 14
    # RSI overbought/oversold thresholds
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    # MACD parameters
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    # Bollinger Band parameters
    bb_period: int = 20
    bb_std: float = 2.0


# ---------------------------------------------------------------------------
# Telegram Alert Config
# ---------------------------------------------------------------------------

@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


# ---------------------------------------------------------------------------
# Fee Manager Config (Upgrade 1)
# ---------------------------------------------------------------------------

@dataclass
class FeeConfig:
    # How long to cache fee rate responses in seconds
    cache_ttl_seconds: int = 60
    # Whether dynamic fee querying is enabled
    # (must be True for live trading since Feb 2026)
    dynamic_fee_enabled: bool = True


# ---------------------------------------------------------------------------
# WebSocket Feed Config (Upgrade 2)
# ---------------------------------------------------------------------------

@dataclass
class PolymarketWSConfig:
    # Whether the Polymarket CLOB WebSocket feed is enabled
    enabled: bool = True
    # Fall back to REST polling if WebSocket is unavailable
    fallback_to_rest: bool = True
    # Seconds of stale data before falling back to REST for mid price
    stale_threshold_seconds: int = 30


# ---------------------------------------------------------------------------
# Position Merger Config (Upgrade 3)
# ---------------------------------------------------------------------------

@dataclass
class MergerConfig:
    # Whether automatic position merging is enabled
    enabled: bool = True
    # Minimum USDC value of positions to merge
    min_merge_amount: float = 0.01
    # Run merge check after every N trade cycles
    merge_check_interval_cycles: int = 1


# ---------------------------------------------------------------------------
# Late-Window Maker Config (Upgrade 4)
# ---------------------------------------------------------------------------

@dataclass
class LateWindowConfig:
    # Whether the late-window maker strategy is enabled
    enabled: bool = True
    # Seconds before window end to start looking for late-window entries
    activation_seconds: int = 60
    # Minimum price move (fraction) in window to trigger late-window
    min_direction_threshold: float = 0.001   # 0.1%
    # Entry price range for maker orders
    entry_price_min: float = 0.90
    entry_price_max: float = 0.95


# ---------------------------------------------------------------------------
# Scalp Strategy Config
# ---------------------------------------------------------------------------

@dataclass
class ScalpConfig:
    enabled: bool = True
    loop_interval: float = 2.0
    state_file: str = "scalp_paper_state.json"
    # Entry thresholds
    btc_min_spread: float = 18.0
    eth_min_spread: float = 0.60
    sol_min_spread: float = 0.04
    min_velocity_pct: float = 0.0003  # 0.03% stored as fraction
    min_secs_remaining: float = 60.0
    poly_prob_low: float = 0.25
    poly_prob_high: float = 0.70
    # Exit thresholds
    take_profit_pct: float = 0.30    # +30%
    stop_loss_pct: float = 0.20      # -20%
    max_hold_seconds: float = 90.0
    emergency_exit_secs: float = 15.0  # exit if window ends in <15s
    # Risk
    max_positions_per_asset: int = 1
    max_total_positions: int = 3
    max_daily_loss: float = 50.0
    loss_cooldown_secs: float = 60.0
    position_size_pct: float = 0.03  # 3% of balance


# ---------------------------------------------------------------------------
# Exchange / WebSocket Config
# ---------------------------------------------------------------------------

@dataclass
class ExchangeConfig:
    # Binance WebSocket base
    binance_ws_url: str = "wss://stream.binance.com:9443/stream"
    # Max price history window in minutes
    price_history_minutes: int = 30
    # Reconnect interval in seconds
    ws_reconnect_seconds: int = 5


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@dataclass
class APIEndpoints:
    gamma_base: str = "https://gamma-api.polymarket.com"
    clob_base: str = "https://clob.polymarket.com"
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    data_api_base: str = "https://data-api.polymarket.com"
    # Heartbeat interval in seconds (must be < 10s or orders are cancelled)
    heartbeat_interval_seconds: int = 5


# ---------------------------------------------------------------------------
# Paper Trading Config
# ---------------------------------------------------------------------------

@dataclass
class PaperConfig:
    initial_balance: float = 500.0
    # Path to persist paper trading state
    state_file: str = "paper_state.json"


# ---------------------------------------------------------------------------
# Monitor / Logging Config
# ---------------------------------------------------------------------------

@dataclass
class MonitorConfig:
    trade_log_file: str = "trades.csv"
    stats_file: str = "stats.json"
    hourly_report: bool = True
    daily_report: bool = True


# ---------------------------------------------------------------------------
# Main Config Object
# ---------------------------------------------------------------------------

@dataclass
class Config:
    credentials: PolymarketCredentials
    risk: RiskConfig
    strategy: StrategyConfig
    telegram: TelegramConfig
    exchange: ExchangeConfig
    api: APIEndpoints
    paper: PaperConfig
    monitor: MonitorConfig
    # Upgrade configs
    fee: FeeConfig = field(default_factory=FeeConfig)
    polymarket_ws: PolymarketWSConfig = field(default_factory=PolymarketWSConfig)
    merger: MergerConfig = field(default_factory=MergerConfig)
    late_window: LateWindowConfig = field(default_factory=LateWindowConfig)
    scalp: ScalpConfig = field(default_factory=ScalpConfig)
    # "paper" or "live"
    trading_mode: str = "paper"

    @property
    def is_paper_mode(self) -> bool:
        return self.trading_mode.lower() == "paper"

    @property
    def is_live_mode(self) -> bool:
        return self.trading_mode.lower() == "live"


# ---------------------------------------------------------------------------
# Factory: build Config from environment
# ---------------------------------------------------------------------------

def load_config() -> Config:
    """
    Build and return a Config instance by reading environment variables.
    Logs warnings for any missing or invalid values.
    """

    trading_mode = os.environ.get("TRADING_MODE", "paper").lower()
    if trading_mode not in ("paper", "live"):
        logger.warning("Invalid TRADING_MODE '%s'; defaulting to 'paper'", trading_mode)
        trading_mode = "paper"

    credentials = PolymarketCredentials(
        private_key=os.environ.get("PRIVATE_KEY", ""),
        api_key=os.environ.get("API_KEY", ""),
        api_secret=os.environ.get("API_SECRET", ""),
        api_passphrase=os.environ.get("API_PASSPHRASE", ""),
        funder_address=os.environ.get("FUNDER_ADDRESS", ""),
        signature_type=_env_int("SIGNATURE_TYPE", 2),
    )

    if trading_mode == "live" and not credentials.is_configured:
        logger.error(
            "TRADING_MODE=live but Polymarket credentials are incomplete. "
            "Set PRIVATE_KEY, API_KEY, API_SECRET, API_PASSPHRASE in .env"
        )

    risk = RiskConfig(
        max_position_pct=_env_float("MAX_POSITION_PCT", 0.03),
        daily_loss_limit_pct=_env_float("DAILY_LOSS_LIMIT_PCT", 0.10),
        max_drawdown_pct=_env_float("MAX_DRAWDOWN_PCT", 0.20),
        min_edge_threshold=_env_float("MIN_EDGE_THRESHOLD", 0.02),
        kelly_fraction=_env_float("KELLY_FRACTION", 0.50),
    )

    assets = _env_list("ASSETS", ["BTC", "ETH", "SOL"])
    primary_strategy = os.environ.get("PRIMARY_STRATEGY", "latency_arb").lower()
    if primary_strategy not in ("latency_arb", "signal_based"):
        logger.warning("Invalid PRIMARY_STRATEGY; defaulting to 'latency_arb'")
        primary_strategy = "latency_arb"

    strategy = StrategyConfig(
        assets=assets,
        primary_strategy=primary_strategy,
        signal_lookback_minutes=_env_int("SIGNAL_LOOKBACK_MINUTES", 15),
        latency_arb_threshold=_env_float("LATENCY_ARB_THRESHOLD", 0.001),
        latency_arb_lookback_seconds=_env_int("LATENCY_ARB_LOOKBACK_SECONDS", 30),
        signal_confidence_threshold=_env_float("SIGNAL_CONFIDENCE_THRESHOLD", 0.60),
    )

    telegram = TelegramConfig(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )

    paper = PaperConfig(
        initial_balance=_env_float("PAPER_BALANCE", 500.0),
    )

    fee_cfg = FeeConfig(
        cache_ttl_seconds=_env_int("FEE_CACHE_TTL_SECONDS", 60),
        dynamic_fee_enabled=_env_bool("DYNAMIC_FEE_ENABLED", True),
    )

    polymarket_ws_cfg = PolymarketWSConfig(
        enabled=_env_bool("POLYMARKET_WS_ENABLED", True),
        fallback_to_rest=_env_bool("POLYMARKET_WS_FALLBACK_REST", True),
        stale_threshold_seconds=_env_int("POLYMARKET_WS_STALE_SECONDS", 30),
    )

    merger_cfg = MergerConfig(
        enabled=_env_bool("POSITION_MERGER_ENABLED", True),
        min_merge_amount=_env_float("POSITION_MERGER_MIN_AMOUNT", 0.01),
        merge_check_interval_cycles=_env_int("POSITION_MERGER_INTERVAL_CYCLES", 1),
    )

    late_window_cfg = LateWindowConfig(
        enabled=_env_bool("LATE_WINDOW_ENABLED", True),
        activation_seconds=_env_int("LATE_WINDOW_ACTIVATION_SECONDS", 60),
        min_direction_threshold=_env_float("LATE_WINDOW_MIN_THRESHOLD", 0.001),
        entry_price_min=_env_float("LATE_WINDOW_ENTRY_MIN", 0.90),
        entry_price_max=_env_float("LATE_WINDOW_ENTRY_MAX", 0.95),
    )

    scalp_cfg = ScalpConfig(
        enabled=_env_bool("SCALP_ENABLED", True),
        loop_interval=_env_float("SCALP_LOOP_INTERVAL", 2.0),
        state_file=os.environ.get("SCALP_STATE_FILE", "scalp_paper_state.json"),
        btc_min_spread=_env_float("SCALP_BTC_MIN_SPREAD", 18.0),
        eth_min_spread=_env_float("SCALP_ETH_MIN_SPREAD", 0.60),
        sol_min_spread=_env_float("SCALP_SOL_MIN_SPREAD", 0.04),
        min_velocity_pct=_env_float("SCALP_MIN_VELOCITY_PCT", 0.0003),
        min_secs_remaining=_env_float("SCALP_MIN_SECS_REMAINING", 60.0),
        poly_prob_low=_env_float("SCALP_POLY_PROB_LOW", 0.25),
        poly_prob_high=_env_float("SCALP_POLY_PROB_HIGH", 0.70),
        take_profit_pct=_env_float("SCALP_TAKE_PROFIT_PCT", 0.30),
        stop_loss_pct=_env_float("SCALP_STOP_LOSS_PCT", 0.20),
        max_hold_seconds=_env_float("SCALP_MAX_HOLD_SECONDS", 90.0),
        emergency_exit_secs=_env_float("SCALP_EMERGENCY_EXIT_SECS", 15.0),
        max_positions_per_asset=_env_int("SCALP_MAX_POS_PER_ASSET", 1),
        max_total_positions=_env_int("SCALP_MAX_TOTAL_POSITIONS", 3),
        max_daily_loss=_env_float("SCALP_MAX_DAILY_LOSS", 50.0),
        loss_cooldown_secs=_env_float("SCALP_LOSS_COOLDOWN_SECS", 60.0),
        position_size_pct=_env_float("SCALP_POSITION_SIZE_PCT", 0.03),
    )

    config = Config(
        credentials=credentials,
        risk=risk,
        strategy=strategy,
        telegram=telegram,
        exchange=ExchangeConfig(),
        api=APIEndpoints(),
        paper=paper,
        monitor=MonitorConfig(),
        fee=fee_cfg,
        polymarket_ws=polymarket_ws_cfg,
        merger=merger_cfg,
        late_window=late_window_cfg,
        scalp=scalp_cfg,
        trading_mode=trading_mode,
    )

    logger.info(
        "Config loaded: mode=%s, assets=%s, strategy=%s",
        config.trading_mode,
        config.strategy.assets,
        config.strategy.primary_strategy,
    )

    return config


# FIX #5: The CONFIG singleton previously ran unconditionally at import time.
# If .env is missing or corrupted this would crash the entire process before
# main() was ever called.  The singleton is now wrapped in a try/except so
# that an import-time failure is non-fatal: it logs a warning and sets CONFIG
# to None.  Any module that needs the config should call load_config() directly
# rather than relying on this singleton.
try:
    CONFIG = load_config()
except Exception as _config_exc:
    logger.warning(
        "CONFIG singleton failed to load at import time: %s — "
        "call load_config() explicitly in your entry point.",
        _config_exc,
    )
    CONFIG = None
