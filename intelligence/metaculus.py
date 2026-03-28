"""Tier 1C: Metaculus Consensus Comparison.

Fetches Metaculus API community predictions, fuzzy-matches to Polymarket markets,
and flags divergences > 5% as edge signals.

No API key required — Metaculus API is public for read access.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger("intelligence.metaculus")


import re as _re

# Synonym mappings for common prediction-market topics
_KEYWORD_SYNONYMS = {
    "bitcoin": {"btc", "bitcoin", "crypto"},
    "btc": {"btc", "bitcoin", "crypto"},
    "federal reserve": {"fed", "federal reserve", "fomc"},
    "fed": {"fed", "federal reserve", "fomc"},
    "crude oil": {"oil", "crude oil", "oil price", "petroleum"},
    "oil": {"oil", "crude oil", "oil price", "petroleum"},
    "interest rate": {"interest rate", "fed rate", "rates", "fomc"},
    "trump": {"trump", "donald trump"},
    "biden": {"biden", "joe biden"},
    "inflation": {"inflation", "cpi", "consumer price"},
    "gdp": {"gdp", "economic growth", "gross domestic product"},
    "ukraine": {"ukraine", "russia ukraine", "kyiv"},
    "china": {"china", "beijing", "prc"},
    "taiwan": {"taiwan", "strait"},
}


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip punctuation."""
    return _re.sub(r"[^\w\s]", " ", text.lower()).strip()


def _extract_entities(text: str) -> set[str]:
    """Extract key entities: capitalized words, numbers, dates, known terms."""
    entities: set[str] = set()
    normalized = _normalize_text(text)
    words = normalized.split()

    # Numbers (years, percentages, amounts)
    for w in words:
        if _re.match(r"\d{4}$", w):  # Year
            entities.add(w)
        elif _re.match(r"\d+", w):  # Any number
            entities.add(w)

    # Named entities: words that were capitalized in original text
    for match in _re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text):
        entities.add(match.group().lower())

    # Known synonym keys
    for key in _KEYWORD_SYNONYMS:
        if key in normalized:
            entities.update(_KEYWORD_SYNONYMS[key])

    return entities


def _keyword_overlap_score(a: str, b: str) -> int:
    """Score based on meaningful keyword overlap between two strings.

    Returns a score 0-100 where:
    - Entity overlap (names, numbers, known terms) is weighted heavily
    - Common content words are weighted normally
    """
    norm_a = _normalize_text(a)
    norm_b = _normalize_text(b)

    # Extract entities
    entities_a = _extract_entities(a)
    entities_b = _extract_entities(b)
    entity_overlap = len(entities_a & entities_b)

    # Content word overlap (exclude very common words)
    stop_words = {
        "will", "the", "a", "an", "be", "is", "are", "was", "were", "by",
        "in", "on", "at", "to", "for", "of", "with", "and", "or", "that",
        "this", "it", "do", "does", "did", "has", "have", "had", "not", "no",
        "yes", "before", "after", "than", "more", "most", "what", "when",
        "where", "who", "how", "which", "there", "here", "if",
    }
    words_a = {w for w in norm_a.split() if w not in stop_words and len(w) > 2}
    words_b = {w for w in norm_b.split() if w not in stop_words and len(w) > 2}

    if not words_a or not words_b:
        return 0

    word_overlap = len(words_a & words_b)
    total_unique = len(words_a | words_b)

    # Weighted score: entity matches count 3x
    raw_score = (word_overlap + entity_overlap * 3) / (total_unique + entity_overlap * 2) if total_unique > 0 else 0
    return int(min(raw_score * 100, 100))


def _fuzzy_ratio(a: str, b: str) -> int:
    """Fuzzy string match ratio. Uses thefuzz if available, else simple fallback."""
    try:
        from thefuzz import fuzz
        return fuzz.ratio(a.lower(), b.lower())
    except ImportError:
        # Simple fallback: word overlap ratio
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0
        overlap = len(words_a & words_b)
        return int(100 * (2 * overlap) / (len(words_a) + len(words_b)))


class MetaculusCompare:
    """Compares Polymarket prices to Metaculus community forecasts."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()
        self._matches_path = self.config.DATA_DIR / "metaculus_matches.json"
        self._divergences_path = self.config.DATA_DIR / "metaculus_divergences.json"
        self._cached_matches: dict = {}  # poly_id -> metaculus_question_id

    async def scan(self, active_markets: list) -> list[Signal]:
        """Compare Polymarket markets to Metaculus predictions. Returns signals."""
        if not self.config.is_enabled("metaculus"):
            logger.debug("Metaculus comparison disabled")
            return []

        self._load_cached_matches()

        # Fetch open binary questions from Metaculus
        meta_questions = await self._fetch_metaculus_questions()
        if not meta_questions:
            logger.warning("No Metaculus questions fetched")
            return []

        signals: list[Signal] = []
        divergences: list[dict] = []

        for market in active_markets:
            try:
                result = await asyncio.wait_for(
                    self._compare_market(market, meta_questions),
                    timeout=self.config.MODULE_TIMEOUT,
                )
                if result:
                    sig, div = result
                    signals.append(sig)
                    divergences.append(div)
            except asyncio.TimeoutError:
                logger.warning("Metaculus comparison timed out for %s", getattr(market, "id", "?"))
            except Exception as e:
                logger.error("Metaculus error for %s: %s", getattr(market, "id", "?"), e)

        # Persist
        self._save_cached_matches()
        self._save_divergences(divergences)

        return signals

    async def _fetch_metaculus_questions(self) -> list[dict]:
        """Fetch open binary questions from Metaculus API.

        Fetches using multiple sort orders to get broader coverage.
        """
        url = f"{self.config.METACULUS_BASE_URL}/questions/"
        all_questions: dict[str, dict] = {}  # keyed by question ID to dedup

        # Try multiple sort orders for broader coverage
        sort_orders = ["-activity", "-publish_time", "-forecasters_count"]

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                for sort_order in sort_orders:
                    try:
                        params = {
                            "status": "open",
                            "type": "binary",
                            "limit": 200,
                            "order_by": sort_order,
                        }
                        resp = await client.get(url, params=params)
                        resp.raise_for_status()
                        data = resp.json()
                        questions = data.get("results", [])
                        for q in questions:
                            qid = str(q.get("id", ""))
                            if qid and qid not in all_questions:
                                all_questions[qid] = q
                        logger.debug("Metaculus fetch (%s): %d questions", sort_order, len(questions))
                    except Exception as e:
                        logger.warning("Metaculus fetch (%s) failed: %s", sort_order, e)

        except httpx.HTTPError as e:
            logger.error("Metaculus API request failed: %s", e)
            return []

        result = list(all_questions.values())
        logger.info("Fetched %d unique Metaculus questions (across %d sort orders)", len(result), len(sort_orders))
        return result

    async def _compare_market(
        self, market, meta_questions: list[dict]
    ) -> tuple[Signal, dict] | None:
        """Match a Polymarket market to a Metaculus question and check divergence."""
        market_id = getattr(market, "id", "")
        question = getattr(market, "question", "")
        if not question:
            return None

        # Check cache first
        cached_meta_id = self._cached_matches.get(market_id)
        meta_q = None

        if cached_meta_id:
            meta_q = next(
                (q for q in meta_questions if str(q.get("id", "")) == str(cached_meta_id)),
                None,
            )

        # If no cached match, fuzzy-match
        if meta_q is None:
            meta_q = self._fuzzy_match(question, meta_questions)
            if meta_q:
                self._cached_matches[market_id] = str(meta_q.get("id", ""))

        if meta_q is None:
            return None

        # Get Metaculus community prediction
        meta_prediction = self._get_metaculus_prediction(meta_q)
        if meta_prediction is None:
            return None

        # Get Polymarket price (YES side)
        prices = getattr(market, "outcome_prices", [])
        if not prices:
            return None
        poly_price = prices[0]  # YES price

        # Calculate divergence
        divergence = meta_prediction - poly_price

        if abs(divergence) < self.config.METACULUS_DIVERGENCE_THRESHOLD:
            return None

        # Signal: if Metaculus is higher than Polymarket → BUY YES
        # If Metaculus is lower than Polymarket → BUY NO
        now = utcnow()
        direction = "YES" if divergence > 0 else "NO"
        strength = min(abs(divergence) / 0.20, 1.0)  # 20% divergence = max strength

        # Confidence based on Metaculus forecaster count
        forecaster_count = meta_q.get("number_of_forecasters", 0)
        confidence = min(forecaster_count / 100, 1.0)

        signal = Signal(
            source="metaculus",
            market_id=market_id,
            market_question=question,
            signal_type="divergence",
            direction=direction,
            strength=strength,
            confidence=confidence,
            details={
                "metaculus_prediction": round(meta_prediction, 3),
                "polymarket_price": round(poly_price, 3),
                "divergence": round(divergence, 3),
                "metaculus_question": meta_q.get("title", ""),
                "metaculus_id": meta_q.get("id"),
                "forecaster_count": forecaster_count,
            },
            timestamp=now,
            expires_at=now + timedelta(hours=2),
        )

        div_record = {
            "market_id": market_id,
            "metaculus_id": meta_q.get("id"),
            "divergence": round(divergence, 3),
            "timestamp": now.isoformat(),
        }

        return signal, div_record

    def _fuzzy_match(self, poly_question: str, meta_questions: list[dict]) -> dict | None:
        """Find the best Metaculus question match for a Polymarket question.

        Uses a combined approach:
        1. Standard fuzzy string ratio
        2. Keyword/entity overlap scoring
        Takes the max of both methods, with a lowered threshold (40) to catch
        matches where platforms word things very differently.
        """
        threshold = min(self.config.METACULUS_FUZZY_THRESHOLD, 40)
        best_match = None
        best_score = 0

        for q in meta_questions:
            meta_title = q.get("title", "")
            if not meta_title:
                continue

            # Score using both methods, take the best
            fuzzy_score = _fuzzy_ratio(poly_question, meta_title)
            keyword_score = _keyword_overlap_score(poly_question, meta_title)
            score = max(fuzzy_score, keyword_score)

            if score > best_score and score >= threshold:
                best_score = score
                best_match = q

        if best_match:
            logger.debug(
                "Matched Poly '%s' -> Meta '%s' (score=%d)",
                poly_question[:50],
                best_match.get("title", "")[:50],
                best_score,
            )

        return best_match

    def _get_metaculus_prediction(self, question: dict) -> float | None:
        """Extract community median prediction from a Metaculus question."""
        # Try community_prediction field first
        community = question.get("community_prediction", {})
        if isinstance(community, dict):
            full = community.get("full", {})
            if isinstance(full, dict):
                q2 = full.get("q2")  # median
                if q2 is not None:
                    return float(q2)

        # Fallback: prediction_timeseries last entry
        timeseries = question.get("prediction_timeseries", [])
        if timeseries:
            last_entry = timeseries[-1]
            if isinstance(last_entry, dict):
                community_pred = last_entry.get("community_prediction")
                if community_pred is not None:
                    return float(community_pred)

        return None

    def _load_cached_matches(self) -> None:
        """Load cached market-to-question mappings from disk."""
        self._cached_matches = load_json(self._matches_path, {})

    def _save_cached_matches(self) -> None:
        """Persist cached matches to disk."""
        try:
            atomic_json_write(self._matches_path, self._cached_matches)
        except Exception as e:
            logger.warning("Failed to save Metaculus matches: %s", e)

    def _save_divergences(self, divergences: list[dict]) -> None:
        """Persist divergence records for dashboard consumption."""
        if not divergences:
            return
        try:
            existing = load_json(self._divergences_path, [])
            if not isinstance(existing, list):
                existing = []
            existing.extend(divergences)
            # Keep last 500 records
            existing = existing[-500:]
            atomic_json_write(self._divergences_path, existing)
        except Exception as e:
            logger.warning("Failed to save Metaculus divergences: %s", e)
