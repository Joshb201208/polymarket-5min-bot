"""
Bankroll management with Kelly criterion sizing and early exit logic.
The money engine — manages the $500 bankroll.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger(__name__)

STATE_FILE = config.DATA_DIR / "bankroll_state.json"


class BankrollManager:
    """Manages bankroll with Kelly criterion sizing."""

    def __init__(self, starting_capital: float = None):
        self.capital = starting_capital or config.STARTING_BANKROLL
        self.positions: list[dict] = []
        self.history: list[dict] = []
        self.total_pnl = 0.0
        self.day_pnl = 0.0
        self.day_start = datetime.now(timezone.utc).date()
        self._load_state()

    def _load_state(self):
        """Load persisted state from disk."""
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.capital = data.get("capital", self.capital)
                self.positions = data.get("positions", [])
                self.history = data.get("history", [])
                self.total_pnl = data.get("total_pnl", 0.0)
                self.day_pnl = data.get("day_pnl", 0.0)
                saved_date = data.get("day_start", "")
                if saved_date:
                    self.day_start = datetime.fromisoformat(saved_date).date()
                logger.info(f"Loaded bankroll state: ${self.capital:.2f}, {len(self.positions)} open positions")
            except Exception as e:
                logger.error(f"Error loading bankroll state: {e}")

    def save_state(self):
        """Persist state to disk."""
        try:
            data = {
                "capital": self.capital,
                "positions": self.positions,
                "history": self.history[-200:],  # Keep last 200 trades
                "total_pnl": self.total_pnl,
                "day_pnl": self.day_pnl,
                "day_start": self.day_start.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            STATE_FILE.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.error(f"Error saving bankroll state: {e}")

    def _reset_day_pnl_if_needed(self):
        """Reset daily P&L at day boundary."""
        today = datetime.now(timezone.utc).date()
        if today != self.day_start:
            self.day_pnl = 0.0
            self.day_start = today

    def kelly_size(self, edge: float, odds: float, confidence: str = "medium") -> float:
        """Calculate bet size using fractional Kelly criterion.

        Kelly formula: f* = (bp - q) / b
        where b = decimal odds - 1, p = our estimated probability, q = 1-p

        We use QUARTER Kelly for safety.
        Then cap at MAX_SINGLE_BET_PCT of current bankroll.

        Args:
            edge: Our estimated edge (e.g., 0.10 = 10%)
            odds: Decimal odds (e.g., price 0.40 means odds = 1/0.40 = 2.5)
            confidence: "low", "medium", "high" — adjusts Kelly fraction
        """
        if edge <= 0 or odds <= 1:
            return 0.0

        # Kelly fraction based on confidence
        kelly_mult = {
            "low": 0.15,
            "medium": config.KELLY_FRACTION,  # 0.25
            "high": 0.35,
        }.get(confidence, config.KELLY_FRACTION)

        b = odds - 1  # net decimal odds
        p = 1 / odds + edge  # our probability = implied + edge
        p = min(p, 0.95)  # cap at 95%
        q = 1 - p

        full_kelly = (b * p - q) / b
        if full_kelly <= 0:
            return 0.0

        fraction_kelly = full_kelly * kelly_mult
        available = self.available_capital()

        # Apply constraints
        bet = fraction_kelly * available
        bet = max(bet, config.MIN_BET) if bet > 0 else 0
        bet = min(bet, config.MAX_BET)
        bet = min(bet, available * config.MAX_SINGLE_BET_PCT / 0.10)  # 10% max
        bet = min(bet, available)  # Can't bet more than we have

        if bet < config.MIN_BET:
            return 0.0

        return round(bet, 2)

    def open_position(self, market_id: str, question: str, side: str,
                      entry_price: float, size: float, edge: float,
                      token_id: str = "", reasoning: str = "") -> dict:
        """Open a new paper position."""
        self._reset_day_pnl_if_needed()

        position = {
            "id": f"{market_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "market_id": market_id,
            "question": question,
            "side": side,
            "entry_price": entry_price,
            "current_price": entry_price,
            "size": size,
            "shares": size / entry_price if entry_price > 0 else 0,
            "edge": edge,
            "token_id": token_id,
            "reasoning": reasoning,
            "status": "OPEN",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "pnl_dollars": 0.0,
            "pnl_pct": 0.0,
        }

        self.positions.append(position)
        self.save_state()
        logger.info(f"Opened position: {side} {question[:60]} @ {entry_price:.2f} for ${size:.2f}")
        return position

    def close_position(self, position_id: str, exit_price: float,
                       reason: str = "resolved") -> dict | None:
        """Close a position and record P&L."""
        self._reset_day_pnl_if_needed()

        for pos in self.positions:
            if pos["id"] == position_id and pos["status"] == "OPEN":
                pos["current_price"] = exit_price
                pos["status"] = self._classify_close(pos, reason)
                pos["closed_at"] = datetime.now(timezone.utc).isoformat()
                pos["close_reason"] = reason

                # Calculate P&L
                if pos["side"] == "YES":
                    pnl = (exit_price - pos["entry_price"]) * pos["shares"]
                else:
                    pnl = (pos["entry_price"] - exit_price) * pos["shares"]

                pos["pnl_dollars"] = round(pnl, 2)
                pos["pnl_pct"] = pnl / pos["size"] if pos["size"] > 0 else 0

                self.capital += pos["size"] + pnl  # Return principal + profit/loss
                self.total_pnl += pnl
                self.day_pnl += pnl

                self.history.append(pos)
                self.positions.remove(pos)
                self.save_state()

                logger.info(f"Closed position: {pos['question'][:50]} P&L: ${pnl:+.2f} ({reason})")
                return pos
        return None

    def _classify_close(self, position: dict, reason: str) -> str:
        """Classify the close type."""
        if reason == "early_exit_profit":
            return "EARLY_EXIT"
        if reason == "stop_loss":
            return "CLOSED_LOSS"
        if "win" in reason.lower() or "resolved_yes" in reason:
            return "CLOSED_WIN"
        if "loss" in reason.lower() or "resolved_no" in reason:
            return "CLOSED_LOSS"
        return "EARLY_EXIT" if "early" in reason else "CLOSED_WIN"

    def should_early_exit(self, position: dict, current_price: float) -> tuple[bool, str]:
        """Check if a position should be sold.

        Exit conditions:
        1. Price moved 15%+ in our favor -> TAKE PROFIT
        2. Price moved 20%+ against us -> STOP LOSS
        3. Resolution within 2 hours and we're profitable -> EXIT
        """
        entry = position["entry_price"]
        side = position["side"]

        if side == "YES":
            pnl_pct = (current_price - entry) / entry if entry > 0 else 0
        else:
            pnl_pct = (entry - current_price) / entry if entry > 0 else 0

        # Take profit
        if pnl_pct >= config.EARLY_EXIT_PROFIT_PCT:
            return True, f"TAKE PROFIT (+{pnl_pct:.1%})"

        # Stop loss
        if pnl_pct <= -config.EARLY_EXIT_LOSS_PCT:
            return True, f"STOP LOSS ({pnl_pct:.1%})"

        # Near resolution and profitable
        end_date_str = position.get("end_date", "")
        if end_date_str and pnl_pct > 0:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                hours_left = (end_date - datetime.now(timezone.utc)).total_seconds() / 3600
                if 0 < hours_left < 2:
                    return True, f"NEAR RESOLUTION ({hours_left:.1f}h left, +{pnl_pct:.1%})"
            except Exception:
                pass

        return False, ""

    def get_exposure(self) -> float:
        """Total capital currently at risk."""
        return sum(p["size"] for p in self.positions if p["status"] == "OPEN")

    def available_capital(self) -> float:
        """Capital available for new bets."""
        return max(0, self.capital - self.get_exposure())

    def get_status(self) -> dict:
        """Get full bankroll status."""
        self._reset_day_pnl_if_needed()
        return {
            "capital": self.capital,
            "exposure": self.get_exposure(),
            "available": self.available_capital(),
            "total_pnl": self.total_pnl,
            "day_pnl": self.day_pnl,
            "open_positions": len([p for p in self.positions if p["status"] == "OPEN"]),
            "total_trades": len(self.history),
            "win_rate": self._win_rate(),
        }

    def _win_rate(self) -> float:
        """Calculate win rate from history."""
        if not self.history:
            return 0.0
        wins = sum(1 for h in self.history if h.get("pnl_dollars", 0) > 0)
        return wins / len(self.history)
