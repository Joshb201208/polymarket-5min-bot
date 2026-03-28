"""NHL stats engine — MoneyPuck analytics + NHL API + ESPN fallback.

Data sources:
1. MoneyPuck (moneypuck.com) — free CSV data: xG, Corsi, Fenwick, PDO, PP%, PK%
2. NHL API (api-web.nhle.com) — schedule, standings, injuries
3. ESPN NHL (fallback standings)
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from nhl_agent.config import NHLConfig
from nhl_agent.models import NHLTeamStats, NHLH2HRecord

logger = logging.getLogger(__name__)

# ── URLs ──────────────────────────────────────────────────────────
_MONEYPUCK_TEAMS_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/regular/teams.csv"
_NHL_API_BASE = "https://api-web.nhle.com/v1"
_ESPN_NHL_STANDINGS = "https://site.api.espn.com/apis/v2/sports/hockey/nhl/standings"

# ── NHL team name/abbreviation mapping ────────────────────────────
# Maps Polymarket slug abbreviations and common names to official abbreviations
_NHL_TEAMS = {
    "ANA": {"name": "Anaheim Ducks", "abbr": "ANA", "city": "Anaheim"},
    "ARI": {"name": "Utah Hockey Club", "abbr": "UTA", "city": "Utah"},
    "UTA": {"name": "Utah Hockey Club", "abbr": "UTA", "city": "Utah"},
    "BOS": {"name": "Boston Bruins", "abbr": "BOS", "city": "Boston"},
    "BUF": {"name": "Buffalo Sabres", "abbr": "BUF", "city": "Buffalo"},
    "CGY": {"name": "Calgary Flames", "abbr": "CGY", "city": "Calgary"},
    "CAR": {"name": "Carolina Hurricanes", "abbr": "CAR", "city": "Carolina"},
    "CHI": {"name": "Chicago Blackhawks", "abbr": "CHI", "city": "Chicago"},
    "COL": {"name": "Colorado Avalanche", "abbr": "COL", "city": "Colorado"},
    "CBJ": {"name": "Columbus Blue Jackets", "abbr": "CBJ", "city": "Columbus"},
    "DAL": {"name": "Dallas Stars", "abbr": "DAL", "city": "Dallas"},
    "DET": {"name": "Detroit Red Wings", "abbr": "DET", "city": "Detroit"},
    "EDM": {"name": "Edmonton Oilers", "abbr": "EDM", "city": "Edmonton"},
    "FLA": {"name": "Florida Panthers", "abbr": "FLA", "city": "Florida"},
    "LA": {"name": "Los Angeles Kings", "abbr": "LAK", "city": "Los Angeles"},
    "LAK": {"name": "Los Angeles Kings", "abbr": "LAK", "city": "Los Angeles"},
    "MIN": {"name": "Minnesota Wild", "abbr": "MIN", "city": "Minnesota"},
    "MTL": {"name": "Montreal Canadiens", "abbr": "MTL", "city": "Montreal"},
    "MON": {"name": "Montreal Canadiens", "abbr": "MTL", "city": "Montreal"},
    "NSH": {"name": "Nashville Predators", "abbr": "NSH", "city": "Nashville"},
    "NJ": {"name": "New Jersey Devils", "abbr": "NJD", "city": "New Jersey"},
    "NJD": {"name": "New Jersey Devils", "abbr": "NJD", "city": "New Jersey"},
    "NYI": {"name": "New York Islanders", "abbr": "NYI", "city": "New York"},
    "NYR": {"name": "New York Rangers", "abbr": "NYR", "city": "New York"},
    "OTT": {"name": "Ottawa Senators", "abbr": "OTT", "city": "Ottawa"},
    "PHI": {"name": "Philadelphia Flyers", "abbr": "PHI", "city": "Philadelphia"},
    "PIT": {"name": "Pittsburgh Penguins", "abbr": "PIT", "city": "Pittsburgh"},
    "SJ": {"name": "San Jose Sharks", "abbr": "SJS", "city": "San Jose"},
    "SJS": {"name": "San Jose Sharks", "abbr": "SJS", "city": "San Jose"},
    "SEA": {"name": "Seattle Kraken", "abbr": "SEA", "city": "Seattle"},
    "STL": {"name": "St. Louis Blues", "abbr": "STL", "city": "St. Louis"},
    "TB": {"name": "Tampa Bay Lightning", "abbr": "TBL", "city": "Tampa Bay"},
    "TBL": {"name": "Tampa Bay Lightning", "abbr": "TBL", "city": "Tampa Bay"},
    "TOR": {"name": "Toronto Maple Leafs", "abbr": "TOR", "city": "Toronto"},
    "VAN": {"name": "Vancouver Canucks", "abbr": "VAN", "city": "Vancouver"},
    "VGK": {"name": "Vegas Golden Knights", "abbr": "VGK", "city": "Vegas"},
    "WSH": {"name": "Washington Capitals", "abbr": "WSH", "city": "Washington"},
    "WAS": {"name": "Washington Capitals", "abbr": "WSH", "city": "Washington"},
    "WPG": {"name": "Winnipeg Jets", "abbr": "WPG", "city": "Winnipeg"},
}

# Reverse lookup: full name / nickname → abbr
_NAME_TO_ABBR: dict[str, str] = {}
for _info in _NHL_TEAMS.values():
    _NAME_TO_ABBR[_info["name"].lower()] = _info["abbr"]
    # Add nickname (last word of name)
    nickname = _info["name"].split()[-1].lower()
    _NAME_TO_ABBR[nickname] = _info["abbr"]
    _NAME_TO_ABBR[_info["city"].lower()] = _info["abbr"]


def resolve_nhl_abbr(abbr: str) -> str:
    """Map a Polymarket slug abbreviation to official NHL abbreviation."""
    entry = _NHL_TEAMS.get(abbr.upper())
    return entry["abbr"] if entry else abbr.upper()


def find_nhl_team_by_abbr(abbr: str) -> dict | None:
    """Look up an NHL team by abbreviation."""
    resolved = resolve_nhl_abbr(abbr)
    for info in _NHL_TEAMS.values():
        if info["abbr"] == resolved:
            return info
    return None


def find_nhl_team_by_name(name: str) -> dict | None:
    """Look up an NHL team by name (fuzzy)."""
    name_lower = name.lower().strip()
    if name_lower in _NAME_TO_ABBR:
        abbr = _NAME_TO_ABBR[name_lower]
        return find_nhl_team_by_abbr(abbr)
    # Partial match
    for key, abbr in _NAME_TO_ABBR.items():
        if name_lower in key or key in name_lower:
            return find_nhl_team_by_abbr(abbr)
    return None


def slugify_nhl_game(slug: str) -> tuple[str, str, str] | None:
    """Extract away_abbr, home_abbr, date from slug like nhl-col-dal-2026-03-28."""
    parts = slug.lower().split("-")
    if len(parts) < 6 or parts[0] != "nhl":
        return None
    try:
        year = int(parts[-3])
        month = int(parts[-2])
        day = int(parts[-1])
        date_str = f"{year}-{month:02d}-{day:02d}"
        team_parts = parts[1:-3]
        if len(team_parts) >= 2:
            return team_parts[0].upper(), team_parts[1].upper(), date_str
    except (ValueError, IndexError):
        pass
    return None


# ═══════════════════════════════════════════════════════════════════
# MoneyPuck data fetcher
# ═══════════════════════════════════════════════════════════════════

class MoneyPuckClient:
    """Fetches team analytics from MoneyPuck CSV data."""

    def __init__(self, proxy_url: str | None = None) -> None:
        self._proxy_url = proxy_url
        self._team_data: dict[str, dict] = {}
        self._cache_ts: datetime | None = None
        self._cache_ttl = timedelta(hours=3)

    async def get_team_stats(self) -> dict[str, dict]:
        """Fetch team-level stats from MoneyPuck. Cached for 3 hours."""
        now = datetime.now(timezone.utc)
        if self._team_data and self._cache_ts and (now - self._cache_ts) < self._cache_ttl:
            return self._team_data

        try:
            client_kwargs: dict = {"timeout": 30.0}
            if self._proxy_url:
                client_kwargs["proxy"] = self._proxy_url

            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(_MONEYPUCK_TEAMS_URL)
                resp.raise_for_status()

            reader = csv.DictReader(io.StringIO(resp.text))
            team_games: dict[str, list[dict]] = defaultdict(list)
            for row in reader:
                team = row.get("team", "")
                if team:
                    team_games[team].append(row)

            # Aggregate to per-team stats (last N games or season averages)
            result: dict[str, dict] = {}
            for team, games in team_games.items():
                if not games:
                    continue
                # Use season totals from the accumulated data
                n = len(games)
                try:
                    xgf = sum(float(g.get("xGoalsFor", 0)) for g in games) / n if n > 0 else 0
                    xga = sum(float(g.get("xGoalsAgainst", 0)) for g in games) / n if n > 0 else 0
                    corsi_for = sum(float(g.get("corsiFor", 0)) for g in games)
                    corsi_against = sum(float(g.get("corsiAgainst", 0)) for g in games)
                    fenwick_for = sum(float(g.get("fenwickFor", 0)) for g in games)
                    fenwick_against = sum(float(g.get("fenwickAgainst", 0)) for g in games)

                    corsi_total = corsi_for + corsi_against
                    fenwick_total = fenwick_for + fenwick_against
                    xg_total = xgf + xga

                    result[team] = {
                        "games": n,
                        "xgf_per_game": round(xgf, 3),
                        "xga_per_game": round(xga, 3),
                        "xgf_pct": round(xgf / xg_total * 100, 1) if xg_total > 0 else 50.0,
                        "corsi_pct": round(corsi_for / corsi_total * 100, 1) if corsi_total > 0 else 50.0,
                        "fenwick_pct": round(fenwick_for / fenwick_total * 100, 1) if fenwick_total > 0 else 50.0,
                        "goals_for_pg": sum(float(g.get("goalsFor", 0)) for g in games) / n if n > 0 else 0,
                        "goals_against_pg": sum(float(g.get("goalsAgainst", 0)) for g in games) / n if n > 0 else 0,
                    }
                except (ValueError, ZeroDivisionError):
                    continue

            self._team_data = result
            self._cache_ts = now
            logger.info("MoneyPuck: loaded analytics for %d teams", len(result))
            return result

        except Exception as e:
            logger.warning("MoneyPuck fetch failed: %s", e)
            return self._team_data  # Return stale cache


# ═══════════════════════════════════════════════════════════════════
# NHL API client
# ═══════════════════════════════════════════════════════════════════

class NHLAPIClient:
    """Fetches schedule, standings, and injuries from the NHL API."""

    def __init__(self, proxy_url: str | None = None) -> None:
        self._proxy_url = proxy_url
        self._standings_cache: list[dict] = []
        self._standings_ts: datetime | None = None
        self._schedule_cache: list[dict] = []
        self._schedule_ts: datetime | None = None
        self._cache_ttl = timedelta(hours=2)

    async def get_standings(self) -> list[dict]:
        """Fetch current NHL standings."""
        now = datetime.now(timezone.utc)
        if self._standings_cache and self._standings_ts and (now - self._standings_ts) < self._cache_ttl:
            return self._standings_cache

        try:
            client_kwargs: dict = {"timeout": 20.0}
            if self._proxy_url:
                client_kwargs["proxy"] = self._proxy_url

            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(f"{_NHL_API_BASE}/standings/now")
                resp.raise_for_status()
                data = resp.json()

            standings = []
            for entry in data.get("standings", []):
                team_name = entry.get("teamName", {})
                # Handle both old and new API formats
                if isinstance(team_name, dict):
                    full_name = team_name.get("default", "")
                else:
                    full_name = str(team_name)

                abbr = entry.get("teamAbbrev", {})
                if isinstance(abbr, dict):
                    abbr = abbr.get("default", "")

                standings.append({
                    "team_name": full_name,
                    "team_abbr": str(abbr),
                    "wins": int(entry.get("wins", 0)),
                    "losses": int(entry.get("losses", 0)),
                    "ot_losses": int(entry.get("otLosses", 0)),
                    "points": int(entry.get("points", 0)),
                    "games_played": int(entry.get("gamesPlayed", 0)),
                    "goals_for": int(entry.get("goalFor", 0)),
                    "goals_against": int(entry.get("goalAgainst", 0)),
                    "goal_diff": int(entry.get("goalDifferential", 0)),
                    "home_wins": int(entry.get("homeWins", 0)),
                    "home_losses": int(entry.get("homeLosses", 0)),
                    "home_ot_losses": int(entry.get("homeOtLosses", 0)),
                    "road_wins": int(entry.get("roadWins", 0)),
                    "road_losses": int(entry.get("roadLosses", 0)),
                    "road_ot_losses": int(entry.get("roadOtLosses", 0)),
                    "l10_wins": int(entry.get("l10Wins", 0)),
                    "l10_losses": int(entry.get("l10Losses", 0)),
                    "l10_ot_losses": int(entry.get("l10OtLosses", 0)),
                    "streak_code": entry.get("streakCode", ""),
                    "streak_count": int(entry.get("streakCount", 0)),
                    "pp_pct": float(entry.get("powerPlayPctg", 0)),
                    "pk_pct": float(entry.get("penaltyKillPctg", 0)),
                })

            self._standings_cache = standings
            self._standings_ts = now
            logger.info("NHL API: loaded standings for %d teams", len(standings))
            return standings

        except Exception as e:
            logger.warning("NHL API standings failed: %s", e)
            return self._standings_cache

    async def get_schedule(self, date_str: str | None = None) -> list[dict]:
        """Fetch NHL schedule for a given date (or today/tomorrow)."""
        now = datetime.now(timezone.utc)
        if self._schedule_cache and self._schedule_ts and (now - self._schedule_ts) < timedelta(minutes=30):
            return self._schedule_cache

        try:
            if not date_str:
                date_str = now.strftime("%Y-%m-%d")

            client_kwargs: dict = {"timeout": 20.0}
            if self._proxy_url:
                client_kwargs["proxy"] = self._proxy_url

            games = []
            async with httpx.AsyncClient(**client_kwargs) as client:
                # Fetch today and tomorrow
                for delta in range(3):
                    d = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
                    resp = await client.get(f"{_NHL_API_BASE}/schedule/{d}")
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    for week in data.get("gameWeek", []):
                        for game in week.get("games", []):
                            away_team = game.get("awayTeam", {})
                            home_team = game.get("homeTeam", {})
                            away_abbr = away_team.get("abbrev", "")
                            home_abbr = home_team.get("abbrev", "")
                            games.append({
                                "game_id": game.get("id"),
                                "date": week.get("date", d),
                                "start_time": game.get("startTimeUTC", ""),
                                "away_team": away_abbr,
                                "home_team": home_abbr,
                                "away_name": away_team.get("placeName", {}).get("default", ""),
                                "home_name": home_team.get("placeName", {}).get("default", ""),
                                "game_state": game.get("gameState", ""),
                                "game_type": game.get("gameType", 2),  # 2=regular, 3=playoff
                            })

            self._schedule_cache = games
            self._schedule_ts = now
            logger.info("NHL API: loaded %d upcoming games", len(games))
            return games

        except Exception as e:
            logger.warning("NHL API schedule failed: %s", e)
            return self._schedule_cache

    async def get_team_schedule(self, team_abbr: str) -> list[dict]:
        """Fetch a team's recent schedule for rest day calculation."""
        try:
            client_kwargs: dict = {"timeout": 15.0}
            if self._proxy_url:
                client_kwargs["proxy"] = self._proxy_url

            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(
                    f"{_NHL_API_BASE}/club-schedule-season/{team_abbr}/now"
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()

            games = []
            for game in data.get("games", []):
                games.append({
                    "date": game.get("gameDate", ""),
                    "game_state": game.get("gameState", ""),
                    "away_abbr": game.get("awayTeam", {}).get("abbrev", ""),
                    "home_abbr": game.get("homeTeam", {}).get("abbrev", ""),
                })
            return games

        except Exception as e:
            logger.warning("NHL team schedule fetch failed for %s: %s", team_abbr, e)
            return []


# ═══════════════════════════════════════════════════════════════════
# ESPN NHL fallback
# ═══════════════════════════════════════════════════════════════════

async def _fetch_espn_nhl_standings() -> dict[str, dict]:
    """Fetch NHL standings from ESPN as fallback."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_ESPN_NHL_STANDINGS)
            resp.raise_for_status()
            data = resp.json()

        result: dict[str, dict] = {}
        for conference in data.get("children", []):
            for division in conference.get("children", []):
                for entry in division.get("standings", {}).get("entries", []):
                    team_info = entry.get("team", {})
                    abbr = team_info.get("abbreviation", "")
                    name = team_info.get("displayName", "")
                    stats_raw = {s["name"]: s for s in entry.get("stats", [])}

                    wins = int(stats_raw.get("wins", {}).get("value", 0))
                    losses = int(stats_raw.get("losses", {}).get("value", 0))
                    ot_losses = int(stats_raw.get("otLosses", {}).get("value", 0))
                    points = int(stats_raw.get("points", {}).get("value", 0))

                    result[abbr] = {
                        "team_name": name,
                        "team_abbr": abbr,
                        "wins": wins,
                        "losses": losses,
                        "ot_losses": ot_losses,
                        "points": points,
                    }

        logger.info("ESPN NHL standings: loaded %d teams", len(result))
        return result

    except Exception as e:
        logger.warning("ESPN NHL standings failed: %s", e)
        return {}


# ═══════════════════════════════════════════════════════════════════
# Main research class
# ═══════════════════════════════════════════════════════════════════

class NHLResearch:
    """Fetches and caches NHL statistics from multiple sources."""

    def __init__(self, config: NHLConfig | None = None) -> None:
        self.config = config or NHLConfig()

        # Build proxy URL if configured
        proxy_url: str | None = None
        if self.config.PROXY_HOST and self.config.PROXY_USER:
            proxy_url = (
                f"http://{self.config.PROXY_USER}:{self.config.PROXY_PASS}"
                f"@{self.config.PROXY_HOST}:{self.config.PROXY_PORT}"
            )

        self.moneypuck = MoneyPuckClient(proxy_url)
        self.nhl_api = NHLAPIClient(proxy_url)

        # ESPN cache
        self._espn_standings: dict[str, dict] = {}
        self._espn_ts: datetime | None = None

    async def get_standings(self) -> list[dict]:
        """Get current NHL standings."""
        standings = await self.nhl_api.get_standings()
        if standings:
            return standings
        # Fallback to ESPN
        espn = await self._get_espn_standings()
        return list(espn.values())

    async def _get_espn_standings(self) -> dict[str, dict]:
        now = datetime.now(timezone.utc)
        if self._espn_standings and self._espn_ts and (now - self._espn_ts) < timedelta(hours=2):
            return self._espn_standings
        result = await _fetch_espn_nhl_standings()
        if result:
            self._espn_standings = result
            self._espn_ts = now
        return self._espn_standings

    async def get_team_stats(self, team_abbr: str) -> NHLTeamStats | None:
        """Build NHLTeamStats combining NHL API standings + MoneyPuck analytics."""
        resolved = resolve_nhl_abbr(team_abbr)
        team_info = find_nhl_team_by_abbr(resolved)
        team_name = team_info["name"] if team_info else resolved

        standings = await self.nhl_api.get_standings()
        standing = None
        for s in standings:
            if s["team_abbr"] == resolved:
                standing = s
                break

        if not standing:
            # Try ESPN fallback
            espn = await self._get_espn_standings()
            for abbr, data in espn.items():
                if abbr == resolved or (team_info and data.get("team_name", "").lower() == team_name.lower()):
                    standing = data
                    break

        if not standing:
            logger.warning("No standings data for %s", resolved)
            return None

        gp = standing.get("games_played", standing.get("wins", 0) + standing.get("losses", 0) + standing.get("ot_losses", 0))
        wins = standing["wins"]
        losses = standing["losses"]
        ot_losses = standing.get("ot_losses", 0)
        win_pct = wins / gp if gp > 0 else 0.0

        hw = standing.get("home_wins", 0)
        hl = standing.get("home_losses", 0)
        hol = standing.get("home_ot_losses", 0)
        rw = standing.get("road_wins", 0)
        rl = standing.get("road_losses", 0)
        rol = standing.get("road_ot_losses", 0)

        l10w = standing.get("l10_wins", 0)
        l10l = standing.get("l10_losses", 0)
        l10ol = standing.get("l10_ot_losses", 0)

        gf = standing.get("goals_for", 0)
        ga = standing.get("goals_against", 0)
        gpg = gf / gp if gp > 0 else 0
        gapg = ga / gp if gp > 0 else 0

        streak_code = standing.get("streak_code", "")
        streak_count = standing.get("streak_count", 0)
        streak_str = f"{streak_code}{streak_count}" if streak_code else ""

        stats = NHLTeamStats(
            team_name=team_name,
            team_abbr=resolved,
            wins=wins,
            losses=losses,
            ot_losses=ot_losses,
            points=standing.get("points", 0),
            win_pct=round(win_pct, 3),
            home_record=f"{hw}-{hl}-{hol}",
            road_record=f"{rw}-{rl}-{rol}",
            last_10=f"{l10w}-{l10l}-{l10ol}",
            goals_pg=round(gpg, 2),
            goals_against_pg=round(gapg, 2),
            goal_diff_pg=round(gpg - gapg, 2),
            current_streak=streak_str,
            last_10_wins=l10w,
            last_10_losses=l10l,
            home_wins=hw,
            home_losses=hl,
            road_wins=rw,
            road_losses=rl,
            pp_pct=float(standing.get("pp_pct", 0)),
            pk_pct=float(standing.get("pk_pct", 0)),
        )

        # Enhance with MoneyPuck advanced stats
        mp_data = await self.moneypuck.get_team_stats()
        mp_team = mp_data.get(resolved)
        if mp_team:
            stats.xgf_pct = mp_team.get("xgf_pct", 50.0)
            stats.corsi_pct = mp_team.get("corsi_pct", 50.0)
            stats.fenwick_pct = mp_team.get("fenwick_pct", 50.0)

        return stats

    async def get_rest_days(self, team_abbr: str, game_date: str | None = None) -> int:
        """Calculate rest days for a team before a given date."""
        resolved = resolve_nhl_abbr(team_abbr)
        games = await self.nhl_api.get_team_schedule(resolved)
        if not games:
            return 1

        now = datetime.now(timezone.utc)
        target = datetime.strptime(game_date, "%Y-%m-%d") if game_date else now

        # Find the most recent completed game before the target date
        completed = []
        for g in games:
            if g.get("game_state") in ("OFF", "FINAL"):
                try:
                    gd = datetime.strptime(g["date"][:10], "%Y-%m-%d")
                    if gd < target:
                        completed.append(gd)
                except (ValueError, KeyError):
                    continue

        if not completed:
            return 2  # Default: assume 2 days rest if no data

        last_game = max(completed)
        return max(0, (target - last_game).days)

    async def build_research(
        self,
        home_abbr: str,
        away_abbr: str,
        game_date: str | None = None,
    ) -> tuple[NHLTeamStats | None, NHLTeamStats | None, NHLH2HRecord | None]:
        """Build full research package for an NHL matchup."""
        home_stats = await self.get_team_stats(home_abbr)
        away_stats = await self.get_team_stats(away_abbr)

        if home_stats and game_date:
            rest = await self.get_rest_days(home_abbr, game_date)
            home_stats.rest_days = rest
            home_stats.is_b2b = rest == 0

        if away_stats and game_date:
            rest = await self.get_rest_days(away_abbr, game_date)
            away_stats.rest_days = rest
            away_stats.is_b2b = rest == 0

        # H2H: no easy free API for this — return empty
        h2h = NHLH2HRecord(
            team_a=home_abbr, team_b=away_abbr
        ) if home_stats and away_stats else None

        return home_stats, away_stats, h2h
