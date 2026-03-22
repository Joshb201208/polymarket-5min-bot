"""
Agent 2 Scanner — Find soccer/football markets on Polymarket.
BIG LEAGUES ONLY — no MLS, no college, no lower divisions.
"""

import logging
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import polymarket_api as pm

logger = logging.getLogger(__name__)

# Only major leagues
SOCCER_LEAGUES = [
    "Premier League", "Champions League", "La Liga", "Serie A",
    "Bundesliga", "Ligue 1", "World Cup", "Europa League",
    "Euro 2026", "Copa America",
]

SOCCER_TAG_SLUGS = ["soccer", "football"]

# Explicitly EXCLUDE these terms
EXCLUDED_TERMS = [
    "ncaa", "college", "march madness", "cbb", "mls", "usl",
    "league one", "league two", "national league",
    "basketball", "nba",
]


def scan_soccer_markets() -> list[dict]:
    """Scan Polymarket for major league soccer markets."""
    all_markets = []
    now = datetime.now(timezone.utc)

    # Search by major league names
    for term in SOCCER_LEAGUES:
        try:
            events = pm.search_events(query=term, limit=20)
            for event in events:
                markets = event.get("markets", [])
                for market_data in markets:
                    # Skip markets not accepting orders
                    if not market_data.get("acceptingOrders", False):
                        continue

                    market = pm.parse_market_data(market_data)
                    if _is_soccer_market(market) and _passes_filters(market, now):
                        market["url"] = pm.get_market_url(market_data)
                        market["search_term"] = term
                        all_markets.append(market)
        except Exception as e:
            logger.error(f"Error scanning term '{term}': {e}")

    # Also search by tag
    for tag_slug in SOCCER_TAG_SLUGS:
        try:
            events = pm.search_events(tag=tag_slug, limit=30)
            for event in events:
                for market_data in event.get("markets", []):
                    # Skip markets not accepting orders
                    if not market_data.get("acceptingOrders", False):
                        continue

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

    # Check for excluded terms FIRST
    for excluded in EXCLUDED_TERMS:
        if excluded in question:
            return False

    soccer_keywords = [
        "premier league", "champions league", "la liga", "bundesliga",
        "serie a", "ligue 1", "soccer", "football",
        "fc ", " fc", "real madrid", "barcelona",
        "liverpool", "arsenal", "chelsea", "manchester",
        "bayern", "psg", "juventus", "inter milan",
        "world cup", "europa league", "euro 2026", "copa america",
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
    if not market.get("accepting_orders", True):
        return False

    # Exclude non-soccer terms
    question_lower = market.get("question", "").lower()
    for excluded in EXCLUDED_TERMS:
        if excluded in question_lower:
            return False

    # HARD REJECT — price at 0c or near-resolved
    price = market.get("yes_price")
    if price is None or price <= 0.03 or price >= 0.97:
        return False
    if not (config.PRICE_RANGE[0] <= price <= config.PRICE_RANGE[1]):
        return False

    if market.get("liquidity", 0) < config.MIN_LIQUIDITY:
        return False

    # Check if market has already ended
    end_date_str = market.get("end_date", "")
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

            # REJECT if market already ended
            if end_date < now:
                return False

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
