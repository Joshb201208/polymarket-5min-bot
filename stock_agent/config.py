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
        "live_positions": os.environ.get("DISCORD_CH_LIVE_POSITIONS", "1488115077805510656"),
    }

    # Trading mode
    MODE = os.environ.get("STOCK_AGENT_MODE", "PAPER")

    # Risk limits. Apply to the broker book after the proposed trade.
    MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.15"))
    MAX_TOTAL_EXPOSURE = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", "1.00"))
    MAX_POSITIONS = 20
    MAX_SECTOR_PCT = 0.40
    MAX_CLUSTER_PCT = float(os.environ.get("MAX_CLUSTER_PCT", "0.48"))
    MIN_FREE_CASH_PCT = 0.02
    MAX_INITIAL_STOP_RISK_PCT = 0.01
    CONVICTION_MAX_ADJUSTMENT = 1
    TOP_UP_TARGET_TOLERANCE = 0.90
    MAX_ENTRY_SIGNAL_HOURS = 96
    MAX_ENTRY_PRICE_DRIFT_PCT = 0.03
    MAX_ENTRY_QUOTE_AGE_SECONDS = 60
    RISK_DRIFT_BUFFER_PCT = 0.02
    RISK_TRIM_HEADROOM_PCT = 0.02
    RISK_DRIFT_CONFIRM_SESSIONS = 2
    RISK_PAUSE_NEW_BUYS = os.environ.get("RISK_PAUSE_NEW_BUYS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    DRAWDOWN_ALERT_PCT = 0.10
    DRAWDOWN_THROTTLE_PCT = 0.15
    DRAWDOWN_PAUSE_PCT = 0.20
    DRAWDOWN_THROTTLE_SCALE = 0.50
    DRAWDOWN_RECOVERY_SESSIONS = 5
    RISK_OPTION_MAX_LOSS_PCT = 0.03
    RISK_OPTION_TOTAL_LOSS_PCT = 0.10
    RISK_OPTION_MAX_STRUCTURES = 6

    # Priority symbols — always injected into the scanned universe regardless
    # of FMP screener output. Used for curated high-conviction names flagged
    # by outside research (e.g. Fundstrat Top 5) so the bot's own thesis
    # process can evaluate them on merit. Env override: comma-separated.
    _priority_env = os.environ.get("PRIORITY_SYMBOLS", "").strip()
    PRIORITY_SYMBOLS: list[str] = (
        [s.strip().upper() for s in _priority_env.split(",") if s.strip()]
        if _priority_env
        else ["ANET", "BK", "GS", "JPM"]
    )

    # Known correlation clusters. A symbol may belong to multiple; all are checked.
    SYMBOL_CLUSTERS: dict[str, list[str]] = {
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

    # Fallback sector map for when FMP profile doesn't return one. Used by
    # portfolio.sync_from_alpaca and the backfill routine in scheduler.
    SECTOR_MAP: dict[str, str] = {
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

    # Conviction-tiered position sizing: conviction score → % of portfolio
    CONVICTION_SIZE_MAP = {
        7: 0.05,   # 5%
        8: 0.07,   # 7%
        9: 0.08,   # 8%
        10: 0.10,  # 10%
    }

    # Fill-anchored ATR stop, with a labelled 7% fallback without usable bars.
    STOP_ATR_PERIOD = 14
    STOP_ATR_MULTIPLE = 2.5
    STOP_LOSS_PCT_MIN = 0.07
    STOP_LOSS_PCT_MAX = 0.12
    STOP_LOSS_PCT = 0.07

    # Strategy params
    MIN_CONVICTION = 7           # Only trade conviction >= 7
    MIN_MARKET_CAP = 10_000_000_000  # $10B minimum
    UNIVERSE_SIZE = 50           # Scan top 50 candidates
    DEEP_ANALYSIS_SIZE = 20      # Deep-dive on top 20

    # Schedule
    WEEKLY_ANALYSIS_DAY = 6      # Sunday (0=Mon, 6=Sun)
    MIDWEEK_ANALYSIS_DAY = 2     # Wednesday
    DAILY_PRESCAN_HOUR = 8       # 8 AM ET — pre-market scan
    MARKET_CLOSE_HOUR = 16       # 4 PM ET
    SCAN_INTERVAL_MINUTES = 30   # Check positions every 30 min during market hours

    # Earnings-reactive scanning
    EARNINGS_CHECK_ENABLED = True
    EARNINGS_LOOKBACK_DAYS = 2   # Check for earnings in last 2 days

    # Data directory
    _local = Path(__file__).parent.parent / "data" / "stock_agent"
    _vps = Path("/root/polymarket-bot/data/stock_agent")
    DATA_DIR = Path(os.environ.get("STOCK_DATA_DIR", ""))
    if not DATA_DIR.name:
        try:
            DATA_DIR = _vps if _vps.exists() else _local
        except (PermissionError, OSError):
            DATA_DIR = _local

    # TipRanks
    TIPRANKS_API_KEY = os.environ.get("TIPRANKS_API_KEY", "TR_SilverArrow")
    TIPRANKS_API_TOKEN = os.environ.get("TIPRANKS_API_TOKEN", "f8ed6170-a853-42a6-a76d-a3c244560c17")
    TIPRANKS_ENABLED = os.environ.get("TIPRANKS_ENABLED", "true").lower() == "true"
    TIPRANKS_MIN_SMART_SCORE = int(os.environ.get("TIPRANKS_MIN_SMART_SCORE", "8"))
    TIPRANKS_UNIVERSE_LIMIT = int(os.environ.get("TIPRANKS_UNIVERSE_LIMIT", "100"))

    # Options trading
    OPTIONS_ENABLED = os.environ.get("OPTIONS_ENABLED", "true").lower() == "true"
    OPTIONS_MAX_POSITIONS = 10              # Max open options positions
    OPTIONS_MAX_SINGLE_TRADE_PCT = 0.07    # Max 5% of portfolio per trade
    OPTIONS_MAX_TOTAL_EXPOSURE_PCT = 0.15  # Max 15% total options exposure
    OPTIONS_AUTO_CLOSE_PROFIT_PCT = 0.50   # Auto-close at 50% profit
    OPTIONS_AUTO_CLOSE_DTE = 3             # Auto-close at 3 DTE
    OPTIONS_MIN_DTE = 14                   # Minimum 14 DTE for new positions
    OPTIONS_MAX_DTE = 45                   # Maximum 45 DTE for new positions
    OPTIONS_MIN_PREMIUM_PCT = 0.005         # Minimum 1% monthly premium for CSPs
    OPTIONS_SPREAD_WIDTH_MIN = 5.0         # Min spread width $5
    OPTIONS_SPREAD_WIDTH_MAX = 10.0        # Max spread width $10
    OPTIONS_MIN_RR_RATIO = 2.0             # Minimum 2:1 reward-to-risk for spreads

    LOG_LEVEL = os.environ.get("STOCK_LOG_LEVEL", "INFO")
