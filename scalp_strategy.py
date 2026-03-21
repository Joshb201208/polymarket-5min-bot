"""
scalp_strategy.py - Core scalp detection and signal generation.

Detects sharp exchange price moves relative to Polymarket's price-to-beat,
generates entry/exit signals for 5-minute crypto scalping.

Every 2 seconds the caller feeds us fresh prices. We track velocity via
ring buffers and emit ScalpSignal / ExitSignal when conditions are met.
"""

import time
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScalpSignal:
    """Entry signal emitted by ScalpStrategy."""
    direction: str           # "YES" or "NO"
    asset: str               # "BTC", "ETH", "SOL"
    entry_price: float       # Polymarket token price to buy at
    token_id_to_buy: str     # Token ID to purchase
    spread: float            # exchange_price - price_to_beat
    velocity: float          # price change % over lookback window
    confidence: float        # 0-1 signal strength
    reasoning: str           # human-readable explanation
    mode: str = "early"      # "early" or "late" — determines exit params


@dataclass
class ExitSignal:
    """Exit signal emitted by ScalpStrategy."""
    reason: str              # "take_profit", "stop_loss", "max_hold", "window_ending"
    exit_price: float        # current token mid price
    pnl_pct: float           # unrealised P&L as fraction


@dataclass
class ScalpPosition:
    """Tracks an open scalp position."""
    position_id: str
    asset: str
    direction: str           # "YES" or "NO"
    token_id: str
    entry_price: float       # Polymarket token price paid
    shares: float
    size_usd: float
    entry_time: float        # Unix timestamp
    entry_fee: float
    mode: str = "early"      # "early" or "late"


# ---------------------------------------------------------------------------
# Ring buffer for price velocity tracking
# ---------------------------------------------------------------------------

@dataclass
class _PriceEntry:
    timestamp: float
    price: float


# ---------------------------------------------------------------------------
# Risk Manager (embedded)
# ---------------------------------------------------------------------------

class ScalpRiskManager:
    """
    Manages risk limits for scalp trading.

    - Max 1 position per asset
    - Max N total positions
    - Daily loss limit
    - Cooldown after losses
    """

    def __init__(
        self,
        max_positions_per_asset: int = 1,
        max_total_positions: int = 3,
        max_daily_loss: float = 50.0,
        loss_cooldown_secs: float = 60.0,
        position_size_pct: float = 0.03,
    ):
        self.max_positions_per_asset = max_positions_per_asset
        self.max_total_positions = max_total_positions
        self.max_daily_loss = max_daily_loss
        self.loss_cooldown_secs = loss_cooldown_secs
        self.position_size_pct = position_size_pct

        self._lock = threading.Lock()

        # Track open positions by asset
        self._open_positions: Dict[str, List[str]] = {}  # asset -> [position_id, ...]

        # Daily P&L tracking
        self._daily_pnl: float = 0.0
        self._daily_reset_date: str = ""

        # Cooldown tracking: asset -> timestamp when cooldown expires
        self._cooldowns: Dict[str, float] = {}

        # Trading halted flag
        self._halted: bool = False

    def can_trade(self, asset: str) -> Tuple[bool, str]:
        """Check if a new trade is allowed for the given asset."""
        with self._lock:
            self._check_daily_reset()

            if self._halted:
                return False, "Trading halted: daily loss limit reached"

            if self._daily_pnl <= -self.max_daily_loss:
                self._halted = True
                return False, f"Daily loss limit reached: ${self._daily_pnl:.2f}"

            # Check cooldown
            cooldown_until = self._cooldowns.get(asset, 0)
            if time.time() < cooldown_until:
                remaining = cooldown_until - time.time()
                return False, f"Cooldown active for {asset}: {remaining:.0f}s remaining"

            # Check per-asset position limit
            asset_positions = self._open_positions.get(asset, [])
            if len(asset_positions) >= self.max_positions_per_asset:
                return False, f"Max positions for {asset}: {len(asset_positions)}"

            # Check total position limit
            total = sum(len(v) for v in self._open_positions.values())
            if total >= self.max_total_positions:
                return False, f"Max total positions reached: {total}"

            return True, "OK"

    def register_position(self, asset: str, position_id: str) -> None:
        """Register a new open position."""
        with self._lock:
            if asset not in self._open_positions:
                self._open_positions[asset] = []
            self._open_positions[asset].append(position_id)

    def close_position(self, asset: str, position_id: str, pnl: float) -> None:
        """Record a closed position and its P&L."""
        with self._lock:
            self._check_daily_reset()
            self._daily_pnl += pnl

            # Remove from open positions
            if asset in self._open_positions:
                try:
                    self._open_positions[asset].remove(position_id)
                except ValueError:
                    pass

            # Set cooldown if loss
            if pnl < 0:
                self._cooldowns[asset] = time.time() + self.loss_cooldown_secs
                logger.info(
                    "ScalpRisk: loss on %s (%.2f), cooldown %.0fs",
                    asset, pnl, self.loss_cooldown_secs,
                )

            # Check daily limit
            if self._daily_pnl <= -self.max_daily_loss:
                self._halted = True
                logger.warning(
                    "ScalpRisk: DAILY LOSS LIMIT HIT — P&L $%.2f, halting trading",
                    self._daily_pnl,
                )

    def get_open_position_ids(self, asset: str) -> List[str]:
        """Return list of open position IDs for an asset."""
        with self._lock:
            return list(self._open_positions.get(asset, []))

    def get_total_open(self) -> int:
        """Return total number of open positions."""
        with self._lock:
            return sum(len(v) for v in self._open_positions.values())

    def get_daily_pnl(self) -> float:
        """Return today's cumulative P&L."""
        with self._lock:
            self._check_daily_reset()
            return self._daily_pnl

    def is_halted(self) -> bool:
        """Check if trading is halted."""
        with self._lock:
            self._check_daily_reset()
            return self._halted

    def calculate_position_size(self, balance: float) -> float:
        """Calculate position size in USD."""
        return round(balance * self.position_size_pct, 2)

    def _check_daily_reset(self) -> None:
        """Reset daily P&L at midnight UTC."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._daily_reset_date:
            if self._daily_reset_date:
                logger.info(
                    "ScalpRisk: daily reset — yesterday P&L was $%.2f",
                    self._daily_pnl,
                )
            self._daily_reset_date = today
            self._daily_pnl = 0.0
            self._halted = False
            self._cooldowns.clear()


# ---------------------------------------------------------------------------
# ScalpStrategy
# ---------------------------------------------------------------------------

class ScalpStrategy:
    """
    Core scalp detection engine.

    Tracks price velocity via ring buffers and generates entry/exit signals
    based on spread, momentum, and Polymarket token pricing.
    """

    # Ring buffer size: 30 entries at 2s intervals = 60s of history
    RING_BUFFER_SIZE = 30

    # Default spread thresholds per asset
    DEFAULT_THRESHOLDS = {
        "BTC": 18.0,
        "ETH": 0.60,
        "SOL": 0.04,
    }

    def __init__(
        self,
        # Entry thresholds
        btc_min_spread: float = 18.0,
        eth_min_spread: float = 0.60,
        sol_min_spread: float = 0.04,
        min_velocity_pct: float = 0.0003,  # 0.03% stored as fraction
        min_secs_remaining: float = 60.0,
        poly_prob_low: float = 0.25,
        poly_prob_high: float = 0.70,
        # Exit thresholds
        take_profit_pct: float = 0.30,
        stop_loss_pct: float = 0.20,
        max_hold_seconds: float = 90.0,
        emergency_exit_secs: float = 15.0,
        # Risk
        max_positions_per_asset: int = 1,
        max_total_positions: int = 3,
        max_daily_loss: float = 50.0,
        loss_cooldown_secs: float = 60.0,
        position_size_pct: float = 0.03,
    ):
        # Entry thresholds
        self._spread_thresholds = {
            "BTC": btc_min_spread,
            "ETH": eth_min_spread,
            "SOL": sol_min_spread,
        }
        self._min_velocity_pct = min_velocity_pct
        self._min_secs_remaining = min_secs_remaining
        self._poly_prob_low = poly_prob_low
        self._poly_prob_high = poly_prob_high

        # Exit thresholds
        self._take_profit_pct = take_profit_pct
        self._stop_loss_pct = stop_loss_pct
        self._max_hold_seconds = max_hold_seconds
        self._emergency_exit_secs = emergency_exit_secs

        # Ring buffers: asset -> deque of _PriceEntry
        self._lock = threading.Lock()
        self._price_buffers: Dict[str, deque] = {}

        # Risk manager
        self.risk_manager = ScalpRiskManager(
            max_positions_per_asset=max_positions_per_asset,
            max_total_positions=max_total_positions,
            max_daily_loss=max_daily_loss,
            loss_cooldown_secs=loss_cooldown_secs,
            position_size_pct=position_size_pct,
        )

    # ------------------------------------------------------------------
    # Price tracking
    # ------------------------------------------------------------------

    def record_price(self, asset: str, price: float) -> None:
        """
        Record an exchange price observation for velocity calculations.
        Call this every loop iteration (every ~2 seconds).
        """
        asset = asset.upper()
        with self._lock:
            if asset not in self._price_buffers:
                self._price_buffers[asset] = deque(maxlen=self.RING_BUFFER_SIZE)
            self._price_buffers[asset].append(_PriceEntry(time.time(), price))

    def _get_velocity(self, asset: str, lookback_seconds: float = 10.0) -> float:
        """
        Calculate price velocity: percentage change over the lookback window.

        Returns:
            Fractional change (e.g. 0.0005 = 0.05%).
        """
        with self._lock:
            buf = self._price_buffers.get(asset)
            if not buf or len(buf) < 2:
                return 0.0

            now = time.time()
            cutoff = now - lookback_seconds

            # Find the oldest point within our lookback window
            old_price = None
            for entry in buf:
                if entry.timestamp >= cutoff:
                    old_price = entry.price
                    break

            if old_price is None or old_price <= 0:
                return 0.0

            current_price = buf[-1].price
            return (current_price - old_price) / old_price

    def _get_velocity_range(self, asset: str) -> float:
        """
        Get the best velocity over 5-15 second windows.
        Uses absolute value — we care about magnitude, not direction.
        """
        best = 0.0
        for lookback in (5.0, 8.0, 10.0, 15.0):
            v = abs(self._get_velocity(asset, lookback))
            if v > best:
                best = v
        return best

    # ------------------------------------------------------------------
    # Late-window scalp config
    # ------------------------------------------------------------------
    LATE_WINDOW_SECS = 150.0        # Enter in last 2.5 minutes
    LATE_MIN_CONFIRMATION = 0.80    # Token must be 0.80+ (strong direction)
    LATE_MAX_ENTRY = 0.88           # Was 0.92 — at 0.92 the +12% target exceeds $1
    LATE_TAKE_PROFIT = 99.0         # Disabled — hold to expiry for max payout
    LATE_STOP_LOSS = 99.0           # Disabled — stop loss was chopping winners (37% WR with it)
    LATE_MAX_HOLD = 9999.0          # Disabled — let window expiry handle exit

    # ------------------------------------------------------------------
    # Entry signal
    # ------------------------------------------------------------------

    def check_entry(
        self,
        asset: str,
        exchange_price: float,
        price_to_beat: float,
        poly_mid_yes: float,
        secs_remaining: float,
        market: dict,
    ) -> Optional[ScalpSignal]:
        """
        Check whether an entry signal should be generated.

        Args:
            asset:           "BTC", "ETH", or "SOL"
            exchange_price:  Current exchange price (e.g. 67500.00)
            price_to_beat:   5-min window PtB from Polymarket
            poly_mid_yes:    Current YES token midpoint (0-1)
            secs_remaining:  Seconds left in current 5-min window
            market:          Market dict from MarketFinder

        Returns:
            ScalpSignal if entry conditions are met, None otherwise.
        """
        asset = asset.upper()

        # Record price for velocity tracking
        self.record_price(asset, exchange_price)

        # 1. Calculate spread
        spread = exchange_price - price_to_beat
        abs_spread = abs(spread)
        threshold = self._spread_thresholds.get(asset, 18.0)

        if abs_spread < threshold:
            logger.debug(
                "[%s] Spread %.2f < threshold %.2f — no entry",
                asset, abs_spread, threshold,
            )
            return None

        # 2. Velocity check
        velocity = self._get_velocity_range(asset)
        if velocity < self._min_velocity_pct:
            logger.debug(
                "[%s] Velocity %.5f < threshold %.5f — no entry",
                asset, velocity, self._min_velocity_pct,
            )
            return None

        # 3. Time check
        if secs_remaining < self._min_secs_remaining:
            logger.debug(
                "[%s] Only %.0fs remaining < %.0fs min — no entry",
                asset, secs_remaining, self._min_secs_remaining,
            )
            return None

        # 4. Direction and token pricing
        if spread > 0:
            direction = "YES"
            our_token_price = poly_mid_yes
            token_id = market.get("token_id_yes", "")
        else:
            direction = "NO"
            our_token_price = 1.0 - poly_mid_yes
            token_id = market.get("token_id_no", "")

        if not token_id:
            logger.debug("[%s] No token_id for %s — no entry", asset, direction)
            return None

        # 5. Token price range check (cheap enough for scalp profit)
        if our_token_price < self._poly_prob_low or our_token_price > self._poly_prob_high:
            logger.debug(
                "[%s] Token price %.3f outside [%.2f, %.2f] — no entry",
                asset, our_token_price, self._poly_prob_low, self._poly_prob_high,
            )
            return None

        # 6. Risk manager check
        can_trade, reason = self.risk_manager.can_trade(asset)
        if not can_trade:
            logger.debug("[%s] Risk blocked: %s", asset, reason)
            return None

        # Compute confidence: based on spread magnitude and velocity
        spread_ratio = min(abs_spread / threshold, 3.0) / 3.0  # 0-1
        vel_ratio = min(velocity / (self._min_velocity_pct * 5), 1.0)  # 0-1
        confidence = 0.3 * spread_ratio + 0.4 * vel_ratio + 0.3 * min(secs_remaining / 180.0, 1.0)

        # Signed velocity for reporting
        signed_velocity = self._get_velocity(asset, 10.0)

        reasoning = (
            f"Spread {spread:+.2f} exceeds {threshold:.2f} threshold, "
            f"velocity {signed_velocity*100:+.4f}%/10s, "
            f"token at {our_token_price:.3f}, "
            f"{secs_remaining:.0f}s remaining"
        )

        logger.info(
            "[%s] SCALP ENTRY SIGNAL: %s | spread=%+.2f vel=%+.5f token=%.3f conf=%.2f",
            asset, direction, spread, signed_velocity, our_token_price, confidence,
        )

        return ScalpSignal(
            direction=direction,
            asset=asset,
            entry_price=our_token_price,
            token_id_to_buy=token_id,
            spread=spread,
            velocity=signed_velocity,
            confidence=confidence,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Late-window entry signal
    # ------------------------------------------------------------------

    def check_late_entry(
        self,
        asset: str,
        exchange_price: float,
        price_to_beat: float,
        poly_mid_yes: float,
        secs_remaining: float,
        market: dict,
    ) -> Optional[ScalpSignal]:
        """
        Late-window scalp: enter when direction is already confirmed
        (one side trading at 0.80+) with 2-3 minutes left.

        Lower risk, lower reward (10-15% target) but higher win rate
        because direction is already established.
        """
        asset = asset.upper()

        # Only in the late window
        if secs_remaining > self.LATE_WINDOW_SECS:
            return None

        # Need at least 30s to execute (entry + exit)
        if secs_remaining < 30:
            return None

        # Risk check
        can_trade, reason = self.risk_manager.can_trade(asset)
        if not can_trade:
            return None

        # Determine which side is confirmed
        # YES side strong = poly_mid_yes >= 0.80
        # NO side strong = (1 - poly_mid_yes) >= 0.80, i.e. poly_mid_yes <= 0.20
        yes_strong = poly_mid_yes >= self.LATE_MIN_CONFIRMATION
        no_strong = (1.0 - poly_mid_yes) >= self.LATE_MIN_CONFIRMATION

        if not yes_strong and not no_strong:
            logger.debug(
                "[%s] Late scalp: no confirmed side (YES=%.2f, NO=%.2f)",
                asset, poly_mid_yes, 1.0 - poly_mid_yes,
            )
            return None

        if yes_strong:
            direction = "YES"
            our_token_price = poly_mid_yes
            token_id = market.get("token_id_yes", "")
        else:
            direction = "NO"
            our_token_price = 1.0 - poly_mid_yes
            token_id = market.get("token_id_no", "")

        if not token_id:
            return None

        # Don't buy too expensive — need room for profit
        if our_token_price > self.LATE_MAX_ENTRY:
            logger.debug(
                "[%s] Late scalp: token %.3f > max entry %.2f — too expensive",
                asset, our_token_price, self.LATE_MAX_ENTRY,
            )
            return None

        # Verify exchange price STRONGLY agrees with direction
        # Require at least half the normal spread threshold for confirmation
        spread = exchange_price - price_to_beat
        min_confirm = self._spread_thresholds.get(asset, 18.0) * 0.5  # half of normal threshold
        if direction == "YES" and spread < min_confirm:
            logger.debug(
                "[%s] Late scalp: YES but spread %.2f < confirm %.2f — skip",
                asset, spread, min_confirm,
            )
            return None
        if direction == "NO" and spread > -min_confirm:
            logger.debug(
                "[%s] Late scalp: NO but spread %.2f > -%.2f — skip",
                asset, spread, min_confirm,
            )
            return None

        # Confidence: based on how strong the confirmation is + time pressure
        strength = (our_token_price - self.LATE_MIN_CONFIRMATION) / (1.0 - self.LATE_MIN_CONFIRMATION)
        time_pressure = max(0, min(1, (self.LATE_WINDOW_SECS - secs_remaining) / self.LATE_WINDOW_SECS))
        confidence = 0.5 * min(strength, 1.0) + 0.3 * time_pressure + 0.2

        signed_velocity = self._get_velocity(asset, 10.0)

        reasoning = (
            f"LATE SCALP: {direction} confirmed at {our_token_price:.2f}, "
            f"spread {spread:+.2f}, {secs_remaining:.0f}s left, "
            f"target +{self.LATE_TAKE_PROFIT*100:.0f}%"
        )

        logger.info(
            "[%s] LATE SCALP ENTRY: %s token=%.3f spread=%+.2f %.0fs left conf=%.2f",
            asset, direction, our_token_price, spread, secs_remaining, confidence,
        )

        return ScalpSignal(
            direction=direction,
            asset=asset,
            entry_price=our_token_price,
            token_id_to_buy=token_id,
            spread=spread,
            velocity=signed_velocity,
            confidence=confidence,
            reasoning=reasoning,
            mode="late",
        )

    # ------------------------------------------------------------------
    # Exit signal
    # ------------------------------------------------------------------

    def check_exit(
        self,
        position: ScalpPosition,
        current_poly_mid_yes: float,
        secs_remaining: float,
    ) -> Optional[ExitSignal]:
        """
        Check whether a position should be exited.

        Args:
            position:              The open ScalpPosition.
            current_poly_mid_yes:  Current YES token midpoint.
            secs_remaining:        Seconds left in current window.

        Returns:
            ExitSignal if exit conditions met, None otherwise.
        """
        # Current token price for our side
        if position.direction == "YES":
            current_price = current_poly_mid_yes
        else:
            current_price = 1.0 - current_poly_mid_yes

        # Guard against zero entry
        if position.entry_price <= 0:
            return ExitSignal(
                reason="stop_loss",
                exit_price=current_price,
                pnl_pct=-1.0,
            )

        pnl_pct = (current_price - position.entry_price) / position.entry_price
        hold_time = time.time() - position.entry_time

        # Select exit params based on mode
        if position.mode == "late":
            tp_pct = self.LATE_TAKE_PROFIT
            sl_pct = self.LATE_STOP_LOSS
            max_hold = self.LATE_MAX_HOLD
        else:
            tp_pct = self._take_profit_pct
            sl_pct = self._stop_loss_pct
            max_hold = self._max_hold_seconds

        # 1. Emergency exit: window ending
        # Late mode: hold to expiry (don't sell early — collect full $1 payout)
        # Only emergency exit early-mode positions
        if secs_remaining < self._emergency_exit_secs and position.mode != "late":
            logger.info(
                "[%s] EMERGENCY EXIT [%s]: window ending in %.0fs, pnl=%.1f%%",
                position.asset, position.mode, secs_remaining, pnl_pct * 100,
            )
            return ExitSignal(
                reason="window_ending",
                exit_price=current_price,
                pnl_pct=pnl_pct,
            )

        # 2. Take profit
        if pnl_pct >= tp_pct:
            logger.info(
                "[%s] TAKE PROFIT [%s]: pnl=%.1f%% >= %.1f%%",
                position.asset, position.mode, pnl_pct * 100, tp_pct * 100,
            )
            return ExitSignal(
                reason="take_profit",
                exit_price=current_price,
                pnl_pct=pnl_pct,
            )

        # 3. Stop loss
        if pnl_pct <= -sl_pct:
            logger.info(
                "[%s] STOP LOSS [%s]: pnl=%.1f%% <= -%.1f%%",
                position.asset, position.mode, pnl_pct * 100, sl_pct * 100,
            )
            return ExitSignal(
                reason="stop_loss",
                exit_price=current_price,
                pnl_pct=pnl_pct,
            )

        # 4. Max hold time
        if hold_time >= max_hold:
            logger.info(
                "[%s] MAX HOLD: held %.0fs >= %.0fs, pnl_pct=%.1f%%",
                position.asset, hold_time, self._max_hold_seconds, pnl_pct * 100,
            )
            return ExitSignal(
                reason="max_hold",
                exit_price=current_price,
                pnl_pct=pnl_pct,
            )

        return None
