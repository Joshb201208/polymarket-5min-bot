"""
risk_manager.py - Position sizing, loss limits, and circuit breakers.

The risk manager is the gatekeeper: no trade may be placed without
calling can_trade() first. It tracks:

  - Daily P&L and loss limits
  - Max drawdown from peak balance
  - Open position count cap
  - Consecutive loss circuit breaker
  - Half-Kelly position sizing

All state is thread-safe via a single lock.
"""

import time
import math
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import RiskConfig
from utils import format_usd, format_pct, safe_divide, clamp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trade Record
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    """Immutable record of a completed trade outcome."""
    trade_id: str
    timestamp: float
    asset: str
    direction: str          # "YES" or "NO"
    strategy: str
    size_usd: float
    entry_price: float
    exit_price: float
    pnl: float              # net profit/loss in USDC
    fees_paid: float
    market_slug: str
    won: bool


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Central risk management controller.

    Usage:
        rm = RiskManager(initial_balance=500.0, config=risk_config)
        allowed, reason = rm.can_trade()
        if allowed:
            size = rm.calculate_position_size(edge=0.05, confidence=0.70)
            # … place trade …
            rm.record_trade(trade_result)
    """

    def __init__(
        self,
        initial_balance: float,
        config: RiskConfig,
    ):
        self._config = config
        self._lock = threading.Lock()

        # Balance tracking
        self.starting_balance: float = initial_balance
        self.current_balance: float = initial_balance
        self.peak_balance: float = initial_balance

        # Daily tracking (resets at midnight UTC)
        self._day_start_balance: float = initial_balance
        self._day_start_ts: float = self._today_midnight()

        # Trade history
        self._trades: List[TradeRecord] = []

        # Open positions: slug → size_usd
        self._open_positions: Dict[str, float] = {}

        # Consecutive loss counter for circuit breaker
        self._consecutive_losses: int = 0
        self._circuit_breaker_until: float = 0.0  # epoch seconds

        # Emergency stop flag
        self._emergency_stopped: bool = False

        logger.info(
            "RiskManager initialised: balance=%s, limits=[daily=%.0f%%, drawdown=%.0f%%, pos=%.0f%%]",
            format_usd(initial_balance),
            config.daily_loss_limit_pct * 100,
            config.max_drawdown_pct * 100,
            config.max_position_pct * 100,
        )

    # ------------------------------------------------------------------
    # Core Gate: can_trade()
    # ------------------------------------------------------------------

    def can_trade(self) -> Tuple[bool, str]:
        """
        Check whether a new trade is currently allowed.

        Returns:
            (True, "") if trading is allowed.
            (False, reason) if blocked.
        """
        with self._lock:
            # Emergency stop
            if self._emergency_stopped:
                return False, "Emergency stop is active"

            # Circuit breaker
            if time.time() < self._circuit_breaker_until:
                remaining = self._circuit_breaker_until - time.time()
                return False, f"Circuit breaker active — {remaining:.0f}s remaining"

            # Reset daily stats if new day
            self._maybe_reset_daily_stats()

            # Daily loss check
            daily_pnl = self.current_balance - self._day_start_balance
            daily_loss_pct = -daily_pnl / self._day_start_balance if daily_pnl < 0 else 0.0
            if daily_loss_pct >= self._config.daily_loss_limit_pct:
                return (
                    False,
                    f"Daily loss limit hit: {format_pct(daily_loss_pct)} ≥ "
                    f"{format_pct(self._config.daily_loss_limit_pct)}",
                )

            # Max drawdown check
            drawdown = (self.peak_balance - self.current_balance) / self.peak_balance
            if drawdown >= self._config.max_drawdown_pct:
                return (
                    False,
                    f"Max drawdown hit: {format_pct(drawdown)} ≥ "
                    f"{format_pct(self._config.max_drawdown_pct)}",
                )

            # Minimum balance check
            if self.current_balance < self._config.min_position_usd * 2:
                return False, f"Balance too low: {format_usd(self.current_balance)}"

            # Concurrent positions check
            open_count = len(self._open_positions)
            if open_count >= self._config.max_concurrent_positions:
                return (
                    False,
                    f"Max concurrent positions reached ({open_count}/{self._config.max_concurrent_positions})",
                )

            return True, ""

    # ------------------------------------------------------------------
    # Position Sizing
    # ------------------------------------------------------------------

    def calculate_position_size(
        self,
        edge: float,
        confidence: float,
        balance: Optional[float] = None,
    ) -> float:
        """
        Calculate position size using fractional Kelly criterion.

        Kelly fraction:
            f* = (edge × confidence) / (1 − edge)
        Actual fraction:
            f = f* × kelly_fraction   (half-Kelly by default)

        Size is capped at max_position_pct of balance and must be ≥ min_position_usd.

        Args:
            edge:       Expected edge (net of fees).
            confidence: Signal confidence [0, 1].
            balance:    Balance to use (defaults to current_balance).

        Returns:
            Position size in USD.
        """
        with self._lock:
            bal = balance if balance is not None else self.current_balance

        if bal <= 0 or edge <= 0:
            return 0.0

        # Cap edge to avoid absurd Kelly fractions
        edge = clamp(edge, 0.001, 0.30)
        confidence = clamp(confidence, 0.1, 1.0)

        # Kelly formula
        numerator = edge * confidence
        denominator = 1.0 - edge
        if denominator <= 0:
            return self._config.min_position_usd

        kelly_f = (numerator / denominator) * self._config.kelly_fraction

        # Apply position cap
        max_size = bal * self._config.max_position_pct
        size = min(kelly_f * bal, max_size)

        # Apply minimum
        size = max(size, self._config.min_position_usd)

        # Never exceed the cap regardless
        size = min(size, max_size)

        # Never risk more than 10% of remaining daily budget
        daily_budget = self._daily_budget_remaining()
        size = min(size, daily_budget)

        size = max(size, 0.0)
        size = round(size, 2)

        logger.debug(
            "Position size: edge=%.4f conf=%.2f kelly_f=%.4f max=%.2f → $%.2f",
            edge, confidence, kelly_f, max_size, size,
        )

        return size

    def _daily_budget_remaining(self) -> float:
        """Return remaining daily budget before daily_loss_limit is hit."""
        max_daily_loss = self._day_start_balance * self._config.daily_loss_limit_pct
        current_daily_loss = max(0.0, self._day_start_balance - self.current_balance)
        return max(0.0, max_daily_loss - current_daily_loss)

    # ------------------------------------------------------------------
    # Trade Recording
    # ------------------------------------------------------------------

    def record_trade(self, trade: TradeRecord) -> None:
        """
        Record a completed trade and update all risk state.

        Call this after every trade resolves (win or loss).
        """
        with self._lock:
            self._trades.append(trade)

            # Update balance
            self.current_balance += trade.pnl - trade.fees_paid
            if self.current_balance > self.peak_balance:
                self.peak_balance = self.current_balance

            # Update consecutive loss counter
            if trade.won:
                self._consecutive_losses = 0
            else:
                self._consecutive_losses += 1
                if self._consecutive_losses >= self._config.circuit_breaker_losses:
                    pause = self._config.circuit_breaker_pause_minutes * 60
                    self._circuit_breaker_until = time.time() + pause
                    logger.warning(
                        "Circuit breaker triggered: %d consecutive losses. "
                        "Pausing for %d minutes.",
                        self._consecutive_losses,
                        self._config.circuit_breaker_pause_minutes,
                    )

            # Remove from open positions if present
            self._open_positions.pop(trade.market_slug, None)

            logger.info(
                "Trade recorded: %s %s %s pnl=%s balance=%s",
                trade.asset,
                trade.direction,
                "WIN" if trade.won else "LOSS",
                format_usd(trade.pnl),
                format_usd(self.current_balance),
            )

    def open_position(self, market_slug: str, size_usd: float) -> None:
        """Register that we have opened a position (before it resolves)."""
        with self._lock:
            self._open_positions[market_slug] = size_usd
            logger.debug("Open positions: %d", len(self._open_positions))

    def close_position(self, market_slug: str) -> None:
        """Remove a position from the open list (after resolution)."""
        with self._lock:
            self._open_positions.pop(market_slug, None)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Return a snapshot of current risk / performance statistics."""
        with self._lock:
            trades = list(self._trades)
            current_balance = self.current_balance
            peak_balance = self.peak_balance
            starting_balance = self.starting_balance

        total = len(trades)
        if total == 0:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "balance": current_balance,
                "peak_balance": peak_balance,
                "drawdown": 0.0,
                "consecutive_losses": self._consecutive_losses,
                "circuit_breaker_active": time.time() < self._circuit_breaker_until,
                "emergency_stopped": self._emergency_stopped,
            }

        wins = sum(1 for t in trades if t.won)
        win_rate = wins / total
        total_pnl = sum(t.pnl - t.fees_paid for t in trades)
        pnls = [t.pnl - t.fees_paid for t in trades]
        best_trade = max(pnls) if pnls else 0.0
        worst_trade = min(pnls) if pnls else 0.0
        avg_trade = total_pnl / total

        # Sharpe estimate (simplified — using per-trade returns)
        if len(pnls) >= 2:
            import statistics
            std_pnl = statistics.stdev(pnls) or 1e-9
            sharpe = avg_trade / std_pnl * math.sqrt(total)  # crude estimate
        else:
            sharpe = 0.0

        drawdown = (peak_balance - current_balance) / peak_balance if peak_balance > 0 else 0.0
        total_return = (current_balance - starting_balance) / starting_balance

        return {
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_trade_pnl": avg_trade,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "sharpe_estimate": sharpe,
            "balance": current_balance,
            "starting_balance": starting_balance,
            "peak_balance": peak_balance,
            "drawdown": drawdown,
            "total_return": total_return,
            "consecutive_losses": self._consecutive_losses,
            "circuit_breaker_active": time.time() < self._circuit_breaker_until,
            "emergency_stopped": self._emergency_stopped,
            "open_positions": len(self._open_positions),
        }

    def get_daily_pnl(self) -> float:
        """Return today's P&L."""
        with self._lock:
            return self.current_balance - self._day_start_balance

    # ------------------------------------------------------------------
    # Emergency Stop
    # ------------------------------------------------------------------

    def emergency_stop(self, reason: str = "Manual emergency stop") -> None:
        """
        Immediately halt all trading.
        Caller is responsible for cancelling open orders.
        """
        with self._lock:
            self._emergency_stopped = True
        logger.critical("EMERGENCY STOP: %s", reason)

    def reset_emergency_stop(self) -> None:
        """Re-enable trading after an emergency stop (manual action)."""
        with self._lock:
            self._emergency_stopped = False
        logger.warning("Emergency stop RESET — trading re-enabled.")

    # ------------------------------------------------------------------
    # Daily Reset
    # ------------------------------------------------------------------

    def _today_midnight(self) -> float:
        """Return the Unix timestamp of midnight UTC today."""
        now = time.time()
        return now - (now % 86400)

    def _maybe_reset_daily_stats(self) -> None:
        """Reset daily stats if we've crossed midnight UTC (call inside lock)."""
        midnight = self._today_midnight()
        if midnight > self._day_start_ts:
            logger.info(
                "RiskManager: new trading day. Daily P&L was %s",
                format_usd(self.current_balance - self._day_start_balance),
            )
            self._day_start_balance = self.current_balance
            self._day_start_ts = midnight

    # ------------------------------------------------------------------
    # Balance Management
    # ------------------------------------------------------------------

    def update_balance(self, new_balance: float) -> None:
        """
        Sync balance from the exchange (live mode).
        Updates peak balance if new balance is higher.
        """
        with self._lock:
            self.current_balance = new_balance
            if new_balance > self.peak_balance:
                self.peak_balance = new_balance

    def get_balance(self) -> float:
        """Return current balance."""
        with self._lock:
            return self.current_balance
