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
            elif market.market_type == NHLMarketType.FUTURES:
                return await self._evaluate_futures(market)
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

    async def _evaluate_futures(self, market: NHLMarket) -> NHLEdgeResult | None:
        """Calculate edge for an NHL Stanley Cup futures market.

        Blend: 70% Vegas / 30% model (standings pace + xGF%).
        Min edge: 6%. Confidence: HIGH >15%, MEDIUM >10%, LOW >6%.
        """
        # Parse team name from question
        # e.g. "Will the Carolina Hurricanes win the 2026 NHL Stanley Cup?"
        team_name = self._parse_futures_team(market.question)
        if not team_name:
            logger.debug("Could not parse team from futures question: %s", market.question)
            return None

        team_info = find_nhl_team_by_name(team_name)
        if not team_info:
            logger.debug("Could not find NHL team for: %s", team_name)
            return None

        # Get Polymarket YES price (first outcome is typically YES)
        if not market.outcome_prices:
            return None
        yes_index = 0
        for i, outcome in enumerate(market.outcomes):
            if outcome.lower() == "yes":
                yes_index = i
                break
        if yes_index >= len(market.outcome_prices):
            return None
        poly_price = market.outcome_prices[yes_index]

        # Get Vegas Stanley Cup odds for this team
        cup_odds = self.odds_client.get_stanley_cup_odds()
        vegas_prob = self._match_futures_team(team_info, cup_odds)

        # Get model component: standings points pace + xGF%
        model_prob = await self._compute_futures_model_prob(team_info["abbr"])

        # Blend: 70% Vegas, 30% model (Vegas more reliable for futures)
        if vegas_prob is not None and vegas_prob > 0:
            fair_prob = 0.70 * vegas_prob + 0.30 * model_prob
            has_vegas = True
        else:
            # No Vegas data — use model only (less reliable)
            fair_prob = model_prob
            has_vegas = False

        edge = fair_prob - poly_price
        if edge <= 0:
            return None

        # Vegas agrees if Vegas prob > Polymarket price
        vegas_agrees = has_vegas and vegas_prob is not None and vegas_prob > poly_price

        confidence = self._classify_futures_confidence(edge, has_vegas, vegas_agrees)

        logger.info(
            "NHL FUTURES: %s Stanley Cup | edge=%.1f%% fair=%.3f market=%.3f vegas=%.3f",
            team_info["name"], edge * 100, fair_prob, poly_price,
            vegas_prob if vegas_prob else 0,
        )

        return NHLEdgeResult(
            market=market,
            our_fair_price=fair_prob,
            market_price=poly_price,
            edge=edge,
            confidence=confidence,
            side="YES",
            side_index=yes_index,
            research=None,
            has_vegas_line=has_vegas,
            vegas_agrees=vegas_agrees,
        )

    def _parse_futures_team(self, question: str) -> str | None:
        """Extract team name from a futures market question.

        Examples:
        - "Will the Carolina Hurricanes win the 2026 NHL Stanley Cup?" → "Carolina Hurricanes"
        - "Carolina Hurricanes" (bare outcome name) → "Carolina Hurricanes"
        """
        import re
        # Pattern: "Will the <TEAM> win ..."
        m = re.search(r"[Ww]ill the (.+?) win", question)
        if m:
            return m.group(1).strip()
        # Pattern: "Will <TEAM> win ..."
        m = re.search(r"[Ww]ill (.+?) win", question)
        if m:
            return m.group(1).strip()
        # Fallback: just try the whole question as a team name
        return question.strip() if len(question) < 40 else None

    def _match_futures_team(
        self, team_info: dict, cup_odds: dict[str, float]
    ) -> float | None:
        """Match an NHL team to the Vegas futures odds dict (lowercase keys)."""
        if not cup_odds:
            return None

        team_name = team_info["name"].lower()
        city = team_info.get("city", "").lower()
        nickname = team_info["name"].split()[-1].lower()

        # Direct match
        if team_name in cup_odds:
            return cup_odds[team_name]

        # Partial match: check if any key contains the team name or vice versa
        for key, prob in cup_odds.items():
            if team_name in key or key in team_name:
                return prob
            if nickname in key or city in key:
                return prob

        return None

    async def _compute_futures_model_prob(self, team_abbr: str) -> float:
        """Compute model probability for a team winning the Stanley Cup.

        Uses current standings points pace and MoneyPuck xGF%.
        Returns a rough probability estimate (not calibrated, blended with Vegas).
        """
        try:
            stats = await self.research.get_team_stats(team_abbr)
            if not stats:
                return 1 / 32  # League average for 32 teams

            gp = stats.wins + stats.losses + stats.ot_losses
            if gp < 10:
                return 1 / 32

            # Points pace: project to 82 games
            max_pts = gp * 2
            pts_pct = stats.points / max_pts if max_pts > 0 else 0.5
            projected_pts = pts_pct * 164  # max 164 points in 82 games

            # xGF% as quality signal (50% is average)
            xgf_factor = stats.xgf_pct / 100.0 if stats.xgf_pct > 0 else 0.5

            # Convert projected points to rough strength rating
            # ~120+ pts = contender, ~100 = average, ~80 = bottom
            # Normalize: (projected - 70) / (130 - 70) clamped to 0.05-0.95
            pts_strength = max(0.05, min(0.95, (projected_pts - 70) / 60))

            # Combine points pace (60%) and xGF% (40%)
            strength = 0.60 * pts_strength + 0.40 * xgf_factor

            # Convert strength to championship probability
            # Top team ~15%, average ~3%, bad team ~0.5%
            # Use exponential scaling: prob = A * exp(B * strength) / sum
            import math
            raw_prob = math.exp(3.0 * strength)

            # Approximate normalization: assume 32 teams with average strength 0.5
            avg_raw = math.exp(3.0 * 0.5)
            normalized = raw_prob / (32 * avg_raw)

            # Clamp to reasonable range
            return max(0.005, min(0.25, normalized))

        except Exception as e:
            logger.warning("Futures model prob failed for %s: %s", team_abbr, e)
            return 1 / 32

    def _classify_futures_confidence(
        self, edge: float, has_vegas: bool, vegas_agrees: bool
    ) -> Confidence:
        """Classify confidence for futures bets."""
        if edge > 0.15 and has_vegas and vegas_agrees:
            return Confidence.HIGH
        elif edge > 0.10:
            return Confidence.MEDIUM
        else:
            return Confidence.LOW

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
