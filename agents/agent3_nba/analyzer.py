"""Agent 3 Analyzer — NBA-specific research + fair probability calculation."""

import logging

from agents.common.config import MIN_EDGE_THRESHOLD
from agents.common.research_engine import (
    research_nba,
    analyze_market_signals,
    calculate_fair_probability,
    determine_confidence,
    suggested_bet_size,
)
from agents.agent1_events.analyzer import _format_date

logger = logging.getLogger(__name__)


def analyze_nba_market(market: dict) -> dict | None:
    """Analyze an NBA market. Returns analysis dict if edge found, else None."""
    question = market.get("question", market.get("title", "Unknown"))
    description = market.get("description", "")
    slug = market.get("slug") or market.get("conditionId", "")
    yes_price = market.get("_yes_price", 0.5)
    event = market.get("_event", {})
    event_url = market.get("_event_url", "")
    end_date = market.get("endDate", "")

    logger.info("Analyzing NBA: %s (YES @ %.2f)", question[:60], yes_price)

    # Step 1: NBA-specific research
    search_query = _extract_nba_query(question, description, event)
    research = research_nba(search_query)
    sentiment = research.get("sentiment", 0.0)
    research_notes = research.get("notes", [])
    sources = research.get("sources", [])

    # Step 2: Market data signals
    market_adj, market_notes = analyze_market_signals(market)

    # Step 3: Fair probability
    volume = _safe_float(market.get("volume", 0)) or 0
    fair_prob = calculate_fair_probability(
        market_prob=yes_price,
        sentiment_score=sentiment,
        market_adjustment=market_adj,
        volume_usd=volume,
    )

    # Step 4: Edge
    edge = fair_prob - yes_price
    all_notes = research_notes + market_notes

    if abs(edge) < MIN_EDGE_THRESHOLD:
        logger.debug("No edge for NBA market %s (edge=%.3f)", slug[:40], edge)
        return None

    confidence = determine_confidence(edge, len(all_notes), volume)
    direction = "BUY YES" if edge > 0 else "BUY NO"
    size = suggested_bet_size(edge, confidence)
    resolves = _format_date(end_date) if end_date else "TBD"

    logger.info(
        "NBA EDGE: %s | edge=%.1f%% | conf=%s",
        slug[:40], abs(edge) * 100, confidence,
    )

    return {
        "market_slug": slug,
        "market_question": question,
        "market_url": event_url,
        "market_price": yes_price,
        "fair_value": fair_prob,
        "edge": edge,
        "confidence": confidence,
        "direction": direction,
        "suggested_size": size,
        "resolves": resolves,
        "end_date": end_date,
        "reasoning": all_notes,
        "sources": sources,
        "condition_id": market.get("conditionId", ""),
    }


def _extract_nba_query(question: str, description: str, event: dict) -> str:
    """Build an NBA-focused search query from market data."""
    event_title = event.get("title", "")
    query = event_title if event_title else question
    query = query.strip().rstrip("?")
    if len(query) > 80:
        query = query[:80]
    return query


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
