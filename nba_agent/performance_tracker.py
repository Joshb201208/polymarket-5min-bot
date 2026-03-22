"""Trade logging, P&L tracking, and summary generation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nba_agent.config import Config
from nba_agent.models import Position, Trade
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger(__name__)


class PerformanceTracker:
    """Tracks positions, trades, and generates performance summaries."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.config.ensure_data_dir()
        self._positions_path = self.config.DATA_DIR / "positions.json"
        self._trades_path = self.config.DATA_DIR / "trades.json"

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def load_positions(self) -> list[Position]:
        """Load all positions from disk."""
        data = load_json(self._positions_path, {"positions": []})
        positions = []
        for d in data.get("positions", []):
            try:
                positions.append(Position.from_dict(d))
            except Exception as e:
                logger.warning("Failed to load position: %s", e)
        return positions

    def get_open_positions(self) -> list[Position]:
        """Get only open positions."""
        return [p for p in self.load_positions() if p.status == "open"]

    def save_position(self, position: Position) -> None:
        """Save or update a position."""
        positions = self.load_positions()

        # Update existing or append
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
        """Load all trades from disk."""
        data = load_json(self._trades_path, {"trades": []})
        trades = []
        for d in data.get("trades", []):
            try:
                trades.append(Trade.from_dict(d))
            except Exception as e:
                logger.warning("Failed to load trade: %s", e)
        return trades

    def log_trade(self, trade: Trade) -> None:
        """Append a trade to the log."""
        trades = self.load_trades()
        trades.append(trade)
        data = {"trades": [t.to_dict() for t in trades]}
        atomic_json_write(self._trades_path, data)

    # ------------------------------------------------------------------
    # Summary generation
    # ------------------------------------------------------------------

    def get_daily_stats(self, date: datetime | None = None) -> dict:
        """Calculate stats for a specific day."""
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

        # Best and worst trades
        best_trade = ""
        worst_trade = ""
        sell_trades_with_pnl = [(t, t.pnl or 0) for t in sells if t.pnl is not None]
        if sell_trades_with_pnl:
            best = max(sell_trades_with_pnl, key=lambda x: x[1])
            worst = min(sell_trades_with_pnl, key=lambda x: x[1])
            best_trade = f"{best[0].market_question} (+${best[1]:.2f})"
            worst_trade = f"{worst[0].market_question} (${worst[1]:.2f})"

        # Win rate
        wins = sum(1 for t in sells if (t.pnl or 0) > 0)
        win_rate = (wins / len(sells) * 100) if sells else 0.0

        # Average edge from buy trades
        avg_edge = 0.0  # We don't store edge on trade objects, use 0 as fallback

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
            "avg_edge": avg_edge,
        }

    def get_weekly_stats(self, end_date: datetime | None = None) -> dict:
        """Calculate stats for the past 7 days."""
        if end_date is None:
            end_date = utcnow()

        start_date = end_date - timedelta(days=7)

        trades = self.load_trades()
        positions = self.load_positions()

        week_trades = []
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.timestamp.replace("Z", "+00:00"))
                if start_date <= ts <= end_date:
                    week_trades.append(t)
            except (ValueError, AttributeError):
                continue

        sells = [t for t in week_trades if t.action == "SELL"]
        total_pnl = sum(t.pnl or 0 for t in sells)
        wins = sum(1 for t in sells if (t.pnl or 0) > 0)
        total_bets = len(sells)

        # Biggest win / loss
        biggest_win = ""
        biggest_loss = ""
        if sells:
            pnl_sorted = sorted(sells, key=lambda t: t.pnl or 0)
            if pnl_sorted:
                worst = pnl_sorted[0]
                best = pnl_sorted[-1]
                if (best.pnl or 0) > 0:
                    biggest_win = f"+${best.pnl:.2f} ({best.market_question})"
                if (worst.pnl or 0) < 0:
                    biggest_loss = f"${worst.pnl:.2f} ({worst.market_question})"

        # Win rates
        actual_wr = (wins / total_bets * 100) if total_bets > 0 else 0.0
        expected_wr = 55.0  # Default expected based on edge targeting

        # ROI
        total_invested = sum(t.amount for t in week_trades if t.action == "BUY")
        roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0

        week_start = start_date.strftime("%B %d")
        week_end = end_date.strftime("%d, %Y")
        week_str = f"Week of {week_start}-{week_end}"

        return {
            "week_str": week_str,
            "total_bets": total_bets,
            "wins": wins,
            "total_pnl": total_pnl,
            "roi": roi,
            "biggest_win": biggest_win,
            "biggest_loss": biggest_loss,
            "expected_wr": expected_wr,
            "actual_wr": actual_wr,
        }

    def check_resolved_positions(self) -> list[Position]:
        """Check for positions whose markets have ended and resolve them."""
        now = utcnow()
        positions = self.load_positions()
        resolved = []

        for pos in positions:
            if pos.status != "open":
                continue

            # Check if market end date has passed
            if pos.market_end_date:
                try:
                    end_dt = datetime.fromisoformat(pos.market_end_date.replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    if end_dt <= now:
                        # Market has ended — in paper mode, we'd need to check the outcome
                        # For now, mark as needing resolution
                        resolved.append(pos)
                except (ValueError, AttributeError):
                    continue

        return resolved

    def has_existing_position(self, market_id: str) -> bool:
        """Check if we already have an open position in this market."""
        positions = self.get_open_positions()
        return any(p.market_id == market_id for p in positions)
