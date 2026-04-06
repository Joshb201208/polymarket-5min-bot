import logging

import httpx

from stock_agent.config import Config

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": self.config.ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.config.ALPACA_BASE_URL}{path}"

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> dict | list | None:
        """Make an authenticated request to Alpaca."""
        client = await self._get_client()
        url = self._url(path)
        headers = self._headers()

        try:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "POST":
                resp = await client.post(url, headers=headers, json=json_body)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                logger.error("Unsupported HTTP method: %s", method)
                return None

            if resp.status_code == 204:
                return {}
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "Alpaca %s %s failed: %s — %s",
                method,
                path,
                e.response.status_code,
                e.response.text[:500],
            )
            return None
        except Exception as e:
            logger.error("Alpaca request %s %s failed: %s", method, path, e)
            return None

    async def get_account(self) -> dict | None:
        """GET /v2/account — account details including buying power and equity."""
        data = await self._request("GET", "/v2/account")
        if isinstance(data, dict):
            return data
        return None

    async def get_positions(self) -> list[dict]:
        """GET /v2/positions — all open positions."""
        data = await self._request("GET", "/v2/positions")
        if isinstance(data, list):
            return data
        return []

    async def place_buy(self, symbol: str, qty: int, stop_loss_price: float) -> dict | None:
        """Place a market buy, then attach a separate GTC stop-loss order.

        Bracket orders on Alpaca paper have been unreliable (fills stuck in
        'accepted'). Using two simple orders is more robust:
          1. Market buy (day) — fills immediately
          2. Stop sell (GTC) — placed after fill confirmed
        """
        if qty <= 0:
            logger.warning("Attempted to buy %d shares of %s — skipping", qty, symbol)
            return None

        # Step 1: Market buy
        buy_body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        }

        logger.info("Placing BUY order: %s x%d", symbol, qty)
        result = await self._request("POST", "/v2/orders", buy_body)

        if not result:
            logger.error("BUY order FAILED for %s x%d", symbol, qty)
            return None

        order_id = result.get("id", "unknown")
        logger.info("BUY order placed: %s — order_id=%s", symbol, order_id)

        # Step 2: Wait briefly for fill, then place stop-loss
        import asyncio
        await asyncio.sleep(3)  # Give the fill a moment

        stop_body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "stop_price": str(round(stop_loss_price, 2)),
            "time_in_force": "gtc",
        }

        logger.info("Placing stop-loss for %s at $%.2f", symbol, stop_loss_price)
        stop_result = await self._request("POST", "/v2/orders", stop_body)

        if stop_result:
            logger.info("Stop-loss placed: %s — order_id=%s", symbol, stop_result.get("id", "unknown"))
        else:
            logger.warning("Stop-loss order FAILED for %s — position is unprotected", symbol)

        return result

    async def place_sell(self, symbol: str, qty: int) -> dict | None:
        """Place a market sell order.

        POST /v2/orders.
        """
        if qty <= 0:
            logger.warning("Attempted to sell %d shares of %s — skipping", qty, symbol)
            return None

        order_body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        }

        logger.info("Placing SELL order: %s x%d", symbol, qty)
        result = await self._request("POST", "/v2/orders", order_body)

        if result:
            logger.info("SELL order placed: %s — order_id=%s", symbol, result.get("id", "unknown"))
        else:
            logger.error("SELL order FAILED for %s x%d", symbol, qty)

        return result

    async def cancel_order(self, order_id: str) -> bool:
        """DELETE /v2/orders/{order_id} — cancel a pending order."""
        result = await self._request("DELETE", f"/v2/orders/{order_id}")
        if result is not None:
            logger.info("Cancelled order %s", order_id)
            return True
        return False

    async def get_order(self, order_id: str) -> dict | None:
        """GET /v2/orders/{order_id} — get order status."""
        data = await self._request("GET", f"/v2/orders/{order_id}")
        if isinstance(data, dict):
            return data
        return None

    async def get_open_orders(self) -> list[dict]:
        """GET /v2/orders?status=open — all open/pending orders."""
        data = await self._request("GET", "/v2/orders?status=open")
        if isinstance(data, list):
            return data
        return []

    async def cancel_all_orders(self) -> bool:
        """DELETE /v2/orders — cancel all open orders."""
        result = await self._request("DELETE", "/v2/orders")
        return result is not None
