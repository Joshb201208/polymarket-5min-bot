"""
Agent 3 — Entry point for NBA markets scanner.
"""

import logging
import traceback

from agents.agent3_nba.scanner import scan_nba_markets, get_scan_count
from agents.agent3_nba.analyzer import analyze_market
from agents.common.paper_tracker import PaperTracker
from agents.common.telegram_alerts import send_edge_alert, send_error_alert

logger = logging.getLogger(__name__)

_alerts_sent = 0
_markets_scanned = 0


def run_agent3(paper_tracker: PaperTracker) -> None:
    """Execute one full scan cycle for Agent 3 (NBA)."""
    global _alerts_sent, _markets_scanned

    logger.info("=" * 50)
    logger.info("Agent 3 (NBA) — starting scan cycle")
    logger.info("=" * 50)

    try:
        candidates = scan_nba_markets()
        _markets_scanned = get_scan_count()

        if not candidates:
            logger.info("No qualifying NBA markets found this cycle")
            return

        logger.info("Analyzing %d NBA candidates...", len(candidates))

        for market, event in candidates:
            try:
                alert = analyze_market(market, event)
                if alert is None:
                    continue

                sent = send_edge_alert(**alert)
                if sent:
                    _alerts_sent += 1
                    logger.info("NBA alert sent: %s", alert["market_title"][:50])

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
                    agent_name="Agent 3 (NBA)",
                    reasoning="; ".join(alert["reasoning"][:3]),
                )

            except Exception as exc:
                logger.error("Error analyzing NBA market: %s", exc)
                continue

        logger.info("Agent 3 cycle complete — %d alerts sent", _alerts_sent)

    except Exception as exc:
        logger.error("Agent 3 scan cycle failed: %s\n%s", exc, traceback.format_exc())
        send_error_alert("Agent 3 (NBA)", str(exc))


def get_daily_stats() -> dict:
    return {
        "name": "Agent 3 (NBA)",
        "alerts": _alerts_sent,
        "scanned": _markets_scanned,
    }


def reset_daily_stats() -> None:
    global _alerts_sent, _markets_scanned
    _alerts_sent = 0
    _markets_scanned = 0
