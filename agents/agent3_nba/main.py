"""
Agent 3 Main — NBA agent entry point.
Scans for NBA markets and EXECUTES trades via executor using nba_api data.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import telegram
from agents.common.bankroll import BankrollManager
from agents.common.executor import Executor
from .scanner import scan_nba_markets
from .researcher import research_market

logger = logging.getLogger(__name__)

AGENT_NAME = "NBA"

_analyzed_cache: dict[str, datetime] = {}


def run_cycle(bankroll: BankrollManager, executor: Executor):
    """Run one analysis cycle for NBA markets."""
    logger.info("=== Agent 3 (NBA): Starting scan ===")

    # Sync bankroll from chain if live
    if executor.mode == "live":
        balance = executor.get_balance()
        if balance > 0:
            bankroll.sync_from_chain(balance)

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
                edge = analysis["edge"]
                price = analysis["price"]
                side = analysis["side"]
                confidence = analysis.get("confidence", "medium")

                # Alert on edge between MIN_EDGE and MIN_EDGE_BET
                if edge >= config.MIN_EDGE and edge < config.MIN_EDGE_BET:
                    telegram.alert_opportunity(AGENT_NAME, market, analysis)

                # EXECUTE trade on edge > MIN_EDGE_BET
                if edge >= config.MIN_EDGE_BET:
                    # HARD REJECT — skip 0c prices and near-resolved
                    if price is None or price <= 0.03 or price >= 0.97:
                        continue

                    # Kelly size with proper decimal odds conversion
                    decimal_odds = 1.0 / price
                    bet_size = bankroll.kelly_size(edge, decimal_odds, confidence)

                    if bet_size < config.MIN_BET:
                        continue
                    if bet_size > bankroll.available_capital():
                        bet_size = bankroll.available_capital()
                    if bet_size < config.MIN_BET:
                        continue

                    # Get token ID
                    token_id = market.get("yes_token") if side == "YES" else market.get("no_token")
                    if not token_id:
                        continue

                    # EXECUTE THE TRADE
                    order = executor.place_buy(
                        token_id, price, bet_size,
                        neg_risk=market.get("neg_risk", False),
                    )

                    if order.get("success"):
                        bankroll.open_position(
                            market_id=market.get("slug", market_id),
                            question=market.get("question", ""),
                            side=side,
                            entry_price=price,
                            size=bet_size,
                            edge=edge,
                            token_id=token_id,
                            confidence=confidence,
                            fair_probability=analysis.get("fair_probability", 0),
                            end_date=market.get("end_date", ""),
                        )

                        telegram.send_trade_executed(
                            agent_name=AGENT_NAME,
                            question=market.get("question", ""),
                            side=side,
                            price=price,
                            size=bet_size,
                            edge=edge,
                            confidence=confidence,
                            reasoning=analysis.get("reasoning", ""),
                            mode=executor.mode,
                            order_id=order.get("order_id", ""),
                            balance=bankroll.available_capital(),
                            url=market.get("url", ""),
                        )
                        opportunities += 1

            time.sleep(2)  # Extra conservative for nba_api rate limits
        except Exception as e:
            logger.error(f"Error researching NBA market {market_id}: {e}")

        if analyzed >= 8:  # Fewer per cycle due to API rate limits
            break

    logger.info(f"Agent 3 done: {analyzed} analyzed, {opportunities} trades executed")
    _clean_cache()


def _clean_cache():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    expired = [k for k, v in _analyzed_cache.items() if v < cutoff]
    for k in expired:
        del _analyzed_cache[k]
