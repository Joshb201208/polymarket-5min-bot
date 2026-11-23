"""Typed Alpaca Trading API client.

Rules this module guarantees (DECISIONS B2):

- A failed request RAISES `BrokerError`. Nothing here returns `None`, `[]` or
  `{}` to mean "it did not work". An empty list means the broker said "none".
- Every order carries a `client_order_id`. After a timeout, a network error
  or a 5xx on `POST /v2/orders` the order is looked up by that id before
  anything else happens; it is resubmitted (with the SAME id) only when the
  broker says it has no such order. If the outcome is still unknown the
  error has `uncertain=True` and the caller must treat the order as possibly
  live until a reconcile resolves it.
- The sell guard: an equity sell is always `sell_to_close`, is checked
  against a position read in the same call, and may never exceed
  `qty_available`. A sell that would open a short is refused here, before
  the broker sees it. Buys that would cover a short are allowed only with
  `buy_to_close` (used by the one-time normalise routine).

Docs used (read 23 Nov 2026):
https://docs.alpaca.markets/docs/orders-at-alpaca
https://docs.alpaca.markets/reference/postorder
https://docs.alpaca.markets/us/openapi/trading-api.json
https://docs.alpaca.markets/reference/patchaccountconfig-1
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from stock_agent.config import Config

logger = logging.getLogger(__name__)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"

OPEN_ORDER_STATUSES = frozenset({
    "new", "accepted", "partially_filled", "pending_new", "accepted_for_bidding",
    "pending_cancel", "pending_replace", "held", "calculated", "stopped", "suspended",
    "done_for_day",
})
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "replaced", "rejected"})

_OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
_EPS = 1e-9

ErrorKind = Literal[
    "rejected",      # 403/422 and similar: the broker said no. Safe to treat as "not placed".
    "not_found",     # 404
    "rate_limited",  # 429 after retries
    "server",        # 5xx after retries
    "timeout",
    "network",
    "bad_response",  # 2xx with a body we cannot read
    "auth",          # 401
    "conflict",      # e.g. cancel of an order that is not cancelable and not terminal
    "guard",         # refused locally by the sell guard or a request check
]


class BrokerError(Exception):
    """Any failure talking to the broker. Never swallowed into an empty value."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        uncertain: bool = False,
        method: str = "",
        path: str = "",
    ):
        self.kind = kind
        self.message = message
        self.status = status
        self.code = code
        self.uncertain = uncertain
        self.method = method
        self.path = path
        super().__init__(self.__str__())

    def __str__(self) -> str:
        where = f"{self.method} {self.path}".strip()
        bits = [self.kind]
        if self.status is not None:
            bits.append(str(self.status))
        if self.code is not None:
            bits.append(str(self.code))
        tail = " (outcome unknown)" if self.uncertain else ""
        return f"Alpaca {where}: {'/'.join(bits)}: {self.message}{tail}"

    @property
    def retryable(self) -> bool:
        return self.kind in ("rate_limited", "server", "timeout", "network")


# --------------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


def _num(v: Any) -> Any:
    if v is None or v == "":
        return None
    return v


class Account(_Model):
    id: str = ""
    status: str = ""
    equity: float
    cash: float
    buying_power: float = 0.0
    last_equity: float | None = None
    long_market_value: float = 0.0
    short_market_value: float = 0.0
    options_buying_power: float | None = None
    multiplier: float | None = None
    shorting_enabled: bool | None = None
    trading_blocked: bool = False
    account_blocked: bool = False
    trade_suspended_by_user: bool = False
    pattern_day_trader: bool = False
    daytrade_count: int | None = None
    options_trading_level: int | None = None

    _n = field_validator(
        "equity", "cash", "buying_power", "last_equity", "long_market_value",
        "short_market_value", "options_buying_power", "multiplier", mode="before",
    )(_num)

    @property
    def can_trade(self) -> bool:
        return not (self.trading_blocked or self.account_blocked or self.trade_suspended_by_user)


class AccountConfig(_Model):
    no_shorting: bool | None = None
    suspend_trade: bool | None = None
    fractional_trading: bool | None = None
    max_margin_multiplier: str | None = None
    max_options_trading_level: int | None = None
    trade_confirm_email: str | None = None
    dtbp_check: str | None = None
    pdt_check: str | None = None
    ptp_no_exception_entry: bool | None = None
    disable_overnight_trading: bool | None = None


class BrokerPosition(_Model):
    symbol: str
    asset_class: str = "us_equity"
    side: str = "long"
    qty: float                  # signed: negative for a short
    qty_available: float = 0.0  # signed like qty; excludes open orders and covered-call locks
    avg_entry_price: float = 0.0
    current_price: float | None = None
    market_value: float | None = None
    cost_basis: float | None = None
    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None

    _n = field_validator(
        "qty", "qty_available", "avg_entry_price", "current_price", "market_value",
        "cost_basis", "unrealized_pl", "unrealized_plpc", mode="before",
    )(_num)

    @property
    def is_option(self) -> bool:
        return self.asset_class == "us_option" or bool(_OCC_RE.match(self.symbol))

    @property
    def is_equity(self) -> bool:
        return self.asset_class == "us_equity" and not self.is_option

    @property
    def is_long(self) -> bool:
        return self.qty > _EPS

    @property
    def is_short(self) -> bool:
        return self.qty < -_EPS

    @property
    def whole_qty(self) -> int:
        """Whole shares held long (0 for shorts)."""
        return int(math.floor(self.qty + _EPS)) if self.is_long else 0

    @property
    def free_whole_qty(self) -> int:
        """Whole long shares not reserved by open orders or covered calls."""
        return int(math.floor(self.qty_available + _EPS)) if self.qty_available > _EPS else 0

    @property
    def fractional_qty(self) -> float:
        return round(self.qty - self.whole_qty, 9) if self.is_long else 0.0


class BrokerOrder(_Model):
    id: str
    client_order_id: str = ""
    symbol: str = ""
    asset_class: str = ""
    side: str = ""
    type: str = Field("", alias="type")
    order_class: str = ""
    status: str
    qty: float | None = None
    notional: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = ""
    position_intent: str | None = None
    extended_hours: bool = False
    submitted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    filled_at: datetime | None = None
    canceled_at: datetime | None = None
    expired_at: datetime | None = None
    expires_at: datetime | None = None
    failed_at: datetime | None = None
    replaced_by: str | None = None
    replaces: str | None = None
    legs: list[BrokerOrder] = Field(default_factory=list)

    _n = field_validator(
        "qty", "notional", "filled_qty", "filled_avg_price", "limit_price", "stop_price",
        mode="before",
    )(_num)

    @field_validator("legs", mode="before")
    @classmethod
    def _legs(cls, v):
        return v or []

    @field_validator("filled_qty", mode="after")
    @classmethod
    def _fq(cls, v):
        return v or 0.0

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_ORDER_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES

    @property
    def remaining_qty(self) -> float:
        return max((self.qty or 0.0) - self.filled_qty, 0.0)

    @property
    def is_sell_stop(self) -> bool:
        return self.side == "sell" and self.type in ("stop", "stop_limit", "trailing_stop")


OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit", "trailing_stop"]
TimeInForce = Literal["day", "gtc", "opg", "cls", "ioc", "fok"]
PositionIntent = Literal["buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"]


class OrderRequest(_Model):
    symbol: str
    side: OrderSide
    qty: float
    type: OrderType
    time_in_force: TimeInForce
    client_order_id: str
    position_intent: PositionIntent
    limit_price: float | None = None
    stop_price: float | None = None
    extended_hours: bool = False
    order_class: str = "simple"

    @field_validator("client_order_id")
    @classmethod
    def _cid(cls, v: str) -> str:
        if not v or len(v) > 128:
            raise ValueError("client_order_id must be 1-128 characters")
        return v

    @field_validator("qty")
    @classmethod
    def _qty(cls, v: float) -> float:
        if not (v > 0) or not math.isfinite(v):
            raise ValueError("qty must be > 0")
        return v

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side,
            "qty": _fmt_qty(self.qty),
            "type": self.type,
            "time_in_force": self.time_in_force,
            "client_order_id": self.client_order_id,
            "position_intent": self.position_intent,
        }
        if self.limit_price is not None:
            body["limit_price"] = _fmt_price(self.limit_price)
        if self.stop_price is not None:
            body["stop_price"] = _fmt_price(self.stop_price)
        if self.extended_hours:
            body["extended_hours"] = True
        if self.order_class and self.order_class != "simple":
            body["order_class"] = self.order_class
        return body


class MarketClock(_Model):
    is_open: bool
    timestamp: datetime
    next_open: datetime
    next_close: datetime


class CalendarDay(_Model):
    date: date
    open: str
    close: str
    session_open: str | None = None
    session_close: str | None = None
    settlement_date: date | None = None


def _fmt_qty(q: float) -> str:
    if abs(q - round(q)) < _EPS:
        return str(int(round(q)))
    return f"{q:.9f}".rstrip("0").rstrip(".")


def round_price(p: float) -> float:
    """Alpaca price increments: cents at or above $1, 4 decimals below."""
    return round(p, 2) if p >= 1 else round(p, 4)


def _fmt_price(p: float) -> str:
    p = round_price(p)
    return f"{p:.2f}" if p >= 1 else f"{p:.4f}"


# --------------------------------------------------------------------- client


def resolve_base_url(config: type[Config] | Any = Config) -> str:
    """One setting decides the endpoint (F-03-14).

    An explicit ALPACA_BASE_URL wins; otherwise STOCK_AGENT_MODE=LIVE selects
    the live host and anything else selects paper.
    """
    explicit = (getattr(config, "ALPACA_BASE_URL", "") or "").rstrip("/")
    if explicit:
        return explicit
    mode = str(getattr(config, "MODE", "PAPER") or "PAPER").upper()
    return LIVE_URL if mode == "LIVE" else PAPER_URL


def is_paper_url(url: str) -> bool:
    return "paper-api." in url


class AlpacaClient:
    """Async Alpaca Trading API client. One instance per process, shared by
    the stock execution engine and the options module."""

    def __init__(
        self,
        base_url: str | None = None,
        key_id: str | None = None,
        secret: str | None = None,
        *,
        config: Any = Config,
        timeout: float = 15.0,
        get_retries: int = 3,
        lookup_attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        data_url: str | None = None,
    ):
        self.base_url = (base_url or resolve_base_url(config)).rstrip("/")
        self.data_url = (data_url or getattr(config, "ALPACA_DATA_URL", "") or "https://data.alpaca.markets").rstrip("/")
        self._key = key_id if key_id is not None else getattr(config, "ALPACA_API_KEY", "")
        self._secret = secret if secret is not None else getattr(config, "ALPACA_SECRET_KEY", "")
        self.timeout = timeout
        self.get_retries = max(1, get_retries)
        self.lookup_attempts = max(1, lookup_attempts)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def is_paper(self) -> bool:
        return is_paper_url(self.base_url)

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kw: dict[str, Any] = {"timeout": self.timeout}
            if self._transport is not None:
                kw["transport"] = self._transport
            self._client = httpx.AsyncClient(**kw)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key or "",
            "APCA-API-SECRET-KEY": self._secret or "",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------ transport

    async def _send_once(self, method: str, path: str, *, params=None, json_body=None,
                         base: str | None = None) -> httpx.Response:
        try:
            return await self._http().request(
                method, f"{base or self.base_url}{path}", headers=self._headers(), params=params, json=json_body,
            )
        except httpx.TimeoutException as e:
            raise BrokerError("timeout", str(e) or "timed out", method=method, path=path) from e
        except httpx.HTTPError as e:
            raise BrokerError("network", str(e) or type(e).__name__, method=method, path=path) from e

    @staticmethod
    def _error_from(resp: httpx.Response, method: str, path: str) -> BrokerError:
        code = None
        message = resp.text[:300]
        try:
            data = resp.json()
            if isinstance(data, dict):
                code = data.get("code")
                message = str(data.get("message") or message)[:300]
        except ValueError:
            pass
        s = resp.status_code
        kind: ErrorKind
        if s == 401:
            kind = "auth"
        elif s == 404:
            kind = "not_found"
        elif s == 429:
            kind = "rate_limited"
        elif s >= 500:
            kind = "server"
        else:
            kind = "rejected"
        return BrokerError(kind, message, status=s, code=code if isinstance(code, int) else None,
                           method=method, path=path)

    @staticmethod
    def _decode(resp: httpx.Response, method: str, path: str) -> Any:
        if resp.status_code in (204, 207) and not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise BrokerError("bad_response", "reply was not JSON", status=resp.status_code,
                              method=method, path=path) from e

    async def _backoff(self, attempt: int, resp: httpx.Response | None) -> None:
        delay = min(2.0 ** attempt, 10.0)
        if resp is not None and resp.status_code == 429:
            reset = resp.headers.get("X-RateLimit-Reset")
            try:
                if reset:
                    wait = float(reset) - datetime.now(timezone.utc).timestamp()
                    delay = min(max(wait, 1.0), 60.0)
            except ValueError:
                pass
        await asyncio.sleep(delay)

    async def _get(self, path: str, params: dict | None = None, *, base: str | None = None) -> Any:
        """Idempotent request with retries on 429/5xx/timeout/network."""
        last: BrokerError | None = None
        for attempt in range(self.get_retries):
            resp: httpx.Response | None = None
            try:
                resp = await self._send_once("GET", path, params=params, base=base)
            except BrokerError as e:
                last = e
            else:
                if resp.status_code < 400:
                    return self._decode(resp, "GET", path)
                last = self._error_from(resp, "GET", path)
                if not last.retryable:
                    raise last
            if attempt + 1 < self.get_retries:
                await self._backoff(attempt, resp)
        assert last is not None
        raise last

    async def _write(self, method: str, path: str, json_body: dict | None = None, params=None) -> Any:
        """Non-idempotent request: one attempt; the caller decides what a failure means."""
        resp = await self._send_once(method, path, params=params, json_body=json_body)
        if resp.status_code >= 400:
            raise self._error_from(resp, method, path)
        return self._decode(resp, method, path)

    # ------------------------------------------------------------ account

    async def get_account(self) -> Account:
        data = await self._get("/v2/account")
        return self._parse(Account, data, "/v2/account")

    async def get_account_config(self) -> AccountConfig:
        data = await self._get("/v2/account/configurations")
        return self._parse(AccountConfig, data, "/v2/account/configurations")

    async def set_account_config(self, **fields: Any) -> AccountConfig:
        """PATCH the configuration, then read it back and verify every field."""
        if not fields:
            raise ValueError("no fields to set")
        await self._write("PATCH", "/v2/account/configurations", fields)
        cfg = await self.get_account_config()
        for k, v in fields.items():
            if getattr(cfg, k, None) != v:
                raise BrokerError("conflict", f"account configuration {k} reads back {getattr(cfg, k, None)!r}, "
                                  f"expected {v!r}", method="PATCH", path="/v2/account/configurations")
        return cfg

    # ------------------------------------------------------------ positions

    async def get_positions(self) -> list[BrokerPosition]:
        data = await self._get("/v2/positions")
        if not isinstance(data, list):
            raise BrokerError("bad_response", "positions reply was not a list", method="GET", path="/v2/positions")
        return [self._parse(BrokerPosition, p, "/v2/positions") for p in data]

    async def get_position(self, symbol: str) -> BrokerPosition | None:
        """None only when the broker answers 404 (no position)."""
        path = f"/v2/positions/{symbol}"
        try:
            data = await self._get(path)
        except BrokerError as e:
            if e.kind == "not_found":
                return None
            raise
        return self._parse(BrokerPosition, data, path)

    async def free_shares(self, symbol: str) -> int:
        """Whole long shares not reserved by open orders (stops) or covered
        calls, read fresh from the broker. 0 when there is no long."""
        pos = await self.get_position(symbol)
        return pos.free_whole_qty if pos is not None and pos.is_long else 0

    # ------------------------------------------------------------ orders

    async def list_orders(
        self,
        status: Literal["open", "closed", "all"] = "open",
        *,
        symbols: list[str] | None = None,
        after: datetime | None = None,
        nested: bool = True,
        limit: int = 500,
        max_pages: int = 20,
    ) -> list[BrokerOrder]:
        out: list[BrokerOrder] = []
        seen: set[str] = set()
        until: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"status": status, "limit": limit, "direction": "desc",
                                      "nested": "true" if nested else "false"}
            if symbols:
                params["symbols"] = ",".join(symbols)
            if after:
                params["after"] = after.astimezone(timezone.utc).isoformat()
            if until:
                params["until"] = until
            data = await self._get("/v2/orders", params)
            if not isinstance(data, list):
                raise BrokerError("bad_response", "orders reply was not a list", method="GET", path="/v2/orders")
            page = [self._parse(BrokerOrder, o, "/v2/orders") for o in data]
            fresh = [o for o in page if o.id not in seen]
            for o in fresh:
                seen.add(o.id)
            out.extend(fresh)
            if len(page) < limit or not fresh:
                break
            oldest = min((o.submitted_at or o.created_at for o in page if (o.submitted_at or o.created_at)),
                         default=None)
            if oldest is None:
                break
            until = oldest.astimezone(timezone.utc).isoformat()
        return out

    async def get_order(self, order_id: str) -> BrokerOrder:
        path = f"/v2/orders/{order_id}"
        return self._parse(BrokerOrder, await self._get(path, {"nested": "true"}), path)

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """None only when the broker answers 404."""
        path = "/v2/orders:by_client_order_id"
        try:
            data = await self._get(path, {"client_order_id": client_order_id})
        except BrokerError as e:
            if e.kind == "not_found":
                return None
            raise
        return self._parse(BrokerOrder, data, path)

    async def submit_order(self, req: OrderRequest, *, skip_guard: bool = False) -> BrokerOrder:
        """Submit one order. Raises BrokerError; `uncertain=True` means the
        order may exist at the broker under `req.client_order_id`."""
        if not skip_guard:
            await self._guard(req)
        body = req.to_body()
        for submit_attempt in range(2):
            try:
                data = await self._write("POST", "/v2/orders", body)
                return self._parse(BrokerOrder, data, "/v2/orders")
            except BrokerError as e:
                if e.kind not in ("timeout", "network", "server", "bad_response", "rate_limited"):
                    raise
                if e.kind == "rate_limited":
                    # 429: the order was not accepted. One wait, one retry.
                    if submit_attempt == 0:
                        await asyncio.sleep(2.0)
                        continue
                    raise
                logger.warning("Order %s: %s; looking it up by client_order_id", req.client_order_id, e)
                found, lookup_ok = await self._lookup_after_failure(req.client_order_id)
                if found is not None:
                    return found
                if not lookup_ok or submit_attempt == 1:
                    raise BrokerError(e.kind, e.message, status=e.status, code=e.code, uncertain=True,
                                      method="POST", path="/v2/orders") from e
                # The broker confirmed it has no such order: resubmit once with the same id.
        raise BrokerError("server", "unreachable", uncertain=True, method="POST", path="/v2/orders")

    async def _lookup_after_failure(self, cid: str) -> tuple[BrokerOrder | None, bool]:
        """(order, lookup_succeeded). lookup_succeeded=False means we could not ask."""
        ok = False
        for attempt in range(self.lookup_attempts):
            await asyncio.sleep(1.0 + attempt * 2.0)
            try:
                found = await self.get_order_by_client_id(cid)
            except BrokerError as e:
                logger.warning("Lookup of %s failed: %s", cid, e)
                continue
            ok = True
            if found is not None:
                return found, True
        return None, ok

    async def cancel_order(self, order_id: str) -> None:
        """Ask the broker to cancel. Returns once accepted; use
        `wait_terminal` to confirm. 422 (not cancelable) re-reads the order:
        terminal -> fine, otherwise conflict."""
        path = f"/v2/orders/{order_id}"
        try:
            await self._write("DELETE", path)
        except BrokerError as e:
            if e.kind == "not_found":
                raise
            if e.status == 422:
                o = await self.get_order(order_id)
                if o.is_terminal:
                    return
                raise BrokerError("conflict", f"order not cancelable in status {o.status}", status=422,
                                  method="DELETE", path=path) from e
            if e.kind in ("timeout", "network", "server"):
                # Cancel is idempotent: check, then try once more.
                o = await self.get_order(order_id)
                if o.is_terminal or o.status == "pending_cancel":
                    return
                await self._write("DELETE", path)
                return
            raise

    async def replace_order(self, order_id: str, **fields: Any) -> BrokerOrder:
        body = {k: (_fmt_price(v) if k in ("limit_price", "stop_price") else
                    (_fmt_qty(v) if k == "qty" else v)) for k, v in fields.items() if v is not None}
        path = f"/v2/orders/{order_id}"
        return self._parse(BrokerOrder, await self._write("PATCH", path, body), path)

    async def wait_terminal(self, order_id: str, *, timeout_s: float, poll_s: float = 2.0) -> BrokerOrder:
        """Poll until the order is terminal or the timeout passes; return the last read."""
        waited = 0.0
        order = await self.get_order(order_id)
        while not order.is_terminal and waited < timeout_s:
            await asyncio.sleep(poll_s)
            waited += poll_s
            order = await self.get_order(order_id)
        return order

    # ------------------------------------------------------------ market hours

    async def get_clock(self) -> MarketClock:
        return self._parse(MarketClock, await self._get("/v2/clock"), "/v2/clock")

    async def get_calendar(self, start: date, end: date) -> list[CalendarDay]:
        data = await self._get("/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})
        if not isinstance(data, list):
            raise BrokerError("bad_response", "calendar reply was not a list", method="GET", path="/v2/calendar")
        return [self._parse(CalendarDay, d, "/v2/calendar") for d in data]

    # ------------------------------------------------------------ activities

    async def get_activities(self, activity_types: tuple[str, ...] | list[str] = ("FILL",), *,
                             after: datetime | None = None, page_size: int = 100,
                             max_pages: int = 20) -> list[dict]:
        """Account activities, newest first, following `page_token` paging."""
        out: list[dict] = []
        token: str | None = None
        path = "/v2/account/activities"
        for _ in range(max_pages):
            params: dict[str, Any] = {"activity_types": ",".join(activity_types), "page_size": page_size,
                                      "direction": "desc"}
            if after:
                params["after"] = after.astimezone(timezone.utc).isoformat()
            if token:
                params["page_token"] = token
            data = await self._get(path, params)
            if not isinstance(data, list):
                raise BrokerError("bad_response", "activities reply was not a list", method="GET", path=path)
            out.extend(data)
            if len(data) < page_size or not data:
                break
            token = data[-1].get("id")
            if not token:
                break
        return out

    # ------------------------------------------------------------ options support (used by the options module)

    async def submit_raw_order(self, body: dict[str, Any]) -> BrokerOrder:
        """Submit an order body as given (options, mleg). Requires
        `client_order_id`. Equity bodies are refused here: use `submit_order`,
        which applies the sell guard. Same lookup-after-failure rule."""
        cid = body.get("client_order_id")
        if not cid:
            raise ValueError("client_order_id is required")
        symbol = str(body.get("symbol") or "")
        if body.get("order_class") != "mleg" and not _OCC_RE.match(symbol):
            raise BrokerError("guard", "equity orders must go through submit_order", method="POST",
                              path="/v2/orders")
        try:
            data = await self._write("POST", "/v2/orders", body)
            return self._parse(BrokerOrder, data, "/v2/orders")
        except BrokerError as e:
            if e.kind not in ("timeout", "network", "server", "bad_response"):
                raise
            found, ok = await self._lookup_after_failure(cid)
            if found is not None:
                return found
            raise BrokerError(e.kind, e.message, status=e.status, code=e.code, uncertain=True,
                              method="POST", path="/v2/orders") from e

    async def get_option_contracts(
        self,
        underlying: str,
        *,
        type: Literal["call", "put"] | None = None,
        expiration_gte: date | None = None,
        expiration_lte: date | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 1000,
        max_pages: int = 20,
    ) -> list[dict]:
        path = "/v2/options/contracts"
        out: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"underlying_symbols": underlying, "limit": limit, "status": "active"}
            if type:
                params["type"] = type
            if expiration_gte:
                params["expiration_date_gte"] = expiration_gte.isoformat()
            if expiration_lte:
                params["expiration_date_lte"] = expiration_lte.isoformat()
            if strike_gte is not None:
                params["strike_price_gte"] = str(strike_gte)
            if strike_lte is not None:
                params["strike_price_lte"] = str(strike_lte)
            if token:
                params["page_token"] = token
            data = await self._get(path, params)
            if not isinstance(data, dict) or not isinstance(data.get("option_contracts"), list):
                raise BrokerError("bad_response", "contracts reply malformed", method="GET", path=path)
            out.extend(data["option_contracts"])
            token = data.get("next_page_token")
            if not token:
                break
        return out

    async def get_option_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Market data: latestQuote, latestTrade, greeks, impliedVolatility per contract."""
        if not symbols:
            return {}
        out: dict[str, dict] = {}
        path = "/v1beta1/options/snapshots"
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            data = await self._get_data(path, {"symbols": ",".join(chunk)})
            snaps = data.get("snapshots") if isinstance(data, dict) else None
            if not isinstance(snaps, dict):
                raise BrokerError("bad_response", "option snapshots reply malformed", method="GET", path=path)
            out.update(snaps)
        return out

    async def exercise(self, symbol: str) -> None:
        await self._write("POST", f"/v2/positions/{symbol}/exercise")

    async def _get_data(self, path: str, params: dict | None = None) -> Any:
        """GET on the market data host with the same retry rules."""
        return await self._get(path, params, base=self.data_url)

    # ------------------------------------------------------------ guard

    async def _guard(self, req: OrderRequest) -> None:
        """Refuse any equity order that could open or enlarge a short, or sell
        more than the broker says is free. Options orders are the options
        module's responsibility and are passed through with checks on intent
        only."""
        is_option = bool(_OCC_RE.match(req.symbol))
        if is_option:
            return
        if req.position_intent == "sell_to_open":
            raise BrokerError("guard", f"refused: {req.symbol} sell_to_open would open a short",
                              method="POST", path="/v2/orders")
        if req.side == "sell":
            if req.position_intent != "sell_to_close":
                raise BrokerError("guard", f"refused: equity sell must be sell_to_close ({req.symbol})",
                                  method="POST", path="/v2/orders")
            pos = await self.get_position(req.symbol)
            if pos is None or not pos.is_long:
                raise BrokerError("guard", f"refused: no long position in {req.symbol}; selling would open a short",
                                  method="POST", path="/v2/orders")
            if req.qty > pos.qty_available + _EPS:
                raise BrokerError("guard", f"refused: sell {req.qty:g} {req.symbol} exceeds available "
                                  f"{pos.qty_available:g} (held {pos.qty:g})", method="POST", path="/v2/orders")
            return
        # buy
        if req.position_intent not in ("buy_to_open", "buy_to_close"):
            raise BrokerError("guard", f"refused: bad buy intent {req.position_intent}", method="POST",
                              path="/v2/orders")
        if req.position_intent == "buy_to_close":
            pos = await self.get_position(req.symbol)
            if pos is None or not pos.is_short:
                raise BrokerError("guard", f"refused: buy_to_close but no short in {req.symbol}",
                                  method="POST", path="/v2/orders")
            if req.qty > abs(pos.qty) + _EPS:
                raise BrokerError("guard", f"refused: cover {req.qty:g} exceeds short {abs(pos.qty):g}",
                                  method="POST", path="/v2/orders")

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _parse(model, data: Any, path: str):
        if not isinstance(data, dict):
            raise BrokerError("bad_response", f"unexpected reply type {type(data).__name__}", path=path)
        try:
            return model.model_validate(data)
        except Exception as e:  # pydantic.ValidationError and friends
            raise BrokerError("bad_response", f"could not read reply: {e}"[:300], path=path) from e


__all__ = [
    "Account", "AccountConfig", "AlpacaClient", "BrokerError", "BrokerOrder", "BrokerPosition",
    "CalendarDay", "MarketClock", "OPEN_ORDER_STATUSES", "OrderRequest", "TERMINAL_ORDER_STATUSES",
    "is_paper_url", "resolve_base_url", "round_price",
]
