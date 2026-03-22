"""
All configuration for Polymarket AI Betting Agents v2.
Loads from environment variables with sensible defaults.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8794513999:AAEIR5KzuZUOoVO6SRKi8XSvcyzzjqh8OTc")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8451693416")

# ── Polymarket APIs ───────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ── Bankroll ──────────────────────────────────────────────────
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "500.0"))
MIN_EDGE = 0.05            # 5% minimum edge to alert
MIN_EDGE_BET = 0.07        # 7% minimum to actually "paper bet"
MAX_SINGLE_BET_PCT = 0.10  # Max 10% of bankroll per bet ($50 on $500)
KELLY_FRACTION = 0.25      # Quarter Kelly (conservative)
MIN_BET = 5.0              # Minimum $5 bet
MAX_BET = 75.0             # Hard cap

# ── Market Filters ────────────────────────────────────────────
MAX_RESOLUTION_DAYS = 14   # Only markets resolving within 2 weeks
MIN_RESOLUTION_HOURS = 2   # Don't bet on markets closing in <2 hours
MIN_LIQUIDITY = 5000       # $5k minimum liquidity
MIN_VOLUME_24H = 1000      # $1k minimum 24h volume
PRICE_RANGE = (0.10, 0.90) # Only bet when price between 10c-90c

# ── Early Exit ────────────────────────────────────────────────
EARLY_EXIT_PROFIT_PCT = 0.15    # Sell if position up 15%+
EARLY_EXIT_LOSS_PCT = 0.20      # Stop loss at 20%
EARLY_EXIT_CHECK_INTERVAL = 1800  # Check every 30 min

# ── Scan Intervals (seconds) ─────────────────────────────────
SCAN_EVENTS = int(os.getenv("SCAN_EVENTS", "3600"))    # 1 hour
SCAN_SOCCER = int(os.getenv("SCAN_SOCCER", "1800"))    # 30 min
SCAN_NBA = int(os.getenv("SCAN_NBA", "1800"))          # 30 min

# ── Market Cooldown ───────────────────────────────────────────
COOLDOWN_HOURS = 4  # Re-analyze same market after 4 hours

# ── Trading Mode ──────────────────────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

# ── Logging ───────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Data Directories ──────────────────────────────────────────
import pathlib
DATA_DIR = pathlib.Path(__file__).parent.parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
