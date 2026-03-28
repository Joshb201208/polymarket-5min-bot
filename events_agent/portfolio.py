"""Portfolio manager — monitors open positions, triggers early exits."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from events_agent.config import EventsConfig
from events_agent.models import Position, Trade
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger(__name__)


class PortfolioManager:
    """Tracks events positions, trades, and manages early exits."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self.config = config or EventsConfig()
        self.config.ensure_data_dir()
        self._positions_path = self.config.DATA_DIR / "events_positions.json"
        self._trades_path = self.config.DATA_DIR / "events_trades.json"

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def load_positions(self) -> list[Position]:
        """Load all events positions from disk."""
        data = load_json(self._positions_path, {"positions": []})
        positions = []
        for d in data.get("positions", []):
            try:
                positions.append(Position.from_dict(d))
            except Exception as e:
                logger.warning("Failed to load events position: %s", e)
        return positions

    def get_open_positions(self) -> list[Position]:
        """Get only open events positions."""
        return [p for p in self.load_positions() if p.status == "open"]

    def save_position(self, position: Position) -> None:
        """Save or update a position."""
        positions = self.load_positions()

        found = False
        for i, p in enumerate(positions):
            if p.id == position.id:
                positions[i] = position
                found = True
                break
        if not found:
            positions.append(position)

        self._write_positions(positions)

    def _write_positions(self, positions: list[Position]) -> None:
        data = {"positions": [p.to_dict() for p in positions]}
        atomic_json_write(self._positions_path, data)

    # ------------------------------------------------------------------
    # Trade logging
    # ------------------------------------------------------------------

    def load_trades(self) -> list[Trade]:
        """Load all events trades from disk."""
        data = load_json(self._trades_path, {"trades": []})
        trades = []
        for d in data.get("trades", []):
            try:
                trades.append(Trade.from_dict(d))
            except Exception as e:
                logger.warning("Failed to load events trade: %s", e)
        return trades

    def log_trade(self, trade: Trade) -> None:
        """Append a trade to the log."""
        trades = self.load_trades()
        trades.append(trade)
        data = {"trades": [t.to_dict() for t in trades]}
        atomic_json_write(self._trades_path, data)

    # ------------------------------------------------------------------
    # Early exit logic
    # ------------------------------------------------------------------

    def should_exit_early(
        self,
        position: Position,
        current_price: float,
    ) -> tuple[bool, str]:
        """Check if an events position should be exited early.

        Events markets CAN be sold early (unlike game-day sports bets).
        Exit triggers:
        - Take profit at +30% gain
        - Stop loss at -25% loss
        - Liquidity concern (handled externally via scanner)
        """
        entry = position.entry_price
        if entry <= 0:
            return False, ""

        pnl_pct = (current_price - entry) / entry

        # Take profit
        if pnl_pct >= self.config.TAKE_PROFIT:
            return True, f"Take profit (+{pnl_pct * 100:.1f}%)"

        # Stop loss
        if pnl_pct <= -self.config.STOP_LOSS:
            return True, f"Stop loss ({pnl_pct * 100:.1f}%)"

        return False, ""

    # ------------------------------------------------------------------
    # Resolution checking
    # ------------------------------------------------------------------

    def check_resolved_positions(self) -> list[Position]:
        """Check for positions whose markets have ended."""
        now = utcnow()
        positions = self.load_positions()
        resolved = []

        for pos in positions:
            if pos.status != "open":
                continue

            if pos.market_end_date:
                try:
                    end_dt = datetime.fromisoformat(
                        pos.market_end_date.replace("Z", "+00:00")
                    )
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    if end_dt <= now:
                        resolved.append(pos)
                except (ValueError, AttributeError):
                    continue

        return resolved

    def has_existing_position(self, market_id: str) -> bool:
        """Check if we already have an open position in this market."""
        positions = self.get_open_positions()
        return any(p.market_id == market_id for p in positions)

    # ------------------------------------------------------------------
    # Summary generation
    # ------------------------------------------------------------------

    def get_daily_stats(self, date: datetime | None = None) -> dict:
        """Calculate stats for a specific day."""
        from datetime import timedelta
        if date is None:
            date = utcnow()

        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        trades = self.load_trades()
        positions = self.load_positions()

        today_trades = []
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
                if day_start <= ts < day_end:
                    today_trades.append(t)
            except (ValueError, AttributeError):
                continue

        buys = [t for t in today_trades if t.action == "BUY"]
        sells = [t for t in today_trades if t.action == "SELL"]
        daily_pnl = sum(t.pnl or 0 for t in sells)

        best_trade = ""
        worst_trade = ""
        sell_trades_with_pnl = [(t, t.pnl or 0) for t in sells if t.pnl is not None]
        if sell_trades_with_pnl:
            best = max(sell_trades_with_pnl, key=lambda x: x[1])
            worst = min(sell_trades_with_pnl, key=lambda x: x[1])
            best_trade = f"{best[0].market_question} (+${best[1]:.2f})"
            worst_trade = f"{worst[0].market_question} (${worst[1]:.2f})"

        wins = sum(1 for t in sells if (t.pnl or 0) > 0)
        win_rate = (wins / len(sells) * 100) if sells else 0.0

        open_positions = [p for p in positions if p.status == "open"]

        return {
            "date_str": date.strftime("%B %d, %Y"),
            "open_positions": len(open_positions),
            "trades_today": len(today_trades),
            "buys_today": len(buys),
            "sells_today": len(sells),
            "daily_pnl": daily_pnl,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "win_rate": win_rate,
            "avg_edge": 0.0,
        }
