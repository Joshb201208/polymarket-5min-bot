import os
from pathlib import Path


class Config:
    # API Keys
    ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
    ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    ALPACA_DATA_URL = "https://data.alpaca.markets"

    PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
    FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Discord
    DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
    DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1488044677889396758")
    DISCORD_CHANNELS = {
        "announcements": os.environ.get("DISCORD_CH_ANNOUNCEMENTS", "1488045682710413312"),
        "portfolio": os.environ.get("DISCORD_CH_PORTFOLIO", "1488045688171266138"),
        "research": os.environ.get("DISCORD_CH_RESEARCH", "1488045698686652427"),
        "thesis": os.environ.get("DISCORD_CH_THESIS", "1488045703929401505"),
        "trades": os.environ.get("DISCORD_CH_TRADES", "1488045709113426080"),
        "daily_pnl": os.environ.get("DISCORD_CH_DAILY_PNL", "1488045714905759895"),
        "risk": os.environ.get("DISCORD_CH_RISK", "1488045720241049661"),
        "market_monitor": os.environ.get("DISCORD_CH_MARKET", "1488045725949366435"),
        "screener": os.environ.get("DISCORD_CH_SCREENER", "1488045736938442784"),
        "analyst": os.environ.get("DISCORD_CH_ANALYST", "1488045742458404886"),
        "macro": os.environ.get("DISCORD_CH_MACRO", "1488045747965395075"),
        "system_logs": os.environ.get("DISCORD_CH_LOGS", "1488045758627315782"),
        "trade_history": os.environ.get("DISCORD_CH_HISTORY", "1488045763995893760"),
        "what_bot_did": os.environ.get("DISCORD_CH_WHAT_BOT_DID", "1488046586272419860"),
        "investing_101": os.environ.get("DISCORD_CH_INVESTING_101", "1488046598641680465"),
        "weekly_eli5": os.environ.get("DISCORD_CH_WEEKLY_ELI5", "1488046604798787605"),
    }

    # Trading mode
    MODE = os.environ.get("STOCK_AGENT_MODE", "PAPER")

    # Risk limits
    MAX_POSITION_PCT = 0.05      # 5% max per position
    MAX_TOTAL_EXPOSURE = 0.25    # 25% max total exposure
    MAX_POSITIONS = 15           # Max concurrent positions
    STOP_LOSS_PCT = 0.05         # 5% hard stop loss
    MAX_SECTOR_PCT = 0.30        # 30% max in any sector

    # Strategy params
    MIN_CONVICTION = 7           # Only trade conviction >= 7
    MIN_MARKET_CAP = 10_000_000_000  # $10B minimum
    UNIVERSE_SIZE = 50           # Scan top 50 candidates
    DEEP_ANALYSIS_SIZE = 20      # Deep-dive on top 20

    # Schedule
    WEEKLY_ANALYSIS_DAY = 6      # Sunday (0=Mon, 6=Sun)
    DAILY_MONITORING_HOUR = 9    # 9 AM ET
    MARKET_CLOSE_HOUR = 16       # 4 PM ET
    SCAN_INTERVAL_MINUTES = 30   # Check positions every 30 min during market hours

    # Data directory
    _local = Path(__file__).parent.parent / "data" / "stock_agent"
    _vps = Path("/root/polymarket-bot/data/stock_agent")
    DATA_DIR = Path(os.environ.get("STOCK_DATA_DIR", ""))
    if not DATA_DIR.name:
        try:
            DATA_DIR = _vps if _vps.exists() else _local
        except (PermissionError, OSError):
            DATA_DIR = _local

    LOG_LEVEL = os.environ.get("STOCK_LOG_LEVEL", "INFO")
