"""Tier 1A: X/Twitter Real-Time Sentiment Scanner.

Works WITH or WITHOUT a TWITTER_BEARER_TOKEN.
- With token: uses X API v2 recent search endpoint.
- Without token: logs that scanning is disabled (graceful degradation).

All sentiment analysis is rule-based — no external LLM calls.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.x_scanner")

# ---------------------------------------------------------------------------
# Sentiment lexicons (rule-based, no LLM)
# ---------------------------------------------------------------------------
_POSITIVE_WORDS = {
    "bullish": 1.5, "surge": 1.2, "soar": 1.2, "rally": 1.0, "win": 0.8,
    "pass": 0.8, "approve": 1.0, "signed": 0.9, "victory": 1.0, "boom": 1.0,
    "breakthrough": 1.2, "deal": 0.7, "success": 0.8, "confirmed": 0.9,
    "gain": 0.7, "up": 0.4, "higher": 0.5, "positive": 0.6, "strong": 0.6,
    "support": 0.5, "likely": 0.5, "yes": 0.3, "agree": 0.5, "accept": 0.6,
    "moon": 1.0, "pump": 0.8, "green": 0.4, "ath": 1.0, "record": 0.7,
    "landslide": 1.0, "unanimous": 1.0, "bipartisan": 0.8, "historic": 0.7,
}

_NEGATIVE_WORDS = {
    "bearish": 1.5, "crash": 1.5, "dump": 1.2, "plunge": 1.2, "fail": 1.0,
    "reject": 1.0, "veto": 1.2, "block": 0.8, "defeat": 1.0, "loss": 0.8,
    "scandal": 1.0, "indictment": 1.2, "impeach": 1.0, "collapse": 1.2,
    "down": 0.4, "lower": 0.5, "negative": 0.6, "weak": 0.6, "oppose": 0.5,
    "unlikely": 0.5, "no": 0.3, "deny": 0.6, "rekt": 1.0, "rug": 1.2,
    "red": 0.4, "fear": 0.8, "panic": 1.0, "crisis": 1.0, "war": 0.8,
    "sanctions": 0.7, "tariff": 0.6, "ban": 0.8, "guilty": 1.0,
}

# ---------------------------------------------------------------------------
# Keyword lists for market matching
# ---------------------------------------------------------------------------
POLITICAL_KEYWORDS = [
    "trump", "biden", "executive order", "congress", "supreme court",
    "indictment", "impeach", "election", "primary", "senate", "house vote",
]
CRYPTO_KEYWORDS = [
    "bitcoin", "ethereum", "SEC crypto", "bitcoin ETF", "fed rate",
    "stablecoin", "defi regulation",
]
GEOPOLITICAL_KEYWORDS = [
    "sanctions", "tariff", "nato", "ukraine", "taiwan", "china trade",
    "missile", "ceasefire", "peace deal",
]


class XScanner:
    """Scans X/Twitter for sentiment signals relevant to active Polymarket markets."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._history_path = self.config.DATA_DIR / "x_sentiment_history.json"
        self._history: dict = {}  # market_id -> list of {timestamp, score}

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan X for sentiment on active markets. Returns Signal list."""
        if not self.config.is_enabled("x_scanner"):
            logger.debug("X scanner disabled")
            return []

        if not self.config.TWITTER_BEARER_TOKEN:
            logger.info("X scanner: no TWITTER_BEARER_TOKEN — running in disabled mode")
            return []

        self._load_history()
        signals: list[Signal] = []

        for market in active_markets:
            try:
                market_signals = await asyncio.wait_for(
                    self._scan_market(market),
                    timeout=self.config.MODULE_TIMEOUT,
                )
                signals.extend(market_signals)
            except asyncio.TimeoutError:
                logger.warning("X scan timed out for market %s", getattr(market, "id", "?"))
            except Exception as e:
                logger.error("X scan error for market %s: %s", getattr(market, "id", "?"), e)

        self._save_history()
        return signals

    async def _scan_market(self, market) -> list[Signal]:
        """Scan tweets for a single market and generate signals."""
        market_id = getattr(market, "id", "")
        question = getattr(market, "question", "")
        if not question:
            return []

        query = self._build_query(question)
        if not query:
            return []

        tweets = await self._fetch_tweets(query)
        if not tweets:
            return []

        # Filter by author quality
        quality_tweets = [
            t for t in tweets
            if self._passes_quality_filter(t)
        ]

        if not quality_tweets:
            return []

        # Score sentiment across all quality tweets
        scores = [self._score_sentiment(t.get("text", "")) for t in quality_tweets]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        # Calculate velocity
        velocity = self._calculate_velocity(market_id, avg_score)

        # Store in history
        now = utcnow()
        if market_id not in self._history:
            self._history[market_id] = []
        self._history[market_id].append({
            "timestamp": now.isoformat(),
            "score": avg_score,
            "tweet_count": len(quality_tweets),
        })
        # Keep last 24h only
        cutoff = (now - timedelta(hours=24)).isoformat()
        self._history[market_id] = [
            h for h in self._history[market_id] if h["timestamp"] > cutoff
        ]

        # Generate signal if velocity exceeds threshold
        if abs(velocity) < self.config.X_SENTIMENT_VELOCITY_THRESHOLD:
            return []

        direction = "YES" if avg_score > 0 else "NO" if avg_score < 0 else "NEUTRAL"
        strength = min(abs(velocity), 1.0)
        confidence = min(len(quality_tweets) / 20.0, 1.0)  # More tweets = higher confidence

        return [Signal(
            source="x_scanner",
            market_id=market_id,
            market_question=question,
            signal_type="sentiment",
            direction=direction,
            strength=strength,
            confidence=confidence,
            details={
                "avg_sentiment": round(avg_score, 3),
                "velocity": round(velocity, 3),
                "tweet_count": len(quality_tweets),
                "total_fetched": len(tweets),
            },
            timestamp=now,
            expires_at=now + timedelta(minutes=30),
        )]

    def _build_query(self, question: str) -> str:
        """Build an X API search query from a market question."""
        # Extract key terms (remove stop words, punctuation)
        stop_words = {
            "will", "the", "a", "an", "be", "is", "are", "was", "were",
            "by", "in", "on", "at", "to", "for", "of", "with", "and", "or",
            "that", "this", "it", "do", "does", "did", "has", "have", "had",
            "not", "no", "yes", "before", "after", "during", "than", "more",
            "most", "some", "any", "each", "every", "all", "both", "few",
        }
        words = re.sub(r"[^\w\s]", "", question.lower()).split()
        keywords = [w for w in words if w not in stop_words and len(w) > 2]

        if not keywords:
            return ""

        # Take up to 5 most distinctive words
        return " ".join(keywords[:5])

    async def _fetch_tweets(self, query: str) -> list[dict]:
        """Fetch recent tweets matching query from X API v2."""
        url = "https://api.twitter.com/2/tweets/search/recent"
        headers = {
            "Authorization": f"Bearer {self.config.TWITTER_BEARER_TOKEN}",
        }
        params = {
            "query": f"{query} -is:retweet lang:en",
            "max_results": 50,
            "tweet.fields": "created_at,public_metrics,author_id",
            "user.fields": "public_metrics,verified",
            "expansions": "author_id",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 429:
                    logger.warning("X API rate limited")
                    return []
                resp.raise_for_status()
                data = resp.json()

                tweets = data.get("data", [])
                # Attach user info to tweets
                users = {
                    u["id"]: u
                    for u in data.get("includes", {}).get("users", [])
                }
                for tweet in tweets:
                    author = users.get(tweet.get("author_id", ""), {})
                    tweet["_author"] = author

                return tweets
        except httpx.HTTPError as e:
            logger.error("X API request failed: %s", e)
            return []

    def _passes_quality_filter(self, tweet: dict) -> bool:
        """Filter tweets by author quality."""
        author = tweet.get("_author", {})
        metrics = author.get("public_metrics", {})
        followers = metrics.get("followers_count", 0)
        return followers >= self.config.X_MIN_FOLLOWERS

    def _score_sentiment(self, text: str) -> float:
        """Rule-based sentiment scoring. Returns -1.0 to 1.0."""
        text_lower = text.lower()
        pos_score = 0.0
        neg_score = 0.0

        for word, weight in _POSITIVE_WORDS.items():
            if word in text_lower:
                pos_score += weight

        for word, weight in _NEGATIVE_WORDS.items():
            if word in text_lower:
                neg_score += weight

        total = pos_score + neg_score
        if total == 0:
            return 0.0

        # Normalize to -1.0 to 1.0
        raw = (pos_score - neg_score) / total
        return max(-1.0, min(1.0, raw))

    def _calculate_velocity(self, market_id: str, current_score: float) -> float:
        """Compare current sentiment to historical average. Returns rate of change."""
        history = self._history.get(market_id, [])
        if not history:
            return 0.0

        # Average of last 24h scores
        past_scores = [h["score"] for h in history]
        if not past_scores:
            return 0.0

        avg_past = sum(past_scores) / len(past_scores)
        return current_score - avg_past

    def _load_history(self) -> None:
        """Load sentiment history from disk."""
        self._history = load_json(self._history_path, {})

    def _save_history(self) -> None:
        """Persist sentiment history to disk."""
        try:
            atomic_json_write(self._history_path, self._history)
        except Exception as e:
            logger.warning("Failed to save X sentiment history: %s", e)
