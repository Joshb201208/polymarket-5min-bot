"""
Agent 1 Researcher — Deep research on general event markets.

STRATEGY: Find SHORT-TERM markets where news creates mispricing.
Target: Markets resolving in 1-14 days with >7% edge.

Research pipeline:
1. Parse market question + description + resolution criteria
2. Search Google News for recent developments (last 24-48h)
3. Search DuckDuckGo for context
4. Analyze market momentum (1h, 1d, 1w price changes)
5. Volume surge detection (new information arriving)
6. Time decay analysis (markets underprice near-term resolution)
7. Crowd efficiency discount (high-volume = efficient pricing)
8. Calculate fair probability and edge
9. Kelly size the bet
"""

import logging
import time
from datetime import datetime, timezone

from agents.common import config
from agents.common import polymarket_api as pm
from agents.common.data_sources import news

logger = logging.getLogger(__name__)


def research_market(market: dict) -> dict | None:
    """Deep research on a single event market. Returns analysis or None if no edge."""
    question = market.get("question", "")
    description = market.get("description", "")
    yes_price = market.get("yes_price")

    if not question or yes_price is None:
        return None

    logger.info(f"Researching: {question[:80]}")

    # 1. Extract keywords and get news
    keywords = news.extract_keywords(question)
    search_query = " ".join(keywords[:5])

    news_data = news.get_comprehensive_news(search_query, max_articles=10)
    time.sleep(0.3)

    # 2. Market momentum analysis
    momentum = _analyze_momentum(market)

    # 3. Volume analysis
    volume_signal = _analyze_volume(market)

    # 4. Time decay analysis
    time_factor = _time_decay_factor(market)

    # 5. News sentiment
    sentiment = news_data.get("sentiment", 0)
    news_count = news_data.get("total_articles", 0)

    # 6. Calculate fair probability
    fair_prob = _calculate_fair_probability(
        yes_price, sentiment, momentum, volume_signal, time_factor, news_count
    )

    # 7. Determine side and edge
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
        logger.info(f"No edge found for: {question[:60]} (best edge: {max(edge_yes, edge_no):.1%})")
        return None

    # 8. Confidence assessment
    confidence = _assess_confidence(edge, news_count, momentum, market)

    # Build reasoning
    reasoning = (
        f"News sentiment: {news_data['sentiment_label']} ({news_count} articles). "
        f"Momentum: 1d {market.get('one_day_change', 0):+.1%}, "
        f"1w {market.get('one_week_change', 0):+.1%}. "
        f"Volume 24h: ${market.get('volume_24h', 0):,.0f}. "
        f"Fair prob: {fair_prob:.0%} vs market {yes_price:.0%}."
    )

    return {
        "side": side,
        "price": price,
        "fair_probability": fair_prob,
        "edge": edge,
        "confidence": confidence,
        "reasoning": reasoning,
        "news_count": news_count,
        "sentiment": sentiment,
        "momentum": momentum,
    }


def _analyze_momentum(market: dict) -> float:
    """Analyze price momentum. Returns -1 to +1 signal."""
    one_day = market.get("one_day_change", 0)
    one_week = market.get("one_week_change", 0)

    # Weighted momentum: recent matters more
    momentum = one_day * 0.7 + one_week * 0.3

    # Normalize to -1 to 1 range
    return max(-1, min(1, momentum * 5))


def _analyze_volume(market: dict) -> float:
    """Analyze volume for information signals. Returns 0 to 1."""
    volume_24h = market.get("volume_24h", 0)
    total_volume = market.get("volume", 1)

    if total_volume <= 0:
        return 0

    ratio = volume_24h / total_volume
    # High ratio = new information, but also means market is adjusting
    return min(1.0, ratio * 3)


def _time_decay_factor(market: dict) -> float:
    """Markets closer to resolution are often underpriced if outcome is becoming clearer."""
    end_date_str = market.get("end_date", "")
    if not end_date_str:
        return 0

    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        hours_left = (end_date - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_left <= 0:
            return 0
        # Markets within 48h often have clearer outcomes
        if hours_left < 48:
            return 0.05  # Small boost
        if hours_left < 168:  # 1 week
            return 0.02
        return 0
    except Exception:
        return 0


def _calculate_fair_probability(yes_price: float, sentiment: float,
                                momentum: float, volume_signal: float,
                                time_factor: float, news_count: int) -> float:
    """Calculate our fair probability estimate.

    Start from market price, then adjust based on signals.
    """
    fair = yes_price

    # Sentiment adjustment (max +/- 8%)
    fair += sentiment * 0.08

    # Momentum: if price is trending, slight continuation bias
    fair += momentum * 0.03

    # Volume surge with momentum = stronger signal
    if volume_signal > 0.3 and abs(momentum) > 0.3:
        fair += momentum * volume_signal * 0.05

    # Time decay boost
    fair += time_factor

    # Crowd efficiency discount: high-volume markets are more efficient
    # Reduce our adjustment for very liquid markets
    if news_count < 3:
        fair *= 0.98  # Less news = less certain

    # Clamp
    return max(0.05, min(0.95, fair))


def _assess_confidence(edge: float, news_count: int,
                       momentum: float, market: dict) -> str:
    """Assess confidence level: low, medium, high."""
    score = 0

    if edge > 0.15:
        score += 2
    elif edge > 0.10:
        score += 1

    if news_count >= 5:
        score += 1
    elif news_count >= 10:
        score += 2

    if abs(momentum) > 0.5:
        score += 1

    if market.get("liquidity", 0) > 20000:
        score += 1

    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"
