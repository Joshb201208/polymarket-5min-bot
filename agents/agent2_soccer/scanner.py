"""
Agent 2 Scanner — Find soccer/football markets on Polymarket.
"""

import logging
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import polymarket_api as pm

logger = logging.getLogger(__name__)

SOCCER_SEARCH_TERMS = [
    "Premier League", "Champions League", "La Liga",
    "Bundesliga", "Serie A", "Ligue 1", "MLS",
    "soccer", "football",
]


def scan_soccer_markets() -> list[dict]:
    """Scan Polymarket for soccer match markets."""
    all_markets = []
    now = datetime.now(timezone.utc)

    # Search by soccer-related terms
    for term in SOCCER_SEARCH_TERMS:
        try:
            events = pm.search_events(query=term, limit=20)
            for event in events:
                markets = event.get("markets", [])
                for market_data in markets:
                    market = pm.parse_market_data(market_data)
                    if _is_soccer_market(market) and _passes_filters(market, now):
                        market["url"] = pm.get_market_url(market_data)
                        market["search_term"] = term
                        all_markets.append(market)
        except Exception as e:
            logger.error(f"Error scanning term '{term}': {e}")

    # Also search by tag
    try:
        events = pm.search_events(tag="soccer", limit=30)
        for event in events:
            for market_data in event.get("markets", []):
                market = pm.parse_market_data(market_data)
                if _passes_filters(market, now):
                    market["url"] = pm.get_market_url(market_data)
                    all_markets.append(market)
    except Exception:
        pass

    # Deduplicate
    seen = set()
    unique = []
    for m in all_markets:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)

    logger.info(f"Found {len(unique)} soccer markets passing filters")
    return unique


def _is_soccer_market(market: dict) -> bool:
    """Check if a market is soccer-related based on question/tags."""
    question = market.get("question", "").lower()
    tags = [t.lower() if isinstance(t, str) else "" for t in market.get("tags", [])]

    soccer_keywords = [
        "premier league", "champions league", "la liga", "bundesliga",
        "serie a", "ligue 1", "mls", "soccer", "football",
        "fc ", " fc", "united", "city", "real madrid", "barcelona",
        "liverpool", "arsenal", "chelsea", "manchester",
        "bayern", "psg", "juventus", "inter milan",
        "match", "win the", "beat",
    ]

    for kw in soccer_keywords:
        if kw in question:
            return True
    for tag in tags:
        if "soccer" in tag or "football" in tag:
            return True

    return False


def _passes_filters(market: dict, now: datetime) -> bool:
    """Check if a market passes basic filters."""
    if not market.get("active") or market.get("closed"):
        return False

    price = market.get("yes_price")
    if price is None:
        return False
    if not (config.PRICE_RANGE[0] <= price <= config.PRICE_RANGE[1]):
        return False

    if market.get("liquidity", 0) < config.MIN_LIQUIDITY:
        return False

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
            return False
    else:
        return False

    return True
