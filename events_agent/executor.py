"""Paper/live execution for events agent via py-clob-client."""

from __future__ import annotations

import logging
import time
from typing import Optional

from events_agent.config import EventsConfig
from events_agent.models import EdgeResult, Position, Trade
from nba_agent.utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fee estimation (mirrors NBA agent pattern)
# ---------------------------------------------------------------------------
import functools
import urllib.request


@functools.lru_cache(maxsize=256)
def _get_fee_rate(token_id: str) -> float:
    """Fetch the fee rate from Polymarket CLOB for a token. Cached."""
    try:
        url = f"https://clob.polymarket.com/fee-rate?token_id={token_id}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json
            data = json.loads(resp.read())
            return float(data.get("base_fee", 0))
    except Exception:
        return 0.0


def _polymarket_taker_fee(shares: float, price: float, token_id: str = "") -> float:
    """Estimate Polymarket taker fee for a trade."""
    if price <= 0 or price >= 1 or shares <= 0:
        return 0.0
    fee_rate = _get_fee_rate(token_id) if token_id else 0.0
    if fee_rate <= 0:
        return 0.0
    fee = shares * price * fee_rate * (price * (1 - price))
    return round(fee, 4)


class EventsExecutor:
    """Executes trades in paper or live mode for events agent."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self.config = config or EventsConfig()
        self._live_client = None

    def _get_live_client(self):
        """Lazily initialize the authenticated CLOB client."""
        if self._live_client is not None:
            return self._live_client

        if not self.config.is_live:
            return None

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            client = ClobClient(
                self.config.CLOB_API_BASE,
                key=self.config.PRIVATE_KEY,
                chain_id=137,
                signature_type=1,
                funder=self.config.FUNDER_ADDRESS,
            )

            if (self.config.POLYMARKET_API_KEY
                    and self.config.POLYMARKET_API_SECRET
                    and self.config.POLYMARKET_API_PASSPHRASE):
                client.set_api_creds(ApiCreds(
                    api_key=self.config.POLYMARKET_API_KEY,
                    api_secret=self.config.POLYMARKET_API_SECRET,
                    api_passphrase=self.config.POLYMARKET_API_PASSPHRASE,
                ))
                logger.info("Using pre-set API credentials from .env")
            else:
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
                logger.info("Derived fresh API credentials from private key")

            self._live_client = client
            logger.info("Initialized live CLOB client for events (funder=%s)",
                        self.config.FUNDER_ADDRESS[:10])
            return self._live_client
        except Exception as e:
            logger.error("Failed to initialize live CLOB client: %s", e)
            return None

    def execute_buy(self, edge_result: EdgeResult, bet_size: float) -> tuple[Position | None, Trade | None]:
        """Execute a buy order (paper or live)."""
        market = edge_result.market
        side_index = edge_result.side_index

        if side_index >= len(market.clob_token_ids):
            logger.error("Invalid side index %d for market %s", side_index, market.id)
            return None, None

        token_id = market.clob_token_ids[side_index]
        price = market.outcome_prices[side_index]
        outcome_name = market.outcomes[side_index] if side_index < len(market.outcomes) else "Unknown"

        if price <= 0:
            return None, None

        now_str = utcnow().isoformat()
        shares = bet_size / price

        entry_fee = _polymarket_taker_fee(shares, price, token_id)

        if self.config.is_paper:
            order_id = f"paper_evt_{int(time.time())}"
            logger.info(
                "PAPER BUY: %s @ %.2f¢ | $%.2f | %s",
                outcome_name,
                price * 100,
                bet_size,
                market.question,
            )
        else:
            order_id = self._execute_live_buy(token_id, bet_size, market.neg_risk)
            if not order_id:
                return None, None

        pos_id = f"evt_pos_{int(time.time())}"
        position = Position(
            id=pos_id,
            market_id=market.id,
            market_question=market.question,
            token_id=token_id,
            side=f"YES ({outcome_name})" if side_index == 0 else f"NO ({outcome_name})",
            entry_price=price,
            shares=round(shares, 4),
            cost=bet_size,
            entry_time=now_str,
            confidence=edge_result.confidence.value,
            edge_at_entry=edge_result.edge,
            our_fair_price=edge_result.our_fair_price,
            mode="paper" if self.config.is_paper else "live",
            agent="events",
            status="open",
            category=market.category.value,
            market_end_date=market.end_date,
            market_slug=market.slug,
            fees_paid=entry_fee,
            edge_source=edge_result.edge_source,
        )

        trade = Trade(
            id=f"evt_trade_{int(time.time())}",
            position_id=pos_id,
            market_id=market.id,
            market_question=market.question,
            action="BUY",
            side=position.side,
            price=price,
            shares=round(shares, 4),
            amount=bet_size,
            timestamp=now_str,
            mode="paper" if self.config.is_paper else "live",
            agent="events",
            order_id=order_id,
        )

        return position, trade

    def execute_sell(self, position: Position, current_price: float, reason: str) -> Trade | None:
        """Execute a sell order to close a position."""
        now_str = utcnow().isoformat()

        if self.config.is_paper:
            order_id = f"paper_evt_{int(time.time())}"
            logger.info(
                "PAPER SELL: %s @ %.2f¢ | %.2f shares | %s",
                position.side,
                current_price * 100,
                position.shares,
                position.market_question,
            )
        else:
            order_id = self._execute_live_sell(position.token_id, position.shares, position.market_id)
            if not order_id:
                return None

        # Calculate P&L (including fees)
        exit_value = position.shares * current_price
        exit_fee = _polymarket_taker_fee(position.shares, current_price, position.token_id)
        total_fees = position.fees_paid + exit_fee
        pnl = exit_value - position.cost - total_fees

        position.status = "closed"
        position.exit_price = current_price
        position.exit_time = now_str
        position.pnl = round(pnl, 2)
        position.exit_reason = reason
        position.fees_paid = round(total_fees, 4)

        logger.info("P&L: $%.2f (fees: $%.4f)", pnl, total_fees)

        trade = Trade(
            id=f"evt_trade_{int(time.time())}",
            position_id=position.id,
            market_id=position.market_id,
            market_question=position.market_question,
            action="SELL",
            side=position.side,
            price=current_price,
            shares=position.shares,
            amount=round(exit_value, 2),
            timestamp=now_str,
            mode=position.mode,
            agent="events",
            order_id=order_id,
            pnl=round(pnl, 2),
        )

        return trade

    def _execute_live_buy(self, token_id: str, amount: float, neg_risk: bool) -> str:
        """Execute a live buy order — limit then fallback to market."""
        try:
            from py_clob_client.clob_types import (
                OrderArgs, OrderType, PartialCreateOrderOptions,
            )
            from py_clob_client.order_builder.constants import BUY

            client = self._get_live_client()
            if not client:
                return ""

            tick_size = str(client.get_tick_size(token_id))
            mid_data = client.get_midpoint(token_id)
            midpoint = float(mid_data.get("mid", 0)) if isinstance(mid_data, dict) else float(mid_data)

            if midpoint <= 0:
                return self._execute_market_buy(token_id, amount, neg_risk)

            limit_price = round(midpoint, len(tick_size.split('.')[-1]) if '.' in tick_size else 2)
            shares = amount / limit_price if limit_price > 0 else 0
            if shares <= 0:
                return ""

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=round(shares, 2),
                side=BUY,
            )
            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk if neg_risk else None,
            )
            signed = client.create_order(order_args, options)
            resp = client.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)

            if order_id:
                logger.info("LIMIT BUY posted: order=%s price=%.2f¢ amount=$%.2f",
                            order_id[:16], limit_price * 100, amount)

                for _ in range(6):
                    time.sleep(5)
                    try:
                        order_info = client.get_order(order_id)
                        if isinstance(order_info, dict):
                            status = order_info.get("status", "").upper()
                            if status in ("MATCHED", "FILLED"):
                                return order_id
                            elif status in ("CANCELLED", "EXPIRED"):
                                break
                    except Exception:
                        pass

                try:
                    client.cancel_orders([order_id])
                except Exception:
                    pass

            return self._execute_market_buy(token_id, amount, neg_risk)

        except Exception as e:
            logger.error("Live BUY failed: %s", e)
            return ""

    def _execute_market_buy(self, token_id: str, amount: float, neg_risk: bool) -> str:
        """Fallback: execute a FOK market buy."""
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            client = self._get_live_client()
            if not client:
                return ""

            mo = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
                order_type=OrderType.FOK,
            )
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)
            logger.info("MARKET BUY (fallback): order=%s amount=$%.2f", order_id, amount)
            return order_id
        except Exception as e:
            logger.error("MARKET BUY failed: %s", e)
            return ""

    def _execute_live_sell(self, token_id: str, shares: float, market_id: str) -> str:
        """Execute a live FOK market sell order."""
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            client = self._get_live_client()
            if not client:
                return ""

            mo = MarketOrderArgs(
                token_id=token_id,
                amount=shares,
                side=SELL,
                order_type=OrderType.FOK,
            )
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)
            logger.info("Live SELL: order=%s shares=%.2f", order_id, shares)
            return order_id
        except Exception as e:
            logger.error("Live SELL failed: %s", e)
            return ""
