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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from shared.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.gdelt")

GDELT_API_BASE = "http://api.gdeltproject.org/api/v2/doc/doc"

# Minimum 24h volume for a market to be queried via GDELT (rate-limit budget)
GDELT_MIN_VOLUME = 50_000

# ---------------------------------------------------------------------------
# Keyword extraction: geopolitical entities, leaders, and noise filters
# ---------------------------------------------------------------------------
_GEO_ENTITIES = frozenset({
    "us", "usa", "iran", "russia", "ukraine", "china", "israel", "nato",
    "eu", "un", "north korea", "syria", "gaza", "taiwan", "india",
    "pakistan", "houthi", "hezbollah", "hamas", "taliban", "iraq",
    "afghanistan", "yemen", "lebanon", "turkey", "saudi arabia",
    "south korea", "japan", "mexico", "canada", "brazil", "uk",
    "germany", "france", "australia", "wti", "opec",
})
_LEADERS = frozenset({
    "trump", "putin", "xi", "jinping", "netanyahu", "zelensky", "khamenei",
    "biden", "modi", "erdogan", "macron", "scholz", "starmer", "milei",
})
# GDELT rejects keywords shorter than 3 characters. Map abbreviations to full forms.
_SHORT_TO_FULL = {
    "us": "United States",
    "uk": "United Kingdom",
    "eu": "European Union",
    "un": "United Nations",
    "xi": "Jinping",
}
_MONTH_NAMES = frozenset({
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
})
_QUESTION_WORDS = frozenset({
    "will", "would", "could", "should", "did", "does", "do", "is", "are",
    "was", "were", "has", "have", "the", "by", "before", "after", "in", "on",
    "be", "been", "being", "get", "win", "lose", "hit", "a", "an", "or",
    "and", "of", "to", "for", "at", "with", "not", "no", "yes", "if",
    "than", "more", "most", "any", "all", "some", "its", "into", "over",
    "under", "about", "from", "each", "but", "their", "other", "between",
    "through", "during", "also", "much", "many", "there", "here",
    "what", "when", "where", "who", "how", "which", "that", "this", "it",
})


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
    1. Match known geopolitical entities and leaders (word-boundary, case-insensitive).
    2. Extract proper nouns including ALL-CAPS tokens (US, NATO, WTI).
    3. Filter out month names and common question/stop words.
    """
    q_lower = question.lower()
    noise = _QUESTION_WORDS | _MONTH_NAMES

    # 1. Check for known entities with word boundaries
    found_entities: list[str] = []
    for entity in _GEO_ENTITIES | _LEADERS:
        # Use regex word boundary to avoid "un" matching inside "June"
        if re.search(r"\b" + re.escape(entity) + r"\b", q_lower):
            found_entities.append(entity)

    # 2. Extract individual proper nouns (Capitalized or ALL-CAPS 2-5 chars)
    proper_nouns = re.findall(r"\b[A-Z][a-z]+\b", question)
    allcaps = re.findall(r"\b[A-Z]{2,5}\b", question)
    candidates = list(dict.fromkeys(proper_nouns + allcaps))  # dedupe, preserve order

    # Filter noise words individually
    candidates = [w for w in candidates if w.lower() not in noise]

    # 3. Build query from entities + proper nouns
    raw_parts: list[str] = list(dict.fromkeys(
        found_entities + [w.lower() for w in candidates[:4]]
    ))
    raw_parts = [p for p in raw_parts if len(p) > 1 and p not in _MONTH_NAMES]

    # 4. Expand short abbreviations that GDELT rejects (< 3 chars)
    parts: list[str] = []
    for p in raw_parts:
        if p in _SHORT_TO_FULL:
            parts.append(f'"{_SHORT_TO_FULL[p]}"')  # quoted phrase for GDELT
        elif len(p) >= 3:
            parts.append(p)
        # else: skip too-short keywords without a mapping

    if parts:
        return " ".join(parts[:4])
    return ""


class GDELTMonitor:
    """Monitors GDELT for news signals related to prediction markets."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._cache_path = self.config.DATA_DIR / "gdelt_cache.json"
        self._article_history: dict[str, list[int]] = {}  # market_id -> [counts]
        # Rate limiting: GDELT allows 1 request per 5 seconds
        self._rate_limiter = asyncio.Semaphore(1)
        self._last_request_time: float = 0.0

    async def _throttled_get(self, client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
        """Make a rate-limited GET request to GDELT (max 1 req / 5.5s)."""
        async with self._rate_limiter:
            elapsed = time.time() - self._last_request_time
            if elapsed < 5.5:
                await asyncio.sleep(5.5 - elapsed)
            self._last_request_time = time.time()
            return await client.get(url, params=params)

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan GDELT for news signals related to active markets.

        Only queries markets with volume_24h >= $50K to stay under rate limits.
        """
        # Filter to high-volume markets only (rate-limit budget)
        high_vol = [m for m in active_markets if getattr(m, "volume_24h", 0) >= GDELT_MIN_VOLUME]
        logger.info(
            "GDELT: scanning %d high-volume markets (of %d total)",
            len(high_vol), len(active_markets),
        )

        signals: list[Signal] = []
        self._load_cache()

        for market in high_vol:
            try:
                signal = await asyncio.wait_for(
                    self._scan_market(market),
                    timeout=30,
                )
                if signal:
                    signals.append(signal)
            except asyncio.TimeoutError:
                logger.debug("GDELT timeout for %s", getattr(market, "id", "?"))
            except Exception as e:
                logger.debug("GDELT error for %s: %s", getattr(market, "id", "?"), e)

        self._save_cache()
        logger.info("GDELT scan: %d signals from %d markets", len(signals), len(high_vol))
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
            async with httpx.AsyncClient(timeout=15.0) as client:
                params = {
                    "query": query,
                    "mode": "TimelineVol",  # was ArtCount which is invalid in v2
                    "format": "json",
                    "timespan": timespan,
                }
                resp = await self._throttled_get(client, GDELT_API_BASE, params)
                if resp.status_code != 200:
                    return 0
                data = resp.json()
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
            async with httpx.AsyncClient(timeout=15.0) as client:
                params = {
                    "query": query,
                    "mode": "ToneChart",
                    "format": "json",
                    "timespan": "24h",
                }
                resp = await self._throttled_get(client, GDELT_API_BASE, params)
                if resp.status_code != 200:
                    return 0.0
                data = resp.json()
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


# Alias for backwards compatibility
GDELTAnalyzer = GDELTMonitor
