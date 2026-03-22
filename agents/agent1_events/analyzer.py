"""Agent 1 Analyzer — researches event markets and calculates fair probability."""

import logging
from datetime import datetime, timezone

from agents.common.config import MIN_EDGE_THRESHOLD
from agents.common.research_engine import (
    research_event,
    analyze_market_signals,
    calculate_fair_probability,
    determine_confidence,
    suggested_bet_size,
)

logger = logging.getLogger(__name__)


def analyze_market(market: dict) -> dict | None:
    """Analyze a single market candidate. Returns analysis dict if edge found, else None."""
    question = market.get("question", market.get("title", "Unknown"))
    description = market.get("description", "")
    slug = market.get("slug") or market.get("conditionId", "")
    yes_price = market.get("_yes_price", 0.5)
    event = market.get("_event", {})
    event_url = market.get("_event_url", "")
    end_date = market.get("endDate", "")

    logger.info("Analyzing: %s (YES @ %.2f)", question[:60], yes_price)

    # Step 1: Web research
    search_query = _build_search_query(question, description)
    research = research_event(search_query)
    sentiment = research.get("sentiment", 0.0)
    research_notes = research.get("notes", [])
    sources = research.get("sources", [])

    # Step 2: Market data signals
    market_adj, market_notes = analyze_market_signals(market)

    # Step 3: Calculate fair probability
    volume = _safe_float(market.get("volume", 0)) or 0
    fair_prob = calculate_fair_probability(
        market_prob=yes_price,
        sentiment_score=sentiment,
        market_adjustment=market_adj,
        volume_usd=volume,
    )

    # Step 4: Calculate edge
    edge = fair_prob - yes_price  # positive = YES underpriced, negative = NO underpriced

    all_notes = research_notes + market_notes

    # Step 5: Check if edge meets threshold
    if abs(edge) < MIN_EDGE_THRESHOLD:
        logger.debug("No edge for %s (edge=%.3f)", slug[:40], edge)
        return None

    confidence = determine_confidence(edge, len(all_notes), volume)
    direction = "BUY YES" if edge > 0 else "BUY NO"
    size = suggested_bet_size(edge, confidence)

    # Format resolution date
    resolves = _format_date(end_date) if end_date else "TBD"

    logger.info(
        "EDGE FOUND: %s | edge=%.1f%% | conf=%s | direction=%s",
        slug[:40], abs(edge) * 100, confidence, direction,
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


def _build_search_query(question: str, description: str) -> str:
    """Build a concise search query from market question/description."""
    # Use the question directly — it's usually a good search term
    query = question.strip().rstrip("?")
    # Limit length for search engines
    if len(query) > 100:
        query = query[:100]
    return query


def _format_date(iso_str: str) -> str:
    """Format ISO date to human-readable string."""
    try:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(iso_str, fmt).replace(tzinfo=timezone.utc)
                return dt.strftime("%b %d, %Y")
            except ValueError:
                continue
    except Exception:
        pass
    return iso_str[:10] if len(iso_str) >= 10 else "TBD"


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
