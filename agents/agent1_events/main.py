"""
Agent 1 Main — Events agent entry point.
Scans for short-term event markets and researches them for edge.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import telegram
from agents.common.bankroll import BankrollManager
from agents.common.paper_tracker import PaperTracker
from .scanner import scan_event_markets, scan_volume_surges
from .researcher import research_market

logger = logging.getLogger(__name__)

# Track recently analyzed markets to avoid re-analysis
_analyzed_cache: dict[str, datetime] = {}


def run_cycle(bankroll: BankrollManager, paper_tracker: PaperTracker):
    """Run one analysis cycle for event markets."""
    logger.info("=== Agent 1 (Events): Starting scan ===")

    # Scan for qualifying markets
    markets = scan_event_markets()
    time.sleep(1)

    # Also check volume surges
    surges = scan_volume_surges()
    surge_ids = {m["id"] for m in surges}

    # Merge, prioritizing surges
    all_markets = surges + [m for m in markets if m["id"] not in surge_ids]

    analyzed = 0
    opportunities = 0

    for market in all_markets:
        market_id = market.get("id", "")

        # Cooldown check
        if market_id in _analyzed_cache:
            last_analyzed = _analyzed_cache[market_id]
            if datetime.now(timezone.utc) - last_analyzed < timedelta(hours=config.COOLDOWN_HOURS):
                continue

        # Research
        try:
            analysis = research_market(market)
            _analyzed_cache[market_id] = datetime.now(timezone.utc)
            analyzed += 1

            if analysis:
                opportunities += 1
                edge = analysis["edge"]

                # Alert on any edge > MIN_EDGE (5%)
                if edge >= config.MIN_EDGE:
                    telegram.alert_opportunity("Events", market, analysis)

                # Paper trade on edge > MIN_EDGE_BET (7%)
                if edge >= config.MIN_EDGE_BET:
                    paper_tracker.place_trade(market, analysis)

            time.sleep(0.5)  # Rate limit between markets
        except Exception as e:
            logger.error(f"Error researching market {market_id}: {e}")

        # Limit per cycle to avoid long runs
        if analyzed >= 15:
            break

    logger.info(f"Agent 1 done: {analyzed} analyzed, {opportunities} opportunities")

    # Clean old cache entries
    _clean_cache()


def _clean_cache():
    """Remove cache entries older than 24h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    expired = [k for k, v in _analyzed_cache.items() if v < cutoff]
    for k in expired:
        del _analyzed_cache[k]
