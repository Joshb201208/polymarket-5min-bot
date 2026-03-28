"""Smart Exit Engine — intelligent, multi-trigger exit decisions.

Replaces the fixed TP/SL logic with dynamic exits based on edge reversal,
trailing stops, liquidity, time-based decay, and regime-aware thresholds.
Runs every scan cycle (~45 min).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from events_agent.config import EventsConfig
from events_agent.models import Position
from nba_agent.utils import parse_utc, utcnow

logger = logging.getLogger("events_agent.smart_exit")


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str
    urgency: str  # "immediate", "next_cycle", "patient"
    method: str  # "market", "limit", "twap"
    trigger_type: str  # "edge_reversal", "smart_tp", "trailing_stop", "smart_sl", "liquidity", "time"
    unrealized_pnl: float
    unrealized_pnl_pct: float
    peak_pnl_pct: float


class SmartExitEngine:
    """Evaluates positions against 6 exit triggers in priority order."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self.config = config or EventsConfig()

    def should_exit(
        self,
        position: Position,
        current_price: float,
        composite_score: float | None = None,
        composite_direction: str | None = None,
        remaining_edge: float | None = None,
        lifecycle_stage: str | None = None,
        regime: str | None = None,
        bid_depth: float | None = None,
    ) -> ExitDecision:
        """Evaluate all exit triggers for a position.

        Args:
            position: The open position to evaluate.
            current_price: Current market price.
            composite_score: Latest composite intelligence score (0-1).
            composite_direction: Latest composite direction ("YES"/"NO"/"NEUTRAL").
            remaining_edge: Estimated remaining edge (0-1).
            lifecycle_stage: Current lifecycle stage ("EARLY","DEVELOPING","MATURE","LATE","TERMINAL").
            regime: Current market regime ("TRENDING","VOLATILE","STALE","CONVERGING").
            bid_depth: Total bid depth in dollars from order book.

        Returns:
            ExitDecision with should_exit=True/False and full reasoning.
        """
        entry = position.entry_price
        if entry <= 0:
            return self._no_exit(position, current_price)

        # Compute P&L metrics
        unrealized_pnl = (current_price - entry) * position.shares
        unrealized_pnl_pct = (current_price - entry) / entry
        peak_pnl_pct = getattr(position, "peak_pnl_pct", None) or 0.0

        # Update peak tracking
        if unrealized_pnl_pct > peak_pnl_pct:
            peak_pnl_pct = unrealized_pnl_pct
            position.peak_pnl_pct = peak_pnl_pct
            position.peak_price = current_price

        # Increment exit check counter
        position.exit_checks = getattr(position, "exit_checks", 0) + 1

        # Update last composite score on position
        if composite_score is not None:
            position.last_composite = composite_score

        # Compute hold_days
        hold_days = self._hold_days(position)
        position.hold_days = hold_days

        # Base values for ExitDecision
        base = {
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 4),
            "peak_pnl_pct": round(peak_pnl_pct, 4),
        }

        # Determine market duration bucket for exit strategy
        duration_days = self._estimate_market_duration(position)

        # --- Priority 1: EDGE REVERSAL (always checked) ---
        decision = self._check_edge_reversal(
            position, composite_direction, unrealized_pnl_pct, base
        )
        if decision:
            return decision

        # SHORT markets (<14d): only exit on edge reversal or hard stop
        # Hold to resolution is 5x better EV than smart exit
        if duration_days is not None and duration_days < 14:
            decision = self._check_smart_sl(
                unrealized_pnl_pct, remaining_edge, composite_score, lifecycle_stage, base,
                hard_stop_only=True,
            )
            if decision:
                return decision
            logger.debug("Short market (%.0fd) — holding to resolution", duration_days)
            return self._no_exit(position, current_price)

        # LONG markets (>60d): use tighter exit thresholds
        if duration_days is not None and duration_days > 60:
            decision = self._check_smart_tp(
                unrealized_pnl_pct, remaining_edge, lifecycle_stage, regime, base,
                long_market=True,
            )
            if decision:
                return decision

            decision = self._check_trailing_stop(
                unrealized_pnl_pct, peak_pnl_pct, base,
                drawdown_override=0.30,
            )
            if decision:
                return decision

            decision = self._check_smart_sl(
                unrealized_pnl_pct, remaining_edge, composite_score, lifecycle_stage, base
            )
            if decision:
                return decision

            decision = self._check_liquidity_exit(bid_depth, base)
            if decision:
                return decision

            decision = self._check_time_exit(
                hold_days, unrealized_pnl_pct, entry, current_price, base,
                max_days_override=20,
            )
            if decision:
                return decision

            return self._no_exit(position, current_price)

        # MEDIUM markets (14-60d): standard rules
        # --- Priority 2: SMART TAKE PROFIT ---
        decision = self._check_smart_tp(
            unrealized_pnl_pct, remaining_edge, lifecycle_stage, regime, base
        )
        if decision:
            return decision

        # --- Priority 3: TRAILING STOP ---
        decision = self._check_trailing_stop(unrealized_pnl_pct, peak_pnl_pct, base)
        if decision:
            return decision

        # --- Priority 4: SMART STOP LOSS ---
        decision = self._check_smart_sl(
            unrealized_pnl_pct, remaining_edge, composite_score, lifecycle_stage, base
        )
        if decision:
            return decision

        # --- Priority 5: LIQUIDITY EXIT ---
        decision = self._check_liquidity_exit(bid_depth, base)
        if decision:
            return decision

        # --- Priority 6: TIME-BASED EXIT ---
        decision = self._check_time_exit(hold_days, unrealized_pnl_pct, entry, current_price, base)
        if decision:
            return decision

        return self._no_exit(position, current_price)

    # ------------------------------------------------------------------
    # Trigger implementations
    # ------------------------------------------------------------------

    def _check_edge_reversal(
        self,
        position: Position,
        composite_direction: str | None,
        unrealized_pnl_pct: float,
        base: dict,
    ) -> ExitDecision | None:
        """Composite score now points opposite to position direction → exit."""
        if not composite_direction or composite_direction == "NEUTRAL":
            return None

        # Determine position direction from the side string
        pos_direction = "YES" if "YES" in position.side.upper() else "NO"

        if composite_direction != pos_direction:
            reason = (
                f"Edge reversal: composite now favors {composite_direction}, "
                f"position is {pos_direction} (P&L {unrealized_pnl_pct * 100:+.1f}%)"
            )
            logger.info("EXIT TRIGGER [edge_reversal]: %s | %s", position.market_question[:50], reason)
            return ExitDecision(
                should_exit=True,
                reason=reason,
                urgency="immediate",
                method="market",
                trigger_type="edge_reversal",
                **base,
            )
        return None

    def _check_smart_tp(
        self,
        pnl_pct: float,
        remaining_edge: float | None,
        lifecycle_stage: str | None,
        regime: str | None,
        base: dict,
        long_market: bool = False,
    ) -> ExitDecision | None:
        """Dynamic take-profit with multiple tiers.

        For long markets (>60d), thresholds are tighter:
        - Tier 4 at +10% instead of +15%
        - Tier 2 at +20% instead of +25%
        """
        # Tier 1: >40% → always take profit
        if pnl_pct > 0.40:
            reason = f"Smart TP: unrealized P&L {pnl_pct * 100:.1f}% > 40% hard cap"
            logger.info("EXIT TRIGGER [smart_tp]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="immediate",
                method="twap", trigger_type="smart_tp", **base,
            )

        # Tier 2: >25% AND late/terminal lifecycle (>20% for long markets)
        tp2_threshold = 0.20 if long_market else 0.25
        if pnl_pct > tp2_threshold and lifecycle_stage in ("LATE", "TERMINAL"):
            reason = (
                f"Smart TP: P&L {pnl_pct * 100:.1f}% > {tp2_threshold * 100:.0f}% in {lifecycle_stage} stage"
            )
            logger.info("EXIT TRIGGER [smart_tp]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="immediate",
                method="twap", trigger_type="smart_tp", **base,
            )

        # Tier 3: >20% AND volatile regime
        if pnl_pct > 0.20 and regime == "VOLATILE":
            reason = f"Smart TP: P&L {pnl_pct * 100:.1f}% > 20% in VOLATILE regime"
            logger.info("EXIT TRIGGER [smart_tp]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="next_cycle",
                method="limit", trigger_type="smart_tp", **base,
            )

        # Tier 4: >15% AND remaining edge < 3% (>10% for long markets)
        tp4_threshold = 0.10 if long_market else 0.15
        if pnl_pct > tp4_threshold and remaining_edge is not None and remaining_edge < 0.03:
            reason = (
                f"Smart TP: P&L {pnl_pct * 100:.1f}% > 15% with "
                f"remaining edge {remaining_edge * 100:.1f}% < 3%"
            )
            logger.info("EXIT TRIGGER [smart_tp]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="next_cycle",
                method="limit", trigger_type="smart_tp", **base,
            )

        return None

    def _check_trailing_stop(
        self, pnl_pct: float, peak_pnl_pct: float, base: dict,
        drawdown_override: float | None = None,
    ) -> ExitDecision | None:
        """Exit if unrealized P&L drops too far from peak (default 40%, 30% for long markets)."""
        if peak_pnl_pct <= 0:
            return None

        drawdown = drawdown_override if drawdown_override is not None else self.config.TRAILING_STOP_DRAWDOWN
        drawdown_from_peak = (peak_pnl_pct - pnl_pct) / peak_pnl_pct if peak_pnl_pct > 0 else 0

        if drawdown_from_peak > drawdown:
            reason = (
                f"Trailing stop: P&L dropped {drawdown_from_peak * 100:.0f}% from peak "
                f"({peak_pnl_pct * 100:.1f}% → {pnl_pct * 100:.1f}%), "
                f"threshold {drawdown * 100:.0f}%"
            )
            logger.info("EXIT TRIGGER [trailing_stop]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="immediate",
                method="market", trigger_type="trailing_stop", **base,
            )
        return None

    def _check_smart_sl(
        self,
        pnl_pct: float,
        remaining_edge: float | None,
        composite_score: float | None,
        lifecycle_stage: str | None,
        base: dict,
        hard_stop_only: bool = False,
    ) -> ExitDecision | None:
        """Dynamic stop loss with multiple tiers.

        For short markets, hard_stop_only=True skips the softer tiers
        and only checks the -30% hard stop.
        """
        hard_stop = self.config.HARD_STOP_LOSS

        # Hard stop: loss > -30% (configurable)
        if pnl_pct <= -hard_stop:
            reason = f"Hard stop loss: P&L {pnl_pct * 100:.1f}% breached -{hard_stop * 100:.0f}%"
            logger.info("EXIT TRIGGER [smart_sl]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="immediate",
                method="market", trigger_type="smart_sl", **base,
            )

        if hard_stop_only:
            return None

        # Early lifecycle losers: cut faster at -10%
        if lifecycle_stage == "EARLY" and pnl_pct <= -0.10:
            reason = f"Smart SL: early-stage loser at {pnl_pct * 100:.1f}%, cutting fast"
            logger.info("EXIT TRIGGER [smart_sl]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="immediate",
                method="market", trigger_type="smart_sl", **base,
            )

        # Loss > -20% with no supporting signals
        if pnl_pct <= -0.20 and (composite_score is None or composite_score < 0.3):
            reason = (
                f"Smart SL: P&L {pnl_pct * 100:.1f}% with weak/no signals "
                f"(composite={composite_score or 0:.2f})"
            )
            logger.info("EXIT TRIGGER [smart_sl]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="immediate",
                method="market", trigger_type="smart_sl", **base,
            )

        # Loss > -15% with low remaining edge
        if pnl_pct <= -0.15 and remaining_edge is not None and remaining_edge < 0.02:
            reason = (
                f"Smart SL: P&L {pnl_pct * 100:.1f}% with remaining edge "
                f"{remaining_edge * 100:.1f}% < 2%"
            )
            logger.info("EXIT TRIGGER [smart_sl]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="next_cycle",
                method="limit", trigger_type="smart_sl", **base,
            )

        return None

    def _check_liquidity_exit(
        self, bid_depth: float | None, base: dict,
    ) -> ExitDecision | None:
        """Exit if market liquidity drops below minimum threshold."""
        if bid_depth is None:
            return None

        min_liq = self.config.MIN_EXIT_LIQUIDITY
        if bid_depth < min_liq:
            reason = f"Liquidity exit: bid depth ${bid_depth:,.0f} < ${min_liq:,.0f} threshold"
            logger.info("EXIT TRIGGER [liquidity]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="immediate",
                method="market", trigger_type="liquidity", **base,
            )
        return None

    def _check_time_exit(
        self,
        hold_days: float,
        pnl_pct: float,
        entry_price: float,
        current_price: float,
        base: dict,
        max_days_override: int | None = None,
    ) -> ExitDecision | None:
        """Exit if held too long with minimal price movement."""
        max_days = max_days_override if max_days_override is not None else self.config.TIME_EXIT_DAYS
        if hold_days <= 0 or hold_days < max_days:
            return None

        price_movement = abs(current_price - entry_price) / entry_price if entry_price > 0 else 0
        if price_movement < 0.05:
            reason = (
                f"Time exit: held {hold_days:.0f} days > {max_days}d with only "
                f"{price_movement * 100:.1f}% price movement"
            )
            logger.info("EXIT TRIGGER [time]: %s", reason)
            return ExitDecision(
                should_exit=True, reason=reason, urgency="patient",
                method="limit", trigger_type="time", **base,
            )
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _estimate_market_duration(self, position: Position) -> float | None:
        """Estimate total market duration in days.

        Uses start_date→end_date if both available, otherwise entry_time→end_date.
        Returns None if end_date is missing.
        """
        end_date_str = getattr(position, "market_end_date", None)
        if not end_date_str:
            return None

        try:
            end_dt = parse_utc(end_date_str)

            start_date_str = getattr(position, "market_start_date", None)
            if start_date_str:
                start_dt = parse_utc(start_date_str)
                return (end_dt - start_dt).total_seconds() / 86400

            # Fallback: entry_time to end_date
            entry_str = position.entry_time
            if entry_str:
                entry_dt = datetime.fromisoformat(entry_str.replace("Z", "+00:00"))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                return (end_dt - entry_dt).total_seconds() / 86400
        except (ValueError, AttributeError, TypeError):
            pass

        return None

    def _hold_days(self, position: Position) -> float:
        """Calculate how many days a position has been held."""
        try:
            entry_str = position.entry_time
            if not entry_str:
                return 0.0
            entry_dt = datetime.fromisoformat(entry_str.replace("Z", "+00:00"))
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            delta = utcnow() - entry_dt
            return delta.total_seconds() / 86400
        except (ValueError, AttributeError):
            return 0.0

    def _no_exit(self, position: Position, current_price: float) -> ExitDecision:
        """Return a hold decision."""
        entry = position.entry_price
        pnl = (current_price - entry) * position.shares if entry > 0 else 0
        pnl_pct = (current_price - entry) / entry if entry > 0 else 0
        peak = getattr(position, "peak_pnl_pct", None) or 0.0
        return ExitDecision(
            should_exit=False,
            reason="Hold — no exit triggers met",
            urgency="patient",
            method="",
            trigger_type="",
            unrealized_pnl=round(pnl, 2),
            unrealized_pnl_pct=round(pnl_pct, 4),
            peak_pnl_pct=round(peak, 4),
        )
