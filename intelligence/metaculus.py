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
        """Fetch open binary questions from Metaculus API."""
        url = f"{self.config.METACULUS_BASE_URL}/questions/"
        params = {
            "status": "open",
            "type": "binary",
            "limit": 100,
            "order_by": "-activity",
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                questions = data.get("results", [])
                logger.info("Fetched %d Metaculus questions", len(questions))
                return questions
        except httpx.HTTPError as e:
            logger.error("Metaculus API request failed: %s", e)
            return []

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
        """Find the best Metaculus question match for a Polymarket question."""
        threshold = self.config.METACULUS_FUZZY_THRESHOLD
        best_match = None
        best_score = 0

        for q in meta_questions:
            meta_title = q.get("title", "")
            score = _fuzzy_ratio(poly_question, meta_title)
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
