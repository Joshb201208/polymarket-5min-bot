"""
Agent 1 — Event market analyzer.

Takes candidate markets from the scanner and runs full research + analysis
to find mispriced opportunities.
"""

import logging
from datetime import datetime, timezone

from agents.common.config import MIN_EDGE_THRESHOLD
from agents.common.polymarket_client import build_event_url, get_market_price
from agents.common.research_engine import ResearchResult, research_event_market

logger = logging.getLogger(__name__)


def analyze_market(market: dict, event: dict) -> dict | None:
    """Analyze a single event market for edge.

    Returns an alert dict if edge exceeds threshold, else None.
    """
    question = market.get("question") or market.get("title", "Unknown")
    slug = event.get("slug") or market.get("slug", "")
    market_prob = get_market_price(market)

    if market_prob is None:
        logger.debug("Skipping %s — no price", question[:60])
        return None

    logger.info("Analyzing: %s (price=%.2f)", question[:60], market_prob)

    # Run full research pipeline
    try:
        result: ResearchResult = research_event_market(market, event)
    except Exception as exc:
        logger.error("Research failed for %s: %s", question[:60], exc)
        return None

    abs_edge = abs(result.edge)
    if abs_edge < MIN_EDGE_THRESHOLD:
        logger.debug(
            "No edge on %s (fair=%.3f, market=%.3f, edge=%.3f)",
            question[:40], result.fair_prob, market_prob, result.edge,
        )
        return None

    # Build resolve date string
    end_date_str = market.get("endDate") or market.get("end_date_iso") or ""
    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        resolves = end_date.strftime("%b %d, %Y")
    except (ValueError, AttributeError):
        resolves = "Unknown"

    alert = {
        "agent_name": "Agent 1 (Events)",
        "market_title": question,
        "market_url": build_event_url(slug),
        "market_slug": slug,
        "market_price": market_prob,
        "fair_value": result.fair_prob,
        "edge": result.edge,
        "confidence": result.confidence,
        "direction": result.direction,
        "suggested_size": result.suggested_size,
        "resolves": resolves,
        "reasoning": result.reasoning,
        "sources": result.sources,
    }

    logger.info(
        "EDGE FOUND: %s — edge=%.1f%% confidence=%s direction=%s",
        question[:50], abs_edge * 100, result.confidence, result.direction,
    )
    return alert
