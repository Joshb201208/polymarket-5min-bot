"""
Compare Polymarket odds against traditional bookmaker implied odds.
Uses ESPN odds data (free) and news-based odds mentions.
"""

import logging
from typing import Optional

from . import espn
from . import news

logger = logging.getLogger(__name__)


def implied_probability_from_american(odds: int) -> float:
    """Convert American odds to implied probability.

    Negative odds (favorite): prob = |odds| / (|odds| + 100)
    Positive odds (underdog): prob = 100 / (odds + 100)
    """
    if odds == 0:
        return 0.5
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def implied_probability_from_decimal(odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if odds <= 0:
        return 0
    return 1 / odds


def compare_to_polymarket(polymarket_prob: float,
                          bookmaker_prob: float) -> dict:
    """Calculate the gap between Polymarket and bookmakers.

    If Polymarket says 40% but bookmakers say 55%, that's a 15% edge on YES.
    Bookmakers are generally sharper on sports.
    """
    edge_yes = bookmaker_prob - polymarket_prob
    edge_no = (1 - bookmaker_prob) - (1 - polymarket_prob)

    result = {
        "polymarket_prob": polymarket_prob,
        "bookmaker_prob": bookmaker_prob,
        "edge_yes": round(edge_yes, 4),
        "edge_no": round(edge_no, 4),
        "best_side": None,
        "best_edge": 0,
        "signal": "no_edge",
    }

    if edge_yes > 0.05:  # 5%+ edge on YES
        result["best_side"] = "YES"
        result["best_edge"] = edge_yes
        result["signal"] = "yes_underpriced"
    elif edge_no > 0.05:  # 5%+ edge on NO
        result["best_side"] = "NO"
        result["best_edge"] = abs(edge_no)
        result["signal"] = "no_underpriced"

    return result


def get_nba_odds_comparison(home_team: str, away_team: str,
                            polymarket_home_prob: float) -> dict | None:
    """Compare NBA Polymarket odds to ESPN bookmaker odds."""
    espn_odds = espn.get_nba_game_odds(home_team=home_team, away_team=away_team)
    if not espn_odds:
        return None

    home_ml = espn_odds.get("home_ml", 0)
    away_ml = espn_odds.get("away_ml", 0)

    if not home_ml or not away_ml:
        return None

    bookie_home_prob = implied_probability_from_american(home_ml)
    bookie_away_prob = implied_probability_from_american(away_ml)

    # Remove vig (normalize to 100%)
    total = bookie_home_prob + bookie_away_prob
    if total > 0:
        bookie_home_prob /= total
        bookie_away_prob /= total

    comparison = compare_to_polymarket(polymarket_home_prob, bookie_home_prob)
    comparison["bookmaker_source"] = espn_odds.get("provider", "ESPN")
    comparison["spread"] = espn_odds.get("spread", 0)
    comparison["over_under"] = espn_odds.get("over_under", 0)
    comparison["home_ml"] = home_ml
    comparison["away_ml"] = away_ml

    return comparison


def get_pinnacle_odds_from_news(search_query: str) -> dict | None:
    """Search for Pinnacle/sharp bookmaker odds mentioned in news/forums."""
    query = f"{search_query} pinnacle odds betting line"
    articles = news.search_google_news(query, max_results=5, hours_back=72)

    if not articles:
        return None

    return {
        "query": search_query,
        "articles": len(articles),
        "sources": [a["title"] for a in articles[:3]],
        "note": "Manual review needed — odds mentioned in news articles",
    }
