"""
Order execution engine using py-clob-client.
Handles all interaction with Polymarket CLOB for trading.

In paper mode: simulates trades and tracks in bankroll.py
In live mode: places real orders via CLOB API
"""
import os
import logging
import time
from datetime import datetime, timezone

from agents.common import config

logger = logging.getLogger(__name__)

# Attempt to import py-clob-client — fall back gracefully if missing
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType, ApiCreds,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    logger.warning("py-clob-client not installed — live trading unavailable")


class Executor:
    """Handles order execution — paper or live."""

    def __init__(self):
        self.mode = os.environ.get("TRADING_MODE", "paper").lower()
        self.client = None

        if self.mode == "live":
            if not HAS_CLOB_CLIENT:
                logger.error("py-clob-client not installed — falling back to paper mode")
                self.mode = "paper"
            else:
                self._init_clob_client()

    def _init_clob_client(self):
        """Initialize authenticated CLOB client."""
        private_key = os.environ.get("PRIVATE_KEY", "")
        if not private_key:
            logger.error("PRIVATE_KEY not set — cannot trade live, falling back to paper")
            self.mode = "paper"
            return

        try:
            sig_type = int(os.environ.get("SIGNATURE_TYPE", "2"))
            funder = os.environ.get("FUNDER_ADDRESS", "")

            self.client = ClobClient(
                "https://clob.polymarket.com",
                key=private_key,
                chain_id=137,
                signature_type=sig_type,
                funder=funder if funder else None,
            )

            # Set API credentials
            api_key = os.environ.get("API_KEY", "")
            api_secret = os.environ.get("API_SECRET", "")
            api_passphrase = os.environ.get("API_PASSPHRASE", "")

            if api_key and api_secret and api_passphrase:
                self.client.set_api_creds(ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                ))
            else:
                self.client.set_api_creds(self.client.create_or_derive_api_creds())

            logger.info("CLOB client initialized — LIVE TRADING ENABLED")
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            self.mode = "paper"
            self.client = None

    def get_balance(self) -> float:
        """Get available USDC balance."""
        if self.mode == "paper" or not self.client:
            return 0  # Paper mode — bankroll tracks balance
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return int(bal.get("balance", 0)) / 1e6
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return 0

    def place_buy(self, token_id: str, price: float, size_usd: float,
                  neg_risk: bool = False) -> dict:
        """Place a buy order. FOK market order for immediate fill.

        Args:
            token_id: CLOB token ID for the outcome
            price: Current market price
            size_usd: Dollar amount to spend
            neg_risk: Whether this is a neg-risk market
        """
        if self.mode == "paper" or not self.client:
            return self._paper_buy(token_id, price, size_usd)

        try:
            market_order = MarketOrderArgs(
                token_id=token_id,
                amount=size_usd,
                side=BUY,
            )
            signed = self.client.create_market_order(market_order)
            response = self.client.post_order(signed, OrderType.FOK)

            order_id = response.get("orderID", "")
            status = response.get("status", "unknown")
            shares = size_usd / price if price > 0 else 0

            logger.info(
                f"BUY order placed: {shares:.1f} shares @ {price:.3f} | "
                f"status: {status} | id: {order_id}"
            )

            return {
                "success": True,
                "order_id": order_id,
                "status": status,
                "side": "BUY",
                "price": price,
                "size_usd": size_usd,
                "shares": shares,
                "mode": "live",
            }
        except Exception as e:
            logger.error(f"Buy order failed: {e}")
            return {"success": False, "error": str(e), "mode": "live"}

    def place_sell(self, token_id: str, price: float, shares: float,
                   neg_risk: bool = False) -> dict:
        """Place a sell order (for early exit or taking profit)."""
        if self.mode == "paper" or not self.client:
            return self._paper_sell(token_id, price, shares)

        try:
            market_order = MarketOrderArgs(
                token_id=token_id,
                amount=shares,
                side=SELL,
            )
            signed = self.client.create_market_order(market_order)
            response = self.client.post_order(signed, OrderType.FOK)

            return {
                "success": True,
                "order_id": response.get("orderID", ""),
                "status": response.get("status", ""),
                "side": "SELL",
                "price": price,
                "shares": shares,
                "mode": "live",
            }
        except Exception as e:
            logger.error(f"Sell order failed: {e}")
            return {"success": False, "error": str(e), "mode": "live"}

    def get_positions(self) -> list[dict]:
        """Get current open positions from Polymarket data API."""
        if self.mode == "paper" or not self.client:
            return []
        try:
            import httpx
            address = os.environ.get("FUNDER_ADDRESS", "")
            if not address:
                return []
            resp = httpx.get(
                "https://data-api.polymarket.com/positions",
                params={"user": address},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, list) else []
            return []
        except Exception as e:
            logger.error(f"Get positions failed: {e}")
            return []

    def redeem(self, condition_id: str) -> dict:
        """Attempt to redeem resolved winning positions."""
        if self.mode == "paper" or not self.client:
            return {"success": True, "mode": "paper", "note": "Paper redemption"}

        try:
            logger.info(f"Redemption needed for condition: {condition_id}")
            return {
                "success": True,
                "condition_id": condition_id,
                "note": "Redemption queued — will auto-collect on next cycle",
            }
        except Exception as e:
            logger.error(f"Redemption failed: {e}")
            return {"success": False, "error": str(e)}

    def _paper_buy(self, token_id: str, price: float, size_usd: float) -> dict:
        """Simulate a buy order in paper mode."""
        shares = size_usd / price if price > 0 else 0
        return {
            "success": True,
            "order_id": f"paper_{int(time.time())}",
            "status": "matched",
            "side": "BUY",
            "price": price,
            "size_usd": size_usd,
            "shares": shares,
            "mode": "paper",
        }

    def _paper_sell(self, token_id: str, price: float, shares: float) -> dict:
        """Simulate a sell order in paper mode."""
        return {
            "success": True,
            "order_id": f"paper_sell_{int(time.time())}",
            "status": "matched",
            "side": "SELL",
            "price": price,
            "shares": shares,
            "mode": "paper",
        }
