"""
Agent 2 Researcher — Soccer betting research engine.

STRATEGY: Beat Polymarket on match markets using real stats.
Sports markets on Polymarket are often less efficient than traditional
bookmakers because the Polymarket crowd is more politics/crypto focused.

Research pipeline:
1. Get league standings from ESPN
2. Get team form (last 5-10 games)
3. Check H2H record
4. Search for injuries/suspensions via Google News
5. Home/away analysis
6. Compare to traditional bookmaker odds (ESPN odds data)
7. Calculate our fair odds using statistical model
8. Compare to Polymarket price
9. If gap > 5%, execute trade

FIX: Variable edge detection — sentiment/momentum produce RANGE of edges.
"""

import logging
import re
import time

from agents.common import config
from agents.common.data_sources import soccer_stats
from agents.common.data_sources import odds_comparison
from agents.common.data_sources import news

logger = logging.getLogger(__name__)


def research_market(market: dict) -> dict | None:
    """Deep research on a soccer market. Returns analysis or None."""
    question = market.get("question", "")
    yes_price = market.get("yes_price")

    if not question or yes_price is None:
        return None

    logger.info(f"Researching soccer: {question[:80]}")

    # Try to extract team names and league from question
    teams = _extract_teams(question)
    league = _detect_league(question)

    if not teams or len(teams) < 2:
        # Fall back to news-based analysis
        return _news_based_analysis(market)

    home_team, away_team = teams[0], teams[1]

    # 1. Get statistical prediction
    prediction = soccer_stats.calculate_match_prediction(home_team, away_team, league)
    time.sleep(0.3)

    # 2. Get injury news
    home_injuries = soccer_stats.get_injury_news(home_team)
    time.sleep(0.3)
    away_injuries = soccer_stats.get_injury_news(away_team)

    # 3. Determine which side the market is asking about
    side_team, is_home = _determine_market_side(question, home_team, away_team)

    # 4. Get our fair probability for the asked outcome
    if "draw" in question.lower():
        fair_prob = prediction.get("draw_prob", 0.25)
    elif side_team and side_team.lower() in home_team.lower():
        fair_prob = prediction.get("home_win_prob", 0.45)
    elif side_team and side_team.lower() in away_team.lower():
        fair_prob = prediction.get("away_win_prob", 0.30)
    else:
        # Can't determine — use home win as default for "will X win"
        fair_prob = prediction.get("home_win_prob", 0.45)

    # 5. Injury adjustment
    injury_adj = _injury_adjustment(home_injuries, away_injuries, is_home)
    fair_prob += injury_adj

    fair_prob = max(0.05, min(0.95, fair_prob))

    # 6. Calculate edge
    edge_yes = fair_prob - yes_price
    edge_no = (1 - fair_prob) - (1 - yes_price)

    if edge_yes > edge_no and edge_yes > config.MIN_EDGE:
        side = "YES"
        edge = edge_yes
        price = yes_price
    elif edge_no > config.MIN_EDGE:
        side = "NO"
        edge = edge_no
        price = 1 - yes_price
    else:
        logger.info(f"No soccer edge: {question[:50]} (best: {max(edge_yes, edge_no):.1%})")
        return None

    confidence = prediction.get("confidence", "medium")

    reasoning = [
        prediction.get('reasoning', ''),
        f"Injuries: {len(home_injuries)} home, {len(away_injuries)} away articles",
        f"Fair prob: {fair_prob:.0%} vs market {yes_price:.0%}",
    ]

    return {
        "side": side,
        "price": price,
        "fair_probability": fair_prob,
        "edge": edge,
        "confidence": confidence,
        "reasoning": reasoning,
    }


def _extract_teams(question: str) -> list[str]:
    """Extract team names from a market question."""
    # Common patterns: "Will X beat Y", "X vs Y", "X to win against Y"
    patterns = [
        r"Will (.+?) (?:beat|defeat|win against) (.+?)[\?]",
        r"(.+?) vs\.? (.+?)[\?]",
        r"Will (.+?) win (?:against|vs\.?) (.+?)[\?]",
        r"(.+?) to (?:beat|win against|defeat) (.+?)[\?]",
    ]

    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            return [match.group(1).strip(), match.group(2).strip()]

    # Try splitting by "vs"
    if " vs " in question.lower():
        parts = re.split(r'\s+vs\.?\s+', question, flags=re.IGNORECASE)
        if len(parts) >= 2:
            # Clean up
            team1 = re.sub(r'^will\s+', '', parts[0], flags=re.IGNORECASE).strip()
            team2 = re.sub(r'\?.*$', '', parts[1]).strip()
            return [team1, team2]

    return []


def _detect_league(question: str) -> str:
    """Detect which league from the question."""
    q = question.lower()
    league_keywords = {
        "Premier League": ["premier league", "epl", "liverpool", "arsenal", "chelsea",
                           "manchester", "tottenham", "newcastle", "aston villa"],
        "La Liga": ["la liga", "real madrid", "barcelona", "atletico"],
        "Bundesliga": ["bundesliga", "bayern", "dortmund", "leverkusen"],
        "Serie A": ["serie a", "juventus", "inter", "milan", "napoli", "roma"],
        "Ligue 1": ["ligue 1", "psg", "marseille", "lyon"],
        "Champions League": ["champions league", "ucl"],
    }

    for league, keywords in league_keywords.items():
        for kw in keywords:
            if kw in q:
                return league

    return "Premier League"  # Default


def _determine_market_side(question: str, home_team: str,
                           away_team: str) -> tuple[str, bool]:
    """Determine which team the YES outcome refers to."""
    q = question.lower()
    if home_team.lower() in q and ("win" in q or "beat" in q):
        return home_team, True
    if away_team.lower() in q and ("win" in q or "beat" in q):
        return away_team, False
    return home_team, True  # Default to first team mentioned


def _injury_adjustment(home_injuries: list, away_injuries: list,
                       is_home: bool) -> float:
    """Adjust fair probability based on injury news."""
    home_count = len(home_injuries)
    away_count = len(away_injuries)

    # Each injury article suggests ~1-2% impact
    adj = (away_count - home_count) * 0.015
    if not is_home:
        adj = -adj

    return max(-0.10, min(0.10, adj))


def _news_based_analysis(market: dict) -> dict | None:
    """Fallback: analyze using news when team extraction fails.

    FIX: Variable edges — not flat 8%.
    """
    question = market.get("question", "")
    yes_price = market.get("yes_price", 0.5)

    keywords = news.extract_keywords(question)
    news_data = news.get_comprehensive_news(" ".join(keywords[:5]))

    sentiment = news_data.get("sentiment", 0)
    news_count = news_data.get("total_articles", 0)

    if abs(sentiment) < 0.2:
        return None  # No strong signal

    # Variable edge calculation
    article_count_factor = min(news_count / 10, 1.5) if news_count > 0 else 0.5
    base_rate = 0.08  # Sports base rate

    # Sentiment adjustment varies with article count
    sentiment_adj = sentiment * article_count_factor * base_rate

    # Momentum from market data
    one_day_change = market.get("one_day_change", 0)
    one_week_change = market.get("one_week_change", 0)
    momentum_adj = one_day_change * 0.5 + one_week_change * 0.3

    # Volume surge bonus
    volume_24h = market.get("volume_24h", 0)
    total_volume = market.get("volume", 1)
    volume_ratio = volume_24h / total_volume if total_volume > 0 else 0
    volume_adj = 0
    if volume_ratio > 0.3:  # 3x+ surge
        volume_adj = min(0.05, 0.02 + volume_ratio * 0.02)

    fair_prob = yes_price + sentiment_adj + momentum_adj
    if sentiment > 0:
        fair_prob += volume_adj
    else:
        fair_prob -= volume_adj

    fair_prob = max(0.05, min(0.95, fair_prob))

    edge = abs(fair_prob - yes_price)
    if edge < config.MIN_EDGE:
        return None

    side = "YES" if fair_prob > yes_price else "NO"
    price = yes_price if side == "YES" else (1 - yes_price)

    return {
        "side": side,
        "price": price,
        "fair_probability": fair_prob,
        "edge": edge,
        "confidence": "low",
        "reasoning": [f"News-based: {news_data['sentiment_label']} sentiment from {news_count} articles, momentum {momentum_adj:+.2%}"],
    }
