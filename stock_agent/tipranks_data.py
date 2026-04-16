"""Read and parse TipRanks CSV exports into structured data."""

import csv
import logging
from pathlib import Path
from typing import Optional

from stock_agent.models import TipRanksStock

logger = logging.getLogger(__name__)

# Map CSV column headers to TipRanksStock fields.
# TipRanks CSV columns:
#   Ticker, Name, Price, Smart Score, Analyst Consensus, Analyst Price Target %,
#   Hedge Fund signal, Insider Signal, News Sentiment, Blogger Consensus,
#   AI Rating, AI Price Target %, Sector, Market Cap, P/E Ratio
_COLUMN_MAP = {
    "Ticker": "symbol",
    "Name": "name",
    "Smart Score": "smart_score",
    "Analyst Consensus": "analyst_consensus",
    "Analyst Price Target %": "analyst_target_upside",
    "Hedge Fund signal": "hedge_fund_signal",
    "Hedge Fund Signal": "hedge_fund_signal",
    "Insider Signal": "insider_signal",
    "News Sentiment": "news_sentiment",
    "Blogger Consensus": "blogger_consensus",
    "AI Rating": "ai_rating",
    "AI Price Target %": "ai_target_upside",
    "Sector": "sector",
    "Market Cap": "market_cap",
    "P/E Ratio": "pe_ratio",
}


def _parse_float(val: str) -> Optional[float]:
    if not val or val.strip() in ("", "-", "N/A", "n/a"):
        return None
    try:
        # Remove percent signs, dollar signs, commas
        cleaned = val.strip().replace("%", "").replace("$", "").replace(",", "")
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_int(val: str) -> int:
    if not val or val.strip() in ("", "-", "N/A", "n/a"):
        return 0
    try:
        cleaned = val.strip().replace(",", "")
        return int(float(cleaned))
    except (ValueError, TypeError):
        return 0


def _parse_row(row: dict) -> Optional[TipRanksStock]:
    """Parse a single CSV row dict into a TipRanksStock."""
    # Build a normalized dict using column map
    mapped = {}
    for csv_col, field in _COLUMN_MAP.items():
        if csv_col in row:
            mapped[field] = row[csv_col]

    symbol = mapped.get("symbol", "").strip()
    if not symbol:
        return None

    try:
        return TipRanksStock(
            symbol=symbol,
            name=mapped.get("name", "").strip(),
            smart_score=_parse_int(mapped.get("smart_score", "0")),
            analyst_consensus=mapped.get("analyst_consensus", "").strip(),
            analyst_target_upside=_parse_float(mapped.get("analyst_target_upside", "")) or 0.0,
            hedge_fund_signal=mapped.get("hedge_fund_signal", "").strip(),
            insider_signal=mapped.get("insider_signal", "").strip(),
            news_sentiment=mapped.get("news_sentiment", "").strip(),
            blogger_consensus=mapped.get("blogger_consensus", "").strip(),
            ai_rating=mapped.get("ai_rating", "").strip() or None,
            ai_target_upside=_parse_float(mapped.get("ai_target_upside", "")),
            sector=mapped.get("sector", "").strip(),
            market_cap=mapped.get("market_cap", "").strip() or None,
            pe_ratio=_parse_float(mapped.get("pe_ratio", "")),
        )
    except Exception as e:
        logger.warning("Failed to parse TipRanks row for %s: %s", symbol, e)
        return None


def _load_csv(path: Path) -> list[TipRanksStock]:
    """Load and parse a TipRanks CSV file."""
    stocks: list[TipRanksStock] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stock = _parse_row(row)
                if stock:
                    stocks.append(stock)
    except Exception as e:
        logger.error("Failed to read TipRanks CSV %s: %s", path, e)
    return stocks


class TipRanksData:
    """Read and query TipRanks stock screener data."""

    def __init__(self, config):
        self.data_dir = Path(config.DATA_DIR)
        self._cache: list[TipRanksStock] | None = None

    def load_latest(self) -> list[TipRanksStock] | None:
        """Load the most recent TipRanks CSV."""
        path = self.data_dir / "tipranks_latest.csv"
        if not path.exists():
            logger.info("No TipRanks CSV found at %s", path)
            return None

        stocks = _load_csv(path)
        if not stocks:
            logger.warning("TipRanks CSV was empty or unparseable")
            return None

        self._cache = stocks
        logger.info("Loaded %d stocks from TipRanks CSV", len(stocks))
        return stocks

    def get_stock(self, symbol: str) -> TipRanksStock | None:
        """Get TipRanks data for a specific symbol."""
        data = self._cache if self._cache is not None else self.load_latest()
        if not data:
            return None
        symbol_upper = symbol.upper()
        return next((s for s in data if s.symbol.upper() == symbol_upper), None)

    def get_high_conviction(self, min_smart_score: int = 9) -> list[TipRanksStock]:
        """Get stocks with high smart scores."""
        data = self._cache if self._cache is not None else self.load_latest()
        if not data:
            return []
        return [s for s in data if s.smart_score >= min_smart_score]

    def get_aligned_signals(self) -> list[TipRanksStock]:
        """Get stocks where analyst + hedge fund + insider signals all align positively."""
        data = self._cache if self._cache is not None else self.load_latest()
        if not data:
            return []
        return [
            s
            for s in data
            if s.analyst_consensus in ("Strong Buy", "Moderate Buy")
            and s.hedge_fund_signal.lower() == "positive"
            and s.insider_signal.lower() == "positive"
        ]
