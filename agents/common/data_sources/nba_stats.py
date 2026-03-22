"""
Real NBA statistical analysis using nba_api.
All data from NBA.com — completely free, no API key.

IMPORTANT: time.sleep(0.6) between all nba_api calls to avoid rate limiting.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from nba_api.stats.endpoints import (
    leaguestandings,
    teamgamelog,
    commonteamroster,
    playergamelog,
    leaguegamefinder,
)
from nba_api.stats.static import teams, players

from . import news

logger = logging.getLogger(__name__)

CURRENT_SEASON = "2025-26"


def _find_team(team_name: str) -> dict | None:
    """Find an NBA team by name (fuzzy match)."""
    team_name_lower = team_name.lower().strip()
    all_teams = teams.get_teams()
    for team in all_teams:
        if (team_name_lower in team["full_name"].lower() or
                team_name_lower in team["nickname"].lower() or
                team_name_lower in team["abbreviation"].lower() or
                team_name_lower == team["city"].lower()):
            return team
    # Try partial match
    for team in all_teams:
        if any(word in team["full_name"].lower() for word in team_name_lower.split()):
            return team
    return None


def get_team_record(team_name: str) -> dict | None:
    """Get W-L record, conference rank, last 10 games."""
    team = _find_team(team_name)
    if not team:
        logger.warning(f"Team not found: {team_name}")
        return None

    try:
        standings = leaguestandings.LeagueStandings(season=CURRENT_SEASON)
        time.sleep(0.6)
        df = standings.get_data_frames()[0]
        team_row = df[df["TeamID"] == team["id"]]
        if team_row.empty:
            return None

        row = team_row.iloc[0]
        return {
            "team": team["full_name"],
            "team_id": team["id"],
            "wins": int(row.get("WINS", 0)),
            "losses": int(row.get("LOSSES", 0)),
            "win_pct": float(row.get("WinPCT", 0)),
            "conference": row.get("Conference", ""),
            "conference_rank": int(row.get("PlayoffRank", 0)),
            "last_10": row.get("L10", ""),
            "streak": row.get("CurrentStreak", ""),
            "home_record": row.get("HOME", ""),
            "road_record": row.get("ROAD", ""),
        }
    except Exception as e:
        logger.error(f"Error getting team record for {team_name}: {e}")
        return None


def get_team_form(team_name: str, n_games: int = 10) -> dict | None:
    """Last N games: W-L, PPG, opponent quality, home/away split."""
    team = _find_team(team_name)
    if not team:
        return None

    try:
        gamelog = teamgamelog.TeamGameLog(
            team_id=team["id"], season=CURRENT_SEASON
        )
        time.sleep(0.6)
        df = gamelog.get_data_frames()[0]

        if df.empty:
            return None

        recent = df.head(n_games)
        wins = len(recent[recent["WL"] == "W"])
        losses = len(recent) - wins
        ppg = recent["PTS"].mean() if "PTS" in recent.columns else 0
        opp_ppg = recent["PTS"].mean() if "PTS" in recent.columns else 0  # Approximation

        # Home/away from recent games
        home_games = recent[recent["MATCHUP"].str.contains("vs.", na=False)]
        away_games = recent[recent["MATCHUP"].str.contains("@", na=False)]
        home_wins = len(home_games[home_games["WL"] == "W"]) if not home_games.empty else 0
        away_wins = len(away_games[away_games["WL"] == "W"]) if not away_games.empty else 0

        return {
            "team": team["full_name"],
            "games": len(recent),
            "wins": wins,
            "losses": losses,
            "win_pct": wins / len(recent) if len(recent) > 0 else 0,
            "ppg": round(ppg, 1),
            "home_wins": home_wins,
            "home_games": len(home_games),
            "away_wins": away_wins,
            "away_games": len(away_games),
            "last_5_results": recent["WL"].tolist()[:5],
        }
    except Exception as e:
        logger.error(f"Error getting team form for {team_name}: {e}")
        return None


def get_h2h_record(team1: str, team2: str, season: str = None) -> dict | None:
    """Head-to-head record between two teams this season."""
    season = season or CURRENT_SEASON
    t1 = _find_team(team1)
    t2 = _find_team(team2)
    if not t1 or not t2:
        return None

    try:
        finder = leaguegamefinder.LeagueGameFinder(
            team_id_nullable=t1["id"],
            season_nullable=season,
            season_type_nullable="Regular Season",
        )
        time.sleep(0.6)
        df = finder.get_data_frames()[0]

        if df.empty:
            return None

        # Filter to games vs team2
        h2h = df[df["MATCHUP"].str.contains(t2["abbreviation"], na=False)]
        if h2h.empty:
            return {"team1": t1["full_name"], "team2": t2["full_name"],
                    "games": 0, "team1_wins": 0, "team2_wins": 0}

        t1_wins = len(h2h[h2h["WL"] == "W"])
        return {
            "team1": t1["full_name"],
            "team2": t2["full_name"],
            "games": len(h2h),
            "team1_wins": t1_wins,
            "team2_wins": len(h2h) - t1_wins,
            "avg_point_diff": round(h2h["PLUS_MINUS"].mean(), 1) if "PLUS_MINUS" in h2h.columns else 0,
        }
    except Exception as e:
        logger.error(f"Error getting H2H for {team1} vs {team2}: {e}")
        return None


def get_team_rest_days(team_name: str) -> int | None:
    """Days since last game (back-to-back detection)."""
    team = _find_team(team_name)
    if not team:
        return None

    try:
        gamelog = teamgamelog.TeamGameLog(
            team_id=team["id"], season=CURRENT_SEASON
        )
        time.sleep(0.6)
        df = gamelog.get_data_frames()[0]

        if df.empty:
            return None

        last_game_date = df.iloc[0]["GAME_DATE"]
        last_date = datetime.strptime(last_game_date, "%b %d, %Y")
        last_date = last_date.replace(tzinfo=timezone.utc)
        days_rest = (datetime.now(timezone.utc) - last_date).days
        return days_rest
    except Exception as e:
        logger.error(f"Error getting rest days for {team_name}: {e}")
        return None


def get_home_away_splits(team_name: str) -> dict | None:
    """Home vs away record splits."""
    record = get_team_record(team_name)
    if not record:
        return None

    return {
        "team": record["team"],
        "home_record": record.get("home_record", ""),
        "road_record": record.get("road_record", ""),
    }


def get_key_player_status(team_name: str) -> list[dict]:
    """Roster + check news for injury reports."""
    team = _find_team(team_name)
    if not team:
        return []

    try:
        roster = commonteamroster.CommonTeamRoster(
            team_id=team["id"], season=CURRENT_SEASON
        )
        time.sleep(0.6)
        df = roster.get_data_frames()[0]

        players_list = []
        for _, row in df.iterrows():
            players_list.append({
                "name": row.get("PLAYER", ""),
                "number": row.get("NUM", ""),
                "position": row.get("POSITION", ""),
            })

        # Check injury news
        injury_news = news.search_google_news(
            f"{team['full_name']} injury NBA", max_results=5, hours_back=48
        )

        injured = []
        for article in injury_news:
            title = article["title"].lower()
            for p in players_list:
                if p["name"].lower().split()[-1] in title:  # Match last name
                    injured.append({
                        **p,
                        "status": "questionable",
                        "news": article["title"],
                    })

        return injured
    except Exception as e:
        logger.error(f"Error getting player status for {team_name}: {e}")
        return []


def calculate_team_power_rating(team_name: str) -> float | None:
    """Composite power rating based on all available stats.

    Factors:
    - Overall W-L (30%)
    - Recent form last 10 (25%)
    - Home/Away context (15%)
    - Rest days advantage (10%)
    - H2H history (10%) — requires opponent, handled in researcher
    - Strength of schedule (10%)
    """
    record = get_team_record(team_name)
    if not record:
        return None

    time.sleep(0.6)
    form = get_team_form(team_name, n_games=10)

    rating = 0.0

    # Overall record (30%) — scale win% to 0-100
    rating += record["win_pct"] * 100 * 0.30

    # Recent form (25%)
    if form:
        rating += form["win_pct"] * 100 * 0.25
    else:
        rating += record["win_pct"] * 100 * 0.25

    # Conference standing (15%) — top teams get more
    conf_rank = record.get("conference_rank", 8)
    rank_score = max(0, (16 - conf_rank)) / 15 * 100
    rating += rank_score * 0.15

    # Rest days (10%)
    time.sleep(0.6)
    rest = get_team_rest_days(team_name)
    if rest is not None:
        if rest == 0:  # Back to back
            rating += 30 * 0.10
        elif rest == 1:
            rating += 50 * 0.10
        elif rest == 2:
            rating += 70 * 0.10
        else:
            rating += 60 * 0.10  # Too much rest can be bad
    else:
        rating += 50 * 0.10

    # Schedule difficulty approximation (10%) — use conference rank as proxy
    rating += rank_score * 0.10

    return round(rating, 1)
