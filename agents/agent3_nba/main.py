"""
Agent 3 Main — NBA agent entry point.
Scans for NBA markets and researches them using nba_api statistical data.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import telegram
from agents.common.bankroll import BankrollManager
from agents.common.paper_tracker import PaperTracker
from .scanner import scan_nba_markets
from .researcher import research_market

logger = logging.getLogger(__name__)

_analyzed_cache: dict[str, datetime] = {}


def run_cycle(bankroll: BankrollManager, paper_tracker: PaperTracker):
    """Run one analysis cycle for NBA markets."""
    logger.info("=== Agent 3 (NBA): Starting scan ===")

    markets = scan_nba_markets()

    analyzed = 0
    opportunities = 0

    for market in markets:
        market_id = market.get("id", "")

        # Cooldown check
        if market_id in _analyzed_cache:
            last = _analyzed_cache[market_id]
            if datetime.now(timezone.utc) - last < timedelta(hours=config.COOLDOWN_HOURS):
                continue

        try:
            analysis = research_market(market)
            _analyzed_cache[market_id] = datetime.now(timezone.utc)
            analyzed += 1

            if analysis:
                opportunities += 1
                edge = analysis["edge"]

                if edge >= config.MIN_EDGE:
                    telegram.alert_opportunity("NBA", market, analysis)

                if edge >= config.MIN_EDGE_BET:
                    paper_tracker.place_trade(market, analysis)

            time.sleep(2)  # Extra conservative for nba_api rate limits
        except Exception as e:
            logger.error(f"Error researching NBA market {market_id}: {e}")

        if analyzed >= 8:  # Fewer per cycle due to API rate limits
            break

    logger.info(f"Agent 3 done: {analyzed} analyzed, {opportunities} opportunities")
    _clean_cache()


def _clean_cache():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    expired = [k for k, v in _analyzed_cache.items() if v < cutoff]
    for k in expired:
        del _analyzed_cache[k]
