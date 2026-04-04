"""Universal Signal 2: Time Decay / Calendar Analysis.

Works for ANY market by analyzing the relationship between:
- Time remaining to resolution
- Current market price
- Implied daily probability needed for YES resolution
- Historical daily probability of similar events occurring

Key insights:
- Short-dated, low-probability events tend to be overpriced (excitement premium)
- "Will X happen by [date]?" with date approaching and no signs → strong NO
- Long-dated, high-probability events that haven't been priced in → YES signal
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from intelligence.sports_filter import is_sports_market
from shared.utils import utcnow, parse_utc

logger = logging.getLogger("intelligence.time_decay_universal")


class TimeDecayUniversal:
    """Universal time-decay analysis for any market with an end date."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan all markets for time-decay-based edge signals.

        Strategies:
        1. Excitement premium: short-dated low-prob events overpriced
        2. Calendar NO: approaching deadline with no movement
        3. Calendar YES: underpriced high-prob events on long timeframes
        4. Implied probability analysis
        """
        if not os.getenv("TIME_DECAY_UNIVERSAL_ENABLED", "true").lower() == "true":
            return []

        signals: list[Signal] = []
        now = utcnow()

        for market in active_markets:
            try:
                if is_sports_market(market):
                    logger.debug("Skipping sports market: %s", getattr(market, "id", ""))
                    continue

                question = getattr(market, "question", "")
                market_id = getattr(market, "id", str(market))
                end_date_str = getattr(market, "end_date", "")
                prices = getattr(market, "outcome_prices", [])

                if not end_date_str or len(prices) < 2:
                    continue

                try:
                    end_dt = parse_utc(end_date_str)
                except (ValueError, TypeError):
                    continue

                hours_remaining = (end_dt - now).total_seconds() / 3600
                days_remaining = hours_remaining / 24

                if hours_remaining <= 4:
                    continue  # Too close to resolution — skip (min 4h)

                yes_price = float(prices[0])
                no_price = float(prices[1])

                if yes_price <= 0.02 or yes_price >= 0.98:
                    continue  # Already resolved

                signal = self._analyze_time_dynamics(
                    market_id=market_id,
                    question=question,
                    yes_price=yes_price,
                    days_remaining=days_remaining,
                    hours_remaining=hours_remaining,
                )

                if signal:
                    signals.append(signal)

            except Exception as e:
                logger.warning("Time decay analysis failed for market: %s", e)

        logger.info("Time decay universal scan: %d signals from %d markets",
                     len(signals), len(active_markets))
        return signals

    def _analyze_time_dynamics(
        self,
        market_id: str,
        question: str,
        yes_price: float,
        days_remaining: float,
        hours_remaining: float,
    ) -> Signal | None:
        """Analyze time dynamics for a single market.

        Returns a Signal if edge detected, None otherwise.
        """
        # --- Strategy 1: Excitement Premium (short-dated, low YES price) ---
        # Markets with <7 days, YES price 5-30% → excitement premium
        # People overpay for unlikely but exciting outcomes
        if days_remaining <= 7 and 0.05 <= yes_price <= 0.30:
            # Implied daily probability needed: P(yes) must average to reach
            # current price, but most political events don't happen daily
            implied_daily_prob = 1.0 - (1.0 - yes_price) ** (1.0 / max(days_remaining, 0.1))

            # Most political/geopolitical events have a daily probability of 0.1-1%
            # If the implied daily prob is much higher, YES is overpriced
            if implied_daily_prob > 0.03:  # Implies >3% daily chance needed
                strength = min(0.80, 0.35 + (implied_daily_prob - 0.03) * 5)
                confidence = min(0.75, 0.30 + (0.30 - yes_price) * 2)

                return Signal(
                    source="time_decay_universal",
                    market_id=market_id,
                    market_question=question,
                    signal_type="excitement_premium",
                    direction="NO",
                    strength=round(strength, 3),
                    confidence=round(confidence, 3),
                    details={
                        "days_remaining": round(days_remaining, 1),
                        "yes_price": round(yes_price, 3),
                        "implied_daily_prob": round(implied_daily_prob, 4),
                        "strategy": "excitement_premium",
                    },
                )

        # --- Strategy 2: Calendar NO (approaching deadline, low movement) ---
        # Markets with <14 days, YES price 5-45% → strong NO signal
        # If nothing has happened yet with limited time left, less likely
        if days_remaining <= 14 and 0.05 <= yes_price <= 0.45:
            # Time pressure factor: closer to expiry = stronger signal
            time_pressure = max(0, 1.0 - (days_remaining / 14))

            # Price factor: lower YES price + less time = more likely NO
            price_factor = 1.0 - yes_price

            combined = time_pressure * 0.6 + price_factor * 0.4
            if combined > 0.40:
                strength = min(0.80, 0.30 + combined * 0.5)
                confidence = min(0.70, 0.25 + time_pressure * 0.35)

                return Signal(
                    source="time_decay_universal",
                    market_id=market_id,
                    market_question=question,
                    signal_type="calendar_no",
                    direction="NO",
                    strength=round(strength, 3),
                    confidence=round(confidence, 3),
                    details={
                        "days_remaining": round(days_remaining, 1),
                        "yes_price": round(yes_price, 3),
                        "time_pressure": round(time_pressure, 3),
                        "combined_score": round(combined, 3),
                        "strategy": "calendar_no",
                    },
                )

        # --- Strategy 3: Calendar YES (long-dated, high YES price not fully priced) ---
        # Markets with >30 days, YES price 55-85% → may be underpriced
        # Consensus is forming but market hasn't fully priced it in
        if days_remaining > 30 and 0.55 <= yes_price <= 0.85:
            # With lots of time and strong consensus already forming,
            # the market may not have fully adjusted
            consensus_strength = yes_price - 0.50  # How far above 50-50

            # Time bonus: more time with strong consensus = more likely correct
            time_bonus = min(0.2, (days_remaining - 30) / 300)

            if consensus_strength > 0.10:
                strength = min(0.70, 0.30 + consensus_strength + time_bonus)
                confidence = min(0.60, 0.25 + consensus_strength * 0.8)

                return Signal(
                    source="time_decay_universal",
                    market_id=market_id,
                    market_question=question,
                    signal_type="calendar_yes",
                    direction="YES",
                    strength=round(strength, 3),
                    confidence=round(confidence, 3),
                    details={
                        "days_remaining": round(days_remaining, 1),
                        "yes_price": round(yes_price, 3),
                        "consensus_strength": round(consensus_strength, 3),
                        "time_bonus": round(time_bonus, 3),
                        "strategy": "calendar_yes",
                    },
                )

        # --- Strategy 4: Stale high-price NO opportunity ---
        # Short-dated markets where NO is very cheap (YES > 75%)
        # but nothing has actually happened yet — YES may be overpriced
        if days_remaining <= 30 and yes_price > 0.75 and yes_price < 0.93:
            # Check if the high YES price might be driven by inertia
            # rather than information — signal opportunity for NO
            days_factor = min(1.0, days_remaining / 30)
            edge_signal = (yes_price - 0.75) * (1.0 - days_factor * 0.3)

            if edge_signal > 0.05:
                strength = min(0.60, 0.25 + edge_signal)
                confidence = min(0.55, 0.20 + edge_signal * 1.2)

                return Signal(
                    source="time_decay_universal",
                    market_id=market_id,
                    market_question=question,
                    signal_type="overpriced_yes",
                    direction="NO",
                    strength=round(strength, 3),
                    confidence=round(confidence, 3),
                    details={
                        "days_remaining": round(days_remaining, 1),
                        "yes_price": round(yes_price, 3),
                        "edge_signal": round(edge_signal, 3),
                        "strategy": "overpriced_yes",
                    },
                )

        return None
