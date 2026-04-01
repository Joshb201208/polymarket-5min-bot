"""GDELT Monitor — real-time global news event monitoring.

Polls the GDELT 2.0 API for article counts, sentiment, and event codes
related to open positions. Generates signals based on article velocity
spikes and sentiment shifts.

Signal weight: 0.20 (primary news signal)
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from shared.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.gdelt")

# Common stop words to filter when extracting keywords from market questions
_STOP_WORDS = frozenset({
    "will", "the", "a", "an", "be", "is", "are", "was", "were", "by",
    "in", "on", "at", "to", "for", "of", "with", "and", "or", "that",
    "this", "it", "do", "does", "did", "has", "have", "had", "not", "no",
    "yes", "before", "after", "than", "more", "most", "what", "when",
    "where", "who", "how", "which", "there", "here", "if", "any", "all",
    "some", "much", "many", "its", "into", "over", "under", "also",
    "between", "through", "during", "about", "from", "each", "but",
    "their", "other", "would", "could", "should", "may", "might",
})

GDELT_API_BASE = "http://api.gdeltproject.org/api/v2/doc/doc"


@dataclass
class GDELTSignal:
    """Structured GDELT signal data."""
    market_id: str
    article_count_15min: int = 0
    article_count_24hr: int = 0
    velocity_ratio: float = 0.0
    avg_tone: float = 0.0
    tone_direction: str = "neutral"
    top_themes: list[str] = field(default_factory=list)
    confidence: float = 0.0


def _extract_query_keywords(question: str) -> str:
    """Extract search keywords from a market question for GDELT queries.

    Strategy:
    - Extract proper nouns (capitalized words)
    - Extract key verbs and nouns (non-stop words)
    - Combine with OR/AND for GDELT query
    """
    # Extract proper nouns (capitalized multi-word names)
    proper_nouns = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", question)

    # Extract all meaningful words
    words = re.findall(r"\b\w+\b", question)
    keywords = [w for w in words if w.lower() not in _STOP_WORDS and len(w) > 2]

    # Proper nouns get priority; combine with OR
    if proper_nouns:
        # Use the most specific proper nouns as query
        query_parts = [f'"{pn}"' if " " in pn else pn for pn in proper_nouns[:3]]
        return " OR ".join(query_parts)

    # Fallback: join top keywords with AND
    if keywords:
        return " AND ".join(keywords[:4])

    return ""


class GDELTMonitor:
    """Monitors GDELT for news signals related to prediction markets."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._cache_path = self.config.DATA_DIR / "gdelt_cache.json"
        self._article_history: dict[str, list[int]] = {}  # market_id -> [counts]

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan GDELT for news signals related to active markets."""
        signals: list[Signal] = []
        self._load_cache()

        for market in active_markets:
            try:
                signal = await asyncio.wait_for(
                    self._scan_market(market),
                    timeout=15,
                )
                if signal:
                    signals.append(signal)
            except asyncio.TimeoutError:
                logger.debug("GDELT timeout for %s", getattr(market, "id", "?"))
            except Exception as e:
                logger.debug("GDELT error for %s: %s", getattr(market, "id", "?"), e)

        self._save_cache()
        logger.info("GDELT scan: %d signals from %d markets", len(signals), len(active_markets))
        return signals

    async def _scan_market(self, market) -> Signal | None:
        """Query GDELT for a single market and generate a signal if warranted."""
        market_id = getattr(market, "id", "")
        question = getattr(market, "question", "")
        if not question:
            return None

        query = _extract_query_keywords(question)
        if not query:
            return None

        # Fetch 15-minute and 24-hour article counts
        count_15min = await self._fetch_article_count(query, timespan="15min")
        count_24hr = await self._fetch_article_count(query, timespan="24h")

        # Calculate velocity: compare 15min rate to 24hr average rate
        expected_15min = count_24hr / 96.0 if count_24hr > 0 else 0
        velocity_ratio = count_15min / expected_15min if expected_15min > 0 else 0.0

        # Track history for this market
        if market_id not in self._article_history:
            self._article_history[market_id] = []
        self._article_history[market_id].append(count_15min)
        self._article_history[market_id] = self._article_history[market_id][-96:]  # Keep 24hr

        # Fetch tone/sentiment
        avg_tone = await self._fetch_tone(query)

        # Determine if this warrants a signal
        # Velocity spike > 2x or strong tone shift
        if velocity_ratio < 2.0 and abs(avg_tone) < 3.0:
            return None

        # Determine tone direction
        if avg_tone > 1.5:
            tone_direction = "positive"
        elif avg_tone < -1.5:
            tone_direction = "negative"
        else:
            tone_direction = "neutral"

        # Signal direction: positive tone = YES, negative = NO
        if tone_direction == "positive":
            direction = "YES"
        elif tone_direction == "negative":
            direction = "NO"
        else:
            direction = "NEUTRAL"

        # Strength based on velocity and tone combined
        velocity_strength = min(velocity_ratio / 10.0, 1.0)
        tone_strength = min(abs(avg_tone) / 10.0, 1.0)
        strength = max(velocity_strength, tone_strength)

        # Confidence based on article volume (more articles = more confident)
        confidence = min(count_24hr / 100.0, 1.0)

        now = utcnow()
        return Signal(
            source="gdelt",
            market_id=market_id,
            market_question=question,
            signal_type="news_velocity",
            direction=direction,
            strength=round(strength, 3),
            confidence=round(confidence, 3),
            details={
                "article_count_15min": count_15min,
                "article_count_24hr": count_24hr,
                "velocity_ratio": round(velocity_ratio, 2),
                "avg_tone": round(avg_tone, 2),
                "tone_direction": tone_direction,
                "query": query,
            },
            timestamp=now,
            expires_at=now + timedelta(minutes=30),
        )

    async def _fetch_article_count(self, query: str, timespan: str = "15min") -> int:
        """Fetch article count from GDELT for a query and timespan."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                params = {
                    "query": query,
                    "mode": "ArtCount",
                    "format": "json",
                    "timespan": timespan,
                }
                resp = await client.get(GDELT_API_BASE, params=params)
                if resp.status_code != 200:
                    return 0
                data = resp.json()
                # GDELT returns timeline data; sum all counts
                timeline = data.get("timeline", [])
                if not timeline:
                    return 0
                total = 0
                for series in timeline:
                    for point in series.get("data", []):
                        total += int(point.get("value", 0))
                return total
        except Exception as e:
            logger.debug("GDELT article count failed: %s", e)
            return 0

    async def _fetch_tone(self, query: str) -> float:
        """Fetch average tone from GDELT for a query (last 24h)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                params = {
                    "query": query,
                    "mode": "ToneChart",
                    "format": "json",
                    "timespan": "24h",
                }
                resp = await client.get(GDELT_API_BASE, params=params)
                if resp.status_code != 200:
                    return 0.0
                data = resp.json()
                # ToneChart returns tone timeline; average it
                timeline = data.get("timeline", [])
                if not timeline:
                    return 0.0
                tones = []
                for series in timeline:
                    for point in series.get("data", []):
                        try:
                            tones.append(float(point.get("value", 0)))
                        except (ValueError, TypeError):
                            pass
                return sum(tones) / len(tones) if tones else 0.0
        except Exception as e:
            logger.debug("GDELT tone fetch failed: %s", e)
            return 0.0

    def _load_cache(self) -> None:
        """Load article history cache from disk."""
        data = load_json(self._cache_path, {})
        self._article_history = data.get("article_history", {})

    def _save_cache(self) -> None:
        """Persist article history cache."""
        try:
            atomic_json_write(self._cache_path, {
                "article_history": self._article_history,
                "updated_at": utcnow().isoformat(),
            })
        except Exception as e:
            logger.warning("Failed to save GDELT cache: %s", e)
