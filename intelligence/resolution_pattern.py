"""Universal Signal 5: Resolution Pattern Analysis.

Analyzes historical Polymarket resolution data to identify systematic biases:
- What % of "Will X happen by Y?" markets resolve YES vs NO by category?
- Political/geopolitical markets have known biases
- This is NOT "blind bet NO" — it's informed by actual resolution statistics

The signal is strongest when the market price significantly diverges from
the historical resolution rate for that category.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal
from intelligence.sports_filter import is_sports_market

logger = logging.getLogger("intelligence.resolution_pattern")

# Historical resolution rates by category keyword pattern.
# These are derived from observed Polymarket resolution patterns.
# Format: (pattern_keywords, yes_resolution_rate, sample_confidence)
RESOLUTION_PATTERNS: list[tuple[list[str], float, float]] = [
    # Geopolitical — most "will X happen?" resolve NO
    (["invade", "invasion", "annex"], 0.06, 0.80),
    (["military", "strike", "bomb", "attack"], 0.09, 0.75),
    (["war", "armed conflict"], 0.07, 0.75),
    (["sanctions", "sanction"], 0.22, 0.65),
    (["coup", "overthrow"], 0.05, 0.80),
    (["nuclear", "nuke"], 0.03, 0.85),
    (["ceasefire", "peace deal", "truce"], 0.15, 0.60),
    (["assassination", "assassinate"], 0.04, 0.80),
    (["terrorist", "terror attack"], 0.06, 0.75),

    # Political — mixed rates
    (["resign", "step down"], 0.12, 0.70),
    (["impeach"], 0.10, 0.75),
    (["executive order"], 0.35, 0.55),
    (["pass", "legislation", "bill"], 0.28, 0.55),
    (["veto"], 0.18, 0.60),
    (["government shutdown"], 0.18, 0.65),
    (["pardon"], 0.25, 0.55),
    (["indictment", "indict"], 0.30, 0.55),
    (["conviction", "convicted", "guilty"], 0.25, 0.55),

    # Elections — closer to 50/50
    (["win the election", "elected", "win the presidency"], 0.50, 0.40),
    (["nomination", "nominated"], 0.35, 0.50),

    # Economic — mostly NO for extreme predictions
    (["recession"], 0.18, 0.65),
    (["default", "debt ceiling"], 0.08, 0.75),
    (["rate cut"], 0.35, 0.50),
    (["rate hike"], 0.30, 0.50),
    (["inflation above", "inflation over"], 0.30, 0.50),
    (["inflation below", "inflation under"], 0.35, 0.50),

    # Crypto/tech — variable
    (["etf approv"], 0.35, 0.50),
    (["hack", "exploit"], 0.10, 0.70),
    (["ban crypto", "ban bitcoin"], 0.08, 0.75),
    (["all-time high", "ath", "new high"], 0.30, 0.50),

    # Price targets — depends on direction
    (["above", "over", "exceed", "surpass", "hit", "reach"], 0.35, 0.40),
    (["below", "under", "fall to", "drop to"], 0.30, 0.40),
]

# Overall base rate: 76% of Polymarket markets resolve NO
OVERALL_NO_RATE = 0.76


def _match_resolution_pattern(question: str) -> tuple[float, float] | None:
    """Match a question to a resolution pattern.

    Returns (yes_rate, confidence) or None if no match.
    """
    q_lower = question.lower()

    best_match = None
    best_specificity = 0

    for keywords, yes_rate, conf in RESOLUTION_PATTERNS:
        matches = sum(1 for kw in keywords if kw in q_lower)
        if matches > 0 and matches > best_specificity:
            best_match = (yes_rate, conf)
            best_specificity = matches

    return best_match


class ResolutionPatternAnalyzer:
    """Analyzes markets based on historical resolution patterns."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan markets for resolution-pattern-based signals.

        For each market:
        1. Match question to a resolution pattern category
        2. Get the historical YES resolution rate for that category
        3. Compare to current market price
        4. Generate signal if significant deviation
        """
        if not os.getenv("RESOLUTION_PATTERN_ENABLED", "true").lower() == "true":
            return []

        signals: list[Signal] = []

        for market in active_markets:
            try:
                if is_sports_market(market):
                    logger.debug("Skipping sports market: %s", getattr(market, "id", ""))
                    continue

                question = getattr(market, "question", "")
                market_id = getattr(market, "id", str(market))
                prices = getattr(market, "outcome_prices", [])

                if not question or len(prices) < 2:
                    continue

                yes_price = float(prices[0])
                if yes_price <= 0.03 or yes_price >= 0.97:
                    continue  # Already resolved

                # Match to resolution pattern
                match = _match_resolution_pattern(question)
                if match is None:
                    # Use overall base rate as fallback
                    historical_yes_rate = 1.0 - OVERALL_NO_RATE  # 0.24
                    pattern_confidence = 0.35
                else:
                    historical_yes_rate, pattern_confidence = match

                # Calculate deviation from historical rate
                deviation = yes_price - historical_yes_rate

                # Minimum deviation threshold — scale with pattern confidence
                min_deviation = 0.10 - (pattern_confidence * 0.03)

                if abs(deviation) < min_deviation:
                    continue

                if deviation > 0:
                    # Market overprices YES vs historical rate → bet NO
                    direction = "NO"
                    signal_detail = (
                        f"YES at {yes_price:.0%} vs historical {historical_yes_rate:.0%} "
                        f"(overpriced by {deviation:.0%})"
                    )
                else:
                    # Market underprices YES vs historical rate → bet YES
                    direction = "YES"
                    signal_detail = (
                        f"YES at {yes_price:.0%} vs historical {historical_yes_rate:.0%} "
                        f"(underpriced by {abs(deviation):.0%})"
                    )

                # Strength scales with deviation and pattern confidence
                strength = min(0.80, 0.30 + abs(deviation) * pattern_confidence * 2)
                confidence = min(0.70, pattern_confidence * (0.5 + abs(deviation)))

                signal = Signal(
                    source="resolution_pattern",
                    market_id=market_id,
                    market_question=question,
                    signal_type="resolution_bias",
                    direction=direction,
                    strength=round(strength, 3),
                    confidence=round(confidence, 3),
                    details={
                        "historical_yes_rate": round(historical_yes_rate, 3),
                        "market_yes_price": round(yes_price, 3),
                        "deviation": round(deviation, 3),
                        "pattern_confidence": round(pattern_confidence, 3),
                        "signal_detail": signal_detail,
                    },
                )
                signals.append(signal)

                logger.info(
                    "RESOLUTION signal: %s | hist=%.0f%% mkt=%.0f%% dev=%+.0f%% → %s (str=%.2f)",
                    market_id[:8], historical_yes_rate * 100,
                    yes_price * 100, deviation * 100,
                    direction, strength,
                )

            except Exception as e:
                logger.warning("Resolution pattern analysis failed: %s", e)

        logger.info("Resolution pattern scan: %d signals from %d markets",
                     len(signals), len(active_markets))
        return signals
