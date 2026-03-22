"""The Odds API integration — Vegas odds comparison for edge detection.

Fetches moneyline, spread, and totals odds from 40+ bookmakers for
upcoming NBA games. Provides consensus (average) and sharp (Pinnacle/
FanDuel) lines to compare against Polymarket prices.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from nba_agent.config import Config

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.the-odds-api.com/v4"
_SPORT = "basketball_nba"
_REGIONS = "us,us2,eu"
_MARKETS = "h2h,spreads,totals"

# Sharp bookmakers (most accurate lines in the industry)
_SHARP_BOOKS = {"pinnacle", "betfair_ex_eu", "matchbook"}
# Major US books (high volume, decent lines)
_MAJOR_BOOKS = {"fanduel", "draftkings", "betmgm", "caesars", "bovada"}


def _american_to_decimal(american: float) -> float:
    """Convert American odds to decimal odds."""
    if american >= 0:
        return 1.0 + (american / 100.0)
    else:
        return 1.0 + (100.0 / abs(american))


def _decimal_to_implied(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if decimal_odds <= 0:
        return 0.0
    return 1.0 / decimal_odds


class VegasLine:
    """Consensus Vegas line for one side of a bet."""

    def __init__(
        self,
        team_name: str,
        consensus_prob: float,
        sharp_prob: float | None,
        num_books: int,
        spread: float | None = None,
        total: float | None = None,
    ):
        self.team_name = team_name
        self.consensus_prob = consensus_prob  # Average implied prob across all books
        self.sharp_prob = sharp_prob          # Pinnacle/sharp implied prob (most accurate)
        self.num_books = num_books
        self.spread = spread
        self.total = total

    def __repr__(self) -> str:
        sharp = f" sharp={self.sharp_prob:.1%}" if self.sharp_prob else ""
        return f"VegasLine({self.team_name}: consensus={self.consensus_prob:.1%}{sharp} books={self.num_books})"


class GameOdds:
    """All Vegas odds for one NBA game."""

    def __init__(
        self,
        event_id: str,
        home_team: str,
        away_team: str,
        commence_time: str,
        home_ml: VegasLine | None = None,
        away_ml: VegasLine | None = None,
        home_spread: VegasLine | None = None,
        away_spread: VegasLine | None = None,
        over: VegasLine | None = None,
        under: VegasLine | None = None,
    ):
        self.event_id = event_id
        self.home_team = home_team
        self.away_team = away_team
        self.commence_time = commence_time
        self.home_ml = home_ml
        self.away_ml = away_ml
        self.home_spread = home_spread
        self.away_spread = away_spread
        self.over = over
        self.under = under


class OddsAPI:
    """Fetches and caches Vegas odds from The Odds API."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.api_key = self.config.ODDS_API_KEY
        self._cache: dict[str, GameOdds] = {}
        self._cache_ts: datetime | None = None
        self._cache_ttl = timedelta(minutes=8)  # Refresh before each 10-min scan
        self._remaining_requests: int | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def get_nba_odds(self) -> list[GameOdds]:
        """Fetch current NBA odds from all bookmakers. Cached for 8 minutes."""
        if not self.is_configured:
            return []

        now = datetime.now(timezone.utc)
        if self._cache and self._cache_ts and (now - self._cache_ts) < self._cache_ttl:
            return list(self._cache.values())

        try:
            resp = httpx.get(
                f"{_BASE_URL}/sports/{_SPORT}/odds",
                params={
                    "apiKey": self.api_key,
                    "regions": _REGIONS,
                    "markets": _MARKETS,
                    "oddsFormat": "american",
                },
                timeout=20.0,
            )
            resp.raise_for_status()

            # Track remaining API credits
            self._remaining_requests = int(resp.headers.get("x-requests-remaining", -1))
            if self._remaining_requests >= 0:
                logger.info("Odds API: %d credits remaining", self._remaining_requests)

            events = resp.json()
            odds_list = []
            for event in events:
                game_odds = self._parse_event(event)
                if game_odds:
                    odds_list.append(game_odds)
                    self._cache[game_odds.event_id] = game_odds

            self._cache_ts = now
            logger.info("Odds API: loaded lines for %d NBA games from %d events",
                        len(odds_list), len(events))
            return odds_list

        except Exception as e:
            logger.error("Odds API fetch failed: %s", e)
            return list(self._cache.values())  # Return stale cache

    def find_game_odds(self, home_team: str, away_team: str) -> GameOdds | None:
        """Find Vegas odds for a specific matchup by team name."""
        odds = self.get_nba_odds()
        home_lower = home_team.lower()
        away_lower = away_team.lower()

        for game in odds:
            gh = game.home_team.lower()
            ga = game.away_team.lower()
            # Match on full name or partial (e.g. "Thunder" in "Oklahoma City Thunder")
            home_match = home_lower in gh or gh in home_lower
            away_match = away_lower in ga or ga in away_lower
            if home_match and away_match:
                return game

        return None

    def _parse_event(self, event: dict) -> GameOdds | None:
        """Parse a single event from the API response."""
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return None

        home = event.get("home_team", "")
        away = event.get("away_team", "")

        game = GameOdds(
            event_id=event.get("id", ""),
            home_team=home,
            away_team=away,
            commence_time=event.get("commence_time", ""),
        )

        # Collect odds from all bookmakers
        h2h_home: list[tuple[float, bool]] = []  # (implied_prob, is_sharp)
        h2h_away: list[tuple[float, bool]] = []
        spread_home: list[tuple[float, float, bool]] = []  # (prob, point, is_sharp)
        spread_away: list[tuple[float, float, bool]] = []
        total_over: list[tuple[float, float, bool]] = []   # (prob, line, is_sharp)
        total_under: list[tuple[float, float, bool]] = []

        for bm in bookmakers:
            bm_key = bm.get("key", "")
            is_sharp = bm_key in _SHARP_BOOKS
            is_major = bm_key in _MAJOR_BOOKS

            for market in bm.get("markets", []):
                mkey = market.get("key", "")
                outcomes = market.get("outcomes", [])

                if mkey == "h2h":
                    for out in outcomes:
                        price = out.get("price", 0)
                        dec = _american_to_decimal(price)
                        prob = _decimal_to_implied(dec)
                        if out.get("name", "") == home:
                            h2h_home.append((prob, is_sharp))
                        elif out.get("name", "") == away:
                            h2h_away.append((prob, is_sharp))

                elif mkey == "spreads":
                    for out in outcomes:
                        price = out.get("price", 0)
                        point = out.get("point", 0)
                        dec = _american_to_decimal(price)
                        prob = _decimal_to_implied(dec)
                        if out.get("name", "") == home:
                            spread_home.append((prob, point, is_sharp))
                        elif out.get("name", "") == away:
                            spread_away.append((prob, point, is_sharp))

                elif mkey == "totals":
                    for out in outcomes:
                        price = out.get("price", 0)
                        point = out.get("point", 0)
                        dec = _american_to_decimal(price)
                        prob = _decimal_to_implied(dec)
                        name = out.get("name", "").lower()
                        if name == "over":
                            total_over.append((prob, point, is_sharp))
                        elif name == "under":
                            total_under.append((prob, point, is_sharp))

        # Build VegasLine objects
        if h2h_home:
            game.home_ml = self._build_line(home, h2h_home)
        if h2h_away:
            game.away_ml = self._build_line(away, h2h_away)
        if spread_home:
            avg_point = sum(pt for _, pt, _ in spread_home) / len(spread_home)
            game.home_spread = self._build_line(home, [(prob, is_s) for prob, _, is_s in spread_home],
                                                spread=avg_point)
        if spread_away:
            avg_point = sum(pt for _, pt, _ in spread_away) / len(spread_away)
            game.away_spread = self._build_line(away, [(prob, is_s) for prob, _, is_s in spread_away],
                                                spread=avg_point)
        if total_over:
            avg_line = sum(pt for _, pt, _ in total_over) / len(total_over)
            game.over = self._build_line("Over", [(prob, is_s) for prob, _, is_s in total_over],
                                         total=avg_line)
        if total_under:
            avg_line = sum(pt for _, pt, _ in total_under) / len(total_under)
            game.under = self._build_line("Under", [(prob, is_s) for prob, _, is_s in total_under],
                                          total=avg_line)

        return game

    def _build_line(
        self,
        name: str,
        probs: list[tuple[float, ...]],
        spread: float | None = None,
        total: float | None = None,
    ) -> VegasLine:
        """Build a VegasLine from collected probabilities."""
        all_probs = [p[0] for p in probs]
        sharp_probs = [p[0] for p in probs if p[-1]]  # Last element is is_sharp flag

        consensus = sum(all_probs) / len(all_probs) if all_probs else 0.5
        sharp = sum(sharp_probs) / len(sharp_probs) if sharp_probs else None

        # Remove vig (normalize so home + away = 1.0)
        # This is a rough devig — proper would be per-book
        # We'll handle devig at comparison time instead

        return VegasLine(
            team_name=name,
            consensus_prob=consensus,
            sharp_prob=sharp,
            num_books=len(all_probs),
            spread=spread,
            total=total,
        )
