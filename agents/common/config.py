"""
Agent-specific configuration.

All values are overridable via environment variables.
"""

import os

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8794513999:AAEIR5KzuZUOoVO6SRKi8XSvcyzzjqh8OTc",
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8451693416")

# ── Polymarket APIs ───────────────────────────────────────────
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

# ── Filtering thresholds ─────────────────────────────────────
MIN_EDGE_THRESHOLD = float(os.environ.get("MIN_EDGE_THRESHOLD", "0.05"))   # 5%
MIN_LIQUIDITY = float(os.environ.get("MIN_LIQUIDITY", "1000"))             # $1 000
MIN_VOLUME_24H = float(os.environ.get("MIN_VOLUME_24H", "500"))            # $500

# ── Persistence ───────────────────────────────────────────────
PAPER_TRADES_FILE = os.environ.get(
    "PAPER_TRADES_FILE", "data/agent_paper_trades.json"
)

# ── Scan intervals (seconds) ─────────────────────────────────
SCAN_INTERVAL_EVENTS = int(os.environ.get("SCAN_INTERVAL_EVENTS", "7200"))   # 2 h
SCAN_INTERVAL_SOCCER = int(os.environ.get("SCAN_INTERVAL_SOCCER", "3600"))   # 1 h
SCAN_INTERVAL_NBA = int(os.environ.get("SCAN_INTERVAL_NBA", "3600"))         # 1 h

# ── Rate limiting ─────────────────────────────────────────────
# Polymarket: 300 req/10s for /markets, 500 req/10s for /events
API_REQUEST_DELAY = float(os.environ.get("API_REQUEST_DELAY", "0.1"))  # 100 ms

# ── Research ──────────────────────────────────────────────────
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
BALLDONTLIE_API = "https://api.balldontlie.io/v1"

# ── Paper trading defaults ────────────────────────────────────
DEFAULT_BET_SIZE = float(os.environ.get("DEFAULT_BET_SIZE", "25"))  # $25 paper bet
MAX_BET_SIZE = float(os.environ.get("MAX_BET_SIZE", "100"))

# ── Cooldowns ─────────────────────────────────────────────────
MARKET_COOLDOWN_HOURS = int(os.environ.get("MARKET_COOLDOWN_HOURS", "12"))  # re-analyze after 12h

# ── Logging ───────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("AGENT_LOG_LEVEL", "INFO")
