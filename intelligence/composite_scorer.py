"""Tier 3B: Composite Scorer — Dynamic Position Sizing / Composite Confidence.

Combines all intelligence signal sources into a single weighted composite score
per market. Used by the events agent to determine bet sizing and confidence.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime

from intelligence.models import CompositeScore, Signal
from nba_agent.utils import utcnow

logger = logging.getLogger("intelligence.composite_scorer")

# Base rate adjustment: 76% of markets resolve NO historically.
# Nudges ambiguous signals toward NO.
NO_BIAS_FACTOR: float = float(os.getenv("EVENTS_NO_BIAS_FACTOR", "0.06"))

# Env var names for each source's ENABLED flag
_SOURCE_ENABLED_VARS: dict[str, str] = {
    "metaculus": "METACULUS_ENABLED",
    "x_scanner": "X_SCANNER_ENABLED",
    "orderbook": "ORDERBOOK_INTEL_ENABLED",
    "whale_tracker": "WHALE_TRACKER_ENABLED",
    "google_trends": "GOOGLE_TRENDS_ENABLED",
    "congress": "CONGRESS_TRACKER_ENABLED",
    "cross_market": "CROSS_MARKET_ENABLED",
}


def _is_source_enabled(source: str) -> bool:
    """Check if a source is enabled via its env var (default true)."""
    env_var = _SOURCE_ENABLED_VARS.get(source)
    if env_var is None:
        return True
    return os.getenv(env_var, "true").lower() == "true"


def _get_active_weights(base_weights: dict[str, float]) -> dict[str, float]:
    """Redistribute weights from disabled sources proportionally to active ones.

    When any source has ENABLED=false, its weight is redistributed proportionally
    to the remaining active sources so they still sum to 1.0.
    """
    active = {s: w for s, w in base_weights.items() if _is_source_enabled(s)}
    disabled = {s: w for s, w in base_weights.items() if not _is_source_enabled(s)}

    if not disabled:
        return dict(base_weights)

    if not active:
        return dict(base_weights)

    disabled_weight = sum(disabled.values())
    active_total = sum(active.values())

    redistributed = {}
    for source, weight in active.items():
        proportion = weight / active_total if active_total > 0 else 0
        redistributed[source] = weight + (disabled_weight * proportion)

    for source in disabled:
        redistributed[source] = 0.0

    logger.info(
        "Weight redistribution: disabled=%s, redistributed %.2f weight to %d active sources",
        list(disabled.keys()), disabled_weight, len(active),
    )
    return redistributed


class CompositeScorer:
    """Combines all signal sources into weighted composite confidence scores."""

    # Default signal source weights (sum to 1.0)
    # X scanner set to 0.0 — weight redistributed to other sources
    DEFAULT_WEIGHTS: dict[str, float] = {
        "metaculus": 0.30,
        "x_scanner": 0.00,
        "orderbook": 0.20,
        "whale_tracker": 0.20,
        "google_trends": 0.12,
        "congress": 0.10,
        "cross_market": 0.08,
    }

    def __init__(self) -> None:
        # Compute runtime weights accounting for disabled sources
        self.WEIGHTS = _get_active_weights(self.DEFAULT_WEIGHTS)

    # Confidence tier thresholds and max bet percentages
    TIERS: list[tuple[float, str, float]] = [
        (0.8, "VERY_HIGH", 0.02),
        (0.6, "HIGH", 0.015),
        (0.4, "MEDIUM", 0.01),
        (0.0, "LOW", 0.0),
    ]

    def score(
        self,
        market_id: str,
        signals: list[Signal],
        lifecycle_overrides: dict[str, float] | None = None,
        quality_multipliers: dict[str, float] | None = None,
    ) -> CompositeScore:
        """Combine signals into a composite score for a market.

        Args:
            market_id: Market identifier.
            signals: List of Signal objects for this market.
            lifecycle_overrides: {source: multiplier} from lifecycle stage.
                Applied on top of base weights.
            quality_multipliers: {source: multiplier} from live quality scorer.
                Applied on top of base weights.

        Weight stacking: final_weight = base_weight * lifecycle_override * quality_multiplier

        Steps:
        1. Group signals by source
        2. Take strongest signal per source (highest strength * confidence)
        3. Weighted average using stacked WEIGHTS
        4. Apply direction consensus bonus (5+ sources agree → 20% boost)
        5. Map to confidence tier and max bet pct
        """
        now = utcnow()
        lifecycle_overrides = lifecycle_overrides or {}
        quality_multipliers = quality_multipliers or {}

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

        # 3. Weighted average with stacked overrides
        weighted_sum = 0.0
        weight_total = 0.0
        for source, info in source_scores.items():
            base_weight = self.WEIGHTS.get(source, 0.05)
            lc_mult = lifecycle_overrides.get(source, 1.0)
            qa_mult = quality_multipliers.get(source, 1.0)
            final_weight = base_weight * lc_mult * qa_mult
            weighted_sum += info["score"] * final_weight
            weight_total += final_weight

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

        # 4b. NO bias: nudge ambiguous signals toward NO (76% base rate)
        if consensus_direction == "YES":
            composite = max(composite - NO_BIAS_FACTOR, 0.0)
        elif consensus_direction == "NO":
            composite = min(composite + NO_BIAS_FACTOR, 1.0)

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
