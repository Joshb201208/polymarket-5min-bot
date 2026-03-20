"""
strategy.py - Trading strategy engine for Polymarket 5-minute crypto markets.

Three strategies are implemented:

1. Latency Arbitrage (primary / recommended)
   ─────────────────────────────────────────
   Exploit the lag between Binance price movement and Polymarket's re-pricing.
   - If Binance just moved UP ≥ threshold in last 30s, but Polymarket "Up"
     token is still ≤ 0.50, buy YES (Up) — the market hasn't repriced yet.
   - If Binance just moved DOWN ≥ threshold in last 30s, but Polymarket "Down"
     token is still ≤ 0.50, buy YES (Down).
   - Edge = |exchange_implied_prob - polymarket_mid| − fees

2. Signal-Based (secondary / conservative)
   ────────────────────────────────────────
   Use technical indicators to build directional confidence:
   - RSI + momentum: oversold + positive flip → bullish
   - MACD crossover + Bollinger Band bounce
   - Combined confidence must exceed MIN_CONFIDENCE threshold (default 0.60)

3. Late-Window Maker (high-alpha, zero-fee)
   ──────────────────────────────────────────
   Activates only in the final 15 seconds of a 5-minute window.
   Key insight: 85% of BTC's direction is determined T-10s before window end,
   but Polymarket odds haven't fully reflected this information yet.
   - Place MAKER orders at 0.90-0.95 on the winning side
   - Edge = 1.00 - entry_price (winner pays $1.00 at settlement)
   - Zero fees + maker rebate
   - Higher confidence with larger price move and less time remaining
"""

import time
import math
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

from config import StrategyConfig, RiskConfig
from data_feeds import PriceFeed
from utils import polymarket_fee, current_window, clamp, seconds_until_window_end

# Optional: FeeManager for maker rebate estimation
try:
    from fee_manager import FeeManager
    _FEE_MANAGER_AVAILABLE = True
except ImportError:
    _FEE_MANAGER_AVAILABLE = False

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# TradingSignal — the output of strategy evaluation
# ---------------------------------------------------------------------------

@dataclass
class TradingSignal:
    """Represents a trading decision emitted by the strategy engine."""

    # "YES" = buy Up token | "NO" = buy Down token | None = no trade
    direction: Optional[str]

    # Confidence in the signal [0, 1]; signals below threshold are discarded
    confidence: float = 0.0

    # Edge above fees [0, 1]; the raw profit margin we expect
    edge: float = 0.0

    # Which strategy produced this signal
    strategy_name: str = ""

    # Human-readable explanation
    reasoning: str = ""

    # Suggested entry price (Polymarket probability)
    suggested_price: float = 0.5

    # Exchange-implied probability at time of signal
    exchange_prob: float = 0.5

    # Polymarket midpoint at time of signal
    polymarket_mid: float = 0.5

    # Time remaining in the 5-min window (seconds)
    seconds_remaining: float = 0.0

    # Asset this signal is for
    asset: str = ""

    @property
    def is_valid(self) -> bool:
        return self.direction is not None and self.edge > 0 and self.confidence > 0

    def __str__(self) -> str:
        return (
            f"Signal[{self.strategy_name}] {self.asset} {self.direction} "
            f"conf={self.confidence:.2f} edge={self.edge:.3f} "
            f"pmid={self.polymarket_mid:.2f} exch={self.exchange_prob:.2f}"
        )


NO_SIGNAL = TradingSignal(direction=None, reasoning="No opportunity found")


# ---------------------------------------------------------------------------
# StrategyEngine
# ---------------------------------------------------------------------------

class StrategyEngine:
    """
    Evaluates markets and generates TradingSignal objects.

    Usage:
        engine = StrategyEngine(price_feed, strategy_config, risk_config)
        signal = engine.evaluate(market, polymarket_mid_yes)
        if engine.should_trade(signal):
            # place order …
    """

    def __init__(
        self,
        price_feed: PriceFeed,
        strategy_config: StrategyConfig,
        risk_config: RiskConfig,
        fee_manager=None,
    ):
        self._feed = price_feed
        self._scfg = strategy_config
        self._rcfg = risk_config
        self._fee_manager = fee_manager  # Optional FeeManager for maker rebate estimates

    # ------------------------------------------------------------------
    # Main evaluation entry point
    # ------------------------------------------------------------------

    def evaluate(self, market: dict, polymarket_mid_yes: float) -> TradingSignal:
        """
        Evaluate the market and return the best signal found.

        Args:
            market:             Market dict from MarketFinder.
            polymarket_mid_yes: Current midpoint price of the YES (Up) token.

        Returns:
            TradingSignal — check .is_valid and .direction before trading.
        """
        asset = market.get("asset", "").upper()
        window_start = market.get("window_start", 0)
        window_end = market.get("window_end", 0)
        secs_remaining = max(0.0, window_end - time.time())

        # In the final 15 seconds, try the late-window maker strategy instead of blocking
        if secs_remaining < 15:
            late_signal = self.evaluate_late_window(market, polymarket_mid_yes)
            if late_signal.is_valid:
                return late_signal
            return TradingSignal(
                direction=None,
                reasoning=f"Only {secs_remaining:.0f}s remaining — too late to trade",
                seconds_remaining=secs_remaining,
                asset=asset,
            )

        # In the 15–30 second zone, try late-window strategy first
        if secs_remaining < 30:
            late_signal = self.evaluate_late_window(market, polymarket_mid_yes)
            if late_signal.is_valid:
                return late_signal
            return TradingSignal(
                direction=None,
                reasoning=f"Only {secs_remaining:.0f}s remaining — too late to trade",
                seconds_remaining=secs_remaining,
                asset=asset,
            )

        # Don't trade if we have no price data
        current_price = self._feed.get_current_price(asset)
        if current_price <= 0:
            return TradingSignal(
                direction=None,
                reasoning=f"No price data for {asset}",
                asset=asset,
            )

        # Run primary strategy first
        if self._scfg.primary_strategy == "latency_arb":
            signal = self._latency_arb_signal(
                asset, polymarket_mid_yes, window_start, secs_remaining
            )
        else:
            signal = self._signal_based_signal(asset, polymarket_mid_yes, secs_remaining)

        # If primary strategy found nothing, try secondary
        if not signal.is_valid and self._scfg.primary_strategy == "latency_arb":
            signal = self._signal_based_signal(asset, polymarket_mid_yes, secs_remaining)

        # If both latency_arb and signal_based found nothing,
        # try macro momentum (24h exchange change vs Polymarket mid)
        if not signal.is_valid:
            signal = self._macro_momentum_signal(asset, polymarket_mid_yes, secs_remaining)

        # In the 30–60 second zone, also try the late-window strategy
        # as a supplementary check (it may find a cleaner entry)
        if not signal.is_valid and secs_remaining < 60:
            late_signal = self.evaluate_late_window(market, polymarket_mid_yes)
            if late_signal.is_valid:
                return late_signal

        return signal

    def should_trade(self, signal: TradingSignal) -> bool:
        """
        Final gating check: should we actually place an order for this signal?

        Checks:
          - Signal is valid (has direction, positive edge)
          - Edge exceeds MIN_EDGE_THRESHOLD
          - Confidence exceeds strategy threshold
          - At least 60 seconds remain in the window
            (exception: late_window_maker strategy may have <60s)
        """
        if not signal.is_valid:
            return False

        if signal.edge < self._rcfg.min_edge_threshold:
            logger.debug(
                "should_trade: SKIP — edge %.4f < min %.4f",
                signal.edge, self._rcfg.min_edge_threshold,
            )
            return False

        # Late-window maker and macro momentum strategies use lower confidence thresholds
        if signal.strategy_name == "late_window_maker":
            min_conf = max(0.50, self._scfg.signal_confidence_threshold - 0.10)
        elif signal.strategy_name == "macro_momentum":
            min_conf = max(0.40, self._scfg.signal_confidence_threshold - 0.20)
        else:
            min_conf = self._scfg.signal_confidence_threshold

        if signal.confidence < min_conf:
            logger.debug(
                "should_trade: SKIP — confidence %.2f < min %.2f",
                signal.confidence, min_conf,
            )
            return False

        # Late-window maker can trade with < 60s remaining — that's the point
        if signal.strategy_name != "late_window_maker" and signal.seconds_remaining < 60:
            logger.debug("should_trade: SKIP — only %.0fs remaining", signal.seconds_remaining)
            return False

        logger.info("should_trade: TRADE — %s", signal)
        return True

    # ------------------------------------------------------------------
    # Strategy 3: Late-Window Maker
    # ------------------------------------------------------------------

    def evaluate_late_window(
        self,
        market: dict,
        polymarket_mid_yes: float,
    ) -> TradingSignal:
        """
        Late-window maker strategy — the highest-alpha approach for the
        final seconds of a 5-minute window.

        Key insight (Binance Square, March 2026):
          "85% of the direction of BTC fluctuations is determined T-10s
           before the window ends, but Polymarket's odds have not fully
           reflected this information. Place maker orders at $0.90-0.95
           on the winning side."

        Logic:
          1. Only activates when < 15 seconds remain in the window
             (also useful for 15–60s with partial confidence)
          2. Check price direction since window start
          3. If direction is clear (> 0.1% move), place a MAKER order
             at 0.90–0.95 on the winning side
          4. Edge = 1.00 - entry_price (winner pays $1.00 at settlement)
          5. Zero fees (maker) + potential rebate from FeeManager
          6. Higher confidence closer to window end and with larger move

        Args:
            market:             Market dict from MarketFinder.
            polymarket_mid_yes: Current midpoint price of the YES (Up) token.

        Returns:
            TradingSignal with strategy_name="late_window_maker", or NO_SIGNAL.
        """
        asset = market.get("asset", "").upper()
        window_start = market.get("window_start", 0)
        window_end = market.get("window_end", 0)
        secs_remaining = max(0.0, window_end - time.time())

        # Strategy only fires in the final 60 seconds
        # (with progressively higher confidence closer to the end)
        if secs_remaining > 60:
            return TradingSignal(
                direction=None,
                reasoning=f"Late-window: {secs_remaining:.0f}s remaining — too early",
                strategy_name="late_window_maker",
                asset=asset,
                seconds_remaining=secs_remaining,
            )

        # Get price at window start for direction measurement
        current_price = self._feed.get_current_price(asset)
        if current_price <= 0:
            return TradingSignal(
                direction=None,
                reasoning=f"Late-window: no price data for {asset}",
                strategy_name="late_window_maker",
                asset=asset,
                seconds_remaining=secs_remaining,
            )

        # Get price at window start (lookback = elapsed time in window)
        elapsed = WINDOW_SECONDS - secs_remaining
        history = self._feed.get_price_history(asset, minutes=6)  # full window + buffer
        cutoff = time.time() - elapsed
        window_start_records = [(ts, p) for ts, p in history if ts <= cutoff]

        if not window_start_records:
            # Fall back to 30-second momentum if we don't have full window history
            window_open_price = None
        else:
            # Use oldest available record near window start
            _, window_open_price = min(window_start_records, key=lambda x: abs(x[0] - cutoff))

        if window_open_price is None or window_open_price <= 0:
            # Fall back to 30-second momentum
            momentum_30s = self._feed.get_momentum(asset, window=30)
            window_momentum = momentum_30s
        else:
            window_momentum = (current_price - window_open_price) / window_open_price

        DIRECTION_THRESHOLD = 0.001  # 0.1% move = clear direction

        if abs(window_momentum) < DIRECTION_THRESHOLD:
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Late-window: {asset} move {window_momentum*100:.3f}% "
                    f"below threshold {DIRECTION_THRESHOLD*100:.2f}% — no clear direction"
                ),
                strategy_name="late_window_maker",
                asset=asset,
                seconds_remaining=secs_remaining,
            )

        # Determine winning side
        if window_momentum > 0:
            direction = "YES"  # Price went up → YES (Up) token wins
            poly_prob = polymarket_mid_yes
        else:
            direction = "NO"   # Price went down → NO (Down) token wins
            poly_prob = 1.0 - polymarket_mid_yes

        # Determine entry price based on time remaining
        # With more time → more uncertainty → enter at 0.90
        # Very close to end → higher certainty → can enter at 0.95
        # Scale between 0.90 and 0.95 based on time remaining
        if secs_remaining <= 5:
            entry_price = 0.95
        elif secs_remaining <= 15:
            entry_price = 0.90 + (15 - secs_remaining) / 15 * 0.05  # 0.90 → 0.95
        else:
            # 15–60 seconds: use 0.90 as base, adjust upward with momentum
            momentum_bonus = clamp(abs(window_momentum) * 20, 0.0, 0.05)
            entry_price = 0.90 + momentum_bonus

        entry_price = clamp(round(entry_price, 2), 0.85, 0.97)

        # Edge: at settlement, winner pays $1.00; we enter at entry_price
        # So edge = 1.00 - entry_price (for a MAKER order, no fee deducted)
        edge = 1.0 - entry_price

        # Reduce edge if Polymarket already priced near our entry
        # (less "mispricing" to capture)
        if poly_prob >= entry_price:
            # Market is already at or above our target price — edge may be negative
            edge -= (poly_prob - entry_price)

        if edge <= 0:
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Late-window: edge {edge:.4f} ≤ 0 at entry {entry_price:.2f} "
                    f"(polymarket mid={poly_prob:.2f})"
                ),
                strategy_name="late_window_maker",
                asset=asset,
                seconds_remaining=secs_remaining,
                polymarket_mid=polymarket_mid_yes,
            )

        # Compute confidence
        # Factors:
        #   1. Time factor: higher confidence closer to window end
        #   2. Momentum factor: larger move = more decisive direction
        #   3. Mispricing factor: bigger gap between poly and target = more alpha
        time_factor = clamp(1.0 - secs_remaining / 60.0, 0.1, 1.0)  # 0.1 at 60s, 1.0 at 0s
        momentum_factor = clamp(abs(window_momentum) / DIRECTION_THRESHOLD, 1.0, 5.0) / 5.0
        mispricing = clamp(entry_price - poly_prob, 0.0, 0.20) / 0.20
        mispricing_factor = clamp(mispricing, 0.0, 1.0)

        confidence = (
            0.50 * time_factor
            + 0.30 * momentum_factor
            + 0.20 * mispricing_factor
        )
        confidence = clamp(confidence, 0.0, 1.0)

        # Add maker rebate to edge estimate if FeeManager is available
        rebate = 0.0
        if self._fee_manager is not None:
            rebate = self._fee_manager.estimate_maker_rebate(entry_price)
            edge += rebate  # rebate improves net edge

        reasoning = (
            f"Late-window maker: {asset} moved {window_momentum*100:+.3f}% in window. "
            f"dir={direction} entry={entry_price:.2f} poly_mid={poly_prob:.2f} "
            f"edge={edge:.4f}{'(+rebate)' if rebate>0 else ''} conf={confidence:.2f} "
            f"{secs_remaining:.0f}s remaining"
        )

        logger.info(reasoning)

        return TradingSignal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name="late_window_maker",
            reasoning=reasoning,
            suggested_price=entry_price,
            exchange_prob=0.5 + (window_momentum * 5),  # rough directional estimate
            polymarket_mid=polymarket_mid_yes,
            seconds_remaining=secs_remaining,
            asset=asset,
        )

    # ------------------------------------------------------------------
    # Strategy 1: Latency Arbitrage
    # ------------------------------------------------------------------

    def _latency_arb_signal(
        self,
        asset: str,
        polymarket_mid_yes: float,
        window_start: int,
        secs_remaining: float,
    ) -> TradingSignal:
        """
        Latency arbitrage: exploit the delay between exchange price movement
        and Polymarket's orderbook repricing.

        Logic:
          - Measure price momentum over the last LATENCY_ARB_LOOKBACK_SECONDS.
          - Compute exchange-implied probability based on that momentum.
          - If Polymarket hasn't repriced yet, compute edge and emit a signal.
        """
        lookback = self._scfg.latency_arb_lookback_seconds
        threshold = self._scfg.latency_arb_threshold

        # Current exchange price
        current_price = self._feed.get_current_price(asset)
        if current_price <= 0:
            return NO_SIGNAL

        # Price `lookback` seconds ago
        history = self._feed.get_price_history(asset, minutes=1)
        cutoff = time.time() - lookback

        old_records = [(ts, p) for ts, p in history if ts <= cutoff]
        if not old_records:
            return TradingSignal(
                direction=None,
                reasoning=f"Latency arb: insufficient price history for {asset}",
                asset=asset,
                strategy_name="latency_arb",
            )

        # Use the most recent record before the cutoff
        _, old_price = max(old_records, key=lambda x: x[0])
        if old_price <= 0:
            return NO_SIGNAL

        momentum = (current_price - old_price) / old_price  # signed fraction

        # Compute exchange-implied probability of finishing UP
        # We use a simplified model: larger momentum → higher prob of finishing up.
        volatility = self._feed.get_volatility(asset, window=300)
        exchange_prob = self._momentum_to_probability(momentum, volatility, secs_remaining)

        # Polymarket implied probability for DOWN outcome
        polymarket_mid_no = 1.0 - polymarket_mid_yes

        # Determine direction of the edge
        if momentum >= threshold:
            # Exchange says UP is likely; check if Polymarket agrees
            direction = "YES"        # YES = Up token
            poly_prob = polymarket_mid_yes
            edge_raw = exchange_prob - poly_prob
        elif momentum <= -threshold:
            # Exchange says DOWN is likely
            direction = "NO"         # NO = Down token (we still buy YES for Down)
            poly_prob = polymarket_mid_no
            edge_raw = (1.0 - exchange_prob) - poly_prob
        else:
            # Momentum is too small to be confident
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Latency arb: {asset} momentum {momentum*100:.3f}% "
                    f"below threshold {threshold*100:.2f}%"
                ),
                asset=asset,
                strategy_name="latency_arb",
                exchange_prob=exchange_prob,
                polymarket_mid=polymarket_mid_yes,
                seconds_remaining=secs_remaining,
            )

        # Subtract fee cost from edge
        entry_price = poly_prob
        fee = polymarket_fee(shares=1.0, price=entry_price)
        edge = edge_raw - fee

        if edge <= 0:
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Latency arb: edge {edge_raw:.4f} after fee {fee:.4f} is ≤ 0"
                ),
                asset=asset,
                strategy_name="latency_arb",
                edge=edge,
                exchange_prob=exchange_prob,
                polymarket_mid=polymarket_mid_yes,
                seconds_remaining=secs_remaining,
            )

        # Confidence scales with momentum magnitude and remaining time
        confidence = self._latency_arb_confidence(
            abs(momentum), threshold, secs_remaining, edge
        )

        reasoning = (
            f"Latency arb: {asset} moved {momentum*100:+.3f}% in {lookback}s. "
            f"Exchange prob={exchange_prob:.2f}, Polymarket mid={poly_prob:.2f}, "
            f"edge={edge:.4f}, confidence={confidence:.2f}"
        )

        return TradingSignal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name="latency_arb",
            reasoning=reasoning,
            suggested_price=entry_price,
            exchange_prob=exchange_prob,
            polymarket_mid=polymarket_mid_yes,
            seconds_remaining=secs_remaining,
            asset=asset,
        )

    def _momentum_to_probability(
        self, momentum: float, volatility: float, secs_remaining: float
    ) -> float:
        """
        Convert a price momentum fraction to a probability of finishing UP
        using a simplified log-normal model.

        For very small secs_remaining or zero volatility, we rely more on
        the sign of momentum.
        """
        if secs_remaining <= 0:
            return 0.5 + clamp(momentum * 5, -0.49, 0.49)

        sigma = volatility * math.sqrt(secs_remaining) if volatility > 0 else 0.0

        if sigma == 0:
            # No volatility estimate — use sign of momentum
            if momentum > 0:
                return clamp(0.5 + abs(momentum) * 10, 0.5, 0.95)
            elif momentum < 0:
                return clamp(0.5 - abs(momentum) * 10, 0.05, 0.5)
            return 0.5

        # Normal CDF approximation: prob = Φ(momentum / sigma)
        # Using Abramowitz & Stegun rational approximation
        z = momentum / sigma
        prob = self._normal_cdf(z)
        return clamp(prob, 0.01, 0.99)

    @staticmethod
    def _normal_cdf(z: float) -> float:
        """
        Rational approximation of Φ(z) (normal CDF).
        Max error < 1.5e-7 (Abramowitz & Stegun 26.2.17).
        """
        sign = 1.0 if z >= 0 else -1.0
        z = abs(z)
        t = 1.0 / (1.0 + 0.2316419 * z)
        poly = (
            ((((1.330274429 * t - 1.821255978) * t + 1.781477937) * t
              - 0.356563782) * t + 0.319381530) * t
        )
        return 0.5 + sign * (0.5 - 1.0 / math.sqrt(2 * math.pi)
                             * math.exp(-0.5 * z * z) * poly)

    def _latency_arb_confidence(
        self,
        abs_momentum: float,
        threshold: float,
        secs_remaining: float,
        edge: float,
    ) -> float:
        """
        Estimate confidence in the latency-arb signal.

        Factors:
          - How much larger is momentum vs the threshold?
          - More time remaining → more uncertainty about final direction → lower conf
          - Larger edge → higher confidence
        """
        # Momentum factor: 1.0 when momentum == threshold, grows with excess
        momentum_factor = clamp(abs_momentum / threshold, 1.0, 3.0) / 3.0

        # Time factor: high confidence early in window (price will continue),
        #              but lower confidence if too early (can reverse)
        # Sweet spot: 120–240 seconds remaining
        time_factor = 1.0 - abs(secs_remaining - 180) / 180
        time_factor = clamp(time_factor, 0.3, 1.0)

        # Edge factor: scale edge into [0, 1] range
        edge_factor = clamp(edge / 0.10, 0.0, 1.0)

        confidence = (
            0.50 * momentum_factor
            + 0.30 * time_factor
            + 0.20 * edge_factor
        )
        return clamp(confidence, 0.0, 1.0)

    # ------------------------------------------------------------------
    # Strategy 4: Macro Momentum (24h exchange change vs Polymarket mid)
    # ------------------------------------------------------------------

    def _macro_momentum_signal(
        self,
        asset: str,
        polymarket_mid_yes: float,
        secs_remaining: float,
    ) -> TradingSignal:
        """
        Macro momentum strategy: use the 24-hour price change from the
        exchange as a directional signal.

        Logic:
          - If the exchange shows a strong 24h move (e.g., BTC down -4%),
            the 5-min market should reflect that directional bias.
          - If Polymarket is still at 0.50 (fair coin), there is clear edge.
          - Scale confidence with the magnitude of the 24h move.
          - Use a lower threshold than latency_arb since 24h change is
            a stronger and more persistent signal.

        Thresholds:
          - |24h change| >= 1.0% → consider a trade
          - |24h change| >= 2.0% → moderate confidence
          - |24h change| >= 3.0% → high confidence

        Also incorporates short-term momentum (if available) as confirmation.
        """
        change_24h = self._feed.get_24h_change(asset)

        if abs(change_24h) < 0.005:  # less than 0.5% — not enough signal
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Macro momentum: {asset} 24h change {change_24h*100:+.2f}% "
                    f"below 0.5% threshold"
                ),
                asset=asset,
                strategy_name="macro_momentum",
                seconds_remaining=secs_remaining,
            )

        # Determine direction from 24h change
        if change_24h < 0:
            # Exchange is bearish → expect DOWN in 5-min window
            direction = "NO"          # Buy the Down token
            poly_prob = 1.0 - polymarket_mid_yes  # Current DOWN price
        else:
            # Exchange is bullish → expect UP in 5-min window
            direction = "YES"         # Buy the Up token
            poly_prob = polymarket_mid_yes        # Current UP price

        # Skip if market has already priced in the move heavily
        # (poly_prob > 0.70 means market is already 70%+ confident)
        if poly_prob > 0.70:
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Macro momentum: {asset} market already priced in "
                    f"(poly={poly_prob:.2f} for {direction}) — no edge left"
                ),
                asset=asset,
                strategy_name="macro_momentum",
                seconds_remaining=secs_remaining,
            )

        # Estimate our probability based on 24h change magnitude
        # Larger 24h move → stronger directional bias for the 5-min window
        abs_change = abs(change_24h)
        if abs_change >= 0.05:      # 5%+ move
            our_prob = 0.62
        elif abs_change >= 0.03:    # 3-5% move
            our_prob = 0.58
        elif abs_change >= 0.02:    # 2-3% move
            our_prob = 0.56
        elif abs_change >= 0.01:    # 1-2% move
            our_prob = 0.54
        else:                       # 0.5-1% move
            our_prob = 0.52

        # Short-term momentum confirmation (if available)
        momentum_30s = self._feed.get_momentum(asset, window=30)
        momentum_confirms = (
            (change_24h < 0 and momentum_30s <= 0) or
            (change_24h > 0 and momentum_30s >= 0)
        )
        if momentum_confirms:
            our_prob += 0.02  # boost if short-term agrees with 24h trend

        # Edge = our estimated probability - market price - fees
        edge_raw = our_prob - poly_prob
        fee = polymarket_fee(shares=1.0, price=poly_prob)
        edge = edge_raw - fee

        if edge <= 0:
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Macro momentum: {asset} 24h={change_24h*100:+.2f}% "
                    f"our_prob={our_prob:.2f} poly={poly_prob:.2f} "
                    f"edge={edge:.4f} after fee — not enough"
                ),
                asset=asset,
                strategy_name="macro_momentum",
                edge=edge,
                exchange_prob=our_prob,
                polymarket_mid=polymarket_mid_yes,
                seconds_remaining=secs_remaining,
            )

        # Confidence scales with: magnitude of 24h change, edge, momentum confirmation
        change_factor = clamp(abs_change / 0.03, 0.3, 1.0)  # caps at 3%
        edge_factor = clamp(edge / 0.05, 0.1, 1.0)          # caps at 5 cents edge
        confirm_factor = 0.8 if momentum_confirms else 0.5
        # Time factor: prefer trading earlier in window (more time for our thesis)
        time_factor = clamp(secs_remaining / 240, 0.4, 1.0)

        confidence = (
            0.35 * change_factor
            + 0.25 * edge_factor
            + 0.20 * confirm_factor
            + 0.20 * time_factor
        )
        confidence = clamp(confidence, 0.0, 1.0)

        reasoning = (
            f"Macro momentum: {asset} 24h={change_24h*100:+.2f}% → dir={direction} "
            f"our_prob={our_prob:.2f} poly={poly_prob:.2f} edge={edge:.4f} "
            f"conf={confidence:.2f} confirm={'yes' if momentum_confirms else 'no'} "
            f"{secs_remaining:.0f}s remaining"
        )

        logger.info(reasoning)

        return TradingSignal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name="macro_momentum",
            reasoning=reasoning,
            suggested_price=poly_prob,
            exchange_prob=our_prob,
            polymarket_mid=polymarket_mid_yes,
            seconds_remaining=secs_remaining,
            asset=asset,
        )

    # ------------------------------------------------------------------
    # Strategy 2: Signal-Based
    # ------------------------------------------------------------------

    def _signal_based_signal(
        self,
        asset: str,
        polymarket_mid_yes: float,
        secs_remaining: float,
    ) -> TradingSignal:
        """
        Signal-based strategy using RSI, MACD, and Bollinger Bands.

        Bullish signals (BUY YES = Up):
          - RSI < 30 (oversold) AND positive recent momentum
          - MACD histogram just turned positive (crossover)
          - Price bounced off lower Bollinger Band

        Bearish signals (BUY YES = Down):
          - RSI > 70 (overbought) AND negative recent momentum
          - MACD histogram just turned negative
          - Price rejected at upper Bollinger Band
        """
        lookback = self._scfg.signal_lookback_minutes

        # Get indicators
        rsi = self._feed.get_rsi(asset, period=self._scfg.rsi_period, minutes=lookback)
        macd_line, macd_signal, macd_hist = self._feed.get_macd(
            asset,
            fast=self._scfg.macd_fast,
            slow=self._scfg.macd_slow,
            signal=self._scfg.macd_signal,
            minutes=lookback,
        )
        bb_upper, bb_mid, bb_lower = self._feed.get_bollinger_bands(
            asset,
            period=self._scfg.bb_period,
            std=self._scfg.bb_std,
            minutes=lookback,
        )

        current_price = self._feed.get_current_price(asset)
        momentum_30s = self._feed.get_momentum(asset, window=30)
        momentum_60s = self._feed.get_momentum(asset, window=60)

        # --- Bullish scoring ---
        bullish_score = 0.0
        bearish_score = 0.0
        reasons = []

        # RSI signal
        if rsi < self._scfg.rsi_oversold:
            rsi_bull = (self._scfg.rsi_oversold - rsi) / self._scfg.rsi_oversold
            bullish_score += 0.35 * rsi_bull
            reasons.append(f"RSI={rsi:.1f} oversold")
        elif rsi > self._scfg.rsi_overbought:
            rsi_bear = (rsi - self._scfg.rsi_overbought) / (100 - self._scfg.rsi_overbought)
            bearish_score += 0.35 * rsi_bear
            reasons.append(f"RSI={rsi:.1f} overbought")

        # Momentum confirmation
        if momentum_30s > 0 and bullish_score > 0:
            bullish_score += 0.15
            reasons.append(f"Momentum +{momentum_30s*100:.3f}%")
        elif momentum_30s < 0 and bearish_score > 0:
            bearish_score += 0.15
            reasons.append(f"Momentum {momentum_30s*100:.3f}%")

        # MACD crossover signal
        if macd_hist > 0 and macd_line > macd_signal:
            bullish_score += 0.25
            reasons.append(f"MACD bull crossover hist={macd_hist:.5f}")
        elif macd_hist < 0 and macd_line < macd_signal:
            bearish_score += 0.25
            reasons.append(f"MACD bear crossover hist={macd_hist:.5f}")

        # Bollinger Band bounce
        if bb_lower > 0 and current_price > 0:
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                position = (current_price - bb_lower) / bb_range  # 0 = at lower, 1 = at upper

                if position < 0.15:  # Near lower band — potential bounce up
                    bullish_score += 0.25
                    reasons.append(f"BB lower band bounce pos={position:.2f}")
                elif position > 0.85:  # Near upper band — potential reversal down
                    bearish_score += 0.25
                    reasons.append(f"BB upper band rejection pos={position:.2f}")

        # Pick the dominant direction
        if bullish_score >= bearish_score and bullish_score > 0:
            direction = "YES"
            confidence = bullish_score
            poly_prob = polymarket_mid_yes
            implied_prob = clamp(0.5 + bullish_score * 0.3, 0.5, 0.9)
        elif bearish_score > bullish_score:
            direction = "NO"
            confidence = bearish_score
            poly_prob = 1.0 - polymarket_mid_yes
            implied_prob = clamp(0.5 + bearish_score * 0.3, 0.5, 0.9)
        else:
            return TradingSignal(
                direction=None,
                reasoning=f"Signal: no dominant direction for {asset}",
                asset=asset,
                strategy_name="signal_based",
                seconds_remaining=secs_remaining,
            )

        # Calculate edge
        edge_raw = implied_prob - poly_prob
        fee = polymarket_fee(shares=1.0, price=poly_prob)
        edge = edge_raw - fee

        reasoning = (
            f"Signal [{asset}] dir={direction} | "
            + " | ".join(reasons)
            + f" | edge={edge:.4f} conf={confidence:.2f}"
        )

        return TradingSignal(
            direction=direction,
            confidence=clamp(confidence, 0.0, 1.0),
            edge=edge,
            strategy_name="signal_based",
            reasoning=reasoning,
            suggested_price=poly_prob,
            exchange_prob=implied_prob,
            polymarket_mid=polymarket_mid_yes,
            seconds_remaining=secs_remaining,
            asset=asset,
        )
