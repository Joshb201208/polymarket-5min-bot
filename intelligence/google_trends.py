"""Tier 2A: Google Trends Velocity Tracker.

Tracks search interest velocity for entities related to active markets.
Spikes in search interest precede market moves.

Uses pytrends (rate-limited aggressively — max 10 queries per scan cycle).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import partial

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.google_trends")

_STOP_WORDS = {
    "will", "the", "a", "an", "be", "is", "are", "was", "were",
    "by", "in", "on", "at", "to", "for", "of", "with", "and", "or",
    "that", "this", "it", "do", "does", "did", "has", "have", "had",
    "not", "no", "yes", "before", "after", "during", "than", "more",
    "most", "some", "any", "each", "every", "all", "both", "few",
    "what", "when", "where", "who", "how", "which", "there", "here",
}


class GoogleTrendsTracker:
    """Tracks Google search interest velocity for market-relevant keywords."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._history_path = self.config.DATA_DIR / "google_trends_history.json"
        self._history: dict = {}  # keyword -> list of {timestamp, interest}
        self._query_count = 0  # Track queries per scan cycle

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan Google Trends for velocity signals. Returns Signal list."""
        if not self.config.is_enabled("google_trends"):
            logger.debug("Google Trends tracker disabled")
            return []

        try:
            from pytrends.request import TrendReq
        except ImportError:
            logger.error("pytrends package not installed — Google Trends disabled")
            return []

        self._load_history()
        self._query_count = 0
        signals: list[Signal] = []

        # Extract keywords from all markets
        market_keywords: list[tuple] = []  # (market, keywords)
        for market in active_markets:
            question = getattr(market, "question", "")
            keywords = self._extract_keywords(question)
            if keywords:
                market_keywords.append((market, keywords))

        # Process in batches of 5 (pytrends limit)
        for market, keywords in market_keywords:
            if self._query_count >= self.config.GOOGLE_TRENDS_MAX_QUERIES:
                logger.info("Google Trends query limit reached (%d)", self._query_count)
                break

            try:
                market_signals = await asyncio.wait_for(
                    self._check_keywords(market, keywords),
                    timeout=self.config.MODULE_TIMEOUT,
                )
                signals.extend(market_signals)
            except asyncio.TimeoutError:
                logger.warning("Google Trends timed out for %s", getattr(market, "id", "?"))
            except Exception as e:
                logger.error("Google Trends error for %s: %s", getattr(market, "id", "?"), e)

        self._save_history()
        return signals

    async def _check_keywords(self, market, keywords: list[str]) -> list[Signal]:
        """Fetch Google Trends data for keywords and check velocity."""
        try:
            from pytrends.request import TrendReq
        except ImportError:
            return []

        market_id = getattr(market, "id", "")
        question = getattr(market, "question", "")
        now = utcnow()

        # Run pytrends in executor (it's synchronous and blocks)
        loop = asyncio.get_running_loop()
        try:
            interest_data = await loop.run_in_executor(
                None,
                partial(self._fetch_trends_sync, keywords),
            )
        except Exception as e:
            logger.warning("pytrends fetch failed for %s: %s", keywords, e)
            return []

        self._query_count += 1

        if interest_data is None:
            return []

        signals: list[Signal] = []
        for keyword, interest_value in interest_data.items():
            # Update history
            if keyword not in self._history:
                self._history[keyword] = []
            self._history[keyword].append({
                "timestamp": now.isoformat(),
                "interest": interest_value,
            })
            # Keep last 7 days
            cutoff = (now - timedelta(days=7)).isoformat()
            self._history[keyword] = [
                h for h in self._history[keyword] if h["timestamp"] > cutoff
            ]

            # Calculate velocity
            velocity = self._calculate_velocity(keyword, interest_value)
            if velocity >= self.config.GOOGLE_TRENDS_VELOCITY_THRESHOLD:
                signals.append(Signal(
                    source="google_trends",
                    market_id=market_id,
                    market_question=question,
                    signal_type="search_velocity",
                    direction="NEUTRAL",  # Trend spike doesn't indicate direction
                    strength=min(velocity / 5.0, 1.0),
                    confidence=0.4,  # Google Trends is a weak signal
                    details={
                        "keyword": keyword,
                        "current_interest": interest_value,
                        "velocity": round(velocity, 2),
                    },
                    timestamp=now,
                    expires_at=now + timedelta(hours=4),
                ))

        return signals

    def _fetch_trends_sync(self, keywords: list[str]) -> dict[str, float] | None:
        """Synchronous pytrends fetch — run in executor."""
        try:
            from pytrends.request import TrendReq
            pytrends = TrendReq(hl="en-US", tz=480, timeout=(10, 15))
            # Take up to 5 keywords (pytrends limit)
            kw_list = keywords[:5]
            pytrends.build_payload(kw_list, timeframe="now 7-d")
            df = pytrends.interest_over_time()

            if df is None or df.empty:
                return None

            # Get most recent value for each keyword
            result = {}
            for kw in kw_list:
                if kw in df.columns:
                    recent_values = df[kw].tail(24)  # Last 24 data points
                    if not recent_values.empty:
                        result[kw] = float(recent_values.iloc[-1])

            return result if result else None
        except Exception as e:
            logger.warning("pytrends error: %s", e)
            return None

    def _extract_keywords(self, market_question: str) -> list[str]:
        """NLP-lite keyword extraction from a market question."""
        # Clean and tokenize
        text = re.sub(r"[^\w\s]", "", market_question.lower())
        words = text.split()

        # Remove stop words, keep words > 2 chars
        keywords = [w for w in words if w not in _STOP_WORDS and len(w) > 2]

        if not keywords:
            return []

        # Build 1-2 word phrases (most effective for Google Trends)
        phrases = []
        # Single important words
        for w in keywords[:3]:
            phrases.append(w)

        # Bigrams from adjacent keywords
        if len(keywords) >= 2:
            phrases.append(f"{keywords[0]} {keywords[1]}")

        # Deduplicate and limit to 3
        seen = set()
        unique = []
        for p in phrases:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        return unique[:3]

    def _calculate_velocity(self, keyword: str, current_interest: float) -> float:
        """Calculate velocity: current_interest / avg_last_24h."""
        history = self._history.get(keyword, [])
        if not history:
            return 0.0

        # Average of last 24h entries
        cutoff = (utcnow() - timedelta(hours=24)).isoformat()
        past_entries = [h for h in history if h["timestamp"] > cutoff]

        if not past_entries:
            return 0.0

        avg_interest = sum(h["interest"] for h in past_entries) / len(past_entries)
        if avg_interest <= 0:
            return 0.0

        return current_interest / avg_interest

    def _load_history(self) -> None:
        """Load trends history from disk."""
        self._history = load_json(self._history_path, {})

    def _save_history(self) -> None:
        """Persist trends history to disk."""
        try:
            atomic_json_write(self._history_path, self._history)
        except Exception as e:
            logger.warning("Failed to save Google Trends history: %s", e)
