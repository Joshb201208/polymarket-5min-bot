"""Flat-percentage position sizing with exposure limits.

Replaces the Half-Kelly model which amplified edge calculation errors.
Now uses a simple flat 2% of bankroll per bet — predictable, consistent,
and doesn't compound model mistakes into sizing mistakes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nba_agent.config import Config
from nba_agent.models import Confidence, EdgeResult, Position
from nba_agent.utils import load_json, atomic_json_write

logger = logging.getLogger(__name__)


class BankrollManager:
    """Manages bankroll, position sizing, and exposure limits."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self._state_path = self.config.DATA_DIR / "bankroll.json"
        self._load_state()

    def _load_state(self) -> None:
        state = load_json(self._state_path, {})
        self.starting_bankroll = float(state.get("starting_bankroll", self.config.STARTING_BANKROLL))
        self.current_bankroll = float(state.get("current_bankroll", self.starting_bankroll))
        self.peak_bankroll = float(state.get("peak_bankroll", self.starting_bankroll))
        self.is_paused = bool(state.get("is_paused", False))
        self.is_reduced = bool(state.get("is_reduced", False))

    def save_state(self) -> None:
        self.config.ensure_data_dir()
        atomic_json_write(self._state_path, {
            "starting_bankroll": self.starting_bankroll,
            "current_bankroll": self.current_bankroll,
            "peak_bankroll": self.peak_bankroll,
            "is_paused": self.is_paused,
            "is_reduced": self.is_reduced,
        })

    def calculate_bet_size(self, edge_result: EdgeResult) -> float:
        """Calculate bet size — flat percentage of bankroll.

        Simple and predictable:
        - LOW confidence:    1.5% of bankroll
        - MEDIUM confidence: 2.0% of bankroll
        - HIGH confidence:   2.5% of bankroll

        No Kelly, no model-dependent sizing. Every bet is roughly the same
        size, so one bad edge calculation doesn't blow up a position.
        """
        if self.is_paused:
            logger.warning("Bankroll manager is paused — no bets allowed")
            return 0.0

        # Flat sizing by confidence tier
        if edge_result.confidence == Confidence.HIGH:
            pct = 0.025  # 2.5%
        elif edge_result.confidence == Confidence.MEDIUM:
            pct = 0.020  # 2.0%
        else:
            pct = 0.015  # 1.5%

        bet_size = self.current_bankroll * pct

        # If in reduced mode (drawdown protection), halve bet sizes
        if self.is_reduced:
            bet_size *= 0.5

        # Floor at $2 — below this, fees and slippage eat the edge
        if bet_size < 2.0:
            return 0.0

        # Cap at MAX_BET_PCT of bankroll (safety net)
        max_bet = self.current_bankroll * self.config.MAX_BET_PCT
        bet_size = min(bet_size, max_bet)

        return round(bet_size, 2)

    def check_game_exposure(
        self,
        game_slug: str,
        open_positions: list[Position],
        proposed_bet: float,
    ) -> bool:
        """Check if adding this bet would exceed per-game exposure limit."""
        current_exposure = sum(
            p.cost for p in open_positions
            if p.status == "open" and game_slug in p.market_slug
        )
        max_game_exposure = self.current_bankroll * self.config.MAX_GAME_EXPOSURE_PCT
        return (current_exposure + proposed_bet) <= max_game_exposure

    def check_total_exposure(
        self,
        open_positions: list[Position],
        proposed_bet: float,
    ) -> bool:
        """Check if adding this bet would exceed total exposure limit."""
        total_open = sum(p.cost for p in open_positions if p.status == "open")
        max_total = self.current_bankroll * self.config.MAX_TOTAL_EXPOSURE_PCT
        return (total_open + proposed_bet) <= max_total

    def update_bankroll(self, pnl: float) -> None:
        """Update bankroll after a trade settles."""
        self.current_bankroll += pnl
        if self.current_bankroll > self.peak_bankroll:
            self.peak_bankroll = self.current_bankroll

        self._check_stop_loss()
        self.save_state()

    def _check_stop_loss(self) -> None:
        """Check stop-loss conditions."""
        # If below 50% of peak — pause trading
        if self.current_bankroll < self.peak_bankroll * 0.50:
            if not self.is_paused:
                logger.critical(
                    "STOP LOSS: Bankroll $%.2f is below 50%% of peak $%.2f — PAUSING",
                    self.current_bankroll,
                    self.peak_bankroll,
                )
                self.is_paused = True
                self.is_reduced = False
            return

        # If below 80% of starting — reduce bet sizes
        if self.current_bankroll < self.starting_bankroll * 0.80:
            if not self.is_reduced:
                logger.warning(
                    "Bankroll $%.2f is below 80%% of starting $%.2f — reducing bet sizes by 50%%",
                    self.current_bankroll,
                    self.starting_bankroll,
                )
                self.is_reduced = True
        else:
            self.is_reduced = False

        # Reset pause if bankroll recovers above 50% of peak
        if self.is_paused and self.current_bankroll >= self.peak_bankroll * 0.50:
            logger.info("Bankroll recovered above 50%% of peak — resuming trading")
            self.is_paused = False

    def should_exit_early(
        self,
        position: Position,
        current_price: float,
    ) -> tuple[bool, str]:
        """Check if a position should be exited early.

        Game-day moneyline bets: NEVER sell early. Hold to resolution.
        The new strategy bets favorites at 45-80¢ — these resolve to $1
        on win. Selling early caps the upside.
        """
        # Game-day bets always hold to resolution
        return False, ""
