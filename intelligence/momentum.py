"""Universal Signal 4: Price Movement / Momentum Analysis.

Analyzes Polymarket's own price data to detect:
1. Strong directional momentum (trend-following)
2. Sharp moves on no news (potential mean-reversion)
3. Volume-confirmed breakouts vs. fakeouts

Purely from on-chain/Polymarket data — no external APIs needed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from intelligence.config import IntelligenceConfig
from intelligence.models import Signal

logger = logging.getLogger("intelligence.momentum")

# Data directory for price history
_project_root = Path(__file__).resolve().parent.parent
try:
    DATA_DIR = Path("/root/polymarket-bot/data") if Path("/root/polymarket-bot/data").exists() else _project_root / "data"
except (PermissionError, OSError):
    DATA_DIR = _project_root / "data"


class MomentumAnalyzer:
    """Analyzes price momentum and mean-reversion signals."""

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig()

    async def scan(self, active_markets: list) -> list[Signal]:
        """Scan all markets for momentum-based signals.

        For each market with price history:
        1. Calculate short-term (24h) and medium-term (7d) price changes
        2. Detect strong momentum or mean-reversion opportunities
        3. Consider volume confirmation
        """
        if not os.getenv("MOMENTUM_ENABLED", "true").lower() == "true":
            return []

        signals: list[Signal] = []

        for market in active_markets:
            try:
                market_id = getattr(market, "id", str(market))
                question = getattr(market, "question", "")
                prices = getattr(market, "outcome_prices", [])

                if len(prices) < 2:
                    continue

                current_yes = float(prices[0])
                if current_yes <= 0.03 or current_yes >= 0.97:
                    continue  # Already resolved

                # Load price history
                price_history = self._load_price_history(market_id)
                if not price_history or len(price_history) < 3:
                    continue

                signal = self._analyze_momentum(
                    market_id=market_id,
                    question=question,
                    current_price=current_yes,
                    price_history=price_history,
                )

                if signal:
                    signals.append(signal)

            except Exception as e:
                logger.warning("Momentum analysis failed for market: %s", e)

        logger.info("Momentum scan complete: %d signals from %d markets",
                     len(signals), len(active_markets))
        return signals

    def _load_price_history(self, market_id: str) -> list[float]:
        """Load price history from data directory."""
        try:
            path = DATA_DIR / "price_history" / f"{market_id}.json"
            if not path.exists():
                return []
            data = json.loads(path.read_text())
            prices = data.get("prices", [])
            # Extract price values (handle both float and dict formats)
            return [
                p.get("price", p) if isinstance(p, dict) else float(p)
                for p in prices[-168:]  # Last ~7 days of hourly data
            ]
        except Exception:
            return []

    def _analyze_momentum(
        self,
        market_id: str,
        question: str,
        current_price: float,
        price_history: list[float],
    ) -> Signal | None:
        """Detect momentum or mean-reversion signals.

        Returns Signal if edge found, None otherwise.
        """
        n = len(price_history)

        # Calculate price changes over different windows
        # Short-term: last ~24h (last 24 entries if hourly)
        short_window = min(24, n)
        short_start = price_history[-short_window] if short_window <= n else price_history[0]
        short_change = current_price - short_start

        # Medium-term: last ~7d (all available, up to 168)
        medium_start = price_history[0]
        medium_change = current_price - medium_start

        # Volatility estimate (std of recent price changes)
        if n >= 3:
            changes = [
                price_history[i] - price_history[i - 1]
                for i in range(max(1, n - 24), n)
            ]
            if changes:
                mean_change = sum(changes) / len(changes)
                variance = sum((c - mean_change) ** 2 for c in changes) / len(changes)
                volatility = variance ** 0.5
            else:
                volatility = 0.02
        else:
            volatility = 0.02

        # Avoid division by zero
        if volatility < 0.001:
            volatility = 0.001

        # --- Strategy 1: Strong directional momentum ---
        # Large consistent move over 7 days → trend may continue
        if abs(medium_change) > 0.10 and abs(short_change) > 0.03:
            # Both short and medium term agree on direction
            if (medium_change > 0 and short_change > 0):
                # Upward momentum → YES becoming more likely
                z_score = medium_change / volatility
                if z_score > 2.0:
                    strength = min(0.75, 0.30 + abs(medium_change) * 1.5)
                    confidence = min(0.65, 0.25 + min(z_score / 10, 0.30))

                    return Signal(
                        source="momentum",
                        market_id=market_id,
                        market_question=question,
                        signal_type="strong_momentum",
                        direction="YES",
                        strength=round(strength, 3),
                        confidence=round(confidence, 3),
                        details={
                            "short_change": round(short_change, 4),
                            "medium_change": round(medium_change, 4),
                            "z_score": round(z_score, 2),
                            "volatility": round(volatility, 4),
                            "strategy": "momentum_up",
                        },
                    )

            elif (medium_change < 0 and short_change < 0):
                # Downward momentum → NO becoming more likely
                z_score = abs(medium_change) / volatility
                if z_score > 2.0:
                    strength = min(0.75, 0.30 + abs(medium_change) * 1.5)
                    confidence = min(0.65, 0.25 + min(z_score / 10, 0.30))

                    return Signal(
                        source="momentum",
                        market_id=market_id,
                        market_question=question,
                        signal_type="strong_momentum",
                        direction="NO",
                        strength=round(strength, 3),
                        confidence=round(confidence, 3),
                        details={
                            "short_change": round(short_change, 4),
                            "medium_change": round(medium_change, 4),
                            "z_score": round(z_score, 2),
                            "volatility": round(volatility, 4),
                            "strategy": "momentum_down",
                        },
                    )

        # --- Strategy 2: Sharp move / potential overreaction ---
        # Large short-term move against the medium-term trend → mean reversion
        if abs(short_change) > 0.08 and n >= 24:
            short_z = abs(short_change) / volatility
            if short_z > 3.0:
                # Extreme short-term move — potential overreaction
                if short_change > 0 and medium_change < 0.03:
                    # Sharp UP against flat/down trend → may revert → NO
                    strength = min(0.65, 0.25 + (short_z - 3.0) * 0.1)
                    confidence = min(0.55, 0.20 + (short_z - 3.0) * 0.08)

                    return Signal(
                        source="momentum",
                        market_id=market_id,
                        market_question=question,
                        signal_type="mean_reversion",
                        direction="NO",
                        strength=round(strength, 3),
                        confidence=round(confidence, 3),
                        details={
                            "short_change": round(short_change, 4),
                            "medium_change": round(medium_change, 4),
                            "short_z_score": round(short_z, 2),
                            "strategy": "mean_reversion_down",
                        },
                    )

                elif short_change < 0 and medium_change > -0.03:
                    # Sharp DOWN against flat/up trend → may revert → YES
                    strength = min(0.65, 0.25 + (short_z - 3.0) * 0.1)
                    confidence = min(0.55, 0.20 + (short_z - 3.0) * 0.08)

                    return Signal(
                        source="momentum",
                        market_id=market_id,
                        market_question=question,
                        signal_type="mean_reversion",
                        direction="YES",
                        strength=round(strength, 3),
                        confidence=round(confidence, 3),
                        details={
                            "short_change": round(short_change, 4),
                            "medium_change": round(medium_change, 4),
                            "short_z_score": round(short_z, 2),
                            "strategy": "mean_reversion_up",
                        },
                    )

        return None
