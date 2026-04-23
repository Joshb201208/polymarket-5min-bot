"""Options order execution — places and manages options orders via Alpaca REST API.

All options orders are placed against the paper trading endpoint.
Supported strategies: single-leg (covered calls, cash-secured puts, long
calls/puts) and two-leg defined-risk spreads (bull call / bear put spreads).

Alpaca options order constraints:
    - qty must be a positive whole number
    - time_in_force: "day" or "gtc"
    - type: "market" or "limit" (stop/stop_limit for single-leg only)
    - extended_hours must be False or omitted
    - notional must not be populated
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from stock_agent.config import Config
from stock_agent.options_models import OptionOrderType, OptionSide, OptionsPosition

logger = logging.getLogger(__name__)

_TRADING_BASE = "https://paper-api.alpaca.markets"


class OptionsExecutor:
    """Async wrapper for Alpaca options order placement and position management.

    Usage::

        executor = OptionsExecutor(config)
        order = await executor.place_option_order(
            contract_symbol="AAPL260418C00255000",
            qty=1,
            side=OptionSide.BUY,
            order_type=OptionOrderType.LIMIT,
            limit_price=3.50,
        )
        await executor.close()
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    # ── HTTP lifecycle ───────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": self.config.ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }

    # ── Single-leg order ─────────────────────────────────────────────

    async def place_option_order(
        self,
        contract_symbol: str,
        qty: int,
        side: OptionSide,
        order_type: OptionOrderType = OptionOrderType.LIMIT,
        limit_price: Optional[float] = None,
        time_in_force: str = "day",
    ) -> Optional[dict]:
        """Place a single-leg options order.

        Args:
            contract_symbol: OCC-format symbol, e.g. ``"AAPL260418C00255000"``.
            qty:             Number of contracts (positive whole number).
            side:            ``OptionSide.BUY`` or ``OptionSide.SELL``.
            order_type:      ``OptionOrderType.MARKET`` or ``OptionOrderType.LIMIT``.
            limit_price:     Required when ``order_type`` is LIMIT. Per-share price.
            time_in_force:   ``"day"`` (default) or ``"gtc"``.

        Returns:
            Alpaca order dict on success, ``None`` on failure.
        """
        if qty < 1:
            logger.error("place_option_order: qty must be >= 1, got %d", qty)
            return None

        if order_type == OptionOrderType.LIMIT and limit_price is None:
            logger.error("place_option_order: limit_price required for LIMIT orders")
            return None

        payload: dict = {
            "symbol": contract_symbol,
            "qty": str(qty),
            "side": side.value,
            "type": order_type.value,
            "time_in_force": time_in_force,
        }

        if limit_price is not None:
            payload["limit_price"] = str(round(limit_price, 2))

        return await self._submit_order(payload, label=f"{side.value} {qty}x {contract_symbol}")

    # ── Spread order ─────────────────────────────────────────────────

    async def place_spread_order(
        self,
        long_symbol: str,
        short_symbol: str,
        qty: int,
        order_type: OptionOrderType = OptionOrderType.LIMIT,
        net_debit: Optional[float] = None,
        time_in_force: str = "day",
    ) -> tuple[Optional[dict], Optional[dict]]:
        """Place a two-leg defined-risk spread as two separate orders.

        Alpaca paper accounts process spread legs individually.  We submit the
        long leg first (debit) then immediately the short leg (credit).

        Args:
            long_symbol:    OCC symbol for the leg we buy (lower strike call or
                            higher strike put for spreads).
            short_symbol:   OCC symbol for the leg we sell.
            qty:            Number of spread contracts.
            order_type:     Order type for both legs.
            net_debit:      Target net debit per spread.  When provided, long leg
                            is submitted at ``net_debit * 1.05`` and short leg at
                            ``net_debit * 0.95`` (heuristic to fill near mid).
            time_in_force:  ``"day"`` (default) or ``"gtc"``.

        Returns:
            Tuple of (long_order, short_order) dicts.  Either may be ``None`` on
            failure.
        """
        long_limit: Optional[float] = None
        short_limit: Optional[float] = None

        if order_type == OptionOrderType.LIMIT and net_debit is not None:
            # We'll use market orders for individual legs as a fallback if
            # limit prices cannot be determined, but a net_debit hint lets us
            # set reasonable limit prices.
            long_limit = round(net_debit * 1.10, 2)   # Pay up to 10% over target for long
            short_limit = round(net_debit * 0.90, 2)  # Accept down to 10% under target for short

        logger.info(
            "Submitting spread: BUY %s / SELL %s (qty=%d)",
            long_symbol, short_symbol, qty,
        )

        long_order = await self.place_option_order(
            contract_symbol=long_symbol,
            qty=qty,
            side=OptionSide.BUY,
            order_type=order_type,
            limit_price=long_limit,
            time_in_force=time_in_force,
        )

        short_order = await self.place_option_order(
            contract_symbol=short_symbol,
            qty=qty,
            side=OptionSide.SELL,
            order_type=order_type,
            limit_price=short_limit,
            time_in_force=time_in_force,
        )

        return long_order, short_order

    # ── Close a position ─────────────────────────────────────────────

    async def close_option_position(
        self,
        contract_symbol: str,
        qty: int = 1,
        position_side: str = "long",
    ) -> Optional[dict]:
        """Close an open options position using an offsetting market order.

        Uses a regular BUY/SELL order rather than DELETE /v2/positions,
        which Alpaca rejects as an uncovered option on paper accounts.

        Args:
            contract_symbol: OCC contract symbol.
            qty:             Number of contracts to close (positive integer).
            position_side:   ``"long"`` (we hold it, sell to close) or
                             ``"short"`` (we wrote it, buy to close).

        Returns:
            Alpaca order dict on success, ``None`` on failure.
        """
        close_side = OptionSide.SELL if position_side == "long" else OptionSide.BUY
        logger.info(
            "Closing %s option position: %s x%d (%s to close)",
            position_side, contract_symbol, qty, close_side.value,
        )
        return await self.place_option_order(
            contract_symbol=contract_symbol,
            qty=abs(qty),
            side=close_side,
            order_type=OptionOrderType.MARKET,
        )

    async def _close_option_position_legacy(
        self,
        contract_symbol: str,
        qty: Optional[int] = None,
    ) -> Optional[dict]:
        """Legacy DELETE-based close (kept for reference — rejected on paper)."""
        client = await self._get_client()
        url = f"{_TRADING_BASE}/v2/positions/{contract_symbol}"
        params: dict = {}
        if qty is not None:
            params["qty"] = str(qty)

        try:
            resp = await client.delete(url, params=params, headers=self._auth_headers())
            resp.raise_for_status()
            order = resp.json()
            logger.info("Closed options position %s: order_id=%s", contract_symbol, order.get("id"))
            return order
        except httpx.HTTPStatusError as exc:
            logger.error(
                "close_option_position failed for %s: %s — %s",
                contract_symbol, exc.response.status_code, exc.response.text,
            )
        except Exception as exc:
            logger.error("close_option_position error for %s: %s", contract_symbol, exc)

        return None

    async def close_spread_position(
        self,
        long_symbol: str,
        short_symbol: str,
        qty: Optional[int] = None,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """Close both legs of a spread position.

        Args:
            long_symbol:  OCC symbol of the leg that was bought.
            short_symbol: OCC symbol of the leg that was sold.
            qty:          Contracts to close.  ``None`` closes full position.

        Returns:
            Tuple of (long_close_order, short_close_order).
        """
        long_close = await self.close_option_position(long_symbol, qty=qty)
        short_close = await self.close_option_position(short_symbol, qty=qty)
        return long_close, short_close

    # ── Get current options positions ────────────────────────────────

    async def get_option_positions(self) -> list[dict]:
        """Return all open options positions from Alpaca.

        Alpaca returns both equity and options positions from the same endpoint.
        This method filters to return only options positions.

        Returns:
            List of raw Alpaca position dicts where ``asset_class == "us_option"``.
        """
        client = await self._get_client()
        url = f"{_TRADING_BASE}/v2/positions"

        try:
            resp = await client.get(url, headers=self._auth_headers())
            resp.raise_for_status()
            all_positions: list[dict] = resp.json()
            options = [p for p in all_positions if p.get("asset_class") == "us_option"]
            logger.debug("get_option_positions: found %d options positions", len(options))
            return options
        except httpx.HTTPStatusError as exc:
            logger.error(
                "get_option_positions failed: %s — %s",
                exc.response.status_code, exc.response.text,
            )
        except Exception as exc:
            logger.error("get_option_positions error: %s", exc)

        return []

    async def get_option_activities(self, limit: int = 500) -> list[dict]:
        """Return recent FILL activities for option contracts only.

        Used by :meth:`OptionsPortfolio.sync_from_alpaca` to determine close
        prices for positions that were manually closed.
        """
        client = await self._get_client()
        url = f"{_TRADING_BASE}/v2/account/activities/FILL"
        try:
            resp = await client.get(url, headers=self._auth_headers(), params={"page_size": str(limit), "direction": "desc"})
            resp.raise_for_status()
            acts: list[dict] = resp.json()
            # Filter to option symbols (OCC symbols are length > 5)
            opt_acts = [a for a in acts if len(a.get("symbol", "")) > 5]
            return opt_acts
        except Exception as exc:
            logger.error("get_option_activities error: %s", exc)
            return []

    async def get_option_position(self, contract_symbol: str) -> Optional[dict]:
        """Fetch a specific options position by contract symbol.

        Args:
            contract_symbol: OCC-format symbol.

        Returns:
            Alpaca position dict, or ``None`` if not found.
        """
        client = await self._get_client()
        url = f"{_TRADING_BASE}/v2/positions/{contract_symbol}"

        try:
            resp = await client.get(url, headers=self._auth_headers())
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            logger.error(
                "get_option_position failed for %s: %s — %s",
                contract_symbol, exc.response.status_code, exc.response.text,
            )
        except Exception as exc:
            logger.error("get_option_position error for %s: %s", contract_symbol, exc)

        return None

    # ── Order management ─────────────────────────────────────────────

    async def get_open_orders(self) -> list[dict]:
        """Return all open options orders.

        Returns:
            List of Alpaca order dicts filtered to ``asset_class == "us_option"``.
        """
        client = await self._get_client()
        url = f"{_TRADING_BASE}/v2/orders"

        try:
            resp = await client.get(
                url,
                params={"status": "open", "limit": 500},
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            all_orders: list[dict] = resp.json()
            return [o for o in all_orders if o.get("asset_class") == "us_option"]
        except Exception as exc:
            logger.error("get_open_orders failed: %s", exc)
            return []

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open options order by ID.

        Returns:
            ``True`` if cancelled successfully, ``False`` otherwise.
        """
        client = await self._get_client()
        url = f"{_TRADING_BASE}/v2/orders/{order_id}"

        try:
            resp = await client.delete(url, headers=self._auth_headers())
            resp.raise_for_status()
            logger.info("Cancelled order %s", order_id)
            return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "cancel_order failed for %s: %s — %s",
                order_id, exc.response.status_code, exc.response.text,
            )
        except Exception as exc:
            logger.error("cancel_order error for %s: %s", order_id, exc)

        return False

    # ── Exercise ─────────────────────────────────────────────────────

    async def exercise_option(self, contract_symbol: str) -> Optional[dict]:
        """Exercise an American-style options contract early.

        This should only be used for ITM options where early exercise is
        economically advantageous.  For most strategies, let the position
        auto-close or expire.

        Args:
            contract_symbol: OCC-format symbol of the contract to exercise.

        Returns:
            Alpaca response dict on success, ``None`` on failure.
        """
        client = await self._get_client()
        url = f"{_TRADING_BASE}/v2/positions/{contract_symbol}/exercise"

        try:
            resp = await client.post(url, headers=self._auth_headers())
            resp.raise_for_status()
            logger.info("Exercised option %s", contract_symbol)
            return resp.json() if resp.content else {"status": "exercised"}
        except httpx.HTTPStatusError as exc:
            logger.error(
                "exercise_option failed for %s: %s — %s",
                contract_symbol, exc.response.status_code, exc.response.text,
            )
        except Exception as exc:
            logger.error("exercise_option error for %s: %s", contract_symbol, exc)

        return None

    # ── Internal helpers ─────────────────────────────────────────────

    async def _submit_order(self, payload: dict, label: str = "") -> Optional[dict]:
        """POST to /v2/orders and return the response dict."""
        client = await self._get_client()
        url = f"{_TRADING_BASE}/v2/orders"

        try:
            resp = await client.post(url, json=payload, headers=self._auth_headers())
            resp.raise_for_status()
            order = resp.json()
            logger.info(
                "Order submitted [%s]: order_id=%s status=%s",
                label, order.get("id"), order.get("status"),
            )
            return order
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Order submission failed [%s]: %s — %s",
                label, exc.response.status_code, exc.response.text,
            )
        except Exception as exc:
            logger.error("Order submission error [%s]: %s", label, exc)

        return None
