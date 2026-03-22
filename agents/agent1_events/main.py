"""
Agent 1 Main — Events agent entry point.
Scans for high-quality event markets and EXECUTES trades via executor.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

from agents.common import config
from agents.common import telegram
from agents.common.bankroll import BankrollManager
from agents.common.executor import Executor
from .scanner import scan_event_markets, scan_volume_surges
from .researcher import research_market

logger = logging.getLogger(__name__)

AGENT_NAME = "Events"

# Track recently analyzed markets to avoid re-analysis
_analyzed_cache: dict[str, datetime] = {}


def run_cycle(bankroll: BankrollManager, executor: Executor):
    """Run one analysis cycle for event markets."""
    logger.info("=== Agent 1 (Events): Starting scan ===")

    # Sync bankroll from chain if live
    if executor.mode == "live":
        balance = executor.get_balance()
        if balance > 0:
            bankroll.sync_from_chain(balance)

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
                edge = analysis["edge"]
                price = analysis["price"]
                side = analysis["side"]
                confidence = analysis.get("confidence", "medium")

                # Alert on any edge > MIN_EDGE (4%)
                if edge >= config.MIN_EDGE and edge < config.MIN_EDGE_BET:
                    telegram.alert_opportunity(AGENT_NAME, market, analysis)

                # EXECUTE trade on edge > MIN_EDGE_BET (5%)
                if edge >= config.MIN_EDGE_BET:
                    # HARD REJECT — skip 0c prices and near-resolved
                    if price is None or price <= 0.03 or price >= 0.97:
                        continue

                    # Kelly size the bet using price -> decimal odds
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
                        # Track in bankroll
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

                        # Send Telegram confirmation
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

            time.sleep(0.5)  # Rate limit between markets
        except Exception as e:
            logger.error(f"Error researching market {market_id}: {e}")

        # Limit per cycle to avoid long runs
        if analyzed >= 15:
            break

    logger.info(f"Agent 1 done: {analyzed} analyzed, {opportunities} trades executed")

    # Clean old cache entries
    _clean_cache()


def _clean_cache():
    """Remove cache entries older than 24h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    expired = [k for k, v in _analyzed_cache.items() if v < cutoff]
    for k in expired:
        del _analyzed_cache[k]
