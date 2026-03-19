"""
executor.py - Order execution via Polymarket CLOB API.

Wraps the py-clob-client SDK with:
  - Heartbeat thread (5s interval — mandatory to keep orders alive)
  - Order placement (FOK, GTC, etc.)
  - Orderbook and midpoint queries
  - Position management
  - Rate-limit-aware retries
  - Comprehensive logging of every order action
"""

import time
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from utils import sync_retry, round_to_tick

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    """Outcome of a single order attempt."""
    success: bool
    order_id: str = ""
    status: str = ""           # "filled", "open", "cancelled", "error"
    filled_price: float = 0.0
    filled_size: float = 0.0
    remaining_size: float = 0.0
    fee_paid: float = 0.0
    error: str = ""
    raw: Optional[Dict] = None

    @property
    def is_filled(self) -> bool:
        return self.success and self.status == "matched"


@dataclass
class Position:
    """A single open/closed position."""
    condition_id: str
    asset: str
    outcome: str           # "YES" or "NO"
    token_id: str
    size: float
    avg_entry_price: float
    current_price: float = 0.0

    @property
    def unrealised_pnl(self) -> float:
        return (self.current_price - self.avg_entry_price) * self.size


# ---------------------------------------------------------------------------
# OrderExecutor
# ---------------------------------------------------------------------------

class OrderExecutor:
    """
    Handles all trading interactions with the Polymarket CLOB API.

    In paper mode (credentials not provided) only the read-only public
    endpoints (orderbook, midpoint) are available — order placement raises
    a ValueError.
    """

    CLOB_BASE = "https://clob.polymarket.com"
    HEARTBEAT_INTERVAL = 5  # seconds

    def __init__(
        self,
        private_key: str = "",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        funder_address: str = "",
        signature_type: int = 2,
        chain_id: int = 137,  # Polygon mainnet
    ):
        self._private_key = private_key
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._funder_address = funder_address
        self._signature_type = signature_type
        self._chain_id = chain_id

        self._client = None          # py-clob-client ClobClient instance
        self._heartbeat_id = ""      # last heartbeat ID from server
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False

        self._order_lock = threading.Lock()
        self._order_count = 0

        # Try to initialise the SDK client (requires credentials for live mode)
        self._init_client()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_client(self) -> None:
        """Attempt to initialise py-clob-client."""
        if not self._private_key:
            logger.info("OrderExecutor: no private key — read-only mode (paper trading).")
            return

        try:
            from py_clob_client.client import ClobClient  # type: ignore
            from py_clob_client.clob_types import ApiCreds  # type: ignore

            if self._api_key and self._api_secret and self._api_passphrase:
                creds = ApiCreds(
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    api_passphrase=self._api_passphrase,
                )
                self._client = ClobClient(
                    host=self.CLOB_BASE,
                    key=self._private_key,
                    chain_id=self._chain_id,
                    creds=creds,
                    signature_type=self._signature_type,
                    funder=self._funder_address or None,
                )
                logger.info("OrderExecutor: ClobClient initialised with L2 credentials.")
            else:
                # Derive credentials from private key
                self._client = ClobClient(
                    host=self.CLOB_BASE,
                    key=self._private_key,
                    chain_id=self._chain_id,
                    signature_type=self._signature_type,
                    funder=self._funder_address or None,
                )
                logger.info("OrderExecutor: ClobClient initialised (will derive L2 creds).")

        except ImportError:
            logger.warning(
                "OrderExecutor: py-clob-client not installed — order placement disabled."
            )
        except Exception as exc:
            logger.error("OrderExecutor: failed to init ClobClient: %s", exc)

    def start_heartbeat(self) -> None:
        """Start the background heartbeat thread."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="CLOB-Heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        logger.info("OrderExecutor: heartbeat started (every %ds).", self.HEARTBEAT_INTERVAL)

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat thread."""
        self._running = False

    def _heartbeat_loop(self) -> None:
        """Send a heartbeat every HEARTBEAT_INTERVAL seconds."""
        while self._running:
            try:
                self._send_heartbeat()
            except Exception as exc:
                logger.warning("OrderExecutor: heartbeat error: %s", exc)
            time.sleep(self.HEARTBEAT_INTERVAL)

    def _send_heartbeat(self) -> None:
        if self._client is None:
            return
        try:
            resp = self._client.post_order_heartbeat(heartbeat_id=self._heartbeat_id or "")
            self._heartbeat_id = resp.get("heartbeat_id", "") if isinstance(resp, dict) else ""
            logger.debug("Heartbeat sent. id=%s", self._heartbeat_id)
        except Exception as exc:
            logger.debug("Heartbeat error: %s", exc)

    # ------------------------------------------------------------------
    # Order Placement
    # ------------------------------------------------------------------

    def place_order(
        self,
        market: dict,
        side: str,             # "YES" or "NO"
        size: float,           # USDC amount to spend
        price: float,          # limit price (probability, 0–1)
        order_type: str = "FOK",
    ) -> OrderResult:
        """
        Place an order on Polymarket.

        Args:
            market:     Market dict from MarketFinder.
            side:       "YES" to buy Up token, "NO" to buy Down token.
            size:       Position size in USDC.
            price:      Limit price (Polymarket probability).
            order_type: "FOK", "GTC", "GTD", "FAK".

        Returns:
            OrderResult with fill details.
        """
        if self._client is None:
            return OrderResult(
                success=False,
                error="ClobClient not initialised — are credentials set?",
            )

        # Choose the correct token ID
        if side.upper() == "YES":
            token_id = market.get("token_id_yes", "")
        else:
            token_id = market.get("token_id_no", "")

        if not token_id:
            return OrderResult(
                success=False,
                error=f"No token_id for side={side} in market {market.get('slug')}",
            )

        price = round_to_tick(price)
        size = round(size, 2)

        logger.info(
            "Placing %s order: %s %s | token=%s price=%.3f size=$%.2f",
            order_type, side, market.get("asset"), token_id[:16], price, size,
        )

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore

            order_type_map = {
                "FOK": OrderType.FOK,
                "GTC": OrderType.GTC,
                "GTD": OrderType.GTD,
                "FAK": OrderType.FAK,
            }
            ot = order_type_map.get(order_type.upper(), OrderType.FOK)

            # Calculate number of shares from USDC amount and price
            # shares = USDC / price_per_share
            shares = size / price if price > 0 else size

            with self._order_lock:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=round(shares, 4),
                    side="BUY",
                    order_type=ot,
                )
                resp = self._client.create_and_post_order(order_args)
                self._order_count += 1

            return self._parse_order_response(resp, size, price, side)

        except Exception as exc:
            logger.error("OrderExecutor: place_order failed: %s", exc, exc_info=True)
            return OrderResult(success=False, error=str(exc))

    def _parse_order_response(
        self, resp: Any, size: float, price: float, side: str
    ) -> OrderResult:
        """Parse the py-clob-client order response into an OrderResult."""
        if resp is None:
            return OrderResult(success=False, error="Null response from CLOB")

        # py-clob-client returns a dict-like object
        if hasattr(resp, "__dict__"):
            data = vars(resp)
        elif isinstance(resp, dict):
            data = resp
        else:
            data = {"status": str(resp)}

        status = str(data.get("status", "")).lower()
        order_id = str(data.get("orderID", data.get("order_id", "")))

        filled_size = float(data.get("size_matched", data.get("filled", 0)) or 0)
        remaining = float(data.get("size_remaining", data.get("remaining", size - filled_size)) or 0)
        filled_price = float(data.get("average_price", price) or price)

        success = status in ("matched", "live", "open")

        return OrderResult(
            success=success,
            order_id=order_id,
            status=status,
            filled_price=filled_price,
            filled_size=filled_size,
            remaining_size=remaining,
            raw=data,
        )

    # ------------------------------------------------------------------
    # Orderbook & Price Queries (public — no auth required)
    # ------------------------------------------------------------------

    @sync_retry(max_retries=3, delay=0.3, exceptions=(httpx.RequestError,))
    def get_orderbook(self, token_id: str) -> Dict:
        """
        Fetch the full orderbook for a token.

        Returns:
            Dict with "bids" and "asks" lists, or empty dict on error.
        """
        try:
            with httpx.Client(timeout=8) as client:
                resp = client.get(
                    f"{self.CLOB_BASE}/book",
                    params={"token_id": token_id},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.warning("get_orderbook error for %s: %s", token_id[:16], exc)
            return {}

    @sync_retry(max_retries=3, delay=0.3, exceptions=(httpx.RequestError,))
    def get_midpoint(self, token_id: str) -> float:
        """
        Return the current midpoint price for a token.

        Returns:
            Float in [0, 1], or 0.5 if unavailable.
        """
        try:
            with httpx.Client(timeout=8) as client:
                resp = client.get(
                    f"{self.CLOB_BASE}/midpoint",
                    params={"token_id": token_id},
                )
                resp.raise_for_status()
                data = resp.json()
                mid = float(data.get("mid", 0.5) or 0.5)
                return mid
        except Exception as exc:
            logger.debug("get_midpoint error for %s: %s", token_id[:16], exc)
            return 0.5

    def get_price(self, token_id: str) -> float:
        """
        Return the best available price for a token using the /price endpoint.
        Falls back to midpoint if /price is unavailable.
        """
        try:
            with httpx.Client(timeout=8) as client:
                resp = client.get(
                    f"{self.CLOB_BASE}/price",
                    params={"token_id": token_id, "side": "buy"},
                )
                resp.raise_for_status()
                data = resp.json()
                return float(data.get("price", 0.5) or 0.5)
        except Exception:
            return self.get_midpoint(token_id)

    def calculate_optimal_entry_price(
        self,
        token_id: str,
        side: str,
        size: float,
    ) -> float:
        """
        Calculate the optimal limit price for an order by walking the book.

        For a FOK order we want to price at or just above (for buys)
        the current best ask to ensure fill.

        Args:
            token_id: Token to trade.
            side:     "buy" or "sell".
            size:     Size in shares.

        Returns:
            Optimal price as float in [0.01, 0.99].
        """
        book = self.get_orderbook(token_id)
        if not book:
            return self.get_midpoint(token_id)

        asks = book.get("asks", [])
        bids = book.get("bids", [])

        if side.lower() == "buy" and asks:
            # Walk the ask side to find price for desired size
            cum_size = 0.0
            for level in sorted(asks, key=lambda x: float(x.get("price", 1))):
                cum_size += float(level.get("size", 0))
                if cum_size >= size:
                    price = float(level.get("price", 0.5))
                    return round_to_tick(min(price + 0.01, 0.99))
            # If not enough liquidity, take the top of book
            return round_to_tick(float(asks[0].get("price", 0.99)))
        elif side.lower() == "sell" and bids:
            for level in sorted(bids, key=lambda x: -float(x.get("price", 0))):
                cum_size = float(level.get("size", 0))
                if cum_size >= size:
                    price = float(level.get("price", 0.5))
                    return round_to_tick(max(price - 0.01, 0.01))
            return round_to_tick(float(bids[0].get("price", 0.01)))

        return self.get_midpoint(token_id)

    # ------------------------------------------------------------------
    # Position Management
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Position]:
        """
        Fetch open positions for the configured funder address.

        Returns:
            List of Position objects, or empty list on error.
        """
        if not self._funder_address:
            logger.debug("get_positions: no funder address configured.")
            return []

        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(
                    "https://data-api.polymarket.com/positions",
                    params={"user": self._funder_address, "sizeThreshold": "0.01"},
                )
                resp.raise_for_status()
                data = resp.json()

            positions = []
            for item in (data if isinstance(data, list) else data.get("positions", [])):
                try:
                    pos = Position(
                        condition_id=item.get("conditionId", ""),
                        asset=item.get("title", ""),
                        outcome=item.get("outcome", ""),
                        token_id=item.get("asset", ""),
                        size=float(item.get("size", 0)),
                        avg_entry_price=float(item.get("avgPrice", 0.5)),
                        current_price=float(item.get("currentValue", 0) or 0),
                    )
                    positions.append(pos)
                except Exception:
                    pass

            return positions

        except Exception as exc:
            logger.warning("get_positions error: %s", exc)
            return []

    def get_balance(self) -> float:
        """
        Return the USDC balance for the connected wallet.

        Returns:
            Balance as float, or 0.0 on error.
        """
        if self._client is None:
            return 0.0
        try:
            balance = self._client.get_balance()
            if isinstance(balance, (int, float)):
                return float(balance)
            if isinstance(balance, dict):
                return float(balance.get("balance", 0))
            return float(balance)
        except Exception as exc:
            logger.warning("get_balance error: %s", exc)
            return 0.0

    def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders.

        Returns:
            True if successful.
        """
        if self._client is None:
            return True
        try:
            self._client.cancel_all()
            logger.info("OrderExecutor: all orders cancelled.")
            return True
        except Exception as exc:
            logger.error("cancel_all_orders error: %s", exc)
            return False

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order by ID."""
        if self._client is None:
            return True
        try:
            self._client.cancel(order_id)
            return True
        except Exception as exc:
            logger.warning("cancel_order %s error: %s", order_id, exc)
            return False

    # ------------------------------------------------------------------
    # Order status
    # ------------------------------------------------------------------

    def get_order_status(self, order_id: str) -> Dict:
        """Fetch current status of an order."""
        if self._client is None:
            return {}
        try:
            resp = self._client.get_order(order_id)
            if hasattr(resp, "__dict__"):
                return vars(resp)
            return dict(resp) if resp else {}
        except Exception as exc:
            logger.warning("get_order_status error: %s", exc)
            return {}

    @property
    def is_ready(self) -> bool:
        """Return True if the executor is ready to place live orders."""
        return self._client is not None
