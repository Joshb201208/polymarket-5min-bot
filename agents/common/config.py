"""Agent-specific configuration — all values have sensible defaults, no paid keys required."""

import os

# ── Telegram ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8794513999:AAEIR5KzuZUOoVO6SRKi8XSvcyzzjqh8OTc",
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8451693416")

# ── Polymarket APIs ─────────────────────────────────────────
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

# ── Filtering thresholds ────────────────────────────────────
MIN_EDGE_THRESHOLD = 0.05   # 5% minimum edge to trigger alert
MIN_LIQUIDITY = 1000        # Minimum $1,000 liquidity
MIN_VOLUME_24H = 500        # Minimum $500 daily volume

# ── Persistence ─────────────────────────────────────────────
PAPER_TRADES_FILE = "data/agent_paper_trades.json"

# ── Scan intervals (seconds) ────────────────────────────────
SCAN_INTERVAL_EVENTS = 7200   # 2 hours
SCAN_INTERVAL_SOCCER = 3600   # 1 hour
SCAN_INTERVAL_NBA = 3600      # 1 hour

# ── Rate limiting ───────────────────────────────────────────
GAMMA_RATE_LIMIT_PER_10S = 500   # /events endpoint
MARKET_RATE_LIMIT_PER_10S = 300  # /markets endpoint
REQUEST_DELAY = 0.05             # 50 ms between requests (conservative)

# ── Research ────────────────────────────────────────────────
GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"

# ── Sports APIs (free, no key) ──────────────────────────────
BALLDONTLIE_API = "https://api.balldontlie.io/v1"
FOOTBALL_DATA_API = "https://api.football-data.org/v4"

# ── Logging ─────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
