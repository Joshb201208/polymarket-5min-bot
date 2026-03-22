"""NBA stats engine — standings, game logs, H2H, team stats.

Uses the NBA CDN schedule endpoint (cdn.nba.com) which is publicly
accessible from any IP including cloud/VPS servers, unlike stats.nba.com
which blocks cloud provider IP ranges.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from nba_api.stats.static import teams as nba_teams_static

from nba_agent.config import Config
from nba_agent.models import TeamStats, H2HRecord

logger = logging.getLogger(__name__)

# NBA CDN schedule endpoint — publicly accessible, no auth, no IP blocking
_SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"

# Build team lookup tables from nba_api static data (no HTTP request needed)
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
    if name_lower in _TEAM_BY_NAME:
        return _TEAM_BY_NAME[name_lower]
    for key, team in _TEAM_BY_NAME.items():
        if name_lower in key or key in name_lower:
            return team
    return None


class NBAResearch:
    """Fetches and caches NBA statistics from the CDN schedule endpoint."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.season = self.config.NBA_SEASON

        # Caches
        self._schedule_raw: dict | None = None
        self._schedule_ts: datetime | None = None
        self._team_games: dict[int, list[dict]] | None = None
        self._team_records: dict[int, dict] | None = None
        self._cache_ttl = timedelta(hours=2)

    def _fetch_schedule(self) -> dict | None:
        """Download the full season schedule from NBA CDN, cached for 2 hours."""
        now = datetime.now(timezone.utc)
        if self._schedule_raw and self._schedule_ts and (now - self._schedule_ts) < self._cache_ttl:
            return self._schedule_raw

        for attempt in range(3):
            try:
                resp = httpx.get(_SCHEDULE_URL, timeout=30.0)
                resp.raise_for_status()
                data = resp.json()
                self._schedule_raw = data
                self._schedule_ts = now
                self._team_games = None  # Invalidate derived caches
                self._team_records = None
                logger.info("Fetched NBA schedule from CDN (%d game dates)",
                            len(data.get("leagueSchedule", {}).get("gameDates", [])))
                return data
            except Exception as e:
                wait = (attempt + 1) * 2
                logger.warning("Schedule fetch attempt %d/3 failed: %s — retrying in %ds", attempt + 1, e, wait)
                time.sleep(wait)

        logger.error("All 3 schedule fetch attempts failed. NBA CDN may be down.")
        return self._schedule_raw  # Return stale cache if available

    def _build_team_games(self) -> dict[int, list[dict]]:
        """Parse the schedule into per-team game logs."""
        if self._team_games is not None:
            return self._team_games

        schedule = self._fetch_schedule()
        if not schedule:
            return {}

        team_games: dict[int, list[dict]] = defaultdict(list)
        dates = schedule.get("leagueSchedule", {}).get("gameDates", [])

        for dt in dates:
            for g in dt.get("games", []):
                if g.get("gameStatus") != 3:  # Only completed games
                    continue

                ht = g["homeTeam"]
                at = g["awayTeam"]
                game_date = g.get("gameDateEst", g.get("gameDateUTC", ""))[:10]

                # Home team entry
                team_games[ht["teamId"]].append({
                    "date": game_date,
                    "is_home": True,
                    "pts": int(ht.get("score", 0)),
                    "opp_pts": int(at.get("score", 0)),
                    "opp_id": at["teamId"],
                    "opp_tricode": at.get("teamTricode", ""),
                    "won": int(ht.get("score", 0)) > int(at.get("score", 0)),
                })

                # Away team entry
                team_games[at["teamId"]].append({
                    "date": game_date,
                    "is_home": False,
                    "pts": int(at.get("score", 0)),
                    "opp_pts": int(ht.get("score", 0)),
                    "opp_id": ht["teamId"],
                    "opp_tricode": ht.get("teamTricode", ""),
                    "won": int(at.get("score", 0)) > int(ht.get("score", 0)),
                })

        # Sort by date
        for tid in team_games:
            team_games[tid].sort(key=lambda x: x["date"])

        self._team_games = dict(team_games)
        logger.info("Built game logs for %d teams", len(self._team_games))
        return self._team_games

    def _build_team_records(self) -> dict[int, dict]:
        """Build latest W-L records from the schedule."""
        if self._team_records is not None:
            return self._team_records

        schedule = self._fetch_schedule()
        if not schedule:
            return {}

        records: dict[int, dict] = {}
        dates = schedule.get("leagueSchedule", {}).get("gameDates", [])

        # Walk backwards to find most recent record for each team
        for dt in reversed(dates):
            for g in dt.get("games", []):
                if g.get("gameStatus") != 3:
                    continue
                for side in ("homeTeam", "awayTeam"):
                    t = g[side]
                    tid = t["teamId"]
                    if tid not in records:
                        records[tid] = {
                            "TeamID": tid,
                            "TeamName": t.get("teamName", ""),
                            "TeamCity": t.get("teamCity", ""),
                            "TeamSlug": t.get("teamTricode", ""),
                            "WINS": int(t.get("wins", 0)),
                            "LOSSES": int(t.get("losses", 0)),
                        }
            if len(records) >= 30:
                break

        self._team_records = records
        return records

    def get_standings(self) -> list[dict]:
        """Return standings as a list of dicts (one per team)."""
        records = self._build_team_records()
        return list(records.values())

    def get_team_stats(self, team_id: int) -> TeamStats | None:
        """Build TeamStats from CDN schedule data."""
        team_games = self._build_team_games()
        games = team_games.get(team_id, [])
        if not games:
            logger.warning("Team ID %d has no games in schedule", team_id)
            return None

        records = self._build_team_records()
        record = records.get(team_id, {})

        n = len(games)
        total_pts = sum(g["pts"] for g in games)
        total_opp = sum(g["opp_pts"] for g in games)

        home_games = [g for g in games if g["is_home"]]
        road_games = [g for g in games if not g["is_home"]]
        home_w = sum(1 for g in home_games if g["won"])
        home_l = len(home_games) - home_w
        road_w = sum(1 for g in road_games if g["won"])
        road_l = len(road_games) - road_w

        last_10 = games[-10:]
        l10_w = sum(1 for g in last_10 if g["won"])
        l10_l = len(last_10) - l10_w

        # Current streak
        streak_count = 0
        streak_type = "W" if games[-1]["won"] else "L"
        for g in reversed(games):
            if (g["won"] and streak_type == "W") or (not g["won"] and streak_type == "L"):
                streak_count += 1
            else:
                break

        wins = int(record.get("WINS", sum(1 for g in games if g["won"])))
        losses = int(record.get("LOSSES", sum(1 for g in games if not g["won"])))
        total = wins + losses
        win_pct = wins / total if total > 0 else 0.0

        # Look up full team name from static data
        team_info = None
        for t in _ALL_TEAMS:
            if t["id"] == team_id:
                team_info = t
                break

        team_name = (
            f"{record.get('TeamCity', '')} {record.get('TeamName', '')}".strip()
            if record
            else (team_info["full_name"] if team_info else f"Team {team_id}")
        )
        team_abbr = (
            record.get("TeamSlug", "").upper()
            or (team_info["abbreviation"] if team_info else "")
        )

        ppg = total_pts / n if n > 0 else 0.0
        opp_ppg = total_opp / n if n > 0 else 0.0

        ts = TeamStats(
            team_id=team_id,
            team_name=team_name,
            team_abbr=team_abbr,
            wins=wins,
            losses=losses,
            win_pct=win_pct,
            home_record=f"{home_w}-{home_l}",
            road_record=f"{road_w}-{road_l}",
            last_10=f"{l10_w}-{l10_l}",
            points_pg=round(ppg, 1),
            opp_points_pg=round(opp_ppg, 1),
            diff_points_pg=round(ppg - opp_ppg, 1),
            current_streak=f"{streak_type}{streak_count}",
            last_10_wins=l10_w,
            last_10_losses=l10_l,
            home_wins=home_w,
            home_losses=home_l,
            road_wins=road_w,
            road_losses=road_l,
        )

        # Estimate offensive/defensive ratings from points data
        # These are rough approximations — true ratings need possession data
        # We use points per game as proxy (avg NBA pace ~100 possessions)
        ts.off_rating = round(ppg * (100 / 98), 1)  # ~adjust for pace
        ts.def_rating = round(opp_ppg * (100 / 98), 1)
        ts.net_rating = round(ts.off_rating - ts.def_rating, 1)
        ts.pace = 98.0  # Default estimate

        return ts

    def get_team_game_log(self, team_id: int, last_n: int = 10) -> list[dict]:
        """Get recent game log for a team."""
        team_games = self._build_team_games()
        games = team_games.get(team_id, [])
        return games[-last_n:] if games else []

    def get_h2h(self, team_a_id: int, team_b_id: int) -> H2HRecord:
        """Get head-to-head record between two teams this season."""
        h2h = H2HRecord(team_a_id=team_a_id, team_b_id=team_b_id)

        team_games = self._build_team_games()
        games_a = team_games.get(team_a_id, [])

        matchups = [g for g in games_a if g["opp_id"] == team_b_id]
        for g in matchups:
            if g["won"]:
                h2h.team_a_wins += 1
            else:
                h2h.team_b_wins += 1
            h2h.team_a_avg_pts += g["pts"]

        n = len(matchups)
        if n > 0:
            h2h.team_a_avg_pts /= n

        return h2h

    def get_rest_days(self, team_id: int, game_date: str | None = None) -> int:
        """Calculate rest days for a team before a given date."""
        team_games = self._build_team_games()
        games = team_games.get(team_id, [])
        if not games:
            return 1

        try:
            last_game_date_str = games[-1]["date"]
            last_game_dt = datetime.strptime(last_game_date_str[:10], "%Y-%m-%d")

            if game_date:
                target_dt = datetime.strptime(game_date[:10], "%Y-%m-%d")
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
