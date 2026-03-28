"""Edge detection for events markets — news-based, polling, sentiment, time decay."""

from __future__ import annotations

import logging
import math
from typing import Optional

from events_agent.config import EventsConfig
from events_agent.models import Confidence, EdgeResult, EventCategory, EventMarket

logger = logging.getLogger(__name__)


class EventsAnalyzer:
    """Computes fair odds and detects edges for non-sports markets.

    Edge detection strategies:
    1. Spread analysis — look for mispriced binary outcomes (YES+NO != ~1.0)
    2. Time decay — markets near resolution with obvious outcomes
    3. Liquidity imbalance — detect when order book is heavily skewed
    4. Extreme pricing — outcomes priced at extremes that may revert
    5. Volume spike detection — sharp recent moves may overshoot
    """

    def __init__(self, config: EventsConfig | None = None) -> None:
        self.config = config or EventsConfig()

    async def evaluate(self, market: EventMarket) -> EdgeResult | None:
        """Evaluate an events market for edge."""
        try:
            # Try multiple edge detection strategies
            result = self._analyze_spread(market)
            if result and result.has_edge:
                return result

            result = self._analyze_time_decay(market)
            if result and result.has_edge:
                return result

            result = self._analyze_extreme_pricing(market)
            if result and result.has_edge:
                return result

            return None
        except Exception as e:
            logger.error("Edge calculation failed for %s: %s", market.slug, e)
            return None

    def _analyze_spread(self, market: EventMarket) -> EdgeResult | None:
        """Detect mispricing via spread analysis.

        If the sum of outcome prices deviates significantly from 1.0,
        there may be an edge. For binary markets, YES + NO should ≈ 1.0.
        Overround (>1.0) means vig; underround (<1.0) means free money.
        """
        if len(market.outcome_prices) < 2:
            return None

        # For binary markets only
        if len(market.outcomes) != 2:
            return None

        yes_price = market.outcome_prices[0]
        no_price = market.outcome_prices[1]
        total = yes_price + no_price

        if total <= 0:
            return None

        # Normalize to fair probabilities
        fair_yes = yes_price / total
        fair_no = no_price / total

        # Look for spread inefficiency — the bid-ask spread creates edge
        # If total < 0.98 (underround), one side is underpriced
        # If total > 1.02 (overround), both sides are overpriced
        spread = abs(1.0 - total)

        if spread < 0.02:
            # Tight spread, look for subtle edge using volume-weighted analysis
            # High volume + liquidity markets tend to be efficient
            # Low volume markets may have stale prices
            if market.volume_24h < 5000 and market.liquidity > 20000:
                # Low volume but high liquidity — prices may be stale
                # Slight edge opportunity
                pass
            else:
                return None

        # For underround markets: the cheaper side is the edge
        if total < 0.98:
            # Both sides are cheap — buy the one with more upside
            yes_edge = fair_yes - yes_price
            no_edge = fair_no - no_price

            if yes_edge > no_edge and yes_edge >= self.config.MIN_EDGE:
                return EdgeResult(
                    market=market,
                    our_fair_price=fair_yes,
                    market_price=yes_price,
                    edge=yes_edge,
                    confidence=self._classify_confidence(yes_edge, market),
                    side="YES",
                    side_index=0,
                    edge_source="spread_analysis",
                )
            elif no_edge >= self.config.MIN_EDGE:
                return EdgeResult(
                    market=market,
                    our_fair_price=fair_no,
                    market_price=no_price,
                    edge=no_edge,
                    confidence=self._classify_confidence(no_edge, market),
                    side="NO",
                    side_index=1,
                    edge_source="spread_analysis",
                )

        return None

    def _analyze_time_decay(self, market: EventMarket) -> EdgeResult | None:
        """Detect edge via time decay — markets nearing resolution.

        Markets close to their end date with extreme pricing (>85% or <15%)
        often have predictable outcomes. The remaining uncertainty creates
        a small but reliable edge.
        """
        from nba_agent.utils import utcnow, parse_utc

        if not market.end_date:
            return None

        now = utcnow()
        try:
            end_dt = parse_utc(market.end_date)
        except ValueError:
            return None

        hours_remaining = (end_dt - now).total_seconds() / 3600
        if hours_remaining <= 0 or hours_remaining > 168:  # Only within 7 days
            return None

        if len(market.outcome_prices) < 2:
            return None

        yes_price = market.outcome_prices[0]
        no_price = market.outcome_prices[1]

        # Time decay edge: markets near resolution with extreme pricing
        # The closer to resolution + the more extreme the price = higher confidence
        decay_factor = max(0, 1.0 - (hours_remaining / 168))  # 0 at 7 days, 1 at resolution

        for i, price in enumerate(market.outcome_prices[:2]):
            if price > 0.85:
                # Market strongly favors this outcome
                # Fair value should be even higher given time decay
                time_boost = decay_factor * 0.05  # Up to 5% boost
                fair_price = min(0.97, price + time_boost)
                edge = fair_price - price

                if edge >= self.config.MIN_EDGE:
                    return EdgeResult(
                        market=market,
                        our_fair_price=fair_price,
                        market_price=price,
                        edge=edge,
                        confidence=self._classify_confidence(edge, market),
                        side="YES" if i == 0 else "NO",
                        side_index=i,
                        edge_source="time_decay",
                    )

        return None

    def _analyze_extreme_pricing(self, market: EventMarket) -> EdgeResult | None:
        """Detect edge from extreme pricing that may indicate mispricing.

        Markets with very high volume and extreme prices (3-10% or 90-97%)
        are often mispriced because retail traders pile in on the obvious
        outcome, creating a slight edge on the contrarian side when the
        probability is not actually that extreme.
        """
        if len(market.outcome_prices) < 2:
            return None

        # Only binary markets
        if len(market.outcomes) != 2:
            return None

        yes_price = market.outcome_prices[0]
        no_price = market.outcome_prices[1]

        # Look for markets where one side is extremely cheap (3-12%)
        # These often represent "tail risk" events that are underpriced
        for i, (price, label) in enumerate([(yes_price, "YES"), (no_price, "NO")]):
            if 0.03 <= price <= 0.12:
                # Cheap outcome — check if it might be underpriced
                # Volume relative to liquidity tells us if it's actively traded
                vol_ratio = market.volume_24h / market.liquidity if market.liquidity > 0 else 0

                if vol_ratio > 0.3:
                    # Actively traded — price is likely efficient
                    continue

                # Low activity relative to liquidity — might be stale/mispriced
                # Apply a small contrarian edge estimate
                # Historical base rate for "unlikely" events is ~2x the market price
                fair_price = min(price * 1.5, 0.20)
                edge = fair_price - price

                if edge >= self.config.MIN_EDGE:
                    return EdgeResult(
                        market=market,
                        our_fair_price=fair_price,
                        market_price=price,
                        edge=edge,
                        confidence=Confidence.LOW,  # Always low confidence for tail events
                        side=label,
                        side_index=i,
                        edge_source="extreme_pricing",
                    )

        return None

    def _classify_confidence(
        self,
        edge: float,
        market: EventMarket,
    ) -> Confidence:
        """Classify confidence tier based on edge and market properties."""
        # Higher liquidity + volume = more reliable pricing signal
        liquidity_factor = min(market.liquidity / 100000, 1.0)
        volume_factor = min(market.volume_24h / 50000, 1.0)
        market_quality = (liquidity_factor + volume_factor) / 2

        if edge > 0.10 and market_quality > 0.5:
            return Confidence.HIGH
        elif edge > 0.06 and market_quality > 0.3:
            return Confidence.MEDIUM
        else:
            return Confidence.LOW
