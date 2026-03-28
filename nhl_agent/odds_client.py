"""The Odds API integration for NHL Vegas lines.

Fetches moneyline odds from 40+ bookmakers for upcoming NHL games.
Same pattern as nba_agent/odds_api.py but with sport=icehockey_nhl.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from nhl_agent.config import NHLConfig
from nhl_agent.nhl_research import find_nhl_team_by_name

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.the-odds-api.com/v4"
_SPORT = "icehockey_nhl"
_REGIONS = "us,us2,eu"
_MARKETS = "h2h"

# Sharp bookmakers
_SHARP_BOOKS = {"pinnacle", "betfair_ex_eu", "matchbook"}
_MAJOR_BOOKS = {"fanduel", "draftkings", "betmgm", "caesars", "bovada"}


def _american_to_decimal(american: float) -> float:
    if american >= 0:
        return 1.0 + (american / 100.0)
    else:
        return 1.0 + (100.0 / abs(american))


def _decimal_to_implied(decimal_odds: float) -> float:
    if decimal_odds <= 0:
        return 0.0
    return 1.0 / decimal_odds


class NHLVegasLine:
    """Consensus Vegas line for one side of an NHL bet."""

    def __init__(
        self,
        team_name: str,
        consensus_prob: float,
        sharp_prob: float | None,
        num_books: int,
    ):
        self.team_name = team_name
        self.consensus_prob = consensus_prob
        self.sharp_prob = sharp_prob
        self.num_books = num_books

    def __repr__(self) -> str:
        sharp = f" sharp={self.sharp_prob:.1%}" if self.sharp_prob else ""
        return f"NHLVegasLine({self.team_name}: consensus={self.consensus_prob:.1%}{sharp} books={self.num_books})"


class NHLGameOdds:
    """All Vegas odds for one NHL game."""

    def __init__(
        self,
        event_id: str,
        home_team: str,
        away_team: str,
        commence_time: str,
        home_ml: NHLVegasLine | None = None,
        away_ml: NHLVegasLine | None = None,
    ):
        self.event_id = event_id
        self.home_team = home_team
        self.away_team = away_team
        self.commence_time = commence_time
        self.home_ml = home_ml
        self.away_ml = away_ml


class NHLOddsClient:
    """Fetches and caches NHL Vegas odds from The Odds API."""

    def __init__(self, config: NHLConfig | None = None) -> None:
        self.config = config or NHLConfig()
        self.api_key = self.config.ODDS_API_KEY
        self._cache: dict[str, NHLGameOdds] = {}
        self._cache_ts: datetime | None = None
        self._cache_ttl = timedelta(minutes=8)
        self._remaining_requests: int | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def get_nhl_odds(self) -> list[NHLGameOdds]:
        """Fetch current NHL odds from all bookmakers. Cached for 8 minutes."""
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

            self._remaining_requests = int(resp.headers.get("x-requests-remaining", -1))
            if self._remaining_requests >= 0:
                logger.info("NHL Odds API: %d credits remaining", self._remaining_requests)

            events = resp.json()
            odds_list = []
            for event in events:
                game_odds = self._parse_event(event)
                if game_odds:
                    odds_list.append(game_odds)
                    self._cache[game_odds.event_id] = game_odds

            self._cache_ts = now
            logger.info("NHL Odds API: loaded lines for %d NHL games", len(odds_list))
            return odds_list

        except Exception as e:
            logger.error("NHL Odds API fetch failed: %s", e)
            return list(self._cache.values())

    def find_game_odds(self, home_team: str, away_team: str) -> NHLGameOdds | None:
        """Find Vegas odds for a specific NHL matchup."""
        odds = self.get_nhl_odds()
        home_lower = home_team.lower()
        away_lower = away_team.lower()

        for game in odds:
            gh = game.home_team.lower()
            ga = game.away_team.lower()
            home_match = home_lower in gh or gh in home_lower
            away_match = away_lower in ga or ga in away_lower
            if home_match and away_match:
                return game

        return None

    def _parse_event(self, event: dict) -> NHLGameOdds | None:
        """Parse a single event from the API response."""
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return None

        home = event.get("home_team", "")
        away = event.get("away_team", "")

        game = NHLGameOdds(
            event_id=event.get("id", ""),
            home_team=home,
            away_team=away,
            commence_time=event.get("commence_time", ""),
        )

        h2h_home: list[tuple[float, bool]] = []
        h2h_away: list[tuple[float, bool]] = []

        for bm in bookmakers:
            bm_key = bm.get("key", "")
            is_sharp = bm_key in _SHARP_BOOKS

            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for out in market.get("outcomes", []):
                    price = out.get("price", 0)
                    dec = _american_to_decimal(price)
                    prob = _decimal_to_implied(dec)
                    if out.get("name", "") == home:
                        h2h_home.append((prob, is_sharp))
                    elif out.get("name", "") == away:
                        h2h_away.append((prob, is_sharp))

        if h2h_home:
            game.home_ml = self._build_line(home, h2h_home)
        if h2h_away:
            game.away_ml = self._build_line(away, h2h_away)

        return game

    def _build_line(self, name: str, probs: list[tuple[float, bool]]) -> NHLVegasLine:
        """Build a VegasLine from collected probabilities."""
        all_probs = [p[0] for p in probs]
        sharp_probs = [p[0] for p in probs if p[1]]

        consensus = sum(all_probs) / len(all_probs) if all_probs else 0.5
        sharp = sum(sharp_probs) / len(sharp_probs) if sharp_probs else None

        return NHLVegasLine(
            team_name=name,
            consensus_prob=consensus,
            sharp_prob=sharp,
            num_books=len(all_probs),
        )
