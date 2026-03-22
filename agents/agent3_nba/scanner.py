"""
Agent 3 Scanner — Find NBA markets on Polymarket.
Games + futures only. No 1H moneylines, no player props, no college.
"""

import logging
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import polymarket_api as pm

logger = logging.getLogger(__name__)

NBA_SEARCH_TERMS = [
    "NBA", "NBA Championship", "NBA MVP", "NBA Finals",
    "Lakers", "Celtics", "Warriors", "Nuggets", "Bucks",
    "76ers", "Suns", "Heat", "Knicks", "Cavaliers",
    "Thunder", "Mavericks", "Timberwolves", "Clippers",
]

# Exclude these from NBA results
EXCLUDED_TERMS = [
    "ncaa", "college", "march madness", "cbb",
    "1h ", "1st half", "first half",
    "player prop", "points scored by",
]


def scan_nba_markets() -> list[dict]:
    """Scan Polymarket for NBA game/futures markets."""
    all_markets = []
    now = datetime.now(timezone.utc)

    # Search by NBA terms
    for term in NBA_SEARCH_TERMS:
        try:
            events = pm.search_events(query=term, limit=20)
            for event in events:
                markets = event.get("markets", [])
                for market_data in markets:
                    # Skip markets not accepting orders
                    if not market_data.get("acceptingOrders", False):
                        continue

                    market = pm.parse_market_data(market_data)
                    if _is_nba_market(market) and _passes_filters(market, now):
                        market["url"] = pm.get_market_url(market_data)
                        all_markets.append(market)
        except Exception as e:
            logger.error(f"Error scanning NBA term '{term}': {e}")

    # Search by tag
    try:
        events = pm.search_events(tag="nba", limit=30)
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

    logger.info(f"Found {len(unique)} NBA markets passing filters")
    return unique


def _is_nba_market(market: dict) -> bool:
    """Check if market is NBA-related (not college)."""
    question = market.get("question", "").lower()
    tags = [t.lower() if isinstance(t, str) else "" for t in market.get("tags", [])]

    # Exclude college/1H/player props FIRST
    for excluded in EXCLUDED_TERMS:
        if excluded in question:
            return False

    nba_keywords = [
        "nba", "lakers", "celtics", "warriors", "nuggets", "bucks",
        "76ers", "sixers", "suns", "heat", "knicks", "cavaliers", "cavs",
        "thunder", "mavericks", "mavs", "timberwolves", "wolves", "clippers",
        "nets", "raptors", "bulls", "hawks", "pacers", "magic",
        "spurs", "rockets", "grizzlies", "pelicans", "kings",
        "pistons", "hornets", "wizards", "blazers", "jazz",
    ]

    for kw in nba_keywords:
        if kw in question:
            return True
    for tag in tags:
        if "nba" in tag:
            return True

    return False


def _passes_filters(market: dict, now: datetime) -> bool:
    """Check if market passes basic filters."""
    if not market.get("active") or market.get("closed"):
        return False
    if not market.get("accepting_orders", True):
        return False

    # Exclude college/1H
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
