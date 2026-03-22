"""
Agent 3 Researcher — NBA betting research engine.

STRATEGY: Use nba_api data that Polymarket traders might not be factoring in.
NBA has the richest free data.

Research pipeline:
1. Get current standings + conference rank
2. Get team form (last 10 games)
3. Calculate rest days (back-to-back = ~3% disadvantage historically)
4. Get H2H record this season
5. Home/away splits
6. Key player injury check (Google News)
7. Calculate power rating for each team
8. Generate fair probability
9. Compare to Polymarket price
10. Compare to ESPN odds (if available)
11. If edge > 5%, execute trade

FIX: Variable edge detection in news-based fallback.
"""

import logging
import re
import time

from agents.common import config
from agents.common.data_sources import nba_stats
from agents.common.data_sources import odds_comparison
from agents.common.data_sources import news

logger = logging.getLogger(__name__)


def research_market(market: dict) -> dict | None:
    """Deep research on an NBA market. Returns analysis or None."""
    question = market.get("question", "")
    yes_price = market.get("yes_price")

    if not question or yes_price is None:
        return None

    logger.info(f"Researching NBA: {question[:80]}")

    # Extract team names
    teams = _extract_nba_teams(question)
    if not teams or len(teams) < 2:
        return _news_based_analysis(market)

    team1, team2 = teams[0], teams[1]

    # 1. Get team records
    record1 = nba_stats.get_team_record(team1)
    time.sleep(0.6)
    record2 = nba_stats.get_team_record(team2)
    time.sleep(0.6)

    if not record1 or not record2:
        logger.warning(f"Could not get records for {team1} / {team2}")
        return _news_based_analysis(market)

    # 2. Get recent form
    form1 = nba_stats.get_team_form(team1, n_games=10)
    time.sleep(0.6)
    form2 = nba_stats.get_team_form(team2, n_games=10)
    time.sleep(0.6)

    # 3. Rest days
    rest1 = nba_stats.get_team_rest_days(team1)
    time.sleep(0.6)
    rest2 = nba_stats.get_team_rest_days(team2)
    time.sleep(0.6)

    # 4. H2H
    h2h = nba_stats.get_h2h_record(team1, team2)
    time.sleep(0.6)

    # 5. Injuries
    injuries1 = nba_stats.get_key_player_status(team1)
    time.sleep(0.6)
    injuries2 = nba_stats.get_key_player_status(team2)
    time.sleep(0.6)

    # 6. Power ratings
    power1 = nba_stats.calculate_team_power_rating(team1)
    time.sleep(0.6)
    power2 = nba_stats.calculate_team_power_rating(team2)
    time.sleep(0.6)

    # 7. Calculate fair probability
    fair_prob = _calculate_fair_probability(
        record1, record2, form1, form2,
        rest1, rest2, h2h, injuries1, injuries2,
        power1, power2
    )

    # 8. Determine which team market is asking about
    market_team = _determine_market_team(question, team1, team2)
    if market_team == team2:
        fair_prob = 1 - fair_prob  # Flip if asking about team2

    # 9. Compare to ESPN odds
    espn_comparison = None
    try:
        espn_comparison = odds_comparison.get_nba_odds_comparison(
            team1, team2, fair_prob
        )
    except Exception as e:
        logger.debug(f"ESPN odds comparison failed: {e}")

    # Adjust with ESPN data if available
    if espn_comparison and espn_comparison.get("bookmaker_prob"):
        bookie_prob = espn_comparison["bookmaker_prob"]
        # Weight: 60% our model, 40% bookmaker
        fair_prob = fair_prob * 0.6 + bookie_prob * 0.4

    fair_prob = max(0.05, min(0.95, fair_prob))

    # 10. Calculate edge
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
        logger.info(f"No NBA edge: {question[:50]} (best: {max(edge_yes, edge_no):.1%})")
        return None

    # Confidence
    confidence = _assess_confidence(
        edge, record1, record2, form1, form2, espn_comparison
    )

    # Build reasoning
    reasoning_parts = []
    if record1 and record2:
        reasoning_parts.append(
            f"{record1['team']}: {record1['wins']}-{record1['losses']} "
            f"(#{record1.get('conference_rank', '?')} {record1.get('conference', '')})"
        )
        reasoning_parts.append(
            f"{record2['team']}: {record2['wins']}-{record2['losses']} "
            f"(#{record2.get('conference_rank', '?')} {record2.get('conference', '')})"
        )
    if form1:
        reasoning_parts.append(f"Form L10: {form1['wins']}-{form1['losses']}")
    if rest1 is not None and rest2 is not None:
        reasoning_parts.append(f"Rest: {team1} {rest1}d, {team2} {rest2}d")
    if h2h:
        reasoning_parts.append(f"H2H: {h2h['team1_wins']}-{h2h['team2_wins']}")
    if power1 and power2:
        reasoning_parts.append(f"Power: {power1:.1f} vs {power2:.1f}")
    reasoning_parts.append(f"Fair: {fair_prob:.0%} vs market {yes_price:.0%}")

    return {
        "side": side,
        "price": price,
        "fair_probability": fair_prob,
        "edge": edge,
        "confidence": confidence,
        "reasoning": reasoning_parts,
    }


def _extract_nba_teams(question: str) -> list[str]:
    """Extract NBA team names from a market question."""
    from nba_api.stats.static import teams as nba_teams

    all_teams = nba_teams.get_teams()
    found = []

    q_lower = question.lower()
    for team in all_teams:
        names_to_check = [
            team["full_name"].lower(),
            team["nickname"].lower(),
            team["city"].lower(),
        ]
        for name in names_to_check:
            if name in q_lower and team["full_name"] not in found:
                found.append(team["full_name"])
                break

    # Also try common patterns
    if len(found) < 2:
        patterns = [
            r"Will (.+?) (?:beat|defeat|win against) (.+?)[\?]",
            r"(.+?) vs\.? (.+?)[\?]",
        ]
        for pattern in patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                t1, t2 = match.group(1).strip(), match.group(2).strip()
                for team in all_teams:
                    if t1.lower() in team["full_name"].lower() and team["full_name"] not in found:
                        found.append(team["full_name"])
                    if t2.lower() in team["full_name"].lower() and team["full_name"] not in found:
                        found.append(team["full_name"])
                break

    return found[:2]


def _determine_market_team(question: str, team1: str, team2: str) -> str:
    """Determine which team YES refers to."""
    q = question.lower()
    # "Will X beat Y" -> X is the YES team
    for pattern in [r"will (.+?) (?:beat|defeat|win)", r"(.+?) to win"]:
        match = re.search(pattern, q)
        if match:
            text = match.group(1)
            if team1.lower() in text or any(w in text for w in team1.lower().split()):
                return team1
            if team2.lower() in text or any(w in text for w in team2.lower().split()):
                return team2
    return team1  # Default to first team


def _calculate_fair_probability(
    record1, record2, form1, form2,
    rest1, rest2, h2h, injuries1, injuries2,
    power1, power2
) -> float:
    """Calculate fair probability that team1 wins.

    Uses power ratings as base, then adjusts for:
    - Recent form differential
    - Rest day advantage (~3% for B2B)
    - H2H history
    - Injury impact
    """
    # Base from power ratings
    if power1 and power2:
        total = power1 + power2
        base_prob = power1 / total if total > 0 else 0.5
    elif record1 and record2:
        w1 = record1.get("win_pct", 0.5)
        w2 = record2.get("win_pct", 0.5)
        total = w1 + w2
        base_prob = w1 / total if total > 0 else 0.5
    else:
        base_prob = 0.5

    adj = 0

    # Form adjustment
    if form1 and form2:
        form_diff = form1.get("win_pct", 0.5) - form2.get("win_pct", 0.5)
        adj += form_diff * 0.10  # Max ~10% from form

    # Rest day adjustment (back-to-back = ~3% disadvantage)
    if rest1 is not None and rest2 is not None:
        if rest1 == 0 and rest2 > 0:
            adj -= 0.03
        elif rest2 == 0 and rest1 > 0:
            adj += 0.03
        elif rest1 > rest2 + 1:
            adj += 0.01
        elif rest2 > rest1 + 1:
            adj -= 0.01

    # H2H adjustment
    if h2h and h2h.get("games", 0) > 0:
        h2h_wp = h2h["team1_wins"] / h2h["games"]
        adj += (h2h_wp - 0.5) * 0.05  # Small H2H factor

    # Injury adjustment
    inj_diff = len(injuries2) - len(injuries1)
    adj += inj_diff * 0.015  # ~1.5% per injury article differential

    fair = base_prob + adj
    return max(0.10, min(0.90, fair))


def _assess_confidence(edge, record1, record2, form1, form2,
                       espn_comparison) -> str:
    """Assess confidence level."""
    score = 0

    if edge > 0.15:
        score += 2
    elif edge > 0.10:
        score += 1

    if record1 and record2:
        score += 1

    if form1 and form2:
        score += 1

    if espn_comparison and espn_comparison.get("best_edge", 0) > 0.05:
        score += 2  # ESPN agrees = high confidence

    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def _news_based_analysis(market: dict) -> dict | None:
    """Fallback: news-based analysis when team extraction fails.

    FIX: Variable edges — not flat 8%.
    """
    question = market.get("question", "")
    yes_price = market.get("yes_price", 0.5)

    keywords = news.extract_keywords(question)
    news_data = news.get_comprehensive_news(" ".join(keywords[:5]) + " NBA")

    sentiment = news_data.get("sentiment", 0)
    news_count = news_data.get("total_articles", 0)

    if abs(sentiment) < 0.2:
        return None

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
        "reasoning": [f"News-based: {news_data['sentiment_label']} ({news_count} articles), momentum {momentum_adj:+.2%}"],
    }
