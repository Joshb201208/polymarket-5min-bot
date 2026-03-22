"""
Agent 1 — Event/News markets scanner.

Scans Polymarket for general event markets (politics, crypto, culture, etc.)
excluding sports (handled by Agents 2 & 3) and 5-min crypto (existing bot).
"""

import logging
import time
from datetime import datetime, timezone

from agents.common.config import (
    MARKET_COOLDOWN_HOURS,
    MIN_EDGE_THRESHOLD,
    MIN_LIQUIDITY,
    MIN_VOLUME_24H,
)
from agents.common.polymarket_client import (
    fetch_active_events,
    get_event_markets,
    get_market_price,
    passes_filters,
    is_market_tradeable,
)

logger = logging.getLogger(__name__)

# Track already-analyzed markets to avoid spam (slug -> timestamp)
_analyzed_cache: dict[str, float] = {}

# Tags/categories to exclude (handled by other agents or existing bot)
EXCLUDED_TAGS = {"soccer", "football", "nba", "basketball"}
EXCLUDED_TITLE_KEYWORDS = [
    "5-minute", "5 minute", "1-minute", "1 minute",
    "next 5", "next 1",
]


def scan_event_markets() -> list[tuple[dict, dict]]:
    """Scan for qualifying event markets.

    Returns list of (market, event) tuples that pass all filters.
    """
    candidates = []
    now = time.time()

    # Clean up expired cooldowns
    expired = [
        slug for slug, ts in _analyzed_cache.items()
        if now - ts > MARKET_COOLDOWN_HOURS * 3600
    ]
    for slug in expired:
        del _analyzed_cache[slug]

    logger.info("Scanning Polymarket for event markets...")

    # Fetch top events by 24h volume
    events = fetch_active_events(
        limit=100,
        order="volume24hr",
        ascending=False,
    )

    if not events:
        logger.warning("No events returned from Gamma API")
        return []

    logger.info("Fetched %d active events", len(events))
    scanned = 0

    for event in events:
        # Skip sports events (handled by Agent 2 & 3)
        tags = event.get("tags", []) or []
        tag_slugs = set()
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict):
                    tag_slugs.add(tag.get("slug", "").lower())
                elif isinstance(tag, str):
                    tag_slugs.add(tag.lower())

        if tag_slugs & EXCLUDED_TAGS:
            continue

        # Skip 5-min crypto markets (handled by existing bot)
        event_title = (event.get("title") or "").lower()
        if any(kw in event_title for kw in EXCLUDED_TITLE_KEYWORDS):
            continue

        markets = get_event_markets(event)
        for market in markets:
            scanned += 1
            slug = market.get("slug") or market.get("conditionId", "")

            # Skip if recently analyzed
            if slug in _analyzed_cache:
                continue

            # Must be tradeable
            if not is_market_tradeable(market):
                continue

            # Must meet volume/liquidity thresholds
            if not passes_filters(market):
                continue

            # Skip markets resolving within 1 hour
            end_date_str = market.get("endDate") or market.get("end_date_iso")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(
                        end_date_str.replace("Z", "+00:00")
                    )
                    hours_left = (end_date - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_left < 1:
                        continue
                except (ValueError, AttributeError):
                    pass

            # Get current price
            price = get_market_price(market)
            if price is None:
                continue

            # Skip extreme prices (almost certain or impossible)
            if price < 0.03 or price > 0.97:
                continue

            # Mark as analyzed
            _analyzed_cache[slug] = now
            candidates.append((market, event))

    logger.info("Scanned %d markets, found %d candidates", scanned, len(candidates))
    return candidates


def get_scan_count() -> int:
    """Return total markets scanned (for daily summary)."""
    return len(_analyzed_cache)
