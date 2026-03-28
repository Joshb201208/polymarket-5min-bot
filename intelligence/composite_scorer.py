"""Tier 3B: Composite Scorer — Dynamic Position Sizing / Composite Confidence.

Combines all intelligence signal sources into a single weighted composite score
per market. Used by the events agent to determine bet sizing and confidence.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from intelligence.models import CompositeScore, Signal
from nba_agent.utils import utcnow

logger = logging.getLogger("intelligence.composite_scorer")


class CompositeScorer:
    """Combines all signal sources into weighted composite confidence scores."""

    # Signal source weights (sum to 1.0)
    WEIGHTS: dict[str, float] = {
        "metaculus": 0.25,
        "x_scanner": 0.20,
        "orderbook": 0.15,
        "whale_tracker": 0.15,
        "google_trends": 0.10,
        "congress": 0.08,
        "cross_market": 0.07,
    }

    # Confidence tier thresholds and max bet percentages
    TIERS: list[tuple[float, str, float]] = [
        (0.8, "VERY_HIGH", 0.02),
        (0.6, "HIGH", 0.015),
        (0.4, "MEDIUM", 0.01),
        (0.0, "LOW", 0.0),
    ]

    def score(self, market_id: str, signals: list[Signal]) -> CompositeScore:
        """Combine signals into a composite score for a market.

        Steps:
        1. Group signals by source
        2. Take strongest signal per source (highest strength * confidence)
        3. Weighted average using WEIGHTS
        4. Apply direction consensus bonus (5+ sources agree → 20% boost)
        5. Map to confidence tier and max bet pct
        """
        now = utcnow()

        # 1. Group signals by source, filter expired
        by_source: dict[str, list[Signal]] = defaultdict(list)
        for sig in signals:
            if hasattr(sig, "expires_at") and sig.expires_at:
                if isinstance(sig.expires_at, datetime) and sig.expires_at < now:
                    continue
            by_source[sig.source].append(sig)

        if not by_source:
            return self._empty_score(market_id, now)

        # 2. For each source, take the strongest signal
        source_scores: dict[str, dict] = {}
        direction_votes: dict[str, int] = defaultdict(int)

        for source, source_signals in by_source.items():
            best = max(source_signals, key=lambda s: s.strength * s.confidence)
            effective_score = best.strength * best.confidence
            source_scores[source] = {
                "score": effective_score,
                "direction": best.direction,
                "strength": best.strength,
                "confidence": best.confidence,
                "signal_type": best.signal_type,
            }
            if best.direction in ("YES", "NO"):
                direction_votes[best.direction] += 1

        # 3. Weighted average
        weighted_sum = 0.0
        weight_total = 0.0
        for source, info in source_scores.items():
            weight = self.WEIGHTS.get(source, 0.05)
            weighted_sum += info["score"] * weight
            weight_total += weight

        composite = weighted_sum / weight_total if weight_total > 0 else 0.0

        # 4. Direction consensus bonus
        total_directional = sum(direction_votes.values())
        if total_directional == 0:
            consensus_direction = "NEUTRAL"
            consensus_count = 0
        else:
            consensus_direction = max(direction_votes, key=direction_votes.get)
            consensus_count = direction_votes[consensus_direction]

            # Bonus: if 5+ sources agree on direction, boost composite by 20%
            if consensus_count >= 5:
                composite = min(composite * 1.20, 1.0)
            elif consensus_count >= 3:
                composite = min(composite * 1.10, 1.0)

        # 5. Map to confidence tier
        confidence_tier = "LOW"
        max_bet_pct = 0.0
        for threshold, tier, bet_pct in self.TIERS:
            if composite >= threshold:
                confidence_tier = tier
                max_bet_pct = bet_pct
                break

        return CompositeScore(
            market_id=market_id,
            composite=round(composite, 4),
            direction=consensus_direction,
            confidence_tier=confidence_tier,
            max_bet_pct=max_bet_pct,
            signal_breakdown=source_scores,
            consensus_count=consensus_count,
            timestamp=now,
        )

    def _empty_score(self, market_id: str, now: datetime) -> CompositeScore:
        """Return a zero-value composite score when no signals exist."""
        return CompositeScore(
            market_id=market_id,
            composite=0.0,
            direction="NEUTRAL",
            confidence_tier="LOW",
            max_bet_pct=0.0,
            signal_breakdown={},
            consensus_count=0,
            timestamp=now,
        )
