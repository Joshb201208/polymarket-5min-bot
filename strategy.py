"""
strategy.py - Trading strategy engine for Polymarket 5-minute crypto markets.

UPGRADE v3 — March 20, 2026
  - NEW: Oracle Latency Arb (primary) — compares Binance price vs Price-to-Beat
    to detect when oracle lag creates exploitable mispricing.
  - NEW: Regime Filter — ADX + Bollinger Band width + volatility checks.
    Blocks ALL strategies from trading in choppy / low-vol conditions.
  - KEPT: Late-Window Maker, Latency Arb (legacy), Signal-Based, Macro Momentum

Strategies (priority order during evaluation):
  1. oracle_arb        — Oracle latency arb using Price-to-Beat spread (NEW)
  2. late_window_maker — Final-seconds maker orders
  3. latency_arb       — Legacy momentum-based latency arb
  4. signal_based      — RSI / MACD / BB technical signals
  5. macro_momentum    — 24h trend following
"""

import time
import math
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple, List

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
# Regime Filter — blocks trading in choppy / low-vol conditions
# ---------------------------------------------------------------------------

class RegimeFilter:
    """
    Determines whether the current market regime is suitable for trading.

    Checks:
      1. ADX proxy > threshold (trending market)
      2. Bollinger Band width > threshold (enough volatility to profit)
      3. Recent candle body size (not just wicks / noise)

    If the regime is unfavorable, ALL strategies are blocked.
    """

    # ADX-equivalent thresholds (using momentum-based proxy)
    # TUNED: Previous values (ADX 15, BB 0.0015, candle 0.0005) were too
    # aggressive — they blocked 95%+ of windows and prevented oracle_arb
    # from EVER firing. Real BTC ADX proxy sits around 7-12 in normal
    # conditions. Lowered to allow trading in moderately trending markets.
    ADX_THRESHOLD = 8.0           # Was 15.0 — normal crypto ADX is 7-12
    BB_WIDTH_THRESHOLD = 0.0008   # Was 0.0015 — allow moderate volatility
    MIN_CANDLE_BODY_PCT = 0.0002  # Was 0.0005 — allow smaller moves (0.02%)

    def __init__(self, price_feed: PriceFeed):
        self._feed = price_feed

    def is_favorable(self, asset: str) -> Tuple[bool, str]:
        """
        Returns (True, reason) if trading conditions are favorable,
        or (False, reason) if the regime filter blocks trading.
        """
        asset = asset.upper()

        # --- Check 1: ADX proxy (directional strength) ---
        # True ADX requires DM+/DM- calculations over many candles.
        # We approximate using the ratio of directional movement to total
        # movement over the last 5 minutes.
        adx_proxy = self._calculate_adx_proxy(asset)
        if adx_proxy < self.ADX_THRESHOLD:
            return False, (
                f"Regime filter: ADX proxy {adx_proxy:.1f} < {self.ADX_THRESHOLD} "
                f"— market is choppy, skipping"
            )

        # --- Check 2: Bollinger Band width ---
        bb_upper, bb_mid, bb_lower = self._feed.get_bollinger_bands(
            asset, period=20, std=2.0, minutes=15
        )
        if bb_mid > 0 and bb_upper > bb_lower:
            bb_width = (bb_upper - bb_lower) / bb_mid
            if bb_width < self.BB_WIDTH_THRESHOLD:
                return False, (
                    f"Regime filter: BB width {bb_width:.5f} < {self.BB_WIDTH_THRESHOLD} "
                    f"— volatility too low, skipping"
                )

        # --- Check 3: Recent candle body size ---
        momentum_60s = abs(self._feed.get_momentum(asset, window=60))
        if momentum_60s < self.MIN_CANDLE_BODY_PCT:
            # Additional check: is 30s momentum also flat?
            momentum_30s = abs(self._feed.get_momentum(asset, window=30))
            if momentum_30s < self.MIN_CANDLE_BODY_PCT:
                return False, (
                    f"Regime filter: 60s momentum {momentum_60s*100:.4f}% and "
                    f"30s momentum {momentum_30s*100:.4f}% both below "
                    f"{self.MIN_CANDLE_BODY_PCT*100:.3f}% — flat market, skipping"
                )

        return True, f"Regime favorable: ADX={adx_proxy:.1f}, 60s_mom={momentum_60s*100:.3f}%"

    def _calculate_adx_proxy(self, asset: str) -> float:
        """
        Approximate ADX using directional movement ratio.

        Logic:
          - Get price history for last 5 minutes
          - Calculate net directional movement vs total absolute movement
          - Scale to 0-100 range (like ADX)

        A high ratio means the price moved consistently in one direction
        (trending). A low ratio means it oscillated (choppy).
        """
        history = self._feed.get_price_history(asset, minutes=5)
        if len(history) < 10:
            return 50.0  # Neutral — don't block if insufficient data

        prices = [p for _, p in history]

        # Net movement (directional)
        net_move = abs(prices[-1] - prices[0])

        # Total absolute movement (sum of all candle-to-candle changes)
        total_move = sum(abs(prices[i] - prices[i-1]) for i in range(1, len(prices)))

        if total_move == 0:
            return 0.0

        # Directional ratio [0, 1] → scale to [0, 100]
        ratio = net_move / total_move
        adx_proxy = ratio * 100.0

        return adx_proxy


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
        self._regime_filter = RegimeFilter(price_feed)

        # Oracle arb state: track when Binance has been consistently above/below
        # the Price-to-Beat for signal persistence check
        self._oracle_signal_state = {}  # asset -> {direction, start_time, spread}

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

        # Don't trade if we have no price data
        current_price = self._feed.get_current_price(asset)
        if current_price <= 0:
            return TradingSignal(
                direction=None,
                reasoning=f"No price data for {asset}",
                asset=asset,
            )

        # ================================================================
        # STRATEGY 1: Oracle Latency Arb (ALWAYS evaluated first)
        # Oracle arb does NOT need a trending market — it exploits the lag
        # between exchange price and Polymarket book. A flat market can
        # still have oracle lag. The spread check inside oracle_arb is
        # the only filter it needs.
        # ================================================================
        oracle_signal = self._oracle_arb_signal(
            asset, current_price, polymarket_mid_yes, window_start, secs_remaining
        )
        if oracle_signal.is_valid:
            return oracle_signal

        # ================================================================
        # REGIME FILTER — only gates lower-priority strategies
        # Oracle arb above is exempt (structural edge, not trend-dependent)
        # ================================================================
        regime_ok, regime_reason = self._regime_filter.is_favorable(asset)
        if not regime_ok:
            logger.debug("[%s] %s", asset, regime_reason)
            return TradingSignal(
                direction=None,
                reasoning=regime_reason,
                seconds_remaining=secs_remaining,
                asset=asset,
                strategy_name="regime_filter",
            )

        # In the final 30 seconds, try the late-window maker strategy
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

        # ================================================================
        # STRATEGY 2: Legacy Latency Arb (momentum-based)
        # ================================================================
        if self._scfg.primary_strategy == "latency_arb":
            signal = self._latency_arb_signal(
                asset, polymarket_mid_yes, window_start, secs_remaining
            )
        else:
            signal = self._signal_based_signal(asset, polymarket_mid_yes, secs_remaining)

        # If primary strategy found nothing, try secondary
        if not signal.is_valid and self._scfg.primary_strategy == "latency_arb":
            signal = self._signal_based_signal(asset, polymarket_mid_yes, secs_remaining)

        # DISABLED: macro_momentum was the only strategy firing overnight,
        # and it lost money (38% win rate, -$35 P&L). It applies 24h trends
        # to 5-min markets — a fundamentally flawed approach.
        # if not signal.is_valid:
        #     signal = self._macro_momentum_signal(asset, polymarket_mid_yes, secs_remaining)

        # In the 30–60 second zone, also try the late-window strategy
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
            (exception: late_window_maker and oracle_arb may have <60s)
        """
        if not signal.is_valid:
            return False

        if signal.edge < self._rcfg.min_edge_threshold:
            logger.debug(
                "should_trade: SKIP — edge %.4f < min %.4f",
                signal.edge, self._rcfg.min_edge_threshold,
            )
            return False

        # Confidence thresholds vary by strategy
        if signal.strategy_name == "late_window_maker":
            min_conf = max(0.50, self._scfg.signal_confidence_threshold - 0.10)
        elif signal.strategy_name == "macro_momentum":
            min_conf = max(0.40, self._scfg.signal_confidence_threshold - 0.20)
        elif signal.strategy_name == "oracle_arb":
            min_conf = 0.55  # Oracle arb has structural edge — lower bar
        else:
            min_conf = self._scfg.signal_confidence_threshold

        if signal.confidence < min_conf:
            logger.debug(
                "should_trade: SKIP — confidence %.2f < min %.2f",
                signal.confidence, min_conf,
            )
            return False

        # Late-window maker and oracle_arb can trade with < 60s remaining
        if signal.strategy_name not in ("late_window_maker", "oracle_arb") and signal.seconds_remaining < 60:
            logger.debug("should_trade: SKIP — only %.0fs remaining", signal.seconds_remaining)
            return False

        logger.info("should_trade: TRADE — %s", signal)
        return True

    # ------------------------------------------------------------------
    # NEW STRATEGY: Oracle Latency Arbitrage
    # ------------------------------------------------------------------

    def _oracle_arb_signal(
        self,
        asset: str,
        current_price: float,
        polymarket_mid_yes: float,
        window_start: int,
        secs_remaining: float,
    ) -> TradingSignal:
        """
        Oracle latency arbitrage: exploit the 3-10 second lag between
        Binance/exchange price and Chainlink Data Streams oracle.

        Core insight: Polymarket settlement uses Chainlink oracle snapshots
        at window open and close. The oracle lags Binance by several seconds.
        If Binance has moved firmly in one direction, the oracle will follow.

        Logic:
          1. Estimate the "Price to Beat" (oracle opening price) from
             the exchange price near window start
          2. Compare current Binance price vs Price to Beat
          3. If spread > threshold AND persists for 5+ seconds, signal
          4. Higher confidence with: larger spread, less time remaining,
             confirmed across multiple timeframes
        """
        # --- Estimate Price to Beat ---
        # The true Price to Beat is the Chainlink oracle snapshot at window open.
        # We approximate it using the exchange price at window start.
        price_to_beat = self._estimate_price_to_beat(asset, window_start)
        if price_to_beat <= 0:
            return TradingSignal(
                direction=None,
                reasoning=f"Oracle arb: cannot estimate Price-to-Beat for {asset}",
                asset=asset,
                strategy_name="oracle_arb",
                seconds_remaining=secs_remaining,
            )

        # --- Calculate spread ---
        spread = current_price - price_to_beat
        abs_spread = abs(spread)

        # Dynamic threshold based on asset price level
        # TUNED: Previous values ($40/$1.50/$0.08) were too high — real
        # BTC spreads sit around $10-20 in normal conditions, meaning
        # the bot never traded oracle_arb. Halved to get actual trades
        # while still requiring meaningful price divergence.
        if current_price > 10000:      # BTC
            min_spread = 18.0          # Was $40 — real spreads ~$10-20
        elif current_price > 500:       # ETH
            min_spread = 0.60          # Was $1.50 — real spreads ~$0.2-0.5
        else:                           # SOL
            min_spread = 0.04          # Was $0.08 — real spreads ~$0.03-0.06

        # Spread as percentage of price
        spread_pct = abs_spread / price_to_beat if price_to_beat > 0 else 0

        # DIAGNOSTIC: Log spread values to understand why oracle_arb rarely fires
        logger.info(
            "[%s] Oracle spread: $%+.2f (%.3f%%) vs threshold $%.2f | "
            "price=%.2f ptb=%.2f | poly_mid=%.3f | %ds left",
            asset, spread, spread_pct * 100, min_spread,
            current_price, price_to_beat, polymarket_mid_yes, int(secs_remaining),
        )

        if abs_spread < min_spread:
            # Reset signal state if spread collapsed
            if asset in self._oracle_signal_state:
                del self._oracle_signal_state[asset]
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Oracle arb: {asset} spread ${spread:+,.2f} "
                    f"({spread_pct*100:.3f}%) below threshold ${min_spread:.2f}"
                ),
                asset=asset,
                strategy_name="oracle_arb",
                seconds_remaining=secs_remaining,
            )

        # --- Determine direction ---
        direction = "YES" if spread > 0 else "NO"  # YES=Up, NO=Down

        # --- Poly prob filter: skip if market already priced in ---
        # TUNED: Widened from 0.35-0.65 to 0.30-0.72.
        # At 0.65, BTC was being blocked with poly=0.68 even though the
        # exchange spread was $30+. The market is efficient but not perfect;
        # there can still be edge at 0.68-0.72 if our true_prob is higher.
        # Below 0.30: market is heavily against us, no point.
        # Above 0.72: entry too expensive, risk/reward breaks down.
        our_poly_prob = polymarket_mid_yes if direction == "YES" else (1.0 - polymarket_mid_yes)
        if our_poly_prob < 0.30:
            if asset in self._oracle_signal_state:
                del self._oracle_signal_state[asset]
            reason = (
                f"Oracle arb: {asset} {direction} SKIP poly_prob={our_poly_prob:.2f} < 0.30 "
                f"(market heavily against us)"
            )
            logger.info(reason)
            return TradingSignal(
                direction=None, reasoning=reason, asset=asset,
                strategy_name="oracle_arb", seconds_remaining=secs_remaining,
            )
        if our_poly_prob > 0.72:
            if asset in self._oracle_signal_state:
                del self._oracle_signal_state[asset]
            reason = (
                f"Oracle arb: {asset} {direction} SKIP poly_prob={our_poly_prob:.2f} > 0.72 "
                f"(entry too expensive)"
            )
            logger.info(reason)
            return TradingSignal(
                direction=None, reasoning=reason, asset=asset,
                strategy_name="oracle_arb", seconds_remaining=secs_remaining,
            )

        # --- Signal persistence check (5s confirmation) ---
        state = self._oracle_signal_state.get(asset)
        now = time.time()

        if state is None or state["direction"] != direction:
            self._oracle_signal_state[asset] = {
                "direction": direction,
                "start_time": now,
                "spread": spread,
                "peak_spread": abs_spread,
            }
            reason = (
                f"Oracle arb: {asset} {direction} spread=${spread:+,.2f} "
                f"poly={our_poly_prob:.2f} — waiting 5s confirm"
            )
            logger.info(reason)
            return TradingSignal(
                direction=None, reasoning=reason, asset=asset,
                strategy_name="oracle_arb",
                seconds_remaining=secs_remaining,
            )

        signal_age = now - state["start_time"]
        state["peak_spread"] = max(state["peak_spread"], abs_spread)

        CONFIRMATION_SECONDS = 5.0
        if signal_age < CONFIRMATION_SECONDS:
            logger.info(
                "Oracle arb: %s %s confirming %.1f/%.0fs spread=$%+.2f poly=%.2f",
                asset, direction, signal_age, CONFIRMATION_SECONDS, spread, our_poly_prob,
            )
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Oracle arb: {asset} {direction} confirmed {signal_age:.1f}s "
                    f"/ {CONFIRMATION_SECONDS}s — waiting"
                ),
                asset=asset,
                strategy_name="oracle_arb",
                seconds_remaining=secs_remaining,
            )

        # ================================================================
        # SIGNAL CONFIRMED — calculate edge and confidence
        # ================================================================

        # Polymarket probability for the winning side
        if direction == "YES":
            poly_prob = polymarket_mid_yes
        else:
            poly_prob = 1.0 - polymarket_mid_yes

        # Our estimated true probability based on the spread
        # Larger spread = higher true probability of the outcome
        # Base: 55% at threshold, scaling up to 75% at 4x threshold
        # FIX: Previous values (0.70-0.95) were unrealistically high and
        # created phantom edge. Oracle lag gives us a modest informational
        # advantage, not a crystal ball. Realistic range: 55-75%.
        spread_multiple = abs_spread / min_spread  # 1.0 at threshold, grows with spread
        true_prob = clamp(0.55 + (spread_multiple - 1.0) * 0.06, 0.55, 0.75)

        # Time boost: closer to window end = more certainty (less time to reverse)
        if secs_remaining < 60:
            true_prob = clamp(true_prob + 0.03, 0.55, 0.78)
        if secs_remaining < 30:
            true_prob = clamp(true_prob + 0.03, 0.55, 0.80)

        # FIX: Cap true_prob so it can never exceed poly_prob by more than 0.15.
        # If the market prices our side at 0.50, we can claim at most 0.65.
        # This prevents absurd edge calculations like 0.70 - 0.19 = 0.51.
        max_true_prob = poly_prob + 0.15
        true_prob = min(true_prob, max_true_prob)

        # Edge = true_prob - entry_cost (poly prob + fees)
        entry_price = poly_prob
        fee = polymarket_fee(shares=1.0, price=entry_price)
        edge = true_prob - entry_price - fee

        logger.info(
            "Oracle arb: %s %s CONFIRMED spread=$%+.2f true=%.2f poly=%.2f "
            "fee=%.4f edge=%.4f %ds left",
            asset, direction, spread, true_prob, poly_prob, fee, edge,
            int(secs_remaining),
        )

        if edge <= 0:
            return TradingSignal(
                direction=None,
                reasoning=(
                    f"Oracle arb: {asset} {direction} spread=${spread:+,.2f} "
                    f"true_prob={true_prob:.2f} poly={poly_prob:.2f} fee={fee:.4f} "
                    f"edge={edge:.4f} — not profitable"
                ),
                asset=asset,
                strategy_name="oracle_arb",
                edge=edge,
                seconds_remaining=secs_remaining,
            )

        # Confidence factors
        # 1. Spread magnitude (larger = more confident)
        spread_factor = clamp(spread_multiple / 3.0, 0.3, 1.0)
        # 2. Signal persistence (longer confirmed = more confident)
        persistence_factor = clamp(signal_age / 15.0, 0.3, 1.0)
        # 3. Time factor (less time remaining = more certain about direction)
        time_factor = clamp(1.0 - secs_remaining / 300.0, 0.2, 1.0)
        # 4. Cross-timeframe confirmation: 30s and 60s momentum agree?
        momentum_30s = self._feed.get_momentum(asset, window=30)
        momentum_60s = self._feed.get_momentum(asset, window=60)
        momentum_agrees = (
            (direction == "YES" and momentum_30s > 0 and momentum_60s > 0) or
            (direction == "NO" and momentum_30s < 0 and momentum_60s < 0)
        )
        confirm_factor = 1.0 if momentum_agrees else 0.6

        confidence = (
            0.30 * spread_factor
            + 0.20 * persistence_factor
            + 0.25 * time_factor
            + 0.25 * confirm_factor
        )
        confidence = clamp(confidence, 0.0, 1.0)

        reasoning = (
            f"ORACLE ARB: {asset} {direction} | "
            f"Binance=${current_price:,.2f} vs PtB=${price_to_beat:,.2f} | "
            f"spread=${spread:+,.2f} ({spread_pct*100:.3f}%) | "
            f"true_prob={true_prob:.2f} poly={poly_prob:.2f} | "
            f"edge={edge:.4f} conf={confidence:.2f} | "
            f"confirmed={signal_age:.0f}s | {secs_remaining:.0f}s remaining"
        )

        logger.info(reasoning)

        # Clear state after emitting signal (don't re-signal same move)
        del self._oracle_signal_state[asset]

        return TradingSignal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            strategy_name="oracle_arb",
            reasoning=reasoning,
            suggested_price=entry_price,
            exchange_prob=true_prob,
            polymarket_mid=polymarket_mid_yes,
            seconds_remaining=secs_remaining,
            asset=asset,
        )

    def _estimate_price_to_beat(self, asset: str, window_start: int) -> float:
        """
        Estimate the oracle's "Price to Beat" (opening price) for this window.

        The true PtB is the Chainlink Data Stream snapshot at window_start.
        We approximate it using the exchange price closest to window_start
        from our price history.

        If we don't have data from window start, fall back to the earliest
        price in the current window.
        """
        history = self._feed.get_price_history(asset, minutes=6)
        if not history:
            return 0.0

        # Find the price closest to window_start
        target_ts = float(window_start)
        best_price = 0.0
        best_diff = float("inf")

        for ts, price in history:
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best_price = price

        # If our best match is more than 60s from window start, it's unreliable
        if best_diff > 60:
            # Fall back to earliest price in the window
            window_prices = [(ts, p) for ts, p in history if ts >= target_ts]
            if window_prices:
                _, best_price = min(window_prices, key=lambda x: x[0])
            else:
                # Last resort: use current price (worst case)
                best_price = self._feed.get_current_price(asset)

        return best_price

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
            window_open_price = None
        else:
            _, window_open_price = min(window_start_records, key=lambda x: abs(x[0] - cutoff))

        if window_open_price is None or window_open_price <= 0:
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
            direction = "YES"
            poly_prob = polymarket_mid_yes
        else:
            direction = "NO"
            poly_prob = 1.0 - polymarket_mid_yes

        # Determine entry price based on time remaining
        if secs_remaining <= 5:
            entry_price = 0.95
        elif secs_remaining <= 15:
            entry_price = 0.90 + (15 - secs_remaining) / 15 * 0.05
        else:
            momentum_bonus = clamp(abs(window_momentum) * 20, 0.0, 0.05)
            entry_price = 0.90 + momentum_bonus

        entry_price = clamp(round(entry_price, 2), 0.85, 0.97)

        # Edge: at settlement, winner pays $1.00; we enter at entry_price
        edge = 1.0 - entry_price

        # Reduce edge if Polymarket already priced near our entry
        if poly_prob >= entry_price:
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
        time_factor = clamp(1.0 - secs_remaining / 60.0, 0.1, 1.0)
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
            edge += rebate

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
            exchange_prob=0.5 + (window_momentum * 5),
            polymarket_mid=polymarket_mid_yes,
            seconds_remaining=secs_remaining,
            asset=asset,
        )

    # ------------------------------------------------------------------
    # Strategy 1: Latency Arbitrage (legacy)
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
        """
        lookback = self._scfg.latency_arb_lookback_seconds
        threshold = self._scfg.latency_arb_threshold

        current_price = self._feed.get_current_price(asset)
        if current_price <= 0:
            return NO_SIGNAL

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

        _, old_price = max(old_records, key=lambda x: x[0])
        if old_price <= 0:
            return NO_SIGNAL

        momentum = (current_price - old_price) / old_price

        volatility = self._feed.get_volatility(asset, window=300)
        exchange_prob = self._momentum_to_probability(momentum, volatility, secs_remaining)

        polymarket_mid_no = 1.0 - polymarket_mid_yes

        if momentum >= threshold:
            direction = "YES"
            poly_prob = polymarket_mid_yes
            edge_raw = exchange_prob - poly_prob
        elif momentum <= -threshold:
            direction = "NO"
            poly_prob = polymarket_mid_no
            edge_raw = (1.0 - exchange_prob) - poly_prob
        else:
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
        """
        if secs_remaining <= 0:
            return 0.5 + clamp(momentum * 5, -0.49, 0.49)

        sigma = volatility * math.sqrt(secs_remaining) if volatility > 0 else 0.0

        if sigma == 0:
            if momentum > 0:
                return clamp(0.5 + abs(momentum) * 10, 0.5, 0.95)
            elif momentum < 0:
                return clamp(0.5 - abs(momentum) * 10, 0.05, 0.5)
            return 0.5

        z = momentum / sigma
        prob = self._normal_cdf(z)
        return clamp(prob, 0.01, 0.99)

    @staticmethod
    def _normal_cdf(z: float) -> float:
        """Rational approximation of Φ(z) (normal CDF)."""
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
        """Estimate confidence in the latency-arb signal."""
        momentum_factor = clamp(abs_momentum / threshold, 1.0, 3.0) / 3.0
        time_factor = 1.0 - abs(secs_remaining - 180) / 180
        time_factor = clamp(time_factor, 0.3, 1.0)
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
        """
        change_24h = self._feed.get_24h_change(asset)

        if abs(change_24h) < 0.005:
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

        if change_24h < 0:
            direction = "NO"
            poly_prob = 1.0 - polymarket_mid_yes
        else:
            direction = "YES"
            poly_prob = polymarket_mid_yes

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

        abs_change = abs(change_24h)
        if abs_change >= 0.05:
            our_prob = 0.62
        elif abs_change >= 0.03:
            our_prob = 0.58
        elif abs_change >= 0.02:
            our_prob = 0.56
        elif abs_change >= 0.01:
            our_prob = 0.54
        else:
            our_prob = 0.52

        momentum_30s = self._feed.get_momentum(asset, window=30)
        momentum_confirms = (
            (change_24h < 0 and momentum_30s <= 0) or
            (change_24h > 0 and momentum_30s >= 0)
        )
        if momentum_confirms:
            our_prob += 0.02

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

        change_factor = clamp(abs_change / 0.03, 0.3, 1.0)
        edge_factor = clamp(edge / 0.05, 0.1, 1.0)
        confirm_factor = 0.8 if momentum_confirms else 0.5
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
        """
        lookback = self._scfg.signal_lookback_minutes

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

        bullish_score = 0.0
        bearish_score = 0.0
        reasons = []

        if rsi < self._scfg.rsi_oversold:
            rsi_bull = (self._scfg.rsi_oversold - rsi) / self._scfg.rsi_oversold
            bullish_score += 0.35 * rsi_bull
            reasons.append(f"RSI={rsi:.1f} oversold")
        elif rsi > self._scfg.rsi_overbought:
            rsi_bear = (rsi - self._scfg.rsi_overbought) / (100 - self._scfg.rsi_overbought)
            bearish_score += 0.35 * rsi_bear
            reasons.append(f"RSI={rsi:.1f} overbought")

        if momentum_30s > 0 and bullish_score > 0:
            bullish_score += 0.15
            reasons.append(f"Momentum +{momentum_30s*100:.3f}%")
        elif momentum_30s < 0 and bearish_score > 0:
            bearish_score += 0.15
            reasons.append(f"Momentum {momentum_30s*100:.3f}%")

        if macd_hist > 0 and macd_line > macd_signal:
            bullish_score += 0.25
            reasons.append(f"MACD bull crossover hist={macd_hist:.5f}")
        elif macd_hist < 0 and macd_line < macd_signal:
            bearish_score += 0.25
            reasons.append(f"MACD bear crossover hist={macd_hist:.5f}")

        if bb_lower > 0 and current_price > 0:
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                position = (current_price - bb_lower) / bb_range

                if position < 0.15:
                    bullish_score += 0.25
                    reasons.append(f"BB lower band bounce pos={position:.2f}")
                elif position > 0.85:
                    bearish_score += 0.25
                    reasons.append(f"BB upper band rejection pos={position:.2f}")

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
