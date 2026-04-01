"""Kalshi Cross-Market Intelligence — arbitrage signals from CFTC-regulated market.

Compares Kalshi event prices to equivalent Polymarket markets.
When Kalshi price differs from Polymarket by >5%, generates a directional signal.

Signal weight: 0.20 (institutional money signal)
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from shared.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.kalshi")

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Minimum divergence to generate a signal
DIVERGENCE_THRESHOLD = 0.05

# Stop words for matching
_STOP_WORDS = frozenset({
    "will", "the", "a", "an", "be", "is", "are", "was", "were", "by",
    "in", "on", "at", "to", "for", "of", "with", "and", "or", "that",
    "this", "it", "do", "does", "did", "has", "have", "had", "not", "no",
    "yes", "before", "after", "what", "when", "where", "who", "how",
})


def _normalize(text: str) -> str:
    """Normalize text for comparison."""
    return re.sub(r"[^\w\s]", " ", text.lower()).strip()


def _token_overlap_score(a: str, b: str) -> float:
    """Calculate token overlap similarity between two strings.

    Returns 0.0-1.0 where 1.0 is perfect overlap.
    """
    tokens_a = {w for w in _normalize(a).split() if w not in _STOP_WORDS and len(w) > 2}
    tokens_b = {w for w in _normalize(b).split() if w not in _STOP_WORDS and len(w) > 2}

    if not tokens_a or not tokens_b:
        return 0.0

    overlap = len(tokens_a & tokens_b)
    total = len(tokens_a | tokens_b)
    return overlap / total if total > 0 else 0.0


class KalshiCrossMarket:
    """Compares Kalshi prices to Polymarket for cross-market arbitrage signals."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._cache_path = self.config.DATA_DIR / "kalshi_matches.json"
        self._cached_matches: dict[str, str] = {}  # poly_market_id -> kalshi_ticker
        self._kalshi_markets: list[dict] = []

    async def scan(self, active_markets: list) -> list[Signal]:
        """Compare active Polymarket markets to Kalshi prices."""
        self._load_cache()

        # Fetch Kalshi markets
        kalshi_markets = await self._fetch_kalshi_markets()
        if not kalshi_markets:
            logger.info("Kalshi: no markets fetched")
            return []

        self._kalshi_markets = kalshi_markets
        signals: list[Signal] = []

        for market in active_markets:
            try:
                signal = self._compare_market(market, kalshi_markets)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug("Kalshi comparison error for %s: %s", getattr(market, "id", "?"), e)

        self._save_cache()
        logger.info("Kalshi scan: %d signals from %d Kalshi markets", len(signals), len(kalshi_markets))
        return signals

    async def _fetch_kalshi_markets(self) -> list[dict]:
        """Fetch active Kalshi markets."""
        markets: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Fetch events first
                resp = await client.get(
                    f"{KALSHI_API_BASE}/events",
                    params={"status": "open", "limit": 100},
                    headers={"Accept": "application/json"},
                )
                if resp.status_code != 200:
                    logger.warning("Kalshi events API returned %d", resp.status_code)
                    return []

                data = resp.json()
                events = data.get("events", [])

                # Fetch markets for each event
                for event in events[:50]:  # Limit to avoid rate limits
                    event_ticker = event.get("event_ticker", "")
                    if not event_ticker:
                        continue

                    try:
                        resp2 = await client.get(
                            f"{KALSHI_API_BASE}/events/{event_ticker}/markets",
                            headers={"Accept": "application/json"},
                        )
                        if resp2.status_code == 200:
                            event_markets = resp2.json().get("markets", [])
                            for m in event_markets:
                                m["event_title"] = event.get("title", "")
                            markets.extend(event_markets)
                    except Exception:
                        pass

                    await asyncio.sleep(0.1)  # Rate limit

        except Exception as e:
            logger.warning("Kalshi API fetch failed: %s", e)

        return markets

    def _compare_market(self, poly_market, kalshi_markets: list[dict]) -> Signal | None:
        """Match and compare a single Polymarket market to Kalshi."""
        market_id = getattr(poly_market, "id", "")
        question = getattr(poly_market, "question", "")
        if not question:
            return None

        # Get Polymarket YES price
        prices = getattr(poly_market, "outcome_prices", [])
        if not prices:
            return None
        poly_price = float(prices[0])

        # Find matching Kalshi market
        kalshi_match = self._find_match(market_id, question, kalshi_markets)
        if not kalshi_match:
            return None

        # Get Kalshi price (yes_price is in cents on Kalshi, or 0-1 probability)
        kalshi_price = self._get_kalshi_price(kalshi_match)
        if kalshi_price is None:
            return None

        # Calculate divergence
        divergence = kalshi_price - poly_price

        if abs(divergence) < DIVERGENCE_THRESHOLD:
            return None

        # Signal direction: if Kalshi says higher prob, signal YES; lower, NO
        direction = "YES" if divergence > 0 else "NO"
        strength = min(abs(divergence) / 0.20, 1.0)

        # Confidence based on Kalshi volume
        volume = float(kalshi_match.get("volume", 0))
        confidence = min(volume / 10000.0, 1.0)

        now = utcnow()
        return Signal(
            source="kalshi",
            market_id=market_id,
            market_question=question,
            signal_type="cross_market_divergence",
            direction=direction,
            strength=round(strength, 3),
            confidence=round(max(confidence, 0.3), 3),
            details={
                "kalshi_price": round(kalshi_price, 3),
                "polymarket_price": round(poly_price, 3),
                "divergence": round(divergence, 3),
                "kalshi_ticker": kalshi_match.get("ticker", ""),
                "kalshi_title": kalshi_match.get("title", ""),
                "kalshi_volume": volume,
            },
            timestamp=now,
            expires_at=now + timedelta(hours=2),
        )

    def _find_match(self, market_id: str, question: str, kalshi_markets: list[dict]) -> dict | None:
        """Find matching Kalshi market by text similarity."""
        # Check cache
        cached_ticker = self._cached_matches.get(market_id)
        if cached_ticker:
            match = next((m for m in kalshi_markets if m.get("ticker") == cached_ticker), None)
            if match:
                return match

        # Fuzzy match
        best_match = None
        best_score = 0.0

        for km in kalshi_markets:
            kalshi_title = km.get("title", "") or km.get("event_title", "")
            if not kalshi_title:
                continue

            score = _token_overlap_score(question, kalshi_title)
            if score > best_score and score >= 0.6:
                best_score = score
                best_match = km

        if best_match:
            self._cached_matches[market_id] = best_match.get("ticker", "")

        return best_match

    def _get_kalshi_price(self, market: dict) -> float | None:
        """Extract YES probability from a Kalshi market."""
        # Kalshi uses yes_price in cents (0-100) or yes_bid/yes_ask
        yes_price = market.get("yes_price")
        if yes_price is not None:
            price = float(yes_price)
            return price / 100.0 if price > 1 else price

        yes_bid = market.get("yes_bid")
        yes_ask = market.get("yes_ask")
        if yes_bid is not None and yes_ask is not None:
            mid = (float(yes_bid) + float(yes_ask)) / 2.0
            return mid / 100.0 if mid > 1 else mid

        last_price = market.get("last_price")
        if last_price is not None:
            price = float(last_price)
            return price / 100.0 if price > 1 else price

        return None

    def get_kalshi_markets(self) -> list[dict]:
        """Return cached Kalshi markets for use by other modules."""
        return self._kalshi_markets

    def _load_cache(self) -> None:
        data = load_json(self._cache_path, {})
        self._cached_matches = data.get("matches", {})

    def _save_cache(self) -> None:
        try:
            atomic_json_write(self._cache_path, {
                "matches": self._cached_matches,
                "updated_at": utcnow().isoformat(),
            })
        except Exception as e:
            logger.warning("Failed to save Kalshi cache: %s", e)
