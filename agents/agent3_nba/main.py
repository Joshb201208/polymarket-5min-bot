"""Agent 3 Entry Point — NBA Markets Scanner.

Scans Polymarket for NBA/basketball markets, researches teams
and players via free news sources, and alerts when edge is found.
"""

import logging
import time

from agents.agent3_nba.scanner import NBAScanner
from agents.agent3_nba.analyzer import analyze_nba_market
from agents.common.paper_tracker import PaperTracker
from agents.common.telegram_alerts import send_edge_alert, send_error_alert

logger = logging.getLogger(__name__)

AGENT_NAME = "Agent 3 (NBA)"


def run_agent3(paper_tracker: PaperTracker) -> dict:
    """Run one full NBA scan cycle. Returns stats dict."""
    stats = {"alerts_sent": 0, "markets_scanned": 0}

    try:
        scanner = NBAScanner()
        candidates = scanner.scan()
        stats["markets_scanned"] = scanner.markets_scanned

        for market in candidates:
            try:
                result = analyze_nba_market(market)
                if result is None:
                    continue

                sent = send_edge_alert(
                    agent_name=AGENT_NAME,
                    market_title=result["market_question"],
                    market_url=result["market_url"],
                    market_price=result["market_price"] * 100,
                    fair_value=result["fair_value"] * 100,
                    edge=result["edge"],
                    confidence=result["confidence"],
                    direction=result["direction"],
                    suggested_size=result["suggested_size"],
                    resolves=result["resolves"],
                    reasoning=result["reasoning"],
                    sources=result["sources"],
                )
                if sent:
                    stats["alerts_sent"] += 1

                paper_tracker.record_trade(
                    market_slug=result["market_slug"],
                    market_question=result["market_question"],
                    direction="YES" if "YES" in result["direction"] else "NO",
                    entry_price=result["market_price"],
                    recommended_size=result["suggested_size"],
                    fair_prob=result["fair_value"],
                    market_prob=result["market_price"],
                    edge=result["edge"],
                    confidence=result["confidence"],
                    agent_name=AGENT_NAME,
                    reasoning="; ".join(result["reasoning"]),
                    end_date=result.get("end_date", ""),
                    condition_id=result.get("condition_id", ""),
                )

                time.sleep(1)

            except Exception:
                logger.exception("Error analyzing NBA market %s", market.get("slug", "?"))
                continue

        logger.info(
            "Agent 3 cycle complete — %d markets scanned, %d alerts sent",
            stats["markets_scanned"], stats["alerts_sent"],
        )

    except Exception as exc:
        logger.exception("Agent 3 scan cycle failed")
        send_error_alert(AGENT_NAME, str(exc))

    return stats
