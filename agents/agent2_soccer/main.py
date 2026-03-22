"""
Agent 2 — Entry point for Soccer markets scanner.
"""

import logging
import traceback

from agents.agent2_soccer.scanner import scan_soccer_markets, get_scan_count
from agents.agent2_soccer.analyzer import analyze_market
from agents.common.paper_tracker import PaperTracker
from agents.common.telegram_alerts import send_edge_alert, send_error_alert

logger = logging.getLogger(__name__)

_alerts_sent = 0
_markets_scanned = 0


def run_agent2(paper_tracker: PaperTracker) -> None:
    """Execute one full scan cycle for Agent 2 (Soccer)."""
    global _alerts_sent, _markets_scanned

    logger.info("=" * 50)
    logger.info("Agent 2 (Soccer) — starting scan cycle")
    logger.info("=" * 50)

    try:
        candidates = scan_soccer_markets()
        _markets_scanned = get_scan_count()

        if not candidates:
            logger.info("No qualifying soccer markets found this cycle")
            return

        logger.info("Analyzing %d soccer candidates...", len(candidates))

        for market, event in candidates:
            try:
                alert = analyze_market(market, event)
                if alert is None:
                    continue

                sent = send_edge_alert(**alert)
                if sent:
                    _alerts_sent += 1
                    logger.info("Soccer alert sent: %s", alert["market_title"][:50])

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
                    agent_name="Agent 2 (Soccer)",
                    reasoning="; ".join(alert["reasoning"][:3]),
                )

            except Exception as exc:
                logger.error("Error analyzing soccer market: %s", exc)
                continue

        logger.info("Agent 2 cycle complete — %d alerts sent", _alerts_sent)

    except Exception as exc:
        logger.error("Agent 2 scan cycle failed: %s\n%s", exc, traceback.format_exc())
        send_error_alert("Agent 2 (Soccer)", str(exc))


def get_daily_stats() -> dict:
    return {
        "name": "Agent 2 (Soccer)",
        "alerts": _alerts_sent,
        "scanned": _markets_scanned,
    }


def reset_daily_stats() -> None:
    global _alerts_sent, _markets_scanned
    _alerts_sent = 0
    _markets_scanned = 0
