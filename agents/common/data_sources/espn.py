"""
ESPN scoreboard/odds data source (free, no key needed).
Provides odds data for NBA and soccer matches.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
TIMEOUT = 10.0


def _get(url: str) -> dict | None:
    """GET from ESPN."""
    try:
        resp = httpx.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"ESPN error {url}: {e}")
        return None


def get_nba_scoreboard() -> list[dict]:
    """Get today's NBA games with odds from ESPN."""
    data = _get(f"{ESPN_BASE}/basketball/nba/scoreboard")
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        competition = event.get("competitions", [{}])[0]
        competitors = competition.get("competitors", [])
        if len(competitors) < 2:
            continue

        home = away = None
        for c in competitors:
            if c.get("homeAway") == "home":
                home = c
            else:
                away = c

        if not home or not away:
            continue

        # Extract odds
        odds = {}
        for odd in competition.get("odds", []):
            odds = {
                "provider": odd.get("provider", {}).get("name", ""),
                "spread": odd.get("spread", 0),
                "over_under": odd.get("overUnder", 0),
                "home_ml": odd.get("homeTeamOdds", {}).get("moneyLine", 0),
                "away_ml": odd.get("awayTeamOdds", {}).get("moneyLine", 0),
            }
            break  # First provider only

        games.append({
            "id": event.get("id", ""),
            "name": event.get("name", ""),
            "date": event.get("date", ""),
            "status": event.get("status", {}).get("type", {}).get("description", ""),
            "home_team": home.get("team", {}).get("displayName", ""),
            "away_team": away.get("team", {}).get("displayName", ""),
            "home_score": home.get("score", ""),
            "away_score": away.get("score", ""),
            "odds": odds,
        })

    return games


def get_soccer_scoreboard(league_code: str) -> list[dict]:
    """Get soccer matches from ESPN scoreboard."""
    data = _get(f"{ESPN_BASE}/soccer/{league_code}/scoreboard")
    if not data:
        return []

    matches = []
    for event in data.get("events", []):
        competition = event.get("competitions", [{}])[0]
        competitors = competition.get("competitors", [])
        if len(competitors) < 2:
            continue

        home = away = None
        for c in competitors:
            if c.get("homeAway") == "home":
                home = c
            else:
                away = c

        if not home or not away:
            continue

        odds = {}
        for odd in competition.get("odds", []):
            odds = {
                "provider": odd.get("provider", {}).get("name", ""),
                "spread": odd.get("spread", 0),
                "over_under": odd.get("overUnder", 0),
            }
            break

        matches.append({
            "id": event.get("id", ""),
            "name": event.get("name", ""),
            "date": event.get("date", ""),
            "status": event.get("status", {}).get("type", {}).get("description", ""),
            "home_team": home.get("team", {}).get("displayName", ""),
            "away_team": away.get("team", {}).get("displayName", ""),
            "home_score": home.get("score", ""),
            "away_score": away.get("score", ""),
            "odds": odds,
        })

    return matches


def get_nba_game_odds(home_team: str = None, away_team: str = None) -> dict | None:
    """Get odds for a specific NBA game."""
    games = get_nba_scoreboard()
    for game in games:
        if home_team and home_team.lower() not in game["home_team"].lower():
            continue
        if away_team and away_team.lower() not in game["away_team"].lower():
            continue
        return game.get("odds", {})
    return None
