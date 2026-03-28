"""Gamma API market discovery and filtering for NHL markets."""

from __future__ import annotations

import logging
from datetime import timezone

import httpx

from nhl_agent.config import NHLConfig
from nhl_agent.models import NHLMarket, NHLMarketType
from nba_agent.utils import utcnow, parse_utc

logger = logging.getLogger(__name__)

# Slug patterns to reject
_REJECT_CONTAINS = (
    "-points-", "-goals-", "-assists-", "-saves-",
    "-1p-", "-2p-", "-3p-",
    "lol-", "csgo-", "dota-", "valorant-",
    "nba-", "nfl-", "mlb-",
)


class NHLPolymarketScanner:
    """Scans Polymarket Gamma API for NHL markets."""

    def __init__(self, config: NHLConfig | None = None) -> None:
        self.config = config or NHLConfig()
        self.base_url = self.config.GAMMA_API_BASE

    async def scan(self) -> list[NHLMarket]:
        """Fetch and filter NHL markets from Gamma API."""
        raw_events = await self._fetch_events()
        markets: list[NHLMarket] = []

        for event in raw_events:
            event_slug = event.get("slug", "")
            event_title = event.get("title", "")

            if not self._is_nhl_event(event_slug, event_title):
                continue

            for raw_market in event.get("markets", []):
                try:
                    market = NHLMarket.from_api(raw_market, event_slug, event_title)
                    market.market_type = market.detect_market_type()

                    if self._passes_filters(market):
                        markets.append(market)
                except Exception as e:
                    logger.warning("Failed to parse NHL market %s: %s", raw_market.get("id"), e)

        logger.info("NHL Scanner found %d markets after filtering", len(markets))
        return markets

    async def _fetch_events(self) -> list[dict]:
        """Fetch active events from Gamma API."""
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
                    if offset >= 500:
                        break
                except httpx.HTTPError as e:
                    logger.error("Gamma API request failed (offset=%d): %s", offset, e)
                    break

        return all_events

    def _is_nhl_event(self, slug: str, title: str) -> bool:
        """Check if this event is NHL."""
        slug_lower = slug.lower()
        title_lower = title.lower()
        return slug_lower.startswith("nhl-") or "nhl" in title_lower

    def _passes_filters(self, market: NHLMarket) -> bool:
        """Apply all filtering rules to a market."""
        slug_lower = market.slug.lower()
        now = utcnow()

        # Reject player props, period markets, non-NHL
        for pattern in _REJECT_CONTAINS:
            if pattern in slug_lower:
                return False

        if not market.active or market.closed:
            return False

        if not market.accepting_orders:
            return False

        # Liquidity floor (lower than NBA since NHL markets are less liquid)
        if market.liquidity < 5000:
            return False

        # Price bounds
        for price in market.outcome_prices:
            if price < 0.03 or price > 0.97:
                return False

        # End date must be in the future
        if market.end_date:
            try:
                end_dt = parse_utc(market.end_date)
                if end_dt <= now:
                    return False
            except ValueError:
                pass

        # Game must not have started
        if market.is_game_market and market.game_start_time:
            try:
                start_dt = parse_utc(market.game_start_time)
                if start_dt <= now:
                    return False
            except ValueError:
                pass

        # Skip unknown market types
        if market.market_type == NHLMarketType.UNKNOWN:
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
