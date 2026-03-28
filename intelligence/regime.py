"""Regime Detection — classifies market regimes from price action and volume.

Detects whether a market is TRENDING, VOLATILE, STALE, or CONVERGING and
adjusts edge/sizing multipliers accordingly. All computation is local, no
external API calls.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from enum import Enum

from intelligence.models import RegimeAssessment

logger = logging.getLogger("intelligence.regime")


class Regime(Enum):
    TRENDING = "trending"
    VOLATILE = "volatile"
    STALE = "stale"
    CONVERGING = "converging"
    UNKNOWN = "unknown"


# Thresholds (configurable via env)
VOLATILE_THRESHOLD = float(os.getenv("REGIME_VOLATILE_THRESHOLD", "0.10"))
STALE_MOVE_THRESHOLD = float(os.getenv("REGIME_STALE_THRESHOLD", "0.03"))
CONVERGING_PRICE = float(os.getenv("REGIME_CONVERGING_PRICE", "0.85"))
STALE_VOLUME_RATIO = 0.25    # Below 25% of average = very low volume
TREND_MOVE_THRESHOLD = 0.10  # >10% directional move = trending
VOLATILE_SWING_THRESHOLD = 0.15  # >15% swing in both directions = volatile

# Regime-specific multipliers
REGIME_ADJUSTMENTS = {
    Regime.TRENDING: {
        "edge_multiplier": 0.9,      # Slightly lower bar — follow the trend
        "size_multiplier": 1.1,      # Slightly larger positions
        "recommendation": "trade",
    },
    Regime.VOLATILE: {
        "edge_multiplier": 1.4,      # Higher bar — need more edge in chaos
        "size_multiplier": 0.6,      # Smaller positions
        "recommendation": "reduce_size",
    },
    Regime.STALE: {
        "edge_multiplier": 1.5,      # Very high bar — nothing happening
        "size_multiplier": 0.3,      # Tiny positions or avoid
        "recommendation": "avoid",
    },
    Regime.CONVERGING: {
        "edge_multiplier": 0.7,      # Low bar — near-certain outcome
        "size_multiplier": 0.8,      # Moderate size for the free edge
        "recommendation": "hold_to_resolution",
    },
    Regime.UNKNOWN: {
        "edge_multiplier": 1.0,
        "size_multiplier": 1.0,
        "recommendation": "trade",
    },
}


class RegimeDetector:
    """Detects market regime from price action and volume patterns."""

    def detect(
        self,
        market_id: str,
        price_history: list[float] | None = None,
        volume_history: list[float] | None = None,
        current_price: float | None = None,
    ) -> RegimeAssessment:
        """Analyze recent price/volume to determine regime.

        Args:
            market_id: Market identifier for logging.
            price_history: List of recent prices (most recent last). 7 days ideal.
            volume_history: List of recent volume values (most recent last).
            current_price: Current market price (0.0 to 1.0). Used for CONVERGING check.

        Returns:
            RegimeAssessment with regime classification and multipliers.
        """
        try:
            prices = price_history or []
            volumes = volume_history or []

            # Calculate metrics
            volatility = self._calc_volatility(prices)
            trend_strength = self._calc_trend_strength(prices)
            volume_ratio = self._calc_volume_ratio(volumes)
            price_range = self._calc_price_range(prices)
            max_swing = self._calc_max_swing(prices)

            # Current price for converging check
            cp = current_price
            if cp is None and prices:
                cp = prices[-1]

            regime = self._classify_regime(
                volatility=volatility,
                trend_strength=trend_strength,
                volume_ratio=volume_ratio,
                price_range=price_range,
                max_swing=max_swing,
                current_price=cp,
            )

            adjustments = REGIME_ADJUSTMENTS[regime]

            return RegimeAssessment(
                regime=regime.value,
                volatility=round(volatility, 4),
                trend_strength=round(trend_strength, 4),
                volume_ratio=round(volume_ratio, 4),
                edge_multiplier=adjustments["edge_multiplier"],
                size_multiplier=adjustments["size_multiplier"],
                recommendation=adjustments["recommendation"],
            )

        except Exception as e:
            logger.error("Regime detection failed for %s: %s", market_id, e)
            adj = REGIME_ADJUSTMENTS[Regime.UNKNOWN]
            return RegimeAssessment(
                regime="unknown",
                volatility=0.0,
                trend_strength=0.0,
                volume_ratio=1.0,
                edge_multiplier=adj["edge_multiplier"],
                size_multiplier=adj["size_multiplier"],
                recommendation=adj["recommendation"],
            )

    def _classify_regime(
        self,
        volatility: float,
        trend_strength: float,
        volume_ratio: float,
        price_range: float,
        max_swing: float,
        current_price: float | None,
    ) -> Regime:
        """Classify the regime based on computed metrics."""

        # CONVERGING: price near 0 or 1, moving toward extreme
        if current_price is not None:
            if current_price > CONVERGING_PRICE or current_price < (1.0 - CONVERGING_PRICE):
                # Price is near an extreme and trending further toward it
                if abs(trend_strength) > 0.3:
                    return Regime.CONVERGING
                # Or price is very extreme (>90% or <10%)
                if current_price > 0.90 or current_price < 0.10:
                    return Regime.CONVERGING

        # VOLATILE: high std dev, large swings in both directions
        if volatility > VOLATILE_THRESHOLD and max_swing > VOLATILE_SWING_THRESHOLD:
            return Regime.VOLATILE

        if volatility > VOLATILE_THRESHOLD * 1.5:
            # Very high volatility even without confirmed swings
            return Regime.VOLATILE

        # STALE: very little movement, low volume
        if price_range < STALE_MOVE_THRESHOLD and volume_ratio < STALE_VOLUME_RATIO:
            return Regime.STALE

        if price_range < STALE_MOVE_THRESHOLD * 0.5:
            # Extremely flat even with normal volume
            return Regime.STALE

        # TRENDING: directional drift with low volatility
        if abs(trend_strength) > 0.5 and price_range > TREND_MOVE_THRESHOLD:
            return Regime.TRENDING

        if price_range > TREND_MOVE_THRESHOLD and volatility < VOLATILE_THRESHOLD:
            return Regime.TRENDING

        # Default — doesn't strongly match any regime
        return Regime.UNKNOWN

    def _calc_volatility(self, prices: list[float]) -> float:
        """Calculate price volatility (standard deviation of returns)."""
        if len(prices) < 3:
            return 0.0

        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1])

        if not returns:
            return 0.0

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)

    def _calc_trend_strength(self, prices: list[float]) -> float:
        """Calculate trend strength: -1 (strong downtrend) to +1 (strong uptrend).

        Uses simple linear regression slope normalized by price range.
        """
        if len(prices) < 3:
            return 0.0

        n = len(prices)
        x_mean = (n - 1) / 2.0
        y_mean = sum(prices) / n

        numerator = sum((i - x_mean) * (p - y_mean) for i, p in enumerate(prices))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return 0.0

        slope = numerator / denominator

        # Normalize: divide slope by mean price to get % per period
        if y_mean > 0:
            norm_slope = slope / y_mean
        else:
            norm_slope = 0.0

        # Scale to [-1, 1] range (clip at extremes)
        # A slope of 0.01 per period over ~7 days is considered moderate
        scaled = norm_slope * n * 5  # Amplify to make meaningful
        return max(-1.0, min(1.0, scaled))

    def _calc_volume_ratio(self, volumes: list[float]) -> float:
        """Calculate ratio of recent volume to average volume."""
        if len(volumes) < 2:
            return 1.0

        avg = sum(volumes) / len(volumes)
        if avg <= 0:
            return 1.0

        # Recent = last 25% of history
        recent_count = max(1, len(volumes) // 4)
        recent_avg = sum(volumes[-recent_count:]) / recent_count

        return recent_avg / avg

    def _calc_price_range(self, prices: list[float]) -> float:
        """Calculate total price range over the period."""
        if len(prices) < 2:
            return 0.0
        return max(prices) - min(prices)

    def _calc_max_swing(self, prices: list[float]) -> float:
        """Calculate maximum swing (peak to trough) in either direction."""
        if len(prices) < 3:
            return 0.0

        max_up = 0.0
        max_down = 0.0
        local_min = prices[0]
        local_max = prices[0]

        for p in prices[1:]:
            if p > local_max:
                local_max = p
            if p < local_min:
                local_min = p

            up_swing = local_max - local_min if local_max > local_min else 0
            down_swing = local_max - p if local_max > p else 0

            max_up = max(max_up, up_swing)
            max_down = max(max_down, down_swing)

        return max(max_up, max_down)
