"""Fair odds computation and edge detection."""

from __future__ import annotations

import logging
import math
from typing import Optional

from nba_agent.config import Config
from nba_agent.models import (
    Confidence,
    EdgeResult,
    H2HRecord,
    Market,
    MarketType,
    ResearchData,
    TeamStats,
)
from nba_agent.nba_research import NBAResearch, find_team_by_abbr, find_team_by_name
from nba_agent.injury_scanner import InjuryScanner
from nba_agent.utils import slugify_game

logger = logging.getLogger(__name__)

# Home court advantage in NBA: ~3-4 points, roughly 60% win rate at home
_HOME_ADVANTAGE = 0.035  # 3.5% boost to home team


def _normal_cdf(x: float) -> float:
    """Approximate the cumulative distribution function for a standard normal."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class EdgeCalculator:
    """Computes fair odds and detects edges for NBA markets."""

    def __init__(
        self,
        config: Config | None = None,
        research: NBAResearch | None = None,
        injury_scanner: InjuryScanner | None = None,
    ) -> None:
        self.config = config or Config()
        self.research = research or NBAResearch(self.config)
        self.injury_scanner = injury_scanner or InjuryScanner()

    async def evaluate(self, market: Market) -> EdgeResult | None:
        """Evaluate a market and return edge result if edge found."""
        try:
            if market.market_type == MarketType.MONEYLINE:
                return await self._evaluate_moneyline(market)
            elif market.market_type == MarketType.SPREAD:
                return await self._evaluate_spread(market)
            elif market.market_type == MarketType.TOTAL:
                return await self._evaluate_total(market)
            elif market.is_futures_market:
                return self._evaluate_futures(market)
            else:
                return None
        except Exception as e:
            logger.error("Edge calculation failed for %s: %s", market.slug, e)
            return None

    async def _evaluate_moneyline(self, market: Market) -> EdgeResult | None:
        """Calculate edge for a game moneyline market."""
        slug_parts = slugify_game(market.slug)
        if not slug_parts:
            return None

        away_abbr, home_abbr, game_date = slug_parts
        away_team = find_team_by_abbr(away_abbr)
        home_team = find_team_by_abbr(home_abbr)

        if not away_team or not home_team:
            # Try matching from outcome names
            if len(market.outcomes) >= 2:
                home_team = find_team_by_name(market.outcomes[1])
                away_team = find_team_by_name(market.outcomes[0])

        if not away_team or not home_team:
            logger.warning("Cannot resolve teams for %s", market.slug)
            return None

        home_id = home_team["id"]
        away_id = away_team["id"]

        home_stats, away_stats, h2h = self.research.build_research(home_id, away_id, game_date)
        if not home_stats or not away_stats:
            return None

        # Fetch injury info
        home_injuries = await self.injury_scanner.get_injury_summary(home_stats.team_name)
        away_injuries = await self.injury_scanner.get_injury_summary(away_stats.team_name)

        research_data = ResearchData(
            home_team=home_stats,
            away_team=away_stats,
            h2h=h2h,
            home_injuries=home_injuries[:3],
            away_injuries=away_injuries[:3],
        )

        # Compute power ratings
        home_power = self._compute_power_rating(home_stats, is_home=True)
        away_power = self._compute_power_rating(away_stats, is_home=False)

        # H2H adjustment
        if h2h and (h2h.team_a_wins + h2h.team_b_wins) > 0:
            # h2h is from home team's perspective (team_a = home in our build_research call)
            # Actually h2h.team_a_id = home_id (first arg to build_research)
            total_h2h = h2h.team_a_wins + h2h.team_b_wins
            if h2h.team_a_id == home_id:
                h2h_factor = (h2h.team_a_wins / total_h2h - 0.5) * 0.10
            else:
                h2h_factor = (h2h.team_b_wins / total_h2h - 0.5) * 0.10
            home_power += h2h_factor

        # Rest advantage
        rest_diff = home_stats.rest_days - away_stats.rest_days
        if home_stats.is_b2b:
            home_power -= 0.03
        if away_stats.is_b2b:
            away_power -= 0.03
        if rest_diff >= 2:
            home_power += 0.015
        elif rest_diff <= -2:
            away_power += 0.015

        # Injury impact (rough: if team has "out" injuries, slight penalty)
        home_out = sum(1 for inj in home_injuries if "out" in inj.lower())
        away_out = sum(1 for inj in away_injuries if "out" in inj.lower())
        home_power -= home_out * 0.01
        away_power -= away_out * 0.01

        # Fair probability for home team (outcome index 1 for home in most Polymarket slugs)
        # Polymarket convention: outcome[0] = team in first slug position (away), outcome[1] = second (home)
        fair_home = home_power / (home_power + away_power) if (home_power + away_power) > 0 else 0.5
        fair_away = 1.0 - fair_home

        # Determine which side has more edge
        # outcome_prices[0] = away price, outcome_prices[1] = home price
        if len(market.outcome_prices) < 2:
            return None

        away_market_price = market.outcome_prices[0]
        home_market_price = market.outcome_prices[1]

        away_edge = fair_away - away_market_price
        home_edge = fair_home - home_market_price

        if home_edge > away_edge and home_edge > 0:
            return EdgeResult(
                market=market,
                our_fair_price=fair_home,
                market_price=home_market_price,
                edge=home_edge,
                confidence=self._classify_confidence(home_edge, research_data),
                side="YES",
                side_index=1,
                research=research_data,
            )
        elif away_edge > 0:
            return EdgeResult(
                market=market,
                our_fair_price=fair_away,
                market_price=away_market_price,
                edge=away_edge,
                confidence=self._classify_confidence(away_edge, research_data),
                side="YES",
                side_index=0,
                research=research_data,
            )

        return None

    def _compute_power_rating(self, stats: TeamStats, is_home: bool) -> float:
        """
        Compute power rating as a weighted composite.

        Weights:
          - Season win% (25%)
          - Last 10 form (20%)
          - Home/away split (15%)
          - Off/Def rating (15%)
          - Rest advantage (10%) — handled externally
          - H2H (10%) — handled externally
          - Injury (5%) — handled externally

        Returns a value roughly centered around 0.5.
        """
        total_games = stats.wins + stats.losses
        if total_games == 0:
            return 0.5

        # Season win%
        season_wp = stats.win_pct  # 0.0 to 1.0

        # Last 10 form
        l10_total = stats.last_10_wins + stats.last_10_losses
        l10_wp = stats.last_10_wins / l10_total if l10_total > 0 else 0.5

        # Home/away split
        if is_home:
            split_total = stats.home_wins + stats.home_losses
            split_wp = stats.home_wins / split_total if split_total > 0 else season_wp
        else:
            split_total = stats.road_wins + stats.road_losses
            split_wp = stats.road_wins / split_total if split_total > 0 else season_wp

        # Offensive/Defensive rating composite
        # Net rating typically ranges from -15 to +15; normalize to 0-1 range
        net = stats.net_rating
        rating_factor = 0.5 + (net / 30.0)  # -15 → 0.0, 0 → 0.5, +15 → 1.0
        rating_factor = max(0.1, min(0.9, rating_factor))

        # Point differential factor (normalized 0-1)
        diff_factor = 0.5 + (stats.diff_points_pg / 20.0)  # -10 → 0.0, 0 → 0.5, +10 → 1.0
        diff_factor = max(0.1, min(0.9, diff_factor))

        # Weighted composite (sums to 0.75 — rest/H2H/injury are applied externally)
        power = (
            0.25 * season_wp
            + 0.20 * l10_wp
            + 0.15 * split_wp
            + 0.15 * rating_factor
        )
        # Remaining 25% split: streak/momentum (10%), point diff (15%)
        streak_factor = 0.5
        streak = stats.current_streak
        if streak:
            try:
                if streak.startswith("W"):
                    streak_n = int(streak[1:]) if len(streak) > 1 else 1
                    streak_factor = min(0.5 + streak_n * 0.03, 0.8)
                elif streak.startswith("L"):
                    streak_n = int(streak[1:]) if len(streak) > 1 else 1
                    streak_factor = max(0.5 - streak_n * 0.03, 0.2)
            except (ValueError, IndexError):
                pass

        power += 0.10 * streak_factor + 0.15 * diff_factor

        # Home court advantage
        if is_home:
            power += _HOME_ADVANTAGE

        return power

    async def _evaluate_spread(self, market: Market) -> EdgeResult | None:
        """Calculate edge for a spread market."""
        slug_parts = slugify_game(market.slug)
        if not slug_parts:
            return None

        away_abbr, home_abbr, game_date = slug_parts
        away_team = find_team_by_abbr(away_abbr)
        home_team = find_team_by_abbr(home_abbr)

        if not away_team or not home_team:
            return None

        home_stats, away_stats, h2h = self.research.build_research(
            home_team["id"], away_team["id"], game_date
        )
        if not home_stats or not away_stats:
            return None

        research_data = ResearchData(home_team=home_stats, away_team=away_stats, h2h=h2h)

        # Extract spread from slug: e.g., "nba-lac-dal-2026-03-21-spread-away-6pt5"
        slug_lower = market.slug.lower()
        spread_val = self._extract_spread_from_slug(slug_lower)
        if spread_val is None:
            return None

        # Expected margin = home points - away points
        home_diff = home_stats.diff_points_pg
        away_diff = away_stats.diff_points_pg
        expected_margin = (home_diff - away_diff) / 2.0 + 3.0  # Home advantage ~3pts

        # Adjust for rest
        if home_stats.is_b2b:
            expected_margin -= 3.0
        if away_stats.is_b2b:
            expected_margin += 3.0

        # Is the spread on the away team?
        is_away_spread = "away" in slug_lower

        if is_away_spread:
            # Away team getting points: away + spread_val
            # Probability away team covers: P(away_score + spread > home_score)
            # = P(margin < spread_val) where margin = home - away
            prob_cover = _normal_cdf((spread_val - expected_margin) / 12.0)
        else:
            # Home team giving points
            prob_cover = _normal_cdf((expected_margin - spread_val) / 12.0)

        if len(market.outcome_prices) < 2:
            return None

        # Find the side with edge
        cover_price = market.outcome_prices[0]
        no_cover_price = market.outcome_prices[1]

        cover_edge = prob_cover - cover_price
        no_cover_edge = (1.0 - prob_cover) - no_cover_price

        if cover_edge > no_cover_edge and cover_edge > 0:
            return EdgeResult(
                market=market,
                our_fair_price=prob_cover,
                market_price=cover_price,
                edge=cover_edge,
                confidence=self._classify_confidence(cover_edge, research_data),
                side="YES",
                side_index=0,
                research=research_data,
            )
        elif no_cover_edge > 0:
            return EdgeResult(
                market=market,
                our_fair_price=1.0 - prob_cover,
                market_price=no_cover_price,
                edge=no_cover_edge,
                confidence=self._classify_confidence(no_cover_edge, research_data),
                side="YES",
                side_index=1,
                research=research_data,
            )

        return None

    async def _evaluate_total(self, market: Market) -> EdgeResult | None:
        """Calculate edge for a totals (O/U) market."""
        slug_parts = slugify_game(market.slug)
        if not slug_parts:
            return None

        away_abbr, home_abbr, game_date = slug_parts
        away_team = find_team_by_abbr(away_abbr)
        home_team = find_team_by_abbr(home_abbr)

        if not away_team or not home_team:
            return None

        home_stats, away_stats, _ = self.research.build_research(
            home_team["id"], away_team["id"], game_date
        )
        if not home_stats or not away_stats:
            return None

        research_data = ResearchData(home_team=home_stats, away_team=away_stats)

        # Extract total line from slug: e.g., "nba-lac-dal-2026-03-21-total-233pt5"
        total_line = self._extract_total_from_slug(market.slug.lower())
        if total_line is None:
            return None

        # Estimate expected total using pace and ratings
        # Simple: average of both teams' points + opponent points
        if home_stats.pace > 0 and away_stats.pace > 0:
            avg_pace = (home_stats.pace + away_stats.pace) / 2.0
            # Possessions-based estimate
            home_expected = (home_stats.off_rating * avg_pace / 100.0) if home_stats.off_rating > 0 else home_stats.points_pg
            away_expected = (away_stats.off_rating * avg_pace / 100.0) if away_stats.off_rating > 0 else away_stats.points_pg
            expected_total = home_expected + away_expected
        else:
            expected_total = home_stats.points_pg + away_stats.points_pg

        # Probability of going over
        prob_over = 1.0 - _normal_cdf((total_line - expected_total) / 15.0)

        if len(market.outcome_prices) < 2:
            return None

        over_price = market.outcome_prices[0]
        under_price = market.outcome_prices[1]

        over_edge = prob_over - over_price
        under_edge = (1.0 - prob_over) - under_price

        if over_edge > under_edge and over_edge > 0:
            return EdgeResult(
                market=market,
                our_fair_price=prob_over,
                market_price=over_price,
                edge=over_edge,
                confidence=self._classify_confidence(over_edge, research_data),
                side="YES",
                side_index=0,
                research=research_data,
            )
        elif under_edge > 0:
            return EdgeResult(
                market=market,
                our_fair_price=1.0 - prob_over,
                market_price=under_price,
                edge=under_edge,
                confidence=self._classify_confidence(under_edge, research_data),
                side="YES",
                side_index=1,
                research=research_data,
            )

        return None

    def _evaluate_futures(self, market: Market) -> EdgeResult | None:
        """Evaluate futures markets (championship, MVP, conference)."""
        # For futures, we use a simpler model based on standings
        standings = self.research.get_standings()
        if not standings:
            return None

        # Try to match the market question/outcome to a team
        for i, outcome in enumerate(market.outcomes):
            team = find_team_by_name(outcome)
            if not team:
                continue

            team_id = team["id"]
            stats_row = None
            for s in standings:
                if s.get("TeamID") == team_id:
                    stats_row = s
                    break

            if not stats_row:
                continue

            fair_price = self._estimate_futures_probability(
                stats_row, market.market_type, standings
            )
            if fair_price is None:
                continue

            market_price = market.outcome_prices[i] if i < len(market.outcome_prices) else 0.5
            edge = fair_price - market_price

            if edge >= market.min_edge:
                return EdgeResult(
                    market=market,
                    our_fair_price=fair_price,
                    market_price=market_price,
                    edge=edge,
                    confidence=self._classify_confidence(edge, None),
                    side="YES",
                    side_index=i,
                )

        return None

    def _estimate_futures_probability(
        self,
        team_row: dict,
        market_type: MarketType,
        standings: list[dict],
    ) -> float | None:
        """Estimate fair probability for a futures market."""
        win_pct = float(team_row.get("WinPCT", 0))
        conf = team_row.get("Conference", "")
        playoff_rank = int(team_row.get("PlayoffRank", 16))

        if market_type == MarketType.CHAMPIONSHIP:
            # Top seed historically wins ~25-30%
            if playoff_rank <= 1:
                base = 0.25
            elif playoff_rank <= 2:
                base = 0.15
            elif playoff_rank <= 4:
                base = 0.10
            elif playoff_rank <= 6:
                base = 0.05
            elif playoff_rank <= 8:
                base = 0.02
            else:
                base = 0.005
            # Adjust by win percentage differential from league average
            base *= (1.0 + (win_pct - 0.5) * 2.0)
            return max(0.01, min(0.40, base))

        elif market_type == MarketType.CONFERENCE:
            # Conference winner
            if playoff_rank <= 1:
                base = 0.35
            elif playoff_rank <= 2:
                base = 0.22
            elif playoff_rank <= 3:
                base = 0.15
            elif playoff_rank <= 4:
                base = 0.10
            elif playoff_rank <= 6:
                base = 0.06
            elif playoff_rank <= 8:
                base = 0.03
            else:
                base = 0.005
            base *= (1.0 + (win_pct - 0.5) * 1.5)
            return max(0.01, min(0.50, base))

        elif market_type == MarketType.MVP:
            # MVP is much harder to model — based on team record + PPG proxy
            # Without individual stats, use a basic model
            if playoff_rank <= 1 and win_pct > 0.65:
                return 0.20
            elif playoff_rank <= 2 and win_pct > 0.60:
                return 0.12
            elif playoff_rank <= 4:
                return 0.06
            else:
                return 0.02

        return None

    def _extract_spread_from_slug(self, slug: str) -> float | None:
        """Extract spread value from slug like ...-spread-away-6pt5."""
        try:
            parts = slug.split("-spread-")
            if len(parts) < 2:
                return None
            spread_part = parts[1]
            # Remove 'away-' or 'home-' prefix
            for prefix in ("away-", "home-"):
                if spread_part.startswith(prefix):
                    spread_part = spread_part[len(prefix):]
            # Convert 6pt5 → 6.5
            spread_part = spread_part.replace("pt", ".")
            return float(spread_part)
        except (ValueError, IndexError):
            return None

    def _extract_total_from_slug(self, slug: str) -> float | None:
        """Extract total line from slug like ...-total-233pt5."""
        try:
            parts = slug.split("-total-")
            if len(parts) < 2:
                return None
            total_part = parts[1]
            total_part = total_part.replace("pt", ".")
            return float(total_part)
        except (ValueError, IndexError):
            return None

    def _classify_confidence(
        self,
        edge: float,
        research: ResearchData | None,
    ) -> Confidence:
        """Classify confidence tier based on edge and data agreement."""
        data_sources = 0
        if research:
            if research.home_team.wins + research.home_team.losses > 20:
                data_sources += 1
            if research.home_team.last_10_wins + research.home_team.last_10_losses > 0:
                data_sources += 1
            if research.h2h and (research.h2h.team_a_wins + research.h2h.team_b_wins) > 0:
                data_sources += 1
            if research.home_team.off_rating > 0:
                data_sources += 1
            if research.home_team.rest_days >= 0:
                data_sources += 1

        if edge > 0.15 and data_sources >= 3:
            return Confidence.HIGH
        elif edge > 0.10 and data_sources >= 2:
            return Confidence.MEDIUM
        else:
            return Confidence.LOW
