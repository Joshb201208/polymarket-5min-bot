"""
Paper trade tracking with early exit monitoring.
Tracks all paper trades, monitors for exit conditions, and logs daily P&L.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import config
from . import polymarket_api as pm
from . import telegram
from .bankroll import BankrollManager

logger = logging.getLogger(__name__)

TRADES_FILE = config.DATA_DIR / "paper_trades.json"


class PaperTracker:
    """Track paper trades and monitor for early exits."""

    def __init__(self, bankroll: BankrollManager):
        self.bankroll = bankroll

    def place_trade(self, market: dict, analysis: dict) -> dict | None:
        """Place a paper trade based on analysis results."""
        edge = analysis.get("edge", 0)
        if edge < config.MIN_EDGE_BET:
            logger.info(f"Edge {edge:.1%} below minimum {config.MIN_EDGE_BET:.1%}, skipping trade")
            return None

        side = analysis.get("side", "YES")
        price = analysis.get("price", 0)
        if not price or price <= 0:
            return None

        odds = 1 / price if price > 0 else 1
        confidence = analysis.get("confidence", "medium")
        bet_size = self.bankroll.kelly_size(edge, odds, confidence)

        if bet_size <= 0:
            logger.info("Kelly sizing returned 0, skipping trade")
            return None

        token_id = ""
        token_ids = market.get("token_ids", [])
        if token_ids:
            token_id = token_ids[0] if side == "YES" else (token_ids[1] if len(token_ids) > 1 else token_ids[0])

        position = self.bankroll.open_position(
            market_id=market.get("id", ""),
            question=market.get("question", ""),
            side=side,
            entry_price=price,
            size=bet_size,
            edge=edge,
            token_id=token_id,
            reasoning=analysis.get("reasoning", ""),
            confidence=confidence,
            fair_probability=analysis.get("fair_probability", 0.0),
        )

        # Send Telegram alert
        trade_info = {
            "side": side,
            "price": price,
            "size": bet_size,
            "edge": edge,
        }
        telegram.alert_paper_trade("BUY", market, trade_info)

        self._save_trade_log(position, "OPEN")
        return position

    def check_early_exits(self) -> list[dict]:
        """Check all open positions for early exit conditions."""
        exits = []
        for position in list(self.bankroll.positions):
            if position["status"] != "OPEN":
                continue

            token_id = position.get("token_id", "")
            if not token_id:
                continue

            current_price = pm.get_market_price(token_id)
            if current_price is None:
                continue

            position["current_price"] = current_price
            should_exit, reason = self.bankroll.should_early_exit(position, current_price)

            if should_exit:
                entry = position["entry_price"]
                side = position["side"]
                confidence = position.get("confidence", "medium")
                if side == "YES":
                    pnl_pct = (current_price - entry) / entry if entry > 0 else 0
                else:
                    pnl_pct = (entry - current_price) / entry if entry > 0 else 0

                close_reason = "early_exit_profit" if pnl_pct > 0 else "stop_loss"
                closed = self.bankroll.close_position(position["id"], current_price, close_reason)

                if closed:
                    closed["current_price"] = current_price
                    pnl_dollars = closed.get("pnl_dollars", 0)
                    closed["pnl_dollars"] = pnl_dollars

                    telegram.alert_early_exit(closed, reason, pnl_pct, confidence)

                    exits.append(closed)
                    self._save_trade_log(closed, "CLOSED")
                    logger.info(f"Early exit: {reason} | {confidence.upper()} conf | P&L: ${pnl_dollars:+.2f}")

        return exits

    def update_position_prices(self) -> list[dict]:
        """Update all position prices and alert on significant moves."""
        updates = []
        for position in self.bankroll.positions:
            if position["status"] != "OPEN":
                continue

            token_id = position.get("token_id", "")
            if not token_id:
                continue

            current_price = pm.get_market_price(token_id)
            if current_price is None:
                continue

            old_price = position.get("current_price", position["entry_price"])
            position["current_price"] = current_price

            if old_price > 0:
                change_pct = (current_price - old_price) / old_price
                if abs(change_pct) >= 0.10:  # 10%+ move
                    telegram.alert_position_update(position, change_pct)
                    updates.append(position)

        self.bankroll.save_state()
        return updates

    def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        return [p for p in self.bankroll.positions if p["status"] == "OPEN"]

    def get_daily_pnl(self) -> float:
        """Get today's P&L."""
        self.bankroll._reset_day_pnl_if_needed()
        return self.bankroll.day_pnl

    def _save_trade_log(self, trade: dict, action: str):
        """Append trade to log file."""
        try:
            trades = []
            if TRADES_FILE.exists():
                trades = json.loads(TRADES_FILE.read_text())
            trades.append({
                "action": action,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **{k: v for k, v in trade.items() if k != "reasoning"},
            })
            # Keep last 500 entries
            TRADES_FILE.write_text(json.dumps(trades[-500:], indent=2, default=str))
        except Exception as e:
            logger.error(f"Error saving trade log: {e}")
