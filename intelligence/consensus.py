"""Multi-Market Consensus — aggregate probabilities from multiple prediction markets.

Sources: Kalshi, Manifold Markets, PredictIt
Generates signals when Polymarket price diverges from the consensus of other platforms.

Signal weight: 0.15
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

logger = logging.getLogger("intelligence.consensus")

# Minimum consensus-poly divergence to signal
CONSENSUS_THRESHOLD = 0.05

# Platform weights for consensus calculation
PLATFORM_WEIGHTS = {
    "kalshi": 0.40,
    "manifold": 0.30,
    "predictit": 0.30,
}

_STOP_WORDS = frozenset({
    "will", "the", "a", "an", "be", "is", "are", "was", "were", "by",
    "in", "on", "at", "to", "for", "of", "with", "and", "or", "that",
    "this", "it", "do", "does", "did", "has", "have", "had", "not", "no",
    "yes", "before", "after", "what", "when", "where", "who", "how",
})


def _token_overlap(a: str, b: str) -> float:
    """Simple token overlap similarity."""
    norm = lambda s: re.sub(r"[^\w\s]", " ", s.lower())
    tokens_a = {w for w in norm(a).split() if w not in _STOP_WORDS and len(w) > 2}
    tokens_b = {w for w in norm(b).split() if w not in _STOP_WORDS and len(w) > 2}
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


class ConsensusAggregator:
    """Aggregates probabilities from multiple prediction market platforms."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._cache_path = self.config.DATA_DIR / "consensus_cache.json"

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan multiple platforms and generate consensus signals."""
        # Fetch data from all platforms concurrently
        manifold_markets, predictit_markets = await asyncio.gather(
            self._fetch_manifold(),
            self._fetch_predictit(),
            return_exceptions=True,
        )

        if isinstance(manifold_markets, Exception):
            logger.warning("Manifold fetch failed: %s", manifold_markets)
            manifold_markets = []
        if isinstance(predictit_markets, Exception):
            logger.warning("PredictIt fetch failed: %s", predictit_markets)
            predictit_markets = []

        # Try to get Kalshi markets from the Kalshi module (if loaded)
        kalshi_markets = self._get_kalshi_markets()

        signals: list[Signal] = []

        for market in active_markets:
            try:
                signal = self._build_consensus(
                    market, kalshi_markets, manifold_markets, predictit_markets,
                )
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug("Consensus error for %s: %s", getattr(market, "id", "?"), e)

        logger.info(
            "Consensus scan: %d signals (manifold=%d, predictit=%d, kalshi=%d)",
            len(signals), len(manifold_markets), len(predictit_markets), len(kalshi_markets),
        )
        return signals

    async def _fetch_manifold(self) -> list[dict]:
        """Fetch open binary markets from Manifold Markets."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.manifold.markets/v0/search-markets",
                    params={"term": "", "sort": "liquidity", "limit": 100},
                )
                if resp.status_code != 200:
                    return []
                markets = resp.json()
                # Filter to binary markets
                return [
                    m for m in markets
                    if m.get("outcomeType") == "BINARY" and not m.get("isResolved", False)
                ]
        except Exception as e:
            logger.debug("Manifold fetch failed: %s", e)
            return []

    async def _fetch_predictit(self) -> list[dict]:
        """Fetch active PredictIt markets."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://www.predictit.org/api/marketdata/all/",
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                markets = data.get("markets", [])
                # Flatten contracts
                result = []
                for market in markets:
                    for contract in market.get("contracts", []):
                        contract["market_name"] = market.get("name", "")
                        result.append(contract)
                return result
        except Exception as e:
            logger.debug("PredictIt fetch failed: %s", e)
            return []

    def _get_kalshi_markets(self) -> list[dict]:
        """Try to load Kalshi market data from cache file."""
        data = load_json(self.config.DATA_DIR / "kalshi_matches.json", {})
        # Return empty — Kalshi prices come from the kalshi module separately
        # We just track the matches here
        return []

    def _build_consensus(
        self,
        poly_market,
        kalshi_markets: list[dict],
        manifold_markets: list[dict],
        predictit_markets: list[dict],
    ) -> Signal | None:
        """Build consensus probability for a market across platforms."""
        market_id = getattr(poly_market, "id", "")
        question = getattr(poly_market, "question", "")
        if not question:
            return None

        prices = getattr(poly_market, "outcome_prices", [])
        if not prices:
            return None
        poly_price = float(prices[0])

        # Find matches on each platform
        platform_prices: dict[str, float] = {}

        # Manifold match
        manifold_match = self._match_manifold(question, manifold_markets)
        if manifold_match is not None:
            platform_prices["manifold"] = manifold_match

        # PredictIt match
        predictit_match = self._match_predictit(question, predictit_markets)
        if predictit_match is not None:
            platform_prices["predictit"] = predictit_match

        # Need at least 1 external platform match
        if not platform_prices:
            return None

        # Calculate weighted consensus
        total_weight = 0.0
        weighted_sum = 0.0
        for platform, price in platform_prices.items():
            weight = PLATFORM_WEIGHTS.get(platform, 0.25)
            weighted_sum += price * weight
            total_weight += weight

        if total_weight <= 0:
            return None

        consensus_price = weighted_sum / total_weight
        divergence = poly_price - consensus_price

        if abs(divergence) < CONSENSUS_THRESHOLD:
            return None

        # Positive divergence = overpriced on Poly (sell/NO); negative = underpriced (buy/YES)
        direction = "NO" if divergence > 0 else "YES"
        strength = min(abs(divergence) / 0.15, 1.0)
        confidence = min(len(platform_prices) / 3.0, 1.0)

        now = utcnow()
        return Signal(
            source="consensus",
            market_id=market_id,
            market_question=question,
            signal_type="multi_platform_consensus",
            direction=direction,
            strength=round(strength, 3),
            confidence=round(confidence, 3),
            details={
                "polymarket_price": round(poly_price, 3),
                "consensus_price": round(consensus_price, 3),
                "divergence": round(divergence, 3),
                "platform_prices": {k: round(v, 3) for k, v in platform_prices.items()},
                "platforms_matched": len(platform_prices),
            },
            timestamp=now,
            expires_at=now + timedelta(hours=2),
        )

    def _match_manifold(self, question: str, manifold_markets: list[dict]) -> float | None:
        """Find matching Manifold market and return its probability."""
        best_score = 0.0
        best_prob = None

        for m in manifold_markets:
            title = m.get("question", "")
            score = _token_overlap(question, title)
            if score > best_score and score >= 0.5:
                best_score = score
                best_prob = m.get("probability")

        if best_prob is not None:
            return float(best_prob)
        return None

    def _match_predictit(self, question: str, predictit_markets: list[dict]) -> float | None:
        """Find matching PredictIt contract and return its price."""
        best_score = 0.0
        best_price = None

        for c in predictit_markets:
            title = c.get("name", "") or c.get("market_name", "")
            score = _token_overlap(question, title)
            if score > best_score and score >= 0.5:
                best_score = score
                last_price = c.get("lastTradePrice")
                if last_price is not None:
                    best_price = float(last_price)

        return best_price
