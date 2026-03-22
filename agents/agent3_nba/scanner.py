"""
Agent 3 — NBA basketball markets scanner.

Scans Polymarket for NBA markets: championship, MVP, game outcomes, player props.
"""

import logging
import time

from agents.common.config import MARKET_COOLDOWN_HOURS
from agents.common.polymarket_client import (
    fetch_active_events,
    get_event_markets,
    get_market_price,
    is_market_tradeable,
    passes_filters,
    search_events,
)

logger = logging.getLogger(__name__)

_analyzed_cache: dict[str, float] = {}

NBA_TAGS = ["nba"]
NBA_SEARCH_TERMS = [
    "NBA", "basketball", "NBA Championship", "NBA Finals",
    "NBA MVP", "NBA Playoffs",
]


def scan_nba_markets() -> list[tuple[dict, dict]]:
    """Scan for qualifying NBA markets.

    Returns list of (market, event) tuples.
    """
    candidates = []
    now = time.time()
    seen_event_ids: set[str] = set()

    # Clean up expired cooldowns
    expired = [
        slug for slug, ts in _analyzed_cache.items()
        if now - ts > MARKET_COOLDOWN_HOURS * 3600
    ]
    for slug in expired:
        del _analyzed_cache[slug]

    logger.info("Scanning Polymarket for NBA markets...")

    # 1. Fetch by tag_slug
    all_events: list[dict] = []
    for tag in NBA_TAGS:
        events = fetch_active_events(tag_slug=tag, limit=50)
        for e in events:
            eid = e.get("id", "")
            if eid not in seen_event_ids:
                seen_event_ids.add(eid)
                all_events.append(e)

    # 2. Text search
    for term in NBA_SEARCH_TERMS:
        events = search_events(term, limit=20)
        for e in events:
            eid = e.get("id", "")
            if eid not in seen_event_ids:
                seen_event_ids.add(eid)
                all_events.append(e)

    if not all_events:
        logger.info("No NBA events found")
        return []

    logger.info("Found %d unique NBA events", len(all_events))
    scanned = 0

    for event in all_events:
        markets = get_event_markets(event)
        for market in markets:
            scanned += 1
            slug = market.get("slug") or market.get("conditionId", "")

            if slug in _analyzed_cache:
                continue

            if not is_market_tradeable(market):
                continue

            if not passes_filters(market):
                continue

            price = get_market_price(market)
            if price is None or price < 0.03 or price > 0.97:
                continue

            _analyzed_cache[slug] = now
            candidates.append((market, event))

    logger.info("Scanned %d NBA markets, found %d candidates", scanned, len(candidates))
    return candidates


def get_scan_count() -> int:
    return len(_analyzed_cache)
