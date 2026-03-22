"""
Agent 1 Scanner — Find short-term event markets on Polymarket.
Targets markets resolving in 1-14 days with sufficient liquidity.
"""

import logging
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import polymarket_api as pm

logger = logging.getLogger(__name__)

# Tags to search for short-term event markets
EVENT_TAGS = [
    "politics", "crypto", "science", "sports",
    "pop-culture", "business", "world",
]


def scan_event_markets() -> list[dict]:
    """Scan Polymarket for short-term event markets that meet our criteria.

    Filters:
    - Resolves within MAX_RESOLUTION_DAYS (14 days)
    - Not resolving in less than MIN_RESOLUTION_HOURS (2 hours)
    - Minimum liquidity ($5k)
    - Price between 10c-90c (not near-certain)
    """
    all_markets = []
    now = datetime.now(timezone.utc)

    for tag in EVENT_TAGS:
        try:
            events = pm.search_events(tag=tag, limit=30)
            for event in events:
                markets = event.get("markets", [])
                if not markets:
                    continue

                for market_data in markets:
                    market = pm.parse_market_data(market_data)
                    if _passes_filters(market, now):
                        market["url"] = pm.get_market_url(market_data)
                        market["source_tag"] = tag
                        all_markets.append(market)
        except Exception as e:
            logger.error(f"Error scanning tag {tag}: {e}")

    # Deduplicate by market ID
    seen = set()
    unique = []
    for m in all_markets:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)

    logger.info(f"Found {len(unique)} event markets passing filters")
    return unique


def _passes_filters(market: dict, now: datetime) -> bool:
    """Check if a market passes all filters."""
    # Must be active
    if not market.get("active") or market.get("closed"):
        return False

    # Price range check
    price = market.get("yes_price")
    if price is None:
        return False
    if not (config.PRICE_RANGE[0] <= price <= config.PRICE_RANGE[1]):
        return False

    # Liquidity check
    if market.get("liquidity", 0) < config.MIN_LIQUIDITY:
        return False

    # Volume check
    if market.get("volume_24h", 0) < config.MIN_VOLUME_24H:
        return False

    # Resolution time check
    end_date_str = market.get("end_date", "")
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_until = (end_date - now).total_seconds() / 3600
            days_until = hours_until / 24

            if days_until > config.MAX_RESOLUTION_DAYS:
                return False
            if hours_until < config.MIN_RESOLUTION_HOURS:
                return False
        except Exception:
            return False  # Can't parse date = skip
    else:
        return False  # No end date = skip

    return True


def scan_volume_surges() -> list[dict]:
    """Find markets with unusual volume activity (potential news catalyst)."""
    markets = []
    try:
        events = pm.search_events(limit=50)
        for event in events:
            for market_data in event.get("markets", []):
                market = pm.parse_market_data(market_data)

                volume_24h = market.get("volume_24h", 0)
                total_volume = market.get("volume", 0)

                # Volume surge: 24h volume is >20% of all-time volume
                if total_volume > 0 and volume_24h > 0:
                    volume_ratio = volume_24h / total_volume
                    if volume_ratio > 0.20 and volume_24h > config.MIN_VOLUME_24H:
                        market["volume_surge_ratio"] = volume_ratio
                        market["url"] = pm.get_market_url(market_data)
                        markets.append(market)
    except Exception as e:
        logger.error(f"Error scanning volume surges: {e}")

    return sorted(markets, key=lambda x: x.get("volume_surge_ratio", 0), reverse=True)[:10]
