"""Agent 1 Scanner — finds event/news markets on Polymarket (excludes sports + crypto 5-min)."""

import logging
import time
from datetime import datetime, timezone

from agents.common.config import MIN_LIQUIDITY, MIN_VOLUME_24H
from agents.common.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

# Tags/categories that belong to other agents or the existing bot
EXCLUDED_TAGS = {"soccer", "football", "nba", "basketball", "crypto-5min"}
EXCLUDED_KEYWORDS_IN_SLUG = {"5-minute", "5min", "1-minute"}
SPORTS_KEYWORDS = {
    "soccer", "football", "nba", "basketball", "nfl", "mlb", "nhl",
    "premier league", "champions league", "la liga", "serie a", "bundesliga",
    "world cup", "uefa",
}


class EventScanner:
    """Scans Polymarket for general event/news markets with edge potential."""

    def __init__(self) -> None:
        self._client = PolymarketClient()
        self._analyzed_slugs: dict[str, float] = {}  # slug → timestamp of last analysis
        self._cooldown = 3600 * 4  # 4-hour cooldown before re-analyzing same market
        self.markets_scanned = 0

    def scan(self) -> list[dict]:
        """Return list of candidate markets worth analyzing."""
        logger.info("Agent 1 — scanning for event markets...")
        self.markets_scanned = 0

        events = self._client.fetch_events(
            active=True,
            closed=False,
            limit=100,
            order="volume24hr",
            ascending=False,
        )
        logger.info("Fetched %d events from Gamma API", len(events))

        candidates = []
        now = time.time()

        for event in events:
            markets = event.get("markets", [])
            if not markets:
                continue

            event_slug = event.get("slug", "")
            tags = {t.get("slug", "").lower() for t in event.get("tags", [])} if event.get("tags") else set()

            # Skip sports (handled by agents 2 & 3)
            if tags & EXCLUDED_TAGS:
                continue
            title_lower = (event.get("title", "") + " " + event_slug).lower()
            if any(kw in title_lower for kw in SPORTS_KEYWORDS):
                continue

            for market in markets:
                self.markets_scanned += 1
                slug = market.get("slug") or market.get("conditionId", "")

                # Skip crypto 5-min markets (existing bot)
                if any(kw in slug.lower() for kw in EXCLUDED_KEYWORDS_IN_SLUG):
                    continue

                # Cooldown check
                if slug in self._analyzed_slugs:
                    if now - self._analyzed_slugs[slug] < self._cooldown:
                        continue

                # Liquidity filter
                liquidity = _safe_float(market.get("liquidity"))
                if liquidity is not None and liquidity < MIN_LIQUIDITY:
                    continue

                # Volume filter
                vol_24h = _safe_float(market.get("volume24hr", market.get("volume24Hr")))
                if vol_24h is not None and vol_24h < MIN_VOLUME_24H:
                    continue

                # Must be actively tradeable
                if not market.get("acceptingOrders", True):
                    continue
                if market.get("closed", False):
                    continue

                # Check resolution isn't too soon (< 1 hour)
                end_date = market.get("endDate", "")
                if end_date and _resolves_within_hours(end_date, 1):
                    continue

                # Get current price
                yes_price = self._client.get_market_yes_price(market)
                if yes_price is None or yes_price <= 0.01 or yes_price >= 0.99:
                    continue  # already decided or broken

                market["_yes_price"] = yes_price
                market["_event"] = event
                market["_event_url"] = self._client.build_event_url(event)
                candidates.append(market)

                self._analyzed_slugs[slug] = now

        logger.info("Agent 1 — %d candidates from %d markets scanned", len(candidates), self.markets_scanned)
        return candidates


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _resolves_within_hours(end_date_str: str, hours: float) -> bool:
    """Return True if the market resolves within `hours` from now."""
    try:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                end_dt = datetime.strptime(end_date_str, fmt).replace(tzinfo=timezone.utc)
                remaining = (end_dt - datetime.now(timezone.utc)).total_seconds()
                return remaining < hours * 3600
            except ValueError:
                continue
    except Exception:
        pass
    return False
