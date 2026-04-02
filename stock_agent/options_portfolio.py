"""Options portfolio tracker — persists open and closed options positions.

Responsibilities:
    - Track all open options positions with live Greeks and P&L
    - Persist state to data/stock_agent/options_portfolio.json (atomic writes)
    - Expiry management: flag positions within 3 DTE, auto-close candidates
    - Aggregate portfolio Greeks (total delta exposure, daily theta income)
    - P&L accounting for both open and closed positions

Auto-close rules (enforced on each update cycle):
    - 50% profit on the position → take the win
    - 3 DTE or fewer remaining → close to avoid pin risk / assignment
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from stock_agent.config import Config
from stock_agent.options_data import OptionsDataFeed
from stock_agent.options_models import (
    OptionSide,
    OptionsGreeks,
    OptionsPortfolioState,
    OptionsPosition,
    OptionsQuote,
    OptionsSignal,
    OptionsStrategy,
)

logger = logging.getLogger(__name__)

# ── Alert thresholds ──────────────────────────────────────────────────

EXPIRY_ALERT_DTE = 3          # Alert when position reaches 3 DTE
PROFIT_TAKE_PCT = 50.0        # Auto-close trigger: % of max profit reached
LOSS_ALERT_PCT = -75.0        # Alert when position is down 75% (near total loss)


class OptionsPortfolio:
    """Manages the lifecycle of all options positions.

    Usage::

        portfolio = OptionsPortfolio(config, data_feed)
        await portfolio.refresh_positions()   # Update prices from market data
        expiry_alerts = portfolio.get_expiry_alerts()
        close_candidates = portfolio.get_auto_close_candidates()
    """

    def __init__(self, config: Config, data_feed: OptionsDataFeed) -> None:
        self.config = config
        self.data = data_feed
        self._state_path: Path = config.DATA_DIR / "options_portfolio.json"
        self.state: OptionsPortfolioState = self._load()

    # ── Persistence ───────────────────────────────────────────────────

    def _load(self) -> OptionsPortfolioState:
        """Load options portfolio state from disk."""
        if self._state_path.exists():
            try:
                raw = self._state_path.read_text()
                return OptionsPortfolioState.model_validate_json(raw)
            except Exception as exc:
                logger.error("Failed to load options portfolio state: %s", exc)
                backup = self._state_path.with_suffix(".json.bak")
                if backup.exists():
                    try:
                        raw = backup.read_text()
                        return OptionsPortfolioState.model_validate_json(raw)
                    except Exception:
                        pass
        logger.info("Starting with fresh options portfolio state")
        return OptionsPortfolioState()

    def _save(self) -> None:
        """Atomically persist the current state to disk."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.state.model_dump_json(indent=2)

        fd, tmp_path = tempfile.mkstemp(
            dir=self._state_path.parent,
            suffix=".tmp",
            prefix="options_portfolio_",
        )
        try:
            os.write(fd, data.encode())
            os.close(fd)

            if self._state_path.exists():
                backup = self._state_path.with_suffix(".json.bak")
                try:
                    backup.write_text(self._state_path.read_text())
                except Exception:
                    pass

            os.replace(tmp_path, self._state_path)
            logger.debug("Options portfolio saved to %s", self._state_path)
        except Exception as exc:
            logger.error("Failed to save options portfolio state: %s", exc)
            try:
                os.close(fd)
            except Exception:
                pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # ── Position management ───────────────────────────────────────────

    def open_position(
        self,
        signal: OptionsSignal,
        filled_price: float,
        contract_id: str = "",
        leg2_contract_id: str = "",
        portfolio_value: float = 0.0,
        regime: str = "",
    ) -> OptionsPosition:
        """Record a newly opened options position.

        Args:
            signal:          The :class:`OptionsSignal` that triggered the trade.
            filled_price:    Actual fill price per share (debit paid or credit received).
            contract_id:     Alpaca internal contract ID for leg 1.
            leg2_contract_id: Alpaca internal contract ID for leg 2 (spreads only).
            portfolio_value: Total portfolio value at time of entry (for exposure calc).
            regime:          Macro regime at entry.

        Returns:
            The newly created :class:`OptionsPosition`.
        """
        position_id = str(uuid.uuid4())

        # Compute entry cost (positive = debit paid, negative = credit received)
        is_short_leg1 = signal.side == OptionSide.SELL
        entry_cost = -filled_price * signal.qty * 100 if is_short_leg1 else filled_price * signal.qty * 100

        position = OptionsPosition(
            position_id=position_id,
            strategy=signal.strategy,
            underlying_symbol=signal.underlying_symbol,
            contract_symbol=signal.contract_symbol or "",
            contract_id=contract_id,
            option_type=signal.option_type,
            side=signal.side,
            strike=signal.target_strike,
            expiration_date=(
                # Parse from contract symbol if possible, else estimate from max DTE
                self._expiry_from_contract_symbol(signal.contract_symbol)
                or (datetime.utcnow().date() + __import__("datetime").timedelta(days=signal.target_expiry_max_dte))
            ),
            qty=signal.qty,
            leg2_contract_symbol=signal.leg2_contract_symbol,
            leg2_contract_id=leg2_contract_id,
            leg2_option_type=signal.leg2_option_type,
            leg2_side=signal.leg2_side,
            leg2_strike=signal.leg2_target_strike,
            entry_price=filled_price,
            current_price=filled_price,
            entry_cost=entry_cost,
            opened_at=datetime.now(timezone.utc),
            max_profit=signal.estimated_max_profit,
            max_loss=signal.estimated_max_loss,
            regime_at_entry=regime or signal.regime,
        )

        self.state.positions.append(position)
        self._recalculate_summary(portfolio_value=portfolio_value)
        self._save()

        logger.info(
            "Opened options position [%s]: %s %s x%d @ $%.2f",
            position_id, signal.strategy.value, signal.contract_symbol, signal.qty, filled_price,
        )
        return position

    def close_position(
        self,
        position_id: str,
        close_price: float,
        close_reason: str = "manual",
        portfolio_value: float = 0.0,
    ) -> Optional[OptionsPosition]:
        """Mark an options position as closed and move it to the closed list.

        Args:
            position_id:     UUID of the position to close.
            close_price:     Fill price per share at close.
            close_reason:    Human-readable reason, e.g. ``"50% profit target"``.
            portfolio_value: Portfolio value at close (for P&L % calc).

        Returns:
            The closed :class:`OptionsPosition`, or ``None`` if not found.
        """
        position = self._find_position(position_id)
        if position is None:
            logger.warning("close_position: position %s not found", position_id)
            return None

        position.is_open = False
        position.closed_at = datetime.now(timezone.utc)
        position.close_reason = close_reason
        position.current_price = close_price

        # Compute realized P&L
        # For long positions: realized = (close - entry) * qty * 100
        # For short positions: realized = (entry - close) * qty * 100
        if position.side == OptionSide.BUY:
            realized = (close_price - position.entry_price) * position.qty * position.multiplier
        else:
            realized = (position.entry_price - close_price) * position.qty * position.multiplier

        position.realized_pnl = realized
        position.unrealized_pnl = 0.0

        self.state.positions = [p for p in self.state.positions if p.position_id != position_id]
        self.state.closed_positions.append(position)
        self.state.total_realized_pnl += realized

        self._recalculate_summary(portfolio_value=portfolio_value)
        self._save()

        logger.info(
            "Closed options position [%s]: %s @ $%.2f, realized P&L: $%.2f (%s)",
            position_id, position.contract_symbol, close_price, realized, close_reason,
        )
        return position

    def update_position_price(
        self,
        position_id: str,
        current_price: float,
        greeks: Optional[OptionsGreeks] = None,
    ) -> None:
        """Update the current market price and greeks for an open position."""
        position = self._find_position(position_id)
        if position is None:
            return

        position.current_price = current_price

        if greeks is not None:
            position.greeks = greeks

        # Recalculate unrealized P&L
        if position.side == OptionSide.BUY:
            position.unrealized_pnl = (current_price - position.entry_price) * position.qty * position.multiplier
        else:
            position.unrealized_pnl = (position.entry_price - current_price) * position.qty * position.multiplier

        # P&L as % of max loss (for auto-close trigger)
        if position.max_loss and position.max_loss > 0:
            position.unrealized_pnl_pct = (position.unrealized_pnl / position.max_loss) * 100
        elif position.entry_cost != 0:
            position.unrealized_pnl_pct = (position.unrealized_pnl / abs(position.entry_cost)) * 100

    # ── Bulk price refresh ─────────────────────────────────────────────

    async def refresh_positions(self, portfolio_value: float = 0.0) -> None:
        """Fetch latest quotes for all open positions and update P&L.

        Also refreshes greeks and recalculates portfolio-level aggregates.

        Args:
            portfolio_value: Current total portfolio value (for exposure %).
        """
        if not self.state.positions:
            return

        # Collect all contract symbols to refresh
        symbols: list[str] = []
        for pos in self.state.positions:
            if pos.contract_symbol:
                symbols.append(pos.contract_symbol)
            if pos.leg2_contract_symbol:
                symbols.append(pos.leg2_contract_symbol)

        if not symbols:
            return

        quotes: dict[str, OptionsQuote] = await self.data.fetch_snapshot(symbols)

        for pos in self.state.positions:
            quote = quotes.get(pos.contract_symbol)
            if quote and quote.fair_value is not None:
                self.update_position_price(
                    position_id=pos.position_id,
                    current_price=quote.fair_value,
                    greeks=quote.greeks,
                )

        self._recalculate_summary(portfolio_value=portfolio_value)
        self.state.last_updated = datetime.now(timezone.utc)
        self._save()

        logger.info(
            "refresh_positions: updated %d positions, total P&L: $%.2f",
            len(self.state.positions), self.state.total_unrealized_pnl,
        )

    # ── Expiry and auto-close management ──────────────────────────────

    def get_expiry_alerts(self) -> list[OptionsPosition]:
        """Return positions at or within EXPIRY_ALERT_DTE days of expiry."""
        return [p for p in self.state.positions if p.dte <= EXPIRY_ALERT_DTE]

    def get_auto_close_candidates(self) -> list[tuple[OptionsPosition, str]]:
        """Return positions that meet auto-close criteria.

        Returns:
            List of (position, reason) tuples where reason describes the
            trigger (e.g. ``"50% profit target reached"``).
        """
        candidates: list[tuple[OptionsPosition, str]] = []
        for pos in self.state.positions:
            reason = pos.close_trigger_reason
            if reason:
                candidates.append((pos, reason))
        return candidates

    def get_near_expiry_positions(self, days: int = 7) -> list[OptionsPosition]:
        """Return positions expiring within the specified number of days."""
        return [p for p in self.state.positions if p.dte <= days]

    # ── Portfolio-level aggregates ────────────────────────────────────

    def _recalculate_summary(self, portfolio_value: float = 0.0) -> None:
        """Recompute aggregate stats across all open positions."""
        total_value = 0.0
        total_unrealized = 0.0
        total_delta = 0.0
        total_theta = 0.0

        for pos in self.state.positions:
            if pos.side == OptionSide.BUY:
                position_value = pos.current_price * pos.qty * pos.multiplier
            else:
                # Short option position has negative market value from our perspective
                position_value = -pos.current_price * pos.qty * pos.multiplier

            total_value += position_value
            total_unrealized += pos.unrealized_pnl

            # Scale greeks by position size
            if pos.greeks.delta is not None:
                sign = 1 if pos.side == OptionSide.BUY else -1
                total_delta += sign * pos.greeks.delta * pos.qty * pos.multiplier

            if pos.greeks.theta is not None:
                sign = 1 if pos.side == OptionSide.BUY else -1
                total_theta += sign * pos.greeks.theta * pos.qty * pos.multiplier

        self.state.total_options_value = total_value
        self.state.total_unrealized_pnl = total_unrealized
        self.state.portfolio_delta = total_delta
        self.state.portfolio_theta = total_theta

        if portfolio_value > 0:
            self.state.total_options_exposure_pct = abs(total_value) / portfolio_value
        else:
            self.state.total_options_exposure_pct = 0.0

    def get_portfolio_greeks_summary(self) -> dict:
        """Return a human-readable summary of portfolio-level greeks."""
        return {
            "total_positions": len(self.state.positions),
            "total_options_value": round(self.state.total_options_value, 2),
            "total_options_exposure_pct": round(self.state.total_options_exposure_pct * 100, 2),
            "total_unrealized_pnl": round(self.state.total_unrealized_pnl, 2),
            "total_realized_pnl": round(self.state.total_realized_pnl, 2),
            "portfolio_delta": round(self.state.portfolio_delta, 4),
            "portfolio_theta_daily": round(self.state.portfolio_theta, 2),
            "last_updated": self.state.last_updated.isoformat() if self.state.last_updated else None,
        }

    def get_strategy_breakdown(self) -> dict[str, int]:
        """Return count of open positions by strategy type."""
        breakdown: dict[str, int] = {}
        for pos in self.state.positions:
            key = pos.strategy.value
            breakdown[key] = breakdown.get(key, 0) + 1
        return breakdown

    # ── Discord alert helpers ──────────────────────────────────────────

    def format_position_alert(self, position: OptionsPosition, alert_type: str = "update") -> dict:
        """Build a Discord embed dict for a position update alert.

        Args:
            position:    The options position to format.
            alert_type:  One of ``"open"``, ``"close"``, ``"expiry_warning"``,
                         ``"profit_target"``.

        Returns:
            Discord embed dict.
        """
        colors = {
            "open": 0x3B82F6,           # Blue
            "close": 0x22C55E,          # Green (profit)
            "close_loss": 0xEF4444,     # Red (loss)
            "expiry_warning": 0xF59E0B, # Amber
            "profit_target": 0x22C55E,  # Green
            "update": 0x6B7280,         # Gray
        }

        if alert_type == "close" and position.realized_pnl < 0:
            color = colors["close_loss"]
        else:
            color = colors.get(alert_type, 0x6B7280)

        fields = [
            {"name": "Strategy", "value": position.strategy.value.replace("_", " ").title(), "inline": True},
            {"name": "Underlying", "value": position.underlying_symbol, "inline": True},
            {"name": "Contract", "value": f"`{position.contract_symbol}`", "inline": True},
            {"name": "Strike", "value": f"${position.strike:.2f}", "inline": True},
            {"name": "Expiry", "value": str(position.expiration_date), "inline": True},
            {"name": "DTE", "value": str(position.dte), "inline": True},
            {"name": "Entry Price", "value": f"${position.entry_price:.2f}/share", "inline": True},
            {"name": "Current Price", "value": f"${position.current_price:.2f}/share", "inline": True},
            {"name": "Unrealized P&L", "value": f"${position.unrealized_pnl:+.2f} ({position.unrealized_pnl_pct:+.1f}%)", "inline": True},
        ]

        if position.greeks.delta is not None:
            fields.append({"name": "Delta", "value": f"{position.greeks.delta:.3f}", "inline": True})
        if position.greeks.theta is not None:
            fields.append({"name": "Theta (daily)", "value": f"${position.greeks.theta:.2f}", "inline": True})
        if position.greeks.implied_volatility is not None:
            fields.append({"name": "IV", "value": f"{position.greeks.implied_volatility * 100:.1f}%", "inline": True})

        if alert_type in ("close",) and position.realized_pnl != 0:
            fields.append({
                "name": "Realized P&L",
                "value": f"${position.realized_pnl:+.2f}",
                "inline": False,
            })

        if position.close_reason:
            fields.append({"name": "Close Reason", "value": position.close_reason, "inline": False})

        title_map = {
            "open": f"Options Position Opened — {position.underlying_symbol}",
            "close": f"Options Position Closed — {position.underlying_symbol}",
            "expiry_warning": f"Expiry Warning — {position.underlying_symbol} ({position.dte} DTE)",
            "profit_target": f"Profit Target Hit — {position.underlying_symbol}",
            "update": f"Options Update — {position.underlying_symbol}",
        }

        return {
            "title": title_map.get(alert_type, f"Options — {position.underlying_symbol}"),
            "color": color,
            "fields": fields,
        }

    def format_portfolio_summary_embed(self) -> dict:
        """Build a Discord embed with the full options portfolio summary."""
        summary = self.get_portfolio_greeks_summary()
        breakdown = self.get_strategy_breakdown()

        positions_text = ""
        for pos in self.state.positions:
            pnl_str = f"${pos.unrealized_pnl:+.2f} ({pos.unrealized_pnl_pct:+.1f}%)"
            positions_text += (
                f"• **{pos.underlying_symbol}** {pos.strategy.value.replace('_', ' ')} "
                f"${pos.strike:.0f} exp {pos.expiration_date} ({pos.dte} DTE) | {pnl_str}\n"
            )

        if not positions_text:
            positions_text = "No open options positions."

        breakdown_text = "\n".join(
            f"• {k.replace('_', ' ').title()}: {v}" for k, v in breakdown.items()
        ) or "None"

        fields = [
            {"name": "Open Positions", "value": str(summary["total_positions"]), "inline": True},
            {"name": "Options Exposure", "value": f"{summary['total_options_exposure_pct']:.1f}%", "inline": True},
            {"name": "Options Value", "value": f"${summary['total_options_value']:+,.2f}", "inline": True},
            {"name": "Unrealized P&L", "value": f"${summary['total_unrealized_pnl']:+,.2f}", "inline": True},
            {"name": "Realized P&L", "value": f"${summary['total_realized_pnl']:+,.2f}", "inline": True},
            {"name": "\u200b", "value": "\u200b", "inline": True},
            {"name": "Portfolio Delta", "value": f"{summary['portfolio_delta']:+.2f}", "inline": True},
            {"name": "Daily Theta", "value": f"${summary['portfolio_theta_daily']:+.2f}/day", "inline": True},
            {"name": "\u200b", "value": "\u200b", "inline": True},
            {"name": "By Strategy", "value": breakdown_text, "inline": False},
            {"name": "Positions", "value": positions_text[:1024] or "None", "inline": False},
        ]

        return {
            "title": "Options Portfolio Summary",
            "color": 0x3B82F6,
            "fields": fields,
        }

    # ── Internal helpers ───────────────────────────────────────────────

    def _find_position(self, position_id: str) -> Optional[OptionsPosition]:
        """Find an open position by UUID."""
        for pos in self.state.positions:
            if pos.position_id == position_id:
                return pos
        return None

    def find_position_by_contract(self, contract_symbol: str) -> Optional[OptionsPosition]:
        """Find an open position by contract symbol."""
        for pos in self.state.positions:
            if pos.contract_symbol == contract_symbol:
                return pos
        return None

    @property
    def open_positions(self) -> list[OptionsPosition]:
        """Return all currently open positions."""
        return list(self.state.positions)

    @property
    def closed_positions(self) -> list[OptionsPosition]:
        """Return all historically closed positions."""
        return list(self.state.closed_positions)

    @property
    def open_position_count(self) -> int:
        """Number of currently open options positions."""
        return len(self.state.positions)

    @property
    def total_exposure_pct(self) -> float:
        """Options exposure as fraction of portfolio (e.g. 0.08 = 8%)."""
        return self.state.total_options_exposure_pct

    @staticmethod
    def _expiry_from_contract_symbol(symbol: Optional[str]) -> Optional["__import__('datetime').date"]:
        """Parse expiration date from an OCC contract symbol.

        OCC format: AAPL260418C00255000
                         ^^^^^^ = YYMMDD
        """
        import datetime as dt
        if not symbol or len(symbol) < 15:
            return None
        try:
            # Find where the date starts: after the root symbol (variable length)
            # OCC: root (up to 6 chars) + date (6 chars) + type (1) + strike (8)
            # Strategy: locate the 6-digit date after the alpha prefix
            i = 0
            while i < len(symbol) and not symbol[i].isdigit():
                i += 1
            date_str = symbol[i:i+6]
            if len(date_str) != 6:
                return None
            year = 2000 + int(date_str[:2])
            month = int(date_str[2:4])
            day = int(date_str[4:6])
            return dt.date(year, month, day)
        except Exception:
            return None
