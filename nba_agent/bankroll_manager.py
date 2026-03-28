"""Kelly criterion, position sizing, exposure limits."""

from __future__ import annotations

import logging
from pathlib import Path

from nba_agent.config import Config
from nba_agent.models import Confidence, EdgeResult, Position
from nba_agent.utils import load_json, atomic_json_write
from shared.bankroll import check_total_exposure_ok as _cross_sport_exposure_ok

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
        """Calculate optimal bet size using Quarter-Kelly criterion."""
        if self.is_paused:
            logger.warning("Bankroll manager is paused — no bets allowed")
            return 0.0

        edge = edge_result.edge
        market_price = edge_result.market_price

        # Kelly fraction: edge / odds_against
        # odds_against = (1 - prob) / prob, but we use simplified Kelly
        if market_price <= 0 or market_price >= 1:
            return 0.0

        odds_against = (1.0 - market_price) / market_price
        if odds_against <= 0:
            return 0.0

        kelly_fraction = (edge / odds_against) * 0.50  # Half Kelly
        bet_size = self.current_bankroll * kelly_fraction

        # HIGH confidence BLOCKED: 0W/9L in live trading (-$59.60).
        # The edge calculator fundamentally overvalues these — they represent
        # heavy underdogs with large calculated edges that never materialize.
        if edge_result.confidence == Confidence.HIGH:
            logger.info("BLOCKED: HIGH confidence bet on %s (0/9 in live data)",
                        edge_result.market.question)
            return 0.0

        # Vegas agreement boost: if Vegas and our model agree, size up
        if edge_result.has_vegas_line and edge_result.vegas_agrees:
            bet_size *= 1.4  # 40% larger when Vegas confirms our edge
            logger.debug("Vegas agrees — boosting bet size by 40%%")
        elif edge_result.has_vegas_line and not edge_result.vegas_agrees:
            bet_size *= 0.7  # 30% smaller when we're going against Vegas
            logger.debug("Vegas disagrees — reducing bet size by 30%%")

        # Apply maximum per-bet limit
        max_bet = self.current_bankroll * self.config.MAX_BET_PCT
        bet_size = min(bet_size, max_bet)

        # If in reduced mode, halve bet sizes
        if self.is_reduced:
            bet_size *= 0.5

        # Floor at $1
        if bet_size < 1.0:
            return 0.0

        # Round to 2 decimal places
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
        """Check if adding this bet would exceed total exposure limit.

        Uses the shared bankroll module to account for BOTH NBA and NHL
        open positions when checking the 50% cap.
        """
        return _cross_sport_exposure_ok(
            proposed_bet, self.config.MAX_TOTAL_EXPOSURE_PCT
        )

    def update_bankroll(self, pnl: float) -> None:
        """Update bankroll after a trade settles."""
        self.current_bankroll += pnl
        if self.current_bankroll > self.peak_bankroll:
            self.peak_bankroll = self.current_bankroll

        self._check_stop_loss()
        self.save_state()

    def _check_stop_loss(self) -> None:
        """Check stop-loss conditions."""
        # If below 60% of peak — pause trading
        if self.current_bankroll < self.peak_bankroll * 0.60:
            if not self.is_paused:
                logger.critical(
                    "STOP LOSS: Bankroll $%.2f is below 60%% of peak $%.2f — PAUSING",
                    self.current_bankroll,
                    self.peak_bankroll,
                )
                self.is_paused = True
                self.is_reduced = False
            return

        # If below 75-80% of starting — reduce bet sizes
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

        # Reset pause if bankroll recovers above 60% of peak
        if self.is_paused and self.current_bankroll >= self.peak_bankroll * 0.60:
            logger.info("Bankroll recovered above 60%% of peak — resuming trading")
            self.is_paused = False

    def should_exit_early(
        self,
        position: Position,
        current_price: float,
    ) -> tuple[bool, str]:
        """Check if a position should be exited early.

        Game-day bets: NEVER sell early — hold to resolution. The asymmetric
        payoff (underdogs at 10-50c pay 2-10x) means one win covers many
        losses. Selling early caps upside for no reason.

        Futures bets (championship, MVP, conference): Use take-profit and
        stop-loss since these resolve weeks/months away and conditions change.
        """
        entry = position.entry_price
        if entry <= 0:
            return False, ""

        # Determine if this is a futures/long-term position
        is_futures = False
        slug = (position.market_slug or "").lower()
        if any(kw in slug for kw in ("championship", "mvp", "conference", "finals", "award", "leader", "ppg", "rpg")):
            is_futures = True

        # Game-day bets: hold to resolution, never sell early
        if not is_futures:
            return False, ""

        # Futures bets: apply take-profit / stop-loss
        pnl_pct = (current_price - entry) / entry
        conf = position.confidence.upper()

        if conf == Confidence.HIGH.value:
            if pnl_pct >= 0.50:
                return True, "Futures take-profit (HIGH, +50%)"
            if pnl_pct <= -0.30:
                return True, "Futures stop-loss (HIGH, -30%)"
        elif conf == Confidence.MEDIUM.value:
            if pnl_pct >= 0.35:
                return True, "Futures take-profit (MEDIUM, +35%)"
            if pnl_pct <= -0.25:
                return True, "Futures stop-loss (MEDIUM, -25%)"
        else:  # LOW
            if pnl_pct >= 0.25:
                return True, "Futures take-profit (LOW, +25%)"
            if pnl_pct <= -0.20:
                return True, "Futures stop-loss (LOW, -20%)"

        return False, ""
