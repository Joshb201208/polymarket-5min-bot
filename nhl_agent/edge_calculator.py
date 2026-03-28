"""Fair odds computation and edge detection for NHL markets.

Model factors:
- MoneyPuck expected goals differential (xGF%)
- Recent form (last 10 games)
- Home ice advantage (~54%)
- Rest advantage (back-to-back detection)
- Power play / penalty kill efficiency differential
- Head-to-head record (small weight)
- Vegas consensus line blending (60% Vegas, 40% model)
"""

from __future__ import annotations

import logging
from typing import Optional

from nhl_agent.config import NHLConfig
from nhl_agent.models import (
    Confidence,
    NHLEdgeResult,
    NHLMarket,
    NHLMarketType,
    NHLResearchData,
    NHLTeamStats,
    NHLH2HRecord,
)
from nhl_agent.nhl_research import (
    NHLResearch,
    find_nhl_team_by_abbr,
    find_nhl_team_by_name,
    slugify_nhl_game,
)
from nhl_agent.odds_client import NHLOddsClient

logger = logging.getLogger(__name__)

# Home ice advantage in NHL: ~54% win rate historically
_HOME_ADVANTAGE = 0.04  # 4% boost to home team

# Vegas vs model blend weights
_VEGAS_WEIGHT = 0.60
_MODEL_WEIGHT = 0.40

# Minimum entry price floor (block heavy underdogs)
_MIN_ENTRY_PRICE = 0.20


class NHLEdgeCalculator:
    """Computes fair odds and detects edges for NHL markets."""

    def __init__(
        self,
        config: NHLConfig | None = None,
        research: NHLResearch | None = None,
        odds_client: NHLOddsClient | None = None,
    ) -> None:
        self.config = config or NHLConfig()
        self.research = research or NHLResearch(self.config)
        self.odds_client = odds_client or NHLOddsClient(self.config)

    async def evaluate(self, market: NHLMarket) -> NHLEdgeResult | None:
        """Evaluate a market and return edge result if edge found."""
        try:
            if market.market_type == NHLMarketType.MONEYLINE:
                return await self._evaluate_moneyline(market)
            return None
        except Exception as e:
            logger.error("NHL edge calculation failed for %s: %s", market.slug, e)
            return None

    async def _evaluate_moneyline(self, market: NHLMarket) -> NHLEdgeResult | None:
        """Calculate edge for an NHL moneyline market."""
        slug_parts = slugify_nhl_game(market.slug)
        if not slug_parts:
            return None

        away_abbr, home_abbr, game_date = slug_parts
        away_team = find_nhl_team_by_abbr(away_abbr)
        home_team = find_nhl_team_by_abbr(home_abbr)

        if not away_team or not home_team:
            # Try matching from outcome names
            if len(market.outcomes) >= 2:
                home_team = find_nhl_team_by_name(market.outcomes[1])
                away_team = find_nhl_team_by_name(market.outcomes[0])

        if not away_team or not home_team:
            logger.warning("Cannot resolve NHL teams for %s", market.slug)
            return None

        home_stats, away_stats, h2h = await self.research.build_research(
            home_team["abbr"], away_team["abbr"], game_date
        )
        if not home_stats or not away_stats:
            return None

        research_data = NHLResearchData(
            home_team=home_stats,
            away_team=away_stats,
            h2h=h2h,
        )

        # ── Compute fair price from our statistical model ──────────
        home_power = self._compute_power_rating(home_stats, is_home=True)
        away_power = self._compute_power_rating(away_stats, is_home=False)

        # H2H adjustment (small weight)
        if h2h and (h2h.team_a_wins + h2h.team_b_wins) > 0:
            total_h2h = h2h.team_a_wins + h2h.team_b_wins
            h2h_factor = (h2h.team_a_wins / total_h2h - 0.5) * 0.05
            home_power += h2h_factor

        # Rest advantage
        rest_diff = home_stats.rest_days - away_stats.rest_days
        if home_stats.is_b2b:
            home_power -= 0.025
        if away_stats.is_b2b:
            away_power -= 0.025
        if rest_diff >= 2:
            home_power += 0.01
        elif rest_diff <= -2:
            away_power += 0.01

        total_power = home_power + away_power
        model_fair_home = home_power / total_power if total_power > 0 else 0.5

        # ── Get Vegas line if available ────────────────────────────
        vegas_fair_home: float | None = None
        vegas_game = self.odds_client.find_game_odds(home_stats.team_name, away_stats.team_name)
        if vegas_game and vegas_game.home_ml and vegas_game.away_ml:
            raw_h = vegas_game.home_ml.sharp_prob or vegas_game.home_ml.consensus_prob
            raw_a = vegas_game.away_ml.sharp_prob or vegas_game.away_ml.consensus_prob
            total_vig = raw_h + raw_a
            if total_vig > 0:
                vegas_fair_home = raw_h / total_vig
                logger.info("NHL Vegas: %s home=%.1f%% (model=%.1f%%) books=%d",
                            home_stats.team_name, vegas_fair_home * 100,
                            model_fair_home * 100, vegas_game.home_ml.num_books)

        # ── Blend Vegas + model ────────────────────────────────────
        if vegas_fair_home is not None:
            fair_home = _VEGAS_WEIGHT * vegas_fair_home + _MODEL_WEIGHT * model_fair_home
        else:
            fair_home = model_fair_home
        fair_away = 1.0 - fair_home

        if len(market.outcome_prices) < 2:
            return None

        away_market_price = market.outcome_prices[0]
        home_market_price = market.outcome_prices[1]

        away_edge = fair_away - away_market_price
        home_edge = fair_home - home_market_price

        has_vegas = vegas_fair_home is not None
        vegas_home_favored = vegas_fair_home > 0.5 if has_vegas else False

        if home_edge > away_edge and home_edge > 0:
            if home_market_price < _MIN_ENTRY_PRICE:
                return None
            vegas_agrees = has_vegas and vegas_home_favored
            return NHLEdgeResult(
                market=market,
                our_fair_price=fair_home,
                market_price=home_market_price,
                edge=home_edge,
                confidence=self._classify_confidence(home_edge, research_data),
                side="YES",
                side_index=1,
                research=research_data,
                has_vegas_line=has_vegas,
                vegas_agrees=vegas_agrees,
            )
        elif away_edge > 0:
            if away_market_price < _MIN_ENTRY_PRICE:
                return None
            vegas_agrees = has_vegas and not vegas_home_favored
            return NHLEdgeResult(
                market=market,
                our_fair_price=fair_away,
                market_price=away_market_price,
                edge=away_edge,
                confidence=self._classify_confidence(away_edge, research_data),
                side="YES",
                side_index=0,
                research=research_data,
                has_vegas_line=has_vegas,
                vegas_agrees=vegas_agrees,
            )

        return None

    def _compute_power_rating(self, stats: NHLTeamStats, is_home: bool) -> float:
        """Compute NHL power rating.

        Weights:
          - Season points% (20%)
          - Last 10 form (20%)
          - Home/away split (10%)
          - xGF% from MoneyPuck (20%)
          - PP/PK differential (10%)
          - Goal differential (10%)
          - Corsi/Fenwick (10%)
        """
        gp = stats.wins + stats.losses + stats.ot_losses
        if gp == 0:
            return 0.5

        # Season win% (points-based for NHL)
        max_pts = gp * 2
        pts_pct = stats.points / max_pts if max_pts > 0 else 0.5

        # Last 10 form
        l10_total = stats.last_10_wins + stats.last_10_losses
        l10_wp = stats.last_10_wins / l10_total if l10_total > 0 else 0.5

        # Home/away split
        if is_home:
            split_total = stats.home_wins + stats.home_losses
            split_wp = stats.home_wins / split_total if split_total > 0 else pts_pct
        else:
            split_total = stats.road_wins + stats.road_losses
            split_wp = stats.road_wins / split_total if split_total > 0 else pts_pct

        # xGF% from MoneyPuck (normalized to 0-1)
        xgf_factor = stats.xgf_pct / 100.0 if stats.xgf_pct > 0 else 0.5

        # PP/PK combined factor
        pp_pk_factor = 0.5
        if stats.pp_pct > 0 and stats.pk_pct > 0:
            # PP% typically 15-30%, PK% typically 70-90%
            pp_normalized = min(1.0, stats.pp_pct / 30.0) if stats.pp_pct < 1 else min(1.0, stats.pp_pct / 30.0)
            pk_normalized = min(1.0, (stats.pk_pct - 70.0) / 20.0) if stats.pk_pct > 1 else min(1.0, (stats.pk_pct * 100 - 70.0) / 20.0)
            pp_pk_factor = (pp_normalized + max(0, pk_normalized)) / 2.0

        # Goal differential (normalized)
        diff_factor = 0.5 + (stats.goal_diff_pg / 4.0)  # NHL diffs are smaller than NBA
        diff_factor = max(0.1, min(0.9, diff_factor))

        # Corsi/Fenwick average (normalized 0-1)
        possession_factor = 0.5
        if stats.corsi_pct > 0:
            possession_factor = stats.corsi_pct / 100.0

        power = (
            0.20 * pts_pct
            + 0.20 * l10_wp
            + 0.10 * split_wp
            + 0.20 * xgf_factor
            + 0.10 * pp_pk_factor
            + 0.10 * diff_factor
            + 0.10 * possession_factor
        )

        # Home ice advantage
        if is_home:
            power += _HOME_ADVANTAGE

        return power

    def _classify_confidence(
        self,
        edge: float,
        research: NHLResearchData | None,
    ) -> Confidence:
        """Classify confidence tier based on edge and data quality."""
        data_sources = 0
        if research:
            if research.home_team.wins + research.home_team.losses > 20:
                data_sources += 1
            if research.home_team.last_10_wins + research.home_team.last_10_losses > 0:
                data_sources += 1
            if research.home_team.xgf_pct > 0:
                data_sources += 1  # MoneyPuck data available
            if research.home_team.pp_pct > 0:
                data_sources += 1

        if edge > 0.10 and data_sources >= 3:
            return Confidence.HIGH
        elif edge > 0.06 and data_sources >= 2:
            return Confidence.MEDIUM
        else:
            return Confidence.LOW
