"""Agent 2 Scanner — finds soccer/football markets on Polymarket."""

import logging
import time

from agents.common.config import MIN_LIQUIDITY, MIN_VOLUME_24H
from agents.common.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

SOCCER_TAG_SLUGS = ["soccer", "football"]
SOCCER_TEXT_QUERIES = [
    "Premier League",
    "Champions League",
    "La Liga",
    "Serie A",
    "Bundesliga",
    "World Cup",
]


class SoccerScanner:
    """Scans Polymarket for soccer/football markets."""

    def __init__(self) -> None:
        self._client = PolymarketClient()
        self._analyzed_slugs: dict[str, float] = {}
        self._cooldown = 3600 * 2  # 2-hour cooldown
        self.markets_scanned = 0

    def scan(self) -> list[dict]:
        """Return list of soccer market candidates."""
        logger.info("Agent 2 — scanning for soccer markets...")
        self.markets_scanned = 0
        seen_ids: set[str] = set()
        candidates = []
        now = time.time()

        # Search by tag slugs
        for tag in SOCCER_TAG_SLUGS:
            events = self._client.fetch_events(
                active=True, closed=False, limit=50, tag_slug=tag,
            )
            self._process_events(events, candidates, seen_ids, now)
            time.sleep(0.2)

        # Search by text queries
        for query in SOCCER_TEXT_QUERIES:
            events = self._client.fetch_events(
                active=True, closed=False, limit=30, text_query=query,
            )
            self._process_events(events, candidates, seen_ids, now)
            time.sleep(0.2)

        logger.info("Agent 2 — %d soccer candidates from %d markets", len(candidates), self.markets_scanned)
        return candidates

    def _process_events(
        self,
        events: list[dict],
        candidates: list[dict],
        seen_ids: set[str],
        now: float,
    ) -> None:
        for event in events:
            event_id = event.get("id", "")
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)

            markets = event.get("markets", [])
            for market in markets:
                self.markets_scanned += 1
                slug = market.get("slug") or market.get("conditionId", "")

                if slug in self._analyzed_slugs and now - self._analyzed_slugs[slug] < self._cooldown:
                    continue

                if not market.get("acceptingOrders", True):
                    continue
                if market.get("closed", False):
                    continue

                liquidity = _safe_float(market.get("liquidity"))
                if liquidity is not None and liquidity < MIN_LIQUIDITY:
                    continue

                vol_24h = _safe_float(market.get("volume24hr", market.get("volume24Hr")))
                if vol_24h is not None and vol_24h < MIN_VOLUME_24H:
                    continue

                yes_price = self._client.get_market_yes_price(market)
                if yes_price is None or yes_price <= 0.01 or yes_price >= 0.99:
                    continue

                market["_yes_price"] = yes_price
                market["_event"] = event
                market["_event_url"] = self._client.build_event_url(event)
                candidates.append(market)
                self._analyzed_slugs[slug] = now


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
