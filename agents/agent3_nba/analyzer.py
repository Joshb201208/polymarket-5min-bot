"""
Agent 3 — NBA market analyzer.

Runs NBA-specific research: team records, player stats, injuries, home/away.
"""

import logging
from datetime import datetime, timezone

from agents.common.config import MIN_EDGE_THRESHOLD
from agents.common.polymarket_client import build_event_url, get_market_price
from agents.common.research_engine import ResearchResult, research_nba_market

logger = logging.getLogger(__name__)


def analyze_market(market: dict, event: dict) -> dict | None:
    """Analyze an NBA market for edge. Returns alert dict or None."""
    question = market.get("question") or market.get("title", "Unknown")
    slug = event.get("slug") or market.get("slug", "")
    market_prob = get_market_price(market)

    if market_prob is None:
        return None

    logger.info("Analyzing NBA: %s (price=%.2f)", question[:60], market_prob)

    try:
        result: ResearchResult = research_nba_market(market, event)
    except Exception as exc:
        logger.error("NBA research failed for %s: %s", question[:60], exc)
        return None

    if abs(result.edge) < MIN_EDGE_THRESHOLD:
        logger.debug("No edge on %s (edge=%.3f)", question[:40], result.edge)
        return None

    end_date_str = market.get("endDate") or market.get("end_date_iso") or ""
    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        resolves = end_date.strftime("%b %d, %Y")
    except (ValueError, AttributeError):
        resolves = "Unknown"

    alert = {
        "agent_name": "Agent 3 (NBA)",
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
        "NBA EDGE: %s — edge=%.1f%% direction=%s",
        question[:50], abs(result.edge) * 100, result.direction,
    )
    return alert
