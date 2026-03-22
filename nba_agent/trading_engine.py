"""Paper/live execution via py-clob-client."""

from __future__ import annotations

import logging
import time
from typing import Optional

from nba_agent.config import Config
from nba_agent.models import EdgeResult, Position, Trade
from nba_agent.utils import utcnow

logger = logging.getLogger(__name__)


class TradingEngine:
    """Executes trades in paper or live mode."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
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
                signature_type=0,
                funder=self.config.FUNDER_ADDRESS,
            )

            # If API creds are provided in .env, use them
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
                # Derive fresh credentials from private key
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
                logger.info("Derived fresh API credentials from private key")

            self._live_client = client
            logger.info("Initialized live CLOB client (funder=%s)", self.config.FUNDER_ADDRESS[:10])
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

        if self.config.is_paper:
            order_id = f"paper_{int(time.time())}"
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

        pos_id = f"pos_{int(time.time())}"
        position = Position(
            id=pos_id,
            market_id=market.id,
            market_question=market.question,
            token_id=token_id,
            side=f"YES ({outcome_name})",
            entry_price=price,
            shares=round(shares, 4),
            cost=bet_size,
            entry_time=now_str,
            confidence=edge_result.confidence.value,
            edge_at_entry=edge_result.edge,
            our_fair_price=edge_result.our_fair_price,
            mode="paper" if self.config.is_paper else "live",
            status="open",
            game_start_time=market.game_start_time,
            market_end_date=market.end_date,
            market_slug=market.slug,
        )

        trade = Trade(
            id=f"trade_{int(time.time())}",
            position_id=pos_id,
            market_id=market.id,
            market_question=market.question,
            action="BUY",
            side=f"YES ({outcome_name})",
            price=price,
            shares=round(shares, 4),
            amount=bet_size,
            timestamp=now_str,
            mode="paper" if self.config.is_paper else "live",
            order_id=order_id,
        )

        return position, trade

    def execute_sell(self, position: Position, current_price: float, reason: str) -> Trade | None:
        """Execute a sell order to close a position."""
        now_str = utcnow().isoformat()

        if self.config.is_paper:
            order_id = f"paper_{int(time.time())}"
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

        # Calculate P&L
        exit_value = position.shares * current_price
        pnl = exit_value - position.cost

        position.status = "closed"
        position.exit_price = current_price
        position.exit_time = now_str
        position.pnl = round(pnl, 2)
        position.exit_reason = reason

        trade = Trade(
            id=f"trade_{int(time.time())}",
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
            order_id=order_id,
            pnl=round(pnl, 2),
        )

        return trade

    def _execute_live_buy(self, token_id: str, amount: float, neg_risk: bool) -> str:
        """Execute a live FOK market buy order."""
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
            logger.info("Live BUY executed: order=%s amount=$%.2f", order_id, amount)
            return order_id
        except Exception as e:
            logger.error("Live BUY failed: %s", e)
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
            logger.info("Live SELL executed: order=%s shares=%.2f", order_id, shares)
            return order_id
        except Exception as e:
            logger.error("Live SELL failed: %s", e)
            return ""
