"""Trade logging, P&L tracking for NHL agent."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from nhl_agent.config import NHLConfig
from nhl_agent.models import NHLPosition, NHLTrade
from nba_agent.utils import atomic_json_write, load_json, utcnow

logger = logging.getLogger(__name__)


class NHLPerformanceTracker:
    """Tracks NHL positions, trades, and generates performance summaries."""

    def __init__(self, config: NHLConfig | None = None) -> None:
        self.config = config or NHLConfig()
        self.config.ensure_data_dir()
        self._positions_path = self.config.nhl_positions_path
        self._trades_path = self.config.nhl_trades_path

    def load_positions(self) -> list[NHLPosition]:
        data = load_json(self._positions_path, {"positions": []})
        positions = []
        for d in data.get("positions", []):
            try:
                positions.append(NHLPosition.from_dict(d))
            except Exception as e:
                logger.warning("Failed to load NHL position: %s", e)
        return positions

    def get_open_positions(self) -> list[NHLPosition]:
        return [p for p in self.load_positions() if p.status == "open"]

    def save_position(self, position: NHLPosition) -> None:
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

    def _write_positions(self, positions: list[NHLPosition]) -> None:
        data = {"positions": [p.to_dict() for p in positions]}
        atomic_json_write(self._positions_path, data)

    def load_trades(self) -> list[NHLTrade]:
        data = load_json(self._trades_path, {"trades": []})
        trades = []
        for d in data.get("trades", []):
            try:
                trades.append(NHLTrade.from_dict(d))
            except Exception as e:
                logger.warning("Failed to load NHL trade: %s", e)
        return trades

    def log_trade(self, trade: NHLTrade) -> None:
        trades = self.load_trades()
        trades.append(trade)
        data = {"trades": [t.to_dict() for t in trades]}
        atomic_json_write(self._trades_path, data)

    def get_daily_stats(self, date: datetime | None = None) -> dict:
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
        sell_with_pnl = [(t, t.pnl or 0) for t in sells if t.pnl is not None]
        if sell_with_pnl:
            best = max(sell_with_pnl, key=lambda x: x[1])
            worst = min(sell_with_pnl, key=lambda x: x[1])
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

    def get_weekly_stats(self, end_date: datetime | None = None) -> dict:
        if end_date is None:
            end_date = utcnow()
        start_date = end_date - timedelta(days=7)

        trades = self.load_trades()
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

        total_invested = sum(t.amount for t in week_trades if t.action == "BUY")
        roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0

        return {
            "week_str": f"Week of {start_date.strftime('%B %d')}-{end_date.strftime('%d, %Y')}",
            "total_bets": total_bets,
            "wins": wins,
            "total_pnl": total_pnl,
            "roi": roi,
            "biggest_win": "",
            "biggest_loss": "",
            "expected_wr": 55.0,
            "actual_wr": (wins / total_bets * 100) if total_bets > 0 else 0.0,
        }

    def check_resolved_positions(self) -> list[NHLPosition]:
        now = utcnow()
        positions = self.load_positions()
        resolved = []
        for pos in positions:
            if pos.status != "open":
                continue
            if pos.market_end_date:
                try:
                    end_dt = datetime.fromisoformat(pos.market_end_date.replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    if end_dt <= now:
                        resolved.append(pos)
                except (ValueError, AttributeError):
                    continue
        return resolved

    def has_existing_position(self, market_id: str, market_slug: str = "") -> bool:
        positions = self.get_open_positions()
        for p in positions:
            if p.market_id == market_id:
                return True
            if market_slug and p.market_slug:
                slug_parts = market_slug.split("-")[:5]
                pos_parts = p.market_slug.split("-")[:5]
                if slug_parts == pos_parts and len(slug_parts) >= 4:
                    return True
        return False
