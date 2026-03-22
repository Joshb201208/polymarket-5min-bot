"""Gamma API market discovery and filtering."""

from __future__ import annotations

import logging
from datetime import timezone

import httpx

from nba_agent.config import Config
from nba_agent.models import Market, MarketType
from nba_agent.utils import utcnow, parse_utc

logger = logging.getLogger(__name__)

# Slug patterns to reject
_REJECT_PREFIXES = ("cbb-", "ncaa", "cwbb")
_REJECT_CONTAINS = (
    "lol-", "csgo-", "dota-", "valorant-",
    "-points-", "-rebounds-", "-assists-",
    "-1h-",
)


class PolymarketScanner:
    """Scans Polymarket Gamma API for NBA markets."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.base_url = self.config.GAMMA_API_BASE

    async def scan(self) -> list[Market]:
        """Fetch and filter NBA markets from Gamma API."""
        raw_events = await self._fetch_events()
        markets: list[Market] = []

        for event in raw_events:
            event_slug = event.get("slug", "")
            event_title = event.get("title", "")

            # Filter: must be NBA
            if not self._is_nba_event(event_slug, event_title):
                continue

            # Process each market within the event
            for raw_market in event.get("markets", []):
                try:
                    market = Market.from_api(raw_market, event_slug, event_title)
                    market.market_type = market.detect_market_type()

                    if self._passes_filters(market):
                        markets.append(market)
                except Exception as e:
                    logger.warning("Failed to parse market %s: %s", raw_market.get("id"), e)

        logger.info("Scanner found %d NBA markets after filtering", len(markets))
        return markets

    async def _fetch_events(self) -> list[dict]:
        """Fetch active events from Gamma API sorted by liquidity."""
        all_events: list[dict] = []
        offset = 0
        limit = 100

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                try:
                    resp = await client.get(
                        f"{self.base_url}/events",
                        params={
                            "active": "true",
                            "closed": "false",
                            "limit": limit,
                            "offset": offset,
                            "order": "liquidityClob",
                            "ascending": "false",
                        },
                    )
                    resp.raise_for_status()
                    events = resp.json()
                    if not events:
                        break
                    all_events.extend(events)
                    if len(events) < limit:
                        break
                    offset += limit
                    # Don't paginate too deep
                    if offset >= 500:
                        break
                except httpx.HTTPError as e:
                    logger.error("Gamma API request failed (offset=%d): %s", offset, e)
                    break

        logger.info("Fetched %d events from Gamma API", len(all_events))
        return all_events

    def _is_nba_event(self, slug: str, title: str) -> bool:
        """Check if this event is NBA (not college, not esports, etc.)."""
        slug_lower = slug.lower()
        title_lower = title.lower()

        # Reject non-NBA prefixes
        for prefix in _REJECT_PREFIXES:
            if slug_lower.startswith(prefix):
                return False

        # Reject non-NBA content
        for pattern in ("lol-", "csgo-", "dota-", "valorant-"):
            if pattern in slug_lower:
                return False

        # Must be NBA
        return slug_lower.startswith("nba-") or "nba" in title_lower

    def _passes_filters(self, market: Market) -> bool:
        """Apply all filtering rules to a market."""
        slug_lower = market.slug.lower()
        now = utcnow()

        # Reject player props and half lines
        for pattern in _REJECT_CONTAINS:
            if pattern in slug_lower:
                logger.debug("Rejected %s: contains %s", market.slug, pattern)
                return False

        # Must be active and not closed
        if not market.active or market.closed:
            return False

        # Must accept orders
        if not market.accepting_orders:
            return False

        # Liquidity floor
        if market.liquidity < 10000:
            logger.debug("Rejected %s: liquidity %.0f < 10000", market.slug, market.liquidity)
            return False

        # Price bounds: each outcome price between 0.03 and 0.97
        for price in market.outcome_prices:
            if price < 0.03 or price > 0.97:
                logger.debug("Rejected %s: price %.2f out of bounds", market.slug, price)
                return False

        # End date must be in the future
        if market.end_date:
            try:
                end_dt = parse_utc(market.end_date)
                if end_dt <= now:
                    return False
            except ValueError:
                pass

        # For game markets, game must not have started
        if market.is_game_market and market.game_start_time:
            try:
                start_dt = parse_utc(market.game_start_time)
                if start_dt <= now:
                    logger.debug("Rejected %s: game already started", market.slug)
                    return False
            except ValueError:
                pass

        # Skip unknown market types
        if market.market_type == MarketType.UNKNOWN:
            logger.debug("Rejected %s: unknown market type", market.slug)
            return False

        return True

    async def get_market_price(self, token_id: str) -> float | None:
        """Get current midpoint price for a token."""
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(self.config.CLOB_API_BASE)
            mid = client.get_midpoint(token_id)
            return float(mid.get("mid", 0))
        except Exception as e:
            logger.warning("Failed to get midpoint for %s: %s", token_id, e)
            return None

    async def get_order_book(self, token_id: str) -> dict | None:
        """Get order book for a token."""
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(self.config.CLOB_API_BASE)
            book = client.get_order_book(token_id)
            return {
                "bids": [(b.price, b.size) for b in book.bids] if book.bids else [],
                "asks": [(a.price, a.size) for a in book.asks] if book.asks else [],
            }
        except Exception as e:
            logger.warning("Failed to get order book for %s: %s", token_id, e)
            return None
