"""
Real soccer statistical analysis using ESPN API.
ESPN API is free, no key required.
"""

import logging
import time
from typing import Optional

import httpx

from . import news

logger = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports/soccer"
TIMEOUT = 10.0

LEAGUES = {
    "Premier League": "eng.1",
    "La Liga": "esp.1",
    "Bundesliga": "ger.1",
    "Serie A": "ita.1",
    "Ligue 1": "fra.1",
    "Champions League": "uefa.champions",
    "MLS": "usa.1",
}


def _get_league_code(league: str) -> str | None:
    """Get ESPN league code from league name."""
    # Direct match
    if league in LEAGUES:
        return LEAGUES[league]
    # Case-insensitive match
    for name, code in LEAGUES.items():
        if league.lower() in name.lower() or name.lower() in league.lower():
            return code
    # Check if it's already a code
    if league in LEAGUES.values():
        return league
    return None


def _espn_get(url: str, params: dict = None) -> dict | None:
    """GET from ESPN API."""
    try:
        resp = httpx.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"ESPN API error {url}: {e}")
        return None


def get_league_standings(league: str) -> list[dict]:
    """Full league table with points, GD, form."""
    code = _get_league_code(league)
    if not code:
        logger.warning(f"Unknown league: {league}")
        return []

    data = _espn_get(f"{ESPN_STANDINGS_BASE}/{code}/standings")
    if not data:
        return []

    standings = []
    try:
        for group in data.get("children", []):
            for entry in group.get("standings", {}).get("entries", []):
                team = entry.get("team", {})
                stats = {s["name"]: s.get("value", s.get("displayValue", ""))
                         for s in entry.get("stats", [])}
                standings.append({
                    "team": team.get("displayName", ""),
                    "team_id": team.get("id", ""),
                    "rank": int(stats.get("rank", 0) or 0),
                    "points": int(stats.get("points", 0) or 0),
                    "games_played": int(stats.get("gamesPlayed", 0) or 0),
                    "wins": int(stats.get("wins", 0) or 0),
                    "draws": int(stats.get("ties", 0) or 0),
                    "losses": int(stats.get("losses", 0) or 0),
                    "goals_for": int(stats.get("pointsFor", 0) or 0),
                    "goals_against": int(stats.get("pointsAgainst", 0) or 0),
                    "goal_diff": int(stats.get("pointDifferential", 0) or 0),
                })
    except Exception as e:
        logger.error(f"Error parsing standings: {e}")

    return sorted(standings, key=lambda x: x.get("rank", 999))


def get_recent_matches(league: str) -> list[dict]:
    """Recent/upcoming fixtures with scores."""
    code = _get_league_code(league)
    if not code:
        return []

    data = _espn_get(f"{ESPN_BASE}/{code}/scoreboard")
    if not data:
        return []

    matches = []
    try:
        for event in data.get("events", []):
            competition = event.get("competitions", [{}])[0]
            competitors = competition.get("competitors", [])
            if len(competitors) < 2:
                continue

            home = competitors[0] if competitors[0].get("homeAway") == "home" else competitors[1]
            away = competitors[1] if competitors[0].get("homeAway") == "home" else competitors[0]

            matches.append({
                "id": event.get("id", ""),
                "name": event.get("name", ""),
                "date": event.get("date", ""),
                "status": event.get("status", {}).get("type", {}).get("description", ""),
                "home_team": home.get("team", {}).get("displayName", ""),
                "away_team": away.get("team", {}).get("displayName", ""),
                "home_score": home.get("score", ""),
                "away_score": away.get("score", ""),
            })
    except Exception as e:
        logger.error(f"Error parsing matches: {e}")

    return matches


def get_team_form(team_name: str, league: str) -> dict | None:
    """Last results for a team from standings form data and recent matches."""
    standings = get_league_standings(league)
    time.sleep(0.3)

    for team in standings:
        if team_name.lower() in team["team"].lower():
            gp = team["games_played"] or 1
            return {
                "team": team["team"],
                "rank": team["rank"],
                "points": team["points"],
                "wins": team["wins"],
                "draws": team["draws"],
                "losses": team["losses"],
                "goals_for": team["goals_for"],
                "goals_against": team["goals_against"],
                "goal_diff": team["goal_diff"],
                "ppg": round(team["points"] / gp, 2),
                "gpg": round(team["goals_for"] / gp, 2),
                "gapg": round(team["goals_against"] / gp, 2),
            }
    return None


def get_h2h(team1: str, team2: str) -> dict | None:
    """Head-to-head info — primarily from news since ESPN doesn't expose H2H directly."""
    query = f"{team1} vs {team2} head to head history"
    articles = news.search_google_news(query, max_results=5, hours_back=168)  # 1 week
    return {
        "team1": team1,
        "team2": team2,
        "news_articles": len(articles),
        "articles": articles[:3],
        "note": "H2H from recent news coverage",
    }


def get_injury_news(team_name: str) -> list[dict]:
    """Injury/suspension news from Google News RSS."""
    articles = news.search_google_news(
        f"{team_name} injury team news football soccer",
        max_results=5, hours_back=48,
    )
    return articles


def calculate_match_prediction(home_team: str, away_team: str,
                               league: str) -> dict:
    """Statistical prediction for a match.

    Factors:
    - Home advantage (~10% boost)
    - Form comparison (league position, PPG)
    - Goal-scoring trends
    - League position gap

    Returns: {home_win_prob, draw_prob, away_win_prob}
    """
    home_form = get_team_form(home_team, league)
    time.sleep(0.3)
    away_form = get_team_form(away_team, league)

    # Default probabilities (base rates from historical data)
    home_win = 0.45
    draw = 0.25
    away_win = 0.30

    if not home_form or not away_form:
        return {
            "home_team": home_team,
            "away_team": away_team,
            "home_win_prob": home_win,
            "draw_prob": draw,
            "away_win_prob": away_win,
            "confidence": "low",
            "reasoning": "Insufficient data — using base rates",
        }

    # Points per game comparison
    home_ppg = home_form.get("ppg", 1.5)
    away_ppg = away_form.get("ppg", 1.5)
    ppg_diff = home_ppg - away_ppg

    # Rank comparison
    home_rank = home_form.get("rank", 10)
    away_rank = away_form.get("rank", 10)
    rank_diff = away_rank - home_rank  # Positive = home team ranked higher

    # Goal scoring
    home_gpg = home_form.get("gpg", 1.2)
    away_gpg = away_form.get("gpg", 1.2)
    home_gapg = home_form.get("gapg", 1.2)
    away_gapg = away_form.get("gapg", 1.2)

    # Adjust probabilities
    # PPG factor
    if ppg_diff > 0.5:
        home_win += 0.10
        away_win -= 0.10
    elif ppg_diff < -0.5:
        home_win -= 0.10
        away_win += 0.10

    # Rank factor
    if rank_diff > 5:
        home_win += 0.08
        away_win -= 0.08
    elif rank_diff < -5:
        home_win -= 0.08
        away_win += 0.08

    # Goal scoring factor
    attack_diff = (home_gpg - away_gapg) - (away_gpg - home_gapg)
    if attack_diff > 0.3:
        home_win += 0.05
        away_win -= 0.05
    elif attack_diff < -0.3:
        home_win -= 0.05
        away_win += 0.05

    # Home advantage already included in base rates
    # Normalize
    total = home_win + draw + away_win
    home_win /= total
    draw /= total
    away_win /= total

    # Clamp
    home_win = max(0.05, min(0.90, home_win))
    draw = max(0.05, min(0.40, draw))
    away_win = max(0.05, min(0.90, away_win))

    # Re-normalize
    total = home_win + draw + away_win
    home_win /= total
    draw /= total
    away_win /= total

    confidence = "medium"
    if abs(ppg_diff) > 1.0 and abs(rank_diff) > 8:
        confidence = "high"
    elif abs(ppg_diff) < 0.2 and abs(rank_diff) < 3:
        confidence = "low"

    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_win_prob": round(home_win, 3),
        "draw_prob": round(draw, 3),
        "away_win_prob": round(away_win, 3),
        "confidence": confidence,
        "reasoning": (
            f"Home PPG {home_ppg:.2f} vs Away PPG {away_ppg:.2f}. "
            f"Ranks: {home_team} #{home_rank} vs {away_team} #{away_rank}. "
            f"Goal diff: home {home_form['goal_diff']:+d}, away {away_form['goal_diff']:+d}."
        ),
    }
