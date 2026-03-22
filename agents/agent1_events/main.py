"""
Agent 1 — Entry point for Event/News markets scanner.

Runs a single scan cycle: scan -> analyze -> alert -> paper-track.
Called on schedule by the orchestrator.
"""

import logging
import traceback

from agents.agent1_events.scanner import scan_event_markets, get_scan_count
from agents.agent1_events.analyzer import analyze_market
from agents.common.paper_tracker import PaperTracker
from agents.common.telegram_alerts import send_edge_alert, send_error_alert

logger = logging.getLogger(__name__)

# Module-level counters for daily summary
_alerts_sent = 0
_markets_scanned = 0


def run_agent1(paper_tracker: PaperTracker) -> None:
    """Execute one full scan cycle for Agent 1."""
    global _alerts_sent, _markets_scanned

    logger.info("=" * 50)
    logger.info("Agent 1 (Events) — starting scan cycle")
    logger.info("=" * 50)

    try:
        candidates = scan_event_markets()
        _markets_scanned = get_scan_count()

        if not candidates:
            logger.info("No qualifying event markets found this cycle")
            return

        logger.info("Analyzing %d candidate markets...", len(candidates))

        for market, event in candidates:
            try:
                alert = analyze_market(market, event)
                if alert is None:
                    continue

                # Send Telegram alert
                sent = send_edge_alert(**alert)
                if sent:
                    _alerts_sent += 1
                    logger.info("Alert sent for: %s", alert["market_title"][:50])

                # Paper-track the recommendation
                paper_tracker.record_trade(
                    market_slug=alert["market_slug"],
                    market_question=alert["market_title"],
                    direction=alert["direction"],
                    entry_price=alert["market_price"],
                    recommended_size=alert["suggested_size"],
                    fair_prob=alert["fair_value"],
                    market_prob=alert["market_price"],
                    edge=alert["edge"],
                    confidence=alert["confidence"],
                    agent_name="Agent 1 (Events)",
                    reasoning="; ".join(alert["reasoning"][:3]),
                )

            except Exception as exc:
                logger.error("Error analyzing market: %s", exc)
                continue

        logger.info("Agent 1 cycle complete — %d alerts sent", _alerts_sent)

    except Exception as exc:
        logger.error("Agent 1 scan cycle failed: %s\n%s", exc, traceback.format_exc())
        send_error_alert("Agent 1 (Events)", str(exc))


def get_daily_stats() -> dict:
    """Return stats for the daily summary."""
    return {
        "name": "Agent 1 (Events)",
        "alerts": _alerts_sent,
        "scanned": _markets_scanned,
    }


def reset_daily_stats() -> None:
    """Reset counters for a new day."""
    global _alerts_sent, _markets_scanned
    _alerts_sent = 0
    _markets_scanned = 0
