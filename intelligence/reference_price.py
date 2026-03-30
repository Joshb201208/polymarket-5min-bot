"""Reference Price Intelligence Module.

Compares real-world asset spot prices against Polymarket price-target
markets.  Fetches live prices from CoinGecko (crypto) and Yahoo Finance
(commodities / equities) — no API keys required — and produces strong
directional signals when the gap between current price and the market's
target is statistically unlikely to close in the remaining time.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from math import erfc, sqrt

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal

logger = logging.getLogger("intelligence.reference_price")

# ── Asset mapping: name variants → data-source IDs ──────────────────────

ASSET_MAP: dict[str, dict] = {
    # Crypto — CoinGecko primary, Yahoo fallback
    "bitcoin":  {"coingecko_id": "bitcoin",  "yahoo": "BTC-USD", "vol_key": "bitcoin"},
    "btc":      {"coingecko_id": "bitcoin",  "yahoo": "BTC-USD", "vol_key": "bitcoin"},
    "ethereum": {"coingecko_id": "ethereum", "yahoo": "ETH-USD", "vol_key": "ethereum"},
    "eth":      {"coingecko_id": "ethereum", "yahoo": "ETH-USD", "vol_key": "ethereum"},
    "ether":    {"coingecko_id": "ethereum", "yahoo": "ETH-USD", "vol_key": "ethereum"},
    "solana":   {"coingecko_id": "solana",   "yahoo": "SOL-USD", "vol_key": "solana"},
    "sol":      {"coingecko_id": "solana",   "yahoo": "SOL-USD", "vol_key": "solana"},
    "xrp":      {"coingecko_id": "ripple",   "yahoo": "XRP-USD", "vol_key": "xrp"},
    "ripple":   {"coingecko_id": "ripple",   "yahoo": "XRP-USD", "vol_key": "xrp"},
    "dogecoin": {"coingecko_id": "dogecoin", "yahoo": "DOGE-USD", "vol_key": "dogecoin"},
    "doge":     {"coingecko_id": "dogecoin", "yahoo": "DOGE-USD", "vol_key": "dogecoin"},
    # Commodities / equities — Yahoo only
    "crude oil":{"yahoo": "CL=F",  "vol_key": "crude_oil"},
    "oil":      {"yahoo": "CL=F",  "vol_key": "crude_oil"},
    "wti":      {"yahoo": "CL=F",  "vol_key": "crude_oil"},
    "gold":     {"yahoo": "GC=F",  "vol_key": "gold"},
    "silver":   {"yahoo": "SI=F",  "vol_key": "silver"},
    "s&p 500":  {"yahoo": "^GSPC", "vol_key": "sp500"},
    "s&p":      {"yahoo": "^GSPC", "vol_key": "sp500"},
    "spx":      {"yahoo": "^GSPC", "vol_key": "sp500"},
    "sp500":    {"yahoo": "^GSPC", "vol_key": "sp500"},
    "nasdaq":   {"yahoo": "^IXIC", "vol_key": "nasdaq"},
}

# Default daily volatility estimates (fraction, not %)
DAILY_VOLATILITY: dict[str, float] = {
    "bitcoin":    0.035,
    "ethereum":   0.045,
    "solana":     0.055,
    "xrp":        0.050,
    "dogecoin":   0.060,
    "crude_oil":  0.025,
    "gold":       0.012,
    "silver":     0.020,
    "sp500":      0.012,
    "nasdaq":     0.015,
}

# ── Question-parsing regex ───────────────────────────────────────────────

# Match patterns like:
#   "Will Bitcoin be above $70,000 on April 1?"
#   "Will the price of Crude Oil hit $110 by end of March?"
#   "Will Gold dip to $1,800 by March 31?"
#   "Will Ethereum be below $3,000 on March 31?"
#   "Will Bitcoin reach $100,000 by December 31?"
#   "Will Bitcoin (BTC) be above $100,000 ..."
PRICE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"Will\s+(?:the\s+price\s+of\s+)?(.+?)\s+"
        r"(?:be\s+)?(?:above|over|at\s+or\s+above|≥|>=)\s+"
        r"(?:\((?:HIGH|LOW)\)\s*)?\$?([\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"Will\s+(?:the\s+price\s+of\s+)?(.+?)\s+"
        r"(?:be\s+)?(?:below|under|at\s+or\s+below|≤|<=)\s+"
        r"(?:\((?:HIGH|LOW)\)\s*)?\$?([\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"Will\s+(?:the\s+price\s+of\s+)?(.+?)\s+"
        r"(?:hit|reach|touch|break|exceed|surpass)\s+"
        r"(?:\((?:HIGH|LOW)\)\s*)?\$?([\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"Will\s+(?:the\s+price\s+of\s+)?(.+?)\s+"
        r"(?:dip\s+to|fall\s+to|drop\s+to|decline\s+to)\s+"
        r"(?:\((?:HIGH|LOW)\)\s*)?\$?([\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    # Generic catch-all: "Will <asset> <verb> ... $<price>"
    re.compile(
        r"Will\s+(?:the\s+price\s+of\s+)?(.+?)\s+.*?"
        r"\$\s*([\d,]+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
]

# Direction keywords
_ABOVE_KW = {"above", "over", "at or above", "exceed", "surpass", "break",
             "hit", "reach", "touch"}
_BELOW_KW = {"below", "under", "at or below", "dip to", "fall to",
             "drop to", "decline to"}


def _clean_asset_name(raw: str) -> str:
    """Normalise raw asset string from regex capture."""
    # Strip trailing parenthetical tickers — "Crude Oil (CL)" → "crude oil"
    cleaned = re.sub(r"\s*\(.*?\)\s*", " ", raw).strip()
    # Remove trailing "price" — "Bitcoin price" → "Bitcoin"
    cleaned = re.sub(r"\s+price$", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned.lower()


def parse_price_question(question: str) -> dict | None:
    """Parse a Polymarket question into asset, target_price, direction.

    Returns dict with keys: asset, target_price, direction, asset_info
    or None if the question doesn't look like a price-target market.
    """
    for pattern in PRICE_PATTERNS:
        m = pattern.search(question)
        if not m:
            continue
        raw_asset = m.group(1)
        raw_price = m.group(2)

        asset_name = _clean_asset_name(raw_asset)
        target_price = float(raw_price.replace(",", ""))

        # Look up asset
        asset_info = ASSET_MAP.get(asset_name)
        if not asset_info:
            # Try partial match
            for key, info in ASSET_MAP.items():
                if key in asset_name or asset_name in key:
                    asset_info = info
                    asset_name = key
                    break
        if not asset_info:
            continue

        # Determine direction from the question text
        q_lower = question.lower()
        if any(kw in q_lower for kw in ("above", "over", "at or above",
                                         "exceed", "surpass", "break",
                                         "hit", "reach", "touch")):
            direction = "above"
        elif any(kw in q_lower for kw in ("below", "under", "at or below",
                                           "dip to", "fall to", "drop to",
                                           "decline to")):
            direction = "below"
        else:
            direction = "above"  # default for "reach/hit" style

        return {
            "asset": asset_name,
            "target_price": target_price,
            "direction": direction,
            "asset_info": asset_info,
        }
    return None


# ── Spot-price fetching with 5-minute cache ──────────────────────────────

_price_cache: dict[str, tuple[float, float]] = {}  # key → (price, timestamp)
_CACHE_TTL = 300  # 5 minutes


async def _fetch_coingecko(ids: list[str], client: httpx.AsyncClient) -> dict[str, float]:
    """Fetch USD prices from CoinGecko (free, no key)."""
    if not ids:
        return {}
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": ",".join(ids), "vs_currencies": "usd"}
    try:
        resp = await client.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {cid: data[cid]["usd"] for cid in data if "usd" in data[cid]}
    except Exception as e:
        logger.warning("CoinGecko fetch failed: %s", e)
        return {}


async def _fetch_yahoo(symbol: str, client: httpx.AsyncClient) -> float | None:
    """Fetch a single ticker's price from Yahoo Finance (free, no key)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "1d", "interval": "1d"}
    try:
        resp = await client.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"])
    except Exception as e:
        logger.warning("Yahoo Finance fetch failed for %s: %s", symbol, e)
        return None


async def get_spot_price(asset_info: dict, client: httpx.AsyncClient) -> float | None:
    """Get the current spot price for an asset, using cache."""
    cache_key = asset_info.get("coingecko_id") or asset_info.get("yahoo", "")
    now = time.monotonic()

    # Check cache
    if cache_key in _price_cache:
        cached_price, cached_at = _price_cache[cache_key]
        if now - cached_at < _CACHE_TTL:
            return cached_price

    price = None

    # Try CoinGecko first for crypto
    cg_id = asset_info.get("coingecko_id")
    if cg_id:
        prices = await _fetch_coingecko([cg_id], client)
        price = prices.get(cg_id)

    # Fallback to Yahoo
    if price is None and "yahoo" in asset_info:
        price = await _fetch_yahoo(asset_info["yahoo"], client)

    if price is not None:
        _price_cache[cache_key] = (price, now)

    return price


# ── Signal calculation ───────────────────────────────────────────────────

def calculate_signal(
    current_price: float,
    target_price: float,
    direction: str,
    hours_remaining: float,
    daily_volatility: float,
) -> tuple[str, float, str] | None:
    """Calculate directional signal from price gap and time remaining.

    Returns (direction_YES_NO, strength, confidence_tier) or None if
    the situation is too uncertain to produce a signal.
    """
    gap_pct = (target_price - current_price) / current_price
    days = max(hours_remaining / 24, 0.04)  # floor at ~1 hour

    # Expected move range using vol * sqrt(days)
    expected_range = daily_volatility * (days ** 0.5)

    if expected_range > 0:
        z_score = abs(gap_pct) / expected_range
    else:
        z_score = 10.0

    # Probability of reaching the target (one-tail normal CDF complement)
    prob_reaching = 0.5 * erfc(z_score / sqrt(2))

    if direction == "above":
        if current_price >= target_price:
            # Already above — strong YES (likely stays above)
            return "YES", 0.85, "VERY_HIGH"
        # Need price to go UP to target
        if prob_reaching < 0.05:
            return "NO", 0.92, "VERY_HIGH"
        elif prob_reaching < 0.10:
            return "NO", 0.85, "VERY_HIGH"
        elif prob_reaching < 0.25:
            return "NO", 0.75, "HIGH"
        elif prob_reaching < 0.40:
            return "NO", 0.60, "MEDIUM"
        else:
            return None  # Too uncertain

    elif direction in ("below", "dip"):
        if current_price <= target_price:
            # Already below — strong YES
            return "YES", 0.85, "VERY_HIGH"
        # Need price to DROP to target
        if prob_reaching < 0.05:
            return "NO", 0.92, "VERY_HIGH"
        elif prob_reaching < 0.10:
            return "NO", 0.85, "VERY_HIGH"
        elif prob_reaching < 0.25:
            return "NO", 0.75, "HIGH"
        elif prob_reaching < 0.40:
            return "NO", 0.60, "MEDIUM"
        else:
            return None

    return None


# ── Scanner class ────────────────────────────────────────────────────────

class ReferencePriceScanner:
    """Scans Polymarket price-target markets against live spot prices."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan all active markets for price-target opportunities.

        Returns a list of Signal objects for markets where the current
        spot price makes the target statistically unlikely (or nearly
        certain).
        """
        if not self.config.is_enabled("reference_price"):
            logger.debug("Reference price module disabled")
            return []

        signals: list[Signal] = []

        # Parse all markets first, collect those that match price patterns
        parsed: list[tuple] = []  # (market, parse_result)
        for market in active_markets:
            question = getattr(market, "question", "")
            if not question:
                continue
            result = parse_price_question(question)
            if result:
                parsed.append((market, result))

        if not parsed:
            logger.debug("No price-target markets found in %d active markets", len(active_markets))
            return []

        logger.info("Found %d price-target markets to evaluate", len(parsed))

        # Batch-fetch CoinGecko prices (one call for all crypto)
        cg_ids_needed: set[str] = set()
        for _, pr in parsed:
            cg_id = pr["asset_info"].get("coingecko_id")
            if cg_id:
                cg_ids_needed.add(cg_id)

        async with httpx.AsyncClient() as client:
            # Pre-fetch all crypto prices in one batch (cache-aware)
            if cg_ids_needed:
                now = time.monotonic()
                uncached = [cid for cid in cg_ids_needed
                            if cid not in _price_cache
                            or now - _price_cache[cid][1] >= _CACHE_TTL]
                if uncached:
                    prices = await _fetch_coingecko(uncached, client)
                    for cid, price in prices.items():
                        _price_cache[cid] = (price, now)

            # Evaluate each market
            for market, pr in parsed:
                try:
                    spot = await get_spot_price(pr["asset_info"], client)
                    if spot is None:
                        logger.warning("Could not fetch spot price for %s", pr["asset"])
                        continue

                    # Calculate hours remaining
                    end_date_str = getattr(market, "end_date", "")
                    hours_remaining = self._hours_until(end_date_str)
                    if hours_remaining is None or hours_remaining <= 0:
                        continue

                    vol_key = pr["asset_info"].get("vol_key", "")
                    daily_vol = DAILY_VOLATILITY.get(vol_key, 0.03)

                    result = calculate_signal(
                        current_price=spot,
                        target_price=pr["target_price"],
                        direction=pr["direction"],
                        hours_remaining=hours_remaining,
                        daily_volatility=daily_vol,
                    )
                    if result is None:
                        continue

                    sig_direction, strength, confidence = result
                    gap_pct = (pr["target_price"] - spot) / spot * 100

                    market_id = getattr(market, "id", str(market))
                    question = getattr(market, "question", "")

                    signal = Signal(
                        source="reference_price",
                        market_id=market_id,
                        market_question=question,
                        signal_type="price_gap",
                        direction=sig_direction,
                        strength=strength,
                        confidence=strength,
                        details={
                            "asset": pr["asset"],
                            "current_price": spot,
                            "target_price": pr["target_price"],
                            "gap_pct": round(gap_pct, 2),
                            "direction": pr["direction"],
                            "hours_remaining": round(hours_remaining, 1),
                            "daily_volatility": daily_vol,
                            "confidence_tier": confidence,
                        },
                    )
                    signals.append(signal)

                    logger.info(
                        "REF_PRICE signal: %s | %s @ $%.2f → target $%.0f "
                        "(%+.1f%%) | %s %.0fh left | → %s (str=%.2f, tier=%s)",
                        market_id[:8],
                        pr["asset"],
                        spot,
                        pr["target_price"],
                        gap_pct,
                        pr["direction"],
                        hours_remaining,
                        sig_direction,
                        strength,
                        confidence,
                    )

                except Exception as e:
                    logger.error("Error evaluating %s: %s", pr["asset"], e)

        logger.info("Reference price scan complete: %d signals from %d candidates",
                     len(signals), len(parsed))
        return signals

    @staticmethod
    def _hours_until(end_date_str: str) -> float | None:
        """Parse end_date string and return hours remaining, or None."""
        if not end_date_str:
            return None
        try:
            # Handle various ISO-8601 formats
            end_date_str = end_date_str.replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(end_date_str)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = end_dt - now
            return delta.total_seconds() / 3600
        except (ValueError, TypeError):
            return None
