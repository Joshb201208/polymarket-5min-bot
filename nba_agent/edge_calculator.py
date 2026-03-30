"""Vegas Disagreement Edge Calculator.

Strategy: bet on favorites when Polymarket underprices them vs Vegas.
No model, no power ratings — pure price comparison.

Research basis:
- Longshot bias: underdogs are systematically overpriced on prediction markets
  (QuantPedia, 2025; "Biases in the Football Betting Market", 2017)
- Favorites return -3.64% vs outsiders -26.08% across 12,000+ matches
- $40M in arbitrage extracted from Polymarket by bots exploiting mispricings
  (IMDEA Networks Institute, 2025)
"""

from __future__ import annotations

import logging
from typing import Optional

from nba_agent.config import Config
from nba_agent.models import (
    Confidence,
    EdgeResult,
    Market,
    MarketType,
    ResearchData,
)
from nba_agent.nba_research import NBAResearch, find_team_by_abbr, find_team_by_name
from nba_agent.odds_api import OddsAPI
from nba_agent.balldontlie import BDLClient
from nba_agent.injury_scanner import InjuryScanner
from nba_agent.utils import slugify_game

logger = logging.getLogger(__name__)

# ── Strategy parameters ──────────────────────────────────────────────────
# Minimum Polymarket price to enter — we ONLY bet favorites (45¢+)
MIN_ENTRY_PRICE = 0.45

# Maximum entry price — avoid near-certainties where payout is tiny
MAX_ENTRY_PRICE = 0.80

# Minimum edge (Vegas fair - Polymarket price) to trigger a bet
MIN_EDGE = 0.03  # 3%

# We REQUIRE a Vegas line. No Vegas = no bet.
REQUIRE_VEGAS = True


def _devig(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Remove vig from two-way probabilities to get fair values."""
    total = prob_a + prob_b
    if total <= 0:
        return 0.5, 0.5
    return prob_a / total, prob_b / total


class EdgeCalculator:
    """Detects edges by comparing Vegas lines to Polymarket prices.

    Only bets favorites (45-80¢ range) where Vegas says they're underpriced.
    No statistical model, no power ratings, no H2H adjustments.
    """

    def __init__(
        self,
        config: Config | None = None,
        research: NBAResearch | None = None,
        injury_scanner: InjuryScanner | None = None,
        odds_api: OddsAPI | None = None,
        bdl: BDLClient | None = None,
    ) -> None:
        self.config = config or Config()
        self.research = research or NBAResearch(self.config)
        self.injury_scanner = injury_scanner or InjuryScanner()
        self.odds_api = odds_api or OddsAPI(self.config)
        self.bdl = bdl or BDLClient(self.config)

    async def evaluate(self, market: Market) -> EdgeResult | None:
        """Evaluate a market for Vegas disagreement edge."""
        try:
            if market.market_type == MarketType.MONEYLINE:
                return await self._evaluate_moneyline(market)
            # Only moneylines for now — spreads and totals require model-based
            # fair value estimation which is what we're removing.
            # Futures are also dropped — they rely on power-rating models.
            return None
        except Exception as e:
            logger.error("Edge calculation failed for %s: %s", market.slug, e)
            return None

    async def _evaluate_moneyline(self, market: Market) -> EdgeResult | None:
        """Pure Vegas vs Polymarket comparison on moneylines."""

        # ── Step 1: Resolve teams ────────────────────────────────────
        slug_parts = slugify_game(market.slug)
        if not slug_parts:
            return None

        away_abbr, home_abbr, game_date = slug_parts
        away_team = find_team_by_abbr(away_abbr)
        home_team = find_team_by_abbr(home_abbr)

        if not away_team or not home_team:
            if len(market.outcomes) >= 2:
                home_team = find_team_by_name(market.outcomes[1])
                away_team = find_team_by_name(market.outcomes[0])

        if not away_team or not home_team:
            logger.debug("Cannot resolve teams for %s", market.slug)
            return None

        # ── Step 2: Get Vegas line (REQUIRED) ────────────────────────
        vegas_game = self.odds_api.find_game_odds(
            home_team["name"], away_team["name"]
        )
        if not vegas_game or not vegas_game.home_ml or not vegas_game.away_ml:
            logger.debug("No Vegas line for %s — skipping", market.question)
            return None

        # Devig: remove bookmaker margin to get true probabilities
        raw_home = vegas_game.home_ml.sharp_prob or vegas_game.home_ml.consensus_prob
        raw_away = vegas_game.away_ml.sharp_prob or vegas_game.away_ml.consensus_prob
        if raw_home <= 0 or raw_away <= 0:
            return None

        vegas_home, vegas_away = _devig(raw_home, raw_away)

        # ── Step 3: Get Polymarket prices ────────────────────────────
        if len(market.outcome_prices) < 2:
            return None

        poly_away = market.outcome_prices[0]  # outcome[0] = away
        poly_home = market.outcome_prices[1]  # outcome[1] = home

        # ── Step 4: Find the favorite and check for mispricing ───────
        # Vegas edge = Vegas fair price - Polymarket price
        # Positive edge = Polymarket is underpricing this team vs Vegas
        home_edge = vegas_home - poly_home
        away_edge = vegas_away - poly_away

        logger.info(
            "Vegas vs Poly for %s: home=%.1f%% vs %.1f¢ (edge=%+.1f%%), "
            "away=%.1f%% vs %.1f¢ (edge=%+.1f%%) [%d books]",
            market.question,
            vegas_home * 100, poly_home * 100, home_edge * 100,
            vegas_away * 100, poly_away * 100, away_edge * 100,
            vegas_game.home_ml.num_books,
        )

        # Pick the side with more edge (must be positive)
        candidates = []

        if home_edge >= MIN_EDGE and MIN_ENTRY_PRICE <= poly_home <= MAX_ENTRY_PRICE:
            candidates.append(("home", home_edge, poly_home, vegas_home, 1))

        if away_edge >= MIN_EDGE and MIN_ENTRY_PRICE <= poly_away <= MAX_ENTRY_PRICE:
            candidates.append(("away", away_edge, poly_away, vegas_away, 0))

        if not candidates:
            return None

        # Take the best edge
        candidates.sort(key=lambda c: c[1], reverse=True)
        side_label, edge, poly_price, vegas_fair, side_index = candidates[0]

        # ── Step 5: Build minimal research data for logging ──────────
        research_data = None
        try:
            home_id = home_team["id"]
            away_id = away_team["id"]
            home_stats, away_stats, h2h = self.research.build_research(
                home_id, away_id, game_date
            )
            if home_stats and away_stats:
                research_data = ResearchData(
                    home_team=home_stats,
                    away_team=away_stats,
                    h2h=h2h,
                )
        except Exception:
            pass  # Research is optional — edge comes from Vegas, not stats

        # ── Step 6: Classify confidence ──────────────────────────────
        confidence = self._classify_confidence(edge, vegas_game.home_ml.num_books)

        logger.info(
            "EDGE FOUND: %s %s @ %.1f¢ (Vegas fair: %.1f%%, edge: +%.1f%%, %s, %d books)",
            market.question, side_label, poly_price * 100,
            vegas_fair * 100, edge * 100, confidence.value,
            vegas_game.home_ml.num_books,
        )

        return EdgeResult(
            market=market,
            our_fair_price=vegas_fair,
            market_price=poly_price,
            edge=edge,
            confidence=confidence,
            side="YES",
            side_index=side_index,
            research=research_data,
            has_vegas_line=True,
            vegas_agrees=True,  # By definition — our fair price IS Vegas
        )

    def _classify_confidence(
        self,
        edge: float,
        num_books: int,
    ) -> Confidence:
        """Classify based on edge size and number of books in consensus.

        More books = sharper line = more reliable signal.
        """
        if edge >= 0.07 and num_books >= 5:
            return Confidence.HIGH
        elif edge >= 0.05 and num_books >= 3:
            return Confidence.MEDIUM
        else:
            return Confidence.LOW
