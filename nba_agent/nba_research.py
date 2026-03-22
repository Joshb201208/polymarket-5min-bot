"""NBA stats engine — standings, game logs, H2H, advanced stats."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from nba_api.stats.endpoints import (
    leaguestandings,
    teamgamelog,
    leaguegamefinder,
    leaguedashteamstats,
)
from nba_api.stats.static import teams as nba_teams_static

from nba_agent.config import Config
from nba_agent.models import TeamStats, H2HRecord
from nba_agent.utils import parse_record

logger = logging.getLogger(__name__)

# Rate limit delay between nba_api calls
_API_DELAY = 1.0

# Custom headers required by stats.nba.com — without these, cloud/VPS IPs get empty responses
_NBA_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Host": "stats.nba.com",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}


def _sleep() -> None:
    time.sleep(_API_DELAY)


# Build team lookup tables
_ALL_TEAMS = nba_teams_static.get_teams()
_TEAM_BY_ABBR: dict[str, dict] = {t["abbreviation"].upper(): t for t in _ALL_TEAMS}
_TEAM_BY_NAME: dict[str, dict] = {}
for _t in _ALL_TEAMS:
    _TEAM_BY_NAME[_t["full_name"].lower()] = _t
    _TEAM_BY_NAME[_t["nickname"].lower()] = _t
    _TEAM_BY_NAME[_t["city"].lower() + " " + _t["nickname"].lower()] = _t

# Common Polymarket abbreviations to nba_api abbreviations
_ABBR_MAP: dict[str, str] = {
    "BKN": "BKN", "BRK": "BKN",
    "CHA": "CHA", "CHO": "CHA",
    "GS": "GSW", "GSW": "GSW",
    "NY": "NYK", "NYK": "NYK",
    "NO": "NOP", "NOP": "NOP",
    "SA": "SAS", "SAS": "SAS",
    "PHX": "PHX", "PHO": "PHX",
    "WSH": "WAS", "WAS": "WAS",
    "UTA": "UTA", "UTAH": "UTA",
    "OKC": "OKC",
    "POR": "POR",
    "LAL": "LAL",
    "LAC": "LAC",
    "MEM": "MEM",
    "DEN": "DEN",
    "MIL": "MIL",
    "IND": "IND",
    "ATL": "ATL",
    "BOS": "BOS",
    "CHI": "CHI",
    "CLE": "CLE",
    "DAL": "DAL",
    "DET": "DET",
    "HOU": "HOU",
    "MIA": "MIA",
    "MIN": "MIN",
    "ORL": "ORL",
    "PHI": "PHI",
    "SAC": "SAC",
    "TOR": "TOR",
}


def resolve_team_abbr(abbr: str) -> str:
    """Map a Polymarket slug abbreviation to the nba_api abbreviation."""
    return _ABBR_MAP.get(abbr.upper(), abbr.upper())


def find_team_by_abbr(abbr: str) -> dict | None:
    """Look up an nba_api team by abbreviation."""
    resolved = resolve_team_abbr(abbr)
    return _TEAM_BY_ABBR.get(resolved)


def find_team_by_name(name: str) -> dict | None:
    """Look up an nba_api team by name (fuzzy)."""
    name_lower = name.lower().strip()
    # Exact match
    if name_lower in _TEAM_BY_NAME:
        return _TEAM_BY_NAME[name_lower]
    # Partial match
    for key, team in _TEAM_BY_NAME.items():
        if name_lower in key or key in name_lower:
            return team
    return None


class NBAResearch:
    """Fetches and caches NBA statistics."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.season = self.config.NBA_SEASON
        self._standings_cache: list[dict] | None = None
        self._standings_ts: datetime | None = None
        self._standings_fail_ts: datetime | None = None  # Prevent rapid retries after failure
        self._advanced_cache: dict[int, dict] | None = None
        self._advanced_ts: datetime | None = None
        self._cache_ttl = timedelta(hours=2)
        self._fail_cooldown = timedelta(minutes=10)  # Wait 10 min before retrying after failure

    def get_standings(self) -> list[dict]:
        """Fetch league standings, cached for 2 hours. Retries up to 3 times."""
        now = datetime.now(timezone.utc)
        if self._standings_cache and self._standings_ts and (now - self._standings_ts) < self._cache_ttl:
            return self._standings_cache

        # Don't hammer NBA.com if we just failed — wait 10 min
        if self._standings_fail_ts and (now - self._standings_fail_ts) < self._fail_cooldown:
            return self._standings_cache or []

        last_error = None
        for attempt in range(3):
            try:
                standings = leaguestandings.LeagueStandings(
                    season=self.season,
                    season_type="Regular Season",
                    headers=_NBA_HEADERS,
                    timeout=60,
                )
                _sleep()
                df = standings.get_data_frames()[0]
                records = df.to_dict("records")
                self._standings_cache = records
                self._standings_ts = now
                logger.info("Fetched standings: %d teams", len(records))
                return records
            except Exception as e:
                last_error = e
                wait = (attempt + 1) * 2
                logger.warning("Standings fetch attempt %d/3 failed: %s — retrying in %ds", attempt + 1, e, wait)
                time.sleep(wait)

        self._standings_fail_ts = now
        logger.error("All 3 standings fetch attempts failed. Last error: %s", last_error)
        if not self._standings_cache:
            logger.error("No cached standings available — NBA stats may be blocking this IP. "
                         "The bot will continue scanning markets but cannot calculate edges. "
                         "Will retry in 10 minutes.")
        return self._standings_cache or []

    def get_team_stats(self, team_id: int) -> TeamStats | None:
        """Build TeamStats from standings + advanced data."""
        standings = self.get_standings()
        row = None
        for s in standings:
            if s.get("TeamID") == team_id:
                row = s
                break

        if not row:
            logger.warning("Team ID %d not found in standings", team_id)
            return None

        home_w, home_l = parse_record(str(row.get("HOME", "0-0")))
        road_w, road_l = parse_record(str(row.get("ROAD", "0-0")))
        l10_w, l10_l = parse_record(str(row.get("L10", "0-0")))

        ts = TeamStats(
            team_id=team_id,
            team_name=f"{row.get('TeamCity', '')} {row.get('TeamName', '')}".strip(),
            team_abbr=str(row.get("TeamSlug", "")).upper() or str(row.get("TeamAbbreviation", "")).upper(),
            wins=int(row.get("WINS", 0)),
            losses=int(row.get("LOSSES", 0)),
            win_pct=float(row.get("WinPCT", 0)),
            home_record=str(row.get("HOME", "0-0")),
            road_record=str(row.get("ROAD", "0-0")),
            last_10=str(row.get("L10", "0-0")),
            points_pg=float(row.get("PointsPG", 0)),
            opp_points_pg=float(row.get("OppPointsPG", 0)),
            diff_points_pg=float(row.get("DiffPointsPG", 0)),
            current_streak=str(row.get("strCurrentStreak", "")),
            last_10_wins=l10_w,
            last_10_losses=l10_l,
            home_wins=home_w,
            home_losses=home_l,
            road_wins=road_w,
            road_losses=road_l,
        )

        # Get advanced stats
        adv = self._get_advanced_stats(team_id)
        if adv:
            ts.off_rating = adv.get("OFF_RATING", 0.0)
            ts.def_rating = adv.get("DEF_RATING", 0.0)
            ts.net_rating = adv.get("NET_RATING", 0.0)
            ts.pace = adv.get("PACE", 0.0)

        return ts

    def _get_advanced_stats(self, team_id: int) -> dict | None:
        """Fetch advanced stats for all teams, cached for 2 hours."""
        now = datetime.now(timezone.utc)
        if self._advanced_cache and self._advanced_ts and (now - self._advanced_ts) < self._cache_ttl:
            return self._advanced_cache.get(team_id)

        try:
            advanced = leaguedashteamstats.LeagueDashTeamStats(
                season=self.season,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame",
                headers=_NBA_HEADERS,
                timeout=60,
            )
            _sleep()
            df = advanced.get_data_frames()[0]
            records = df.to_dict("records")
            self._advanced_cache = {int(r["TEAM_ID"]): r for r in records}
            self._advanced_ts = now
            return self._advanced_cache.get(team_id)
        except Exception as e:
            logger.error("Failed to fetch advanced stats: %s", e)
            return self._advanced_cache.get(team_id) if self._advanced_cache else None

    def get_team_game_log(self, team_id: int, last_n: int = 10) -> list[dict]:
        """Get recent game log for a team."""
        try:
            log = teamgamelog.TeamGameLog(
                team_id=team_id, season=self.season,
                headers=_NBA_HEADERS, timeout=60,
            )
            _sleep()
            df = log.get_data_frames()[0]
            records = df.head(last_n).to_dict("records")
            return records
        except Exception as e:
            logger.error("Failed to fetch game log for team %d: %s", team_id, e)
            return []

    def get_h2h(self, team_a_id: int, team_b_id: int) -> H2HRecord:
        """Get head-to-head record between two teams this season."""
        h2h = H2HRecord(team_a_id=team_a_id, team_b_id=team_b_id)
        try:
            games = leaguegamefinder.LeagueGameFinder(
                team_id_nullable=team_a_id,
                vs_team_id_nullable=team_b_id,
                season_nullable=self.season,
                season_type_nullable="Regular Season",
                headers=_NBA_HEADERS,
                timeout=60,
            ).get_data_frames()[0]
            _sleep()

            if games.empty:
                return h2h

            for _, row in games.iterrows():
                if row.get("WL") == "W":
                    h2h.team_a_wins += 1
                else:
                    h2h.team_b_wins += 1
                h2h.team_a_avg_pts += float(row.get("PTS", 0))

            n_games = len(games)
            if n_games > 0:
                h2h.team_a_avg_pts /= n_games

            return h2h
        except Exception as e:
            logger.error("Failed to fetch H2H for %d vs %d: %s", team_a_id, team_b_id, e)
            return h2h

    def get_rest_days(self, team_id: int, game_date: str | None = None) -> int:
        """Calculate rest days for a team before a given date."""
        log = self.get_team_game_log(team_id, last_n=3)
        if not log:
            return 1  # Default to 1 day rest

        try:
            last_game_date_str = log[0].get("GAME_DATE", "")
            # Parse date in format "MAR 21, 2026" or similar
            for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
                try:
                    last_game_dt = datetime.strptime(last_game_date_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                return 1

            if game_date:
                target_dt = datetime.strptime(game_date, "%Y-%m-%d")
            else:
                target_dt = datetime.now()

            rest = (target_dt - last_game_dt).days
            return max(0, rest)
        except Exception as e:
            logger.warning("Failed to calculate rest days for %d: %s", team_id, e)
            return 1

    def build_research(
        self,
        home_team_id: int,
        away_team_id: int,
        game_date: str | None = None,
    ) -> tuple[TeamStats | None, TeamStats | None, H2HRecord | None]:
        """Build full research package for a game matchup."""
        home_stats = self.get_team_stats(home_team_id)
        away_stats = self.get_team_stats(away_team_id)

        if home_stats and game_date:
            rest = self.get_rest_days(home_team_id, game_date)
            home_stats.rest_days = rest
            home_stats.is_b2b = rest == 0

        if away_stats and game_date:
            rest = self.get_rest_days(away_team_id, game_date)
            away_stats.rest_days = rest
            away_stats.is_b2b = rest == 0

        h2h = None
        if home_stats and away_stats:
            h2h = self.get_h2h(home_team_id, away_team_id)

        return home_stats, away_stats, h2h
