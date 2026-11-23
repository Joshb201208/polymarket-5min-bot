"""An in-memory simulated Alpaca (trading API + market data API).

Serve it through `FakeInternet` (see `tests/fakes/__init__.py`):

    clock = SimClock.at_et(2026, 11, 23, 9, 35)
    alpaca = FakeAlpaca(clock)
    net = FakeInternet([alpaca]).install(monkeypatch)
    clock.install(monkeypatch)

Behaviour follows the public docs (URLs in tests/fakes/README.md). Where the
docs do not say what the real API does, the behaviour is a named switch on
`AlpacaBehaviour` with the default we believe is right and a note saying
whether it is documented, seen in evidence, or a guess ("unverified").

Simplifications (deliberate, documented):
- Prices are whatever the test sets with `set_price`; there is no order book.
  A buy fills at the ask, a sell at the bid, a limit only when marketable.
- Fills are evaluated whenever the clock moves or a price changes; a fill is
  stamped with the time it became due (submission + delay) but priced at the
  price current when it is evaluated.
- Buying power is an approximation of the documented formulas (see
  `_buying_power`).
- Extended-hours trading is accepted but never fills (switch
  `extended_hours_fills`).
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import httpx

from tests.fakes.base import FakeService, json_response, request_json
from tests.fakes.clock import ET, MarketCalendar, SimClock

TRADING_HOSTS = ("paper-api.alpaca.markets", "api.alpaca.markets")
DATA_HOSTS = ("data.alpaca.markets",)

OPEN_STATUSES = {
    "new", "accepted", "partially_filled", "pending_new", "accepted_for_bidding",
    "pending_cancel", "pending_replace", "held", "calculated", "stopped", "suspended",
}
TERMINAL_STATUSES = {"filled", "canceled", "expired", "replaced", "rejected", "done_for_day"}

OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def _f(x: float | None) -> str | None:
    if x is None:
        return None
    if float(x).is_integer():
        return str(int(x))
    return repr(round(float(x), 9))


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _err(status: int, code: int, message: str, **extra) -> httpx.Response:
    return json_response({"code": code, "message": message, **extra}, status)


def occ_symbol(underlying: str, expiration: date, kind: str, strike: float) -> str:
    """OCC option symbol, e.g. AAPL261218C00255000."""
    return f"{underlying}{expiration:%y%m%d}{kind[0].upper()}{int(round(strike * 1000)):08d}"


def parse_occ(symbol: str) -> tuple[str, date, str, float] | None:
    m = OCC_RE.match(symbol)
    if not m:
        return None
    und, ymd, cp, strike = m.groups()
    exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    return und, exp, ("call" if cp == "C" else "put"), int(strike) / 1000


@dataclass
class AlpacaBehaviour:
    """Named switches for behaviour the docs leave open. Change per test."""

    # Potential wash trade: a new simple order is rejected (403) when an
    # opposite-side market/stop order (or a crossing limit) is already open
    # on the symbol. Complex orders (bracket/oco/oto) and trailing stops are
    # exempt. Documented: https://docs.alpaca.markets/us/docs/user-protection
    wash_trade_check: bool = True
    # Selling more than the long position: real API behaviour not documented
    # (unverified). False = reject 403 insufficient qty; True = flip to short.
    allow_long_to_short_flip: bool = False
    # Text of the rejection when shorting is disabled (unverified wording).
    shorting_disabled_message: str = "account is not allowed to short"
    # A DAY order submitted on a market holiday. Evidence: market buys sent
    # at 09:30 on Good Friday 2026 were cancelled by the broker that day and
    # never filled -> "cancel_at_close". Docs say orders outside hours queue
    # for the next session -> "queue_next_session".
    holiday_day_order_policy: str = "cancel_at_close"
    # What a DAY order becomes when the session closes unfilled. Docs:
    # "automatically canceled" (orders-at-alpaca).
    day_order_end_status: str = "canceled"
    # GTC orders are cancelled after this many days. Evidence: GTC stops
    # cancelled 88-90 days after placement; exact rule unverified. None=never.
    gtc_max_age_days: int | None = 90
    # Seconds between an order becoming eligible and filling, mid-session.
    # Evidence: mid-session paper fills in under 3 seconds.
    fill_delay_seconds: float = 0.0
    # Orders submitted before the open or in the first `opening_window_seconds`
    # of a session fill this long after the open. Evidence: paper buys sent in
    # the first two minutes filled 1 to 10 minutes later.
    opening_fill_delay_seconds: float = 300.0
    opening_window_seconds: float = 120.0
    # Accepted extended-hours orders never fill in the fake (not modelled).
    extended_hours_fills: bool = False
    # Uniqueness of client_order_id: "all" orders ever, or only "open" ones
    # (docs say must be unique; scope unverified).
    client_order_id_unique_scope: str = "all"
    # Cancelling: 0 = canceled at once; >0 = pending_cancel for that long.
    cancel_latency_seconds: float = 0.0
    # Reject requests whose APCA-API-KEY-ID header is empty (401).
    require_auth: bool = False
    # Expire / exercise / assign options automatically at the close of their
    # expiration day (documented auto-exercise of ITM >= $0.01).
    auto_expire_options: bool = True
    # Evidence: the broker closed expiring spreads itself at 15:30 ET on
    # expiry day. None = do not emulate.
    expiry_day_liquidation_time: time | None = None
    # Buying power multiplier applied to short sales (docs: 102%-103% margin).
    short_sale_bp_factor: float = 1.03


@dataclass
class Asset:
    symbol: str
    name: str = ""
    asset_class: str = "us_equity"
    exchange: str = "NASDAQ"
    tradable: bool = True
    marginable: bool = True
    shortable: bool = True
    easy_to_borrow: bool = True
    fractionable: bool = True
    status: str = "active"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json(self) -> dict:
        return {
            "id": self.id, "class": self.asset_class, "exchange": self.exchange,
            "symbol": self.symbol, "name": self.name or f"{self.symbol} Inc.",
            "status": self.status, "tradable": self.tradable, "marginable": self.marginable,
            "shortable": self.shortable, "easy_to_borrow": self.easy_to_borrow,
            "fractionable": self.fractionable, "maintenance_margin_requirement": 30,
            "attributes": [],
        }


@dataclass
class OptionContract:
    symbol: str
    underlying: str
    expiration: date
    type: str  # call | put
    strike: float
    status: str = "active"
    tradable: bool = True
    open_interest: int = 1000
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "name": f"{self.underlying} {self.expiration} {self.type} {self.strike}",
            "status": self.status, "tradable": self.tradable,
            "expiration_date": self.expiration.isoformat(), "root_symbol": self.underlying,
            "underlying_symbol": self.underlying, "underlying_asset_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, self.underlying)),
            "type": self.type, "style": "american", "strike_price": _f(self.strike), "multiplier": "100",
            "size": "100", "open_interest": str(self.open_interest),
            "open_interest_date": None, "close_price": None, "close_price_date": None,
        }


@dataclass
class Quote:
    bid: float
    ask: float
    last: float
    bid_size: int = 100
    ask_size: int = 100
    at: datetime | None = None


@dataclass
class Order:
    id: str
    client_order_id: str
    symbol: str
    asset_class: str
    side: str
    type: str
    time_in_force: str
    qty: float | None
    notional: float | None
    limit_price: float | None
    stop_price: float | None
    order_class: str
    extended_hours: bool
    created_at: datetime
    status: str = "new"
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    filled_at: datetime | None = None
    canceled_at: datetime | None = None
    expired_at: datetime | None = None
    failed_at: datetime | None = None
    replaced_at: datetime | None = None
    replaced_by: str | None = None
    replaces: str | None = None
    position_intent: str | None = None
    parent_id: str | None = None
    leg_ids: list[str] = field(default_factory=list)
    ratio_qty: float | None = None
    trail_percent: float | None = None
    trail_price: float | None = None
    hwm: float | None = None
    # fake-internal
    eligible_at: datetime | None = None  # earliest fill time
    expires_at: datetime | None = None  # when a DAY order dies
    triggered: bool = False  # stop has been hit
    fill_plan: list[float] = field(default_factory=list)  # remaining partial chunks
    next_chunk_at: datetime | None = None
    cancel_at: datetime | None = None
    group: str | None = None  # oco/bracket exit group: one fill cancels the rest

    @property
    def remaining(self) -> float:
        return max((self.qty or 0.0) - self.filled_qty, 0.0)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


@dataclass
class Position:
    symbol: str
    qty: float  # signed: negative = short
    avg_entry_price: float
    asset_class: str = "us_equity"
    lastday_price: float | None = None
    asset_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class FakeAlpaca(FakeService):
    name = "alpaca"
    hosts = TRADING_HOSTS + DATA_HOSTS

    def __init__(
        self,
        clock: SimClock,
        *,
        cash: float = 100_000.0,
        multiplier: int = 4,
        shorting_enabled: bool = True,
        options_level: int = 3,
        pattern_day_trader: bool = False,
        behaviour: AlpacaBehaviour | None = None,
        calendar: MarketCalendar | None = None,
    ):
        super().__init__()
        self.clock = clock
        self.calendar = calendar or clock.calendar
        self.behaviour = behaviour or AlpacaBehaviour()
        self.account_id = str(uuid.uuid4())
        self.cash = float(cash)
        self.multiplier = multiplier
        self.shorting_enabled = shorting_enabled
        self.options_level = options_level
        self.pattern_day_trader = pattern_day_trader
        self.trading_blocked = False
        self.account_blocked = False
        self.last_equity = float(cash)
        self.configurations: dict[str, Any] = {
            "dtbp_check": "entry", "trade_confirm_email": "all", "suspend_trade": False,
            "no_shorting": not shorting_enabled, "fractional_trading": True,
            "max_margin_multiplier": str(multiplier), "max_options_trading_level": options_level,
            "pdt_check": "entry", "ptp_no_exception_entry": False,
        }
        self.assets: dict[str, Asset] = {}
        self.contracts: dict[str, OptionContract] = {}
        self.quotes: dict[str, Quote] = {}
        self.trades_history: dict[str, list[tuple[datetime, float, int]]] = {}
        self.orders: dict[str, Order] = {}
        self.order_seq: list[str] = []
        self.positions: dict[str, Position] = {}
        self.activities: list[dict] = []
        self.equity_history: list[tuple[datetime, float]] = []
        self.news: list[dict] = []
        self.fill_scripts: dict[str, list[list[float]]] = {}
        self._processed_until: datetime = clock.now()
        clock.on_advance(self._on_advance)

    # ------------------------------------------------------------------ setup
    def now(self) -> datetime:
        return self.clock.now()

    def add_asset(self, symbol: str, **kw) -> Asset:
        a = Asset(symbol=symbol, **kw)
        self.assets[symbol] = a
        return a

    def set_price(self, symbol: str, last: float, *, bid: float | None = None, ask: float | None = None,
                  spread: float = 0.0, volume: int = 100) -> None:
        """Set the current price. Creates the asset if needed and re-runs the fill engine."""
        if symbol not in self.assets and not OCC_RE.match(symbol):
            self.add_asset(symbol)
        b = bid if bid is not None else round(last - spread / 2, 4)
        a = ask if ask is not None else round(last + spread / 2, 4)
        q = self.quotes.get(symbol)
        lastday = q.last if q else last
        self.quotes[symbol] = Quote(bid=b, ask=a, last=last, at=self.now())
        self.trades_history.setdefault(symbol, []).append((self.now(), last, volume))
        pos = self.positions.get(symbol)
        if pos and pos.lastday_price is None:
            pos.lastday_price = lastday
        self._process(self.now())

    def set_option_quote(self, symbol: str, bid: float, ask: float, last: float | None = None) -> None:
        self.set_price(symbol, last if last is not None else round((bid + ask) / 2, 4), bid=bid, ask=ask)

    def add_option_contract(self, underlying: str, expiration: date, type: str, strike: float, **kw) -> OptionContract:
        sym = occ_symbol(underlying, expiration, type, strike)
        c = OptionContract(symbol=sym, underlying=underlying, expiration=expiration, type=type, strike=strike, **kw)
        self.contracts[sym] = c
        return c

    def add_option_chain(self, underlying: str, expiration: date, strikes: list[float]) -> list[OptionContract]:
        return [self.add_option_contract(underlying, expiration, t, k) for k in strikes for t in ("call", "put")]

    def seed_position(self, symbol: str, qty: float, avg_entry_price: float, *, price: float | None = None) -> Position:
        """Put a position on the book without a trade (cash unchanged)."""
        cls = "us_option" if OCC_RE.match(symbol) else "us_equity"
        if cls == "us_equity" and symbol not in self.assets:
            self.add_asset(symbol)
        p = Position(symbol=symbol, qty=float(qty), avg_entry_price=float(avg_entry_price), asset_class=cls)
        self.positions[symbol] = p
        if price is not None or symbol not in self.quotes:
            self.set_price(symbol, price if price is not None else avg_entry_price)
        return p

    def script_partial_fills(self, symbol: str, chunks: list[float]) -> None:
        """The next order on `symbol` fills in these quantity chunks, one per minute."""
        self.fill_scripts.setdefault(symbol, []).append(list(chunks))

    def add_news(self, symbols: list[str], headline: str, *, summary: str = "", source: str = "benzinga",
                 created_at: datetime | None = None) -> dict:
        n = {
            "id": len(self.news) + 1, "headline": headline, "author": "fake", "summary": summary,
            "content": "", "url": f"https://example.invalid/news/{len(self.news) + 1}",
            "images": [], "symbols": list(symbols), "source": source,
            "created_at": _iso(created_at or self.now()), "updated_at": _iso(created_at or self.now()),
        }
        self.news.append(n)
        return n

    def pay_dividend(self, symbol: str, per_share: float) -> dict | None:
        """Credit a cash dividend (the real paper API does NOT do this; docs)."""
        pos = self.positions.get(symbol)
        if not pos or pos.qty == 0:
            return None
        amount = round(pos.qty * per_share, 2)
        self.cash += amount
        return self._nta("DIV", symbol=symbol, qty=pos.qty, net_amount=amount, per_share_amount=per_share)

    # --------------------------------------------------------------- queries
    def position(self, symbol: str) -> Position | None:
        p = self.positions.get(symbol)
        return p if p and p.qty != 0 else None

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [self.orders[i] for i in self.order_seq if self.orders[i].is_open and (symbol is None or self.orders[i].symbol == symbol)]

    def orders_for(self, symbol: str) -> list[Order]:
        return [self.orders[i] for i in self.order_seq if self.orders[i].symbol == symbol]

    def price(self, symbol: str) -> float | None:
        q = self.quotes.get(symbol)
        return q.last if q else None

    # ---------------------------------------------------------- accounting
    def _mark(self, symbol: str) -> float:
        q = self.quotes.get(symbol)
        if q:
            return q.last
        p = self.positions.get(symbol)
        return p.avg_entry_price if p else 0.0

    def _mult(self, symbol: str) -> float:
        return 100.0 if OCC_RE.match(symbol) else 1.0

    def _market_values(self) -> tuple[float, float]:
        long_mv = short_mv = 0.0
        for p in self.positions.values():
            mv = p.qty * self._mark(p.symbol) * self._mult(p.symbol)
            if mv >= 0:
                long_mv += mv
            else:
                short_mv += mv
        return long_mv, short_mv

    def equity(self) -> float:
        lm, sm = self._market_values()
        return self.cash + lm + sm

    def _open_order_reserve(self) -> float:
        """Notional reserved by open buy orders and open short sales."""
        total = 0.0
        for o in self.open_orders():
            if o.status == "held" or o.order_class == "mleg_leg":
                continue
            px = o.limit_price or (self.quotes[o.symbol].ask if o.symbol in self.quotes else 0.0)
            if o.side == "buy":
                total += o.remaining * px * self._mult(o.symbol) if o.qty else (o.notional or 0.0)
            elif o.position_intent == "sell_to_open_short":
                total += o.remaining * px * self.behaviour.short_sale_bp_factor
        return total

    def _buying_power(self) -> float:
        """Approximation of https://docs.alpaca.markets/docs/orders-at-alpaca buying power.

        multiplier 1 -> cash; otherwise (equity - maintenance margin) * multiplier,
        using current values instead of last close (documented approximation),
        minus what open orders reserve.
        """
        lm, sm = self._market_values()
        eq = self.cash + lm + sm
        if self.multiplier <= 1:
            bp = self.cash
        else:
            maint = 0.3 * (lm + abs(sm))
            bp = max(eq - maint, 0.0) * min(self.multiplier, 4)
            if self.multiplier == 2:
                bp = max(eq - 0.5 * (lm + abs(sm)), 0.0) * 2
        return max(bp - self._open_order_reserve(), 0.0)

    def _options_buying_power(self) -> float:
        reserve = sum(
            o.remaining * (o.limit_price or 0.0) * 100
            for o in self.open_orders() if o.asset_class == "us_option" and o.side == "buy"
        )
        return max(self.cash - reserve, 0.0)

    def _held_for_orders(self, symbol: str, exclude: str | None = None) -> float:
        held = 0.0
        groups: dict[str, float] = {}
        for o in self.open_orders(symbol):
            if o.id == exclude or o.side != "sell" or o.position_intent == "sell_to_open_short":
                continue
            if o.group:
                groups[o.group] = max(groups.get(o.group, 0.0), o.remaining)
            else:
                held += o.remaining
        return held + sum(groups.values())

    def account_json(self) -> dict:
        lm, sm = self._market_values()
        eq = self.cash + lm + sm
        bp = self._buying_power()
        return {
            "id": self.account_id, "account_number": "PA0FAKE0000", "status": "ACTIVE",
            "crypto_status": "INACTIVE", "currency": "USD",
            "cash": _f(round(self.cash, 2)), "portfolio_value": _f(round(eq, 2)), "equity": _f(round(eq, 2)),
            "last_equity": _f(round(self.last_equity, 2)),
            "long_market_value": _f(round(lm, 2)), "short_market_value": _f(round(sm, 2)),
            "buying_power": _f(round(bp, 2)), "regt_buying_power": _f(round(bp / 2 if self.multiplier > 2 else bp, 2)),
            "daytrading_buying_power": _f(round(bp if self.multiplier >= 4 else 0, 2)),
            "effective_buying_power": _f(round(bp, 2)), "non_marginable_buying_power": _f(round(max(self.cash, 0), 2)),
            "options_buying_power": _f(round(self._options_buying_power(), 2)),
            "options_approved_level": self.options_level, "options_trading_level": self.options_level,
            "multiplier": str(self.multiplier), "shorting_enabled": self.shorting_enabled,
            "pattern_day_trader": self.pattern_day_trader, "trading_blocked": self.trading_blocked,
            "transfers_blocked": False, "account_blocked": self.account_blocked,
            "trade_suspended_by_user": bool(self.configurations.get("suspend_trade")),
            "initial_margin": _f(round(0.5 * (lm + abs(sm)), 2)),
            "maintenance_margin": _f(round(0.3 * (lm + abs(sm)), 2)),
            "last_maintenance_margin": _f(round(0.3 * (lm + abs(sm)), 2)),
            "sma": _f(round(eq, 2)), "daytrade_count": 0, "accrued_fees": "0", "pending_transfer_in": "0",
            "created_at": "2025-01-02T14:30:00Z",
        }

    def position_json(self, p: Position) -> dict:
        mult = self._mult(p.symbol)
        px = self._mark(p.symbol)
        mv = p.qty * px * mult
        cost = p.qty * p.avg_entry_price * mult
        upl = mv - cost
        held = self._held_for_orders(p.symbol) if p.qty > 0 else 0.0
        lastday = p.lastday_price if p.lastday_price is not None else px
        side = "long" if p.qty > 0 else "short"
        return {
            "asset_id": p.asset_id, "symbol": p.symbol, "exchange": "NASDAQ" if mult == 1 else "",
            "asset_class": p.asset_class, "asset_marginable": mult == 1,
            "avg_entry_price": _f(p.avg_entry_price), "qty": _f(p.qty),
            "qty_available": _f(p.qty - held if p.qty > 0 else p.qty), "side": side,
            "market_value": _f(round(mv, 2)), "cost_basis": _f(round(cost, 2)),
            "unrealized_pl": _f(round(upl, 2)),
            "unrealized_plpc": _f(round(upl / abs(cost), 6) if cost else 0),
            "unrealized_intraday_pl": _f(round((px - lastday) * p.qty * mult, 2)),
            "unrealized_intraday_plpc": _f(round((px - lastday) / lastday, 6) if lastday else 0),
            "current_price": _f(px), "lastday_price": _f(lastday),
            "change_today": _f(round((px - lastday) / lastday, 6) if lastday else 0),
        }

    def order_json(self, o: Order, nested: bool = True) -> dict:
        d = {
            "id": o.id, "client_order_id": o.client_order_id,
            "created_at": _iso(o.created_at), "updated_at": _iso(self.now()), "submitted_at": _iso(o.created_at),
            "filled_at": _iso(o.filled_at), "expired_at": _iso(o.expired_at), "canceled_at": _iso(o.canceled_at),
            "failed_at": _iso(o.failed_at), "replaced_at": _iso(o.replaced_at),
            "replaced_by": o.replaced_by, "replaces": o.replaces,
            "asset_id": self.assets[o.symbol].id if o.symbol in self.assets else str(uuid.uuid5(uuid.NAMESPACE_DNS, o.symbol or "mleg")),
            "symbol": o.symbol, "asset_class": o.asset_class,
            "notional": _f(o.notional), "qty": _f(o.qty), "filled_qty": _f(o.filled_qty),
            "filled_avg_price": _f(o.filled_avg_price), "order_class": "" if o.order_class == "simple" else o.order_class,
            "order_type": o.type, "type": o.type, "side": o.side, "time_in_force": o.time_in_force,
            "limit_price": _f(o.limit_price), "stop_price": _f(o.stop_price), "status": o.status,
            "extended_hours": o.extended_hours, "legs": None,
            "trail_percent": _f(o.trail_percent), "trail_price": _f(o.trail_price), "hwm": _f(o.hwm),
            "position_intent": o.position_intent, "ratio_qty": _f(o.ratio_qty),
        }
        if o.order_class == "mleg" and not o.symbol:
            d["symbol"] = ""
            d["asset_class"] = ""
        if o.leg_ids and nested:
            d["legs"] = [self.order_json(self.orders[i], nested=False) for i in o.leg_ids]
        return d

    # ------------------------------------------------------------- activities
    def _activity_id(self, at: datetime) -> str:
        return f"{at.astimezone(timezone.utc):%Y%m%d%H%M%S}{at.microsecond // 1000:03d}::{uuid.uuid4()}"

    def _nta(self, kind: str, **fields) -> dict:
        at = self.now()
        a = {
            "id": self._activity_id(at), "activity_type": kind, "date": at.astimezone(ET).date().isoformat(),
            "status": "executed", "description": "", **{k: (_f(v) if isinstance(v, float) else v) for k, v in fields.items()},
        }
        a["_at"] = at
        a["_seq"] = len(self.activities)
        self.activities.append(a)
        return a

    def _fill_activity(self, o: Order, qty: float, price: float, at: datetime) -> None:
        a = {
            "id": self._activity_id(at), "activity_type": "FILL", "transaction_time": _iso(at),
            "type": "fill" if o.remaining <= 1e-9 else "partial_fill",
            "price": _f(price), "qty": _f(qty), "side": o.side, "symbol": o.symbol,
            "leaves_qty": _f(o.remaining), "order_id": o.id, "cum_qty": _f(o.filled_qty),
            "order_status": o.status, "_at": at, "_seq": len(self.activities),
        }
        self.activities.append(a)

    # ------------------------------------------------------------ fill engine
    def _on_advance(self, old: datetime, new: datetime) -> None:
        self._process(new)

    def _session_close_after(self, at: datetime) -> datetime | None:
        s = self.calendar.current_or_next_session(at)
        return s.close if s else None

    def _process(self, until: datetime) -> None:
        # walk session boundaries so day orders and option expiry happen in order
        start = self._processed_until
        if until < start:
            start = until
        checkpoints: list[datetime] = []
        t = start
        for _ in range(400):  # bound the walk
            nxt = self.calendar.next_close(t) if self.calendar.covers(t.astimezone(ET).date()) else None
            if nxt is None or nxt > until:
                break
            checkpoints.append(nxt)
            t = nxt + timedelta(seconds=1)
        checkpoints.append(until)
        for cp in checkpoints:
            self._step(cp)
        self._processed_until = until

    def _step(self, now: datetime) -> None:
        b = self.behaviour
        # liquidation of expiring options (evidence-only behaviour)
        if b.expiry_day_liquidation_time is not None:
            self._expiry_day_liquidation(now)
        for oid in list(self.order_seq):
            o = self.orders[oid]
            if not o.is_open or o.parent_id and self.orders[o.parent_id].order_class == "mleg":
                continue
            if o.cancel_at is not None and now >= o.cancel_at:
                self._finish(o, "canceled", o.cancel_at)
                continue
            if o.status == "held":
                continue
            # fills that were due before an expiry happen first
            due = o.eligible_at if o.eligible_at is not None else now
            if o.expires_at is not None and o.expires_at <= now and (due >= o.expires_at):
                self._finish(o, b.day_order_end_status, o.expires_at)
                continue
            if o.time_in_force == "gtc" and b.gtc_max_age_days is not None:
                if now - o.created_at >= timedelta(days=b.gtc_max_age_days):
                    self._finish(o, "canceled", o.created_at + timedelta(days=b.gtc_max_age_days))
                    continue
            if due > now:
                continue
            fill_at = max(due, min(self._processed_until, now))
            if self.calendar.is_open(now):
                pass
            elif self.calendar.is_open(fill_at) and now <= self.calendar.next_close(fill_at):
                pass  # `now` is the close of the session in which the fill was due
            elif not (o.extended_hours and b.extended_hours_fills):
                continue
            if o.status == "accepted":
                o.status = "new"
            self._try_fill(o, fill_at)
            if o.expires_at is not None and o.expires_at <= now and o.is_open:
                self._finish(o, b.day_order_end_status, o.expires_at)
        if b.auto_expire_options:
            self._expire_options_due(now)
        # equity snapshot at each session close
        if self.calendar.is_trading_day(now.astimezone(ET).date()):
            s = self.calendar.session(now.astimezone(ET).date())
            if s and now >= s.close and (not self.equity_history or self.equity_history[-1][0] < s.close):
                self.equity_history.append((s.close, self.equity()))
                self.last_equity = self.equity()
                for p in self.positions.values():
                    p.lastday_price = self._mark(p.symbol)

    def _finish(self, o: Order, status: str, at: datetime) -> None:
        o.status = status
        if status == "canceled":
            o.canceled_at = at
        elif status == "expired":
            o.expired_at = at
        for lid in o.leg_ids:
            leg = self.orders[lid]
            if leg.is_open:
                self._finish(leg, status, at)

    def _marketable_price(self, o: Order) -> float | None:
        q = self.quotes.get(o.symbol)
        if q is None:
            return None
        if o.type == "trailing_stop":
            if o.side == "sell":
                o.hwm = max(o.hwm or q.last, q.last)
                stop = o.hwm - o.trail_price if o.trail_price else o.hwm * (1 - (o.trail_percent or 0) / 100)
                o.stop_price = round(stop, 4)
            else:
                o.hwm = min(o.hwm or q.last, q.last)
                stop = o.hwm + o.trail_price if o.trail_price else o.hwm * (1 + (o.trail_percent or 0) / 100)
                o.stop_price = round(stop, 4)
        if o.type in ("stop", "stop_limit", "trailing_stop") and not o.triggered:
            if (o.side == "sell" and q.last <= o.stop_price) or (o.side == "buy" and q.last >= o.stop_price):
                o.triggered = True
            else:
                return None
        px = q.ask if o.side == "buy" else q.bid
        if o.type in ("limit", "stop_limit"):
            if o.side == "buy" and px > o.limit_price:
                return None
            if o.side == "sell" and px < o.limit_price:
                return None
        return px

    def _try_fill(self, o: Order, at: datetime) -> None:
        if o.order_class == "mleg":
            self._try_fill_mleg(o, at)
            return
        px = self._marketable_price(o)
        if px is None:
            return
        if o.qty is None and o.notional:
            o.qty = round(o.notional / px, 9)
        now = self.now()
        while o.is_open:
            if o.next_chunk_at is not None:
                if o.next_chunk_at > now:
                    return
                at = max(at, o.next_chunk_at)
            if o.fill_plan:
                chunk = min(o.fill_plan.pop(0), o.remaining)
                o.next_chunk_at = at + timedelta(minutes=1)
            else:
                chunk = o.remaining
            self._execute(o, chunk, px, at)

    def _execute(self, o: Order, qty: float, px: float, at: datetime) -> None:
        if qty <= 0:
            return
        prev = o.filled_qty
        o.filled_qty = round(prev + qty, 9)
        o.filled_avg_price = round(((o.filled_avg_price or 0) * prev + px * qty) / o.filled_qty, 6)
        o.status = "filled" if o.remaining <= 1e-9 else "partially_filled"
        if o.status == "filled":
            o.filled_at = at
        self._apply_to_position(o.symbol, qty if o.side == "buy" else -qty, px, o.asset_class)
        self._fill_activity(o, qty, px, at)
        if o.status == "filled":
            self._on_filled(o, at)

    def _apply_to_position(self, symbol: str, dq: float, px: float, asset_class: str) -> None:
        mult = self._mult(symbol)
        self.cash -= dq * px * mult
        p = self.positions.get(symbol)
        if p is None or p.qty == 0:
            self.positions[symbol] = Position(symbol=symbol, qty=dq, avg_entry_price=px, asset_class=asset_class,
                                              lastday_price=self._mark(symbol))
            return
        new_qty = round(p.qty + dq, 9)
        if p.qty * dq > 0:  # adding
            p.avg_entry_price = (p.avg_entry_price * abs(p.qty) + px * abs(dq)) / abs(new_qty)
        elif new_qty * p.qty < 0:  # flipped through zero
            p.avg_entry_price = px
        p.qty = new_qty
        if abs(p.qty) < 1e-9:
            del self.positions[symbol]

    def _on_filled(self, o: Order, at: datetime) -> None:
        # bracket / oto parent filled: activate children
        if o.leg_ids and o.order_class in ("bracket", "oto"):
            for lid in o.leg_ids:
                leg = self.orders[lid]
                if leg.status == "held":
                    leg.status = "new"
                    leg.eligible_at = at
        # one exit of an oco/bracket group filled: cancel its siblings
        if o.group:
            for other in self.open_orders(o.symbol):
                if other.group == o.group and other.id != o.id:
                    self._finish(other, "canceled", at)

    def _try_fill_mleg(self, o: Order, at: datetime) -> None:
        legs = [self.orders[i] for i in o.leg_ids]
        net = 0.0
        prices = {}
        for leg in legs:
            q = self.quotes.get(leg.symbol)
            if q is None:
                return
            px = q.ask if leg.side == "buy" else q.bid
            prices[leg.id] = px
            net += (px if leg.side == "buy" else -px) * (leg.ratio_qty or 1)
        if o.type == "limit" and net > o.limit_price + 1e-9:
            return
        qty = o.remaining
        for leg in legs:
            leg.qty = qty * (leg.ratio_qty or 1)
            self._execute(leg, leg.remaining, prices[leg.id], at)
        o.filled_qty = qty
        o.filled_avg_price = round(net, 4)
        o.status = "filled"
        o.filled_at = at

    # -------------------------------------------------------------- options
    def _expire_options_due(self, now: datetime) -> None:
        for sym, p in list(self.positions.items()):
            parsed = parse_occ(sym)
            if not parsed:
                continue
            und, exp, kind, strike = parsed
            s = self.calendar.session(exp)
            close = s.close if s else datetime.combine(exp, time(16), ET).astimezone(timezone.utc)
            if now >= close:
                self.settle_option_expiry(sym, at=close)

    def settle_option_expiry(self, symbol: str, *, at: datetime | None = None) -> list[dict]:
        """Exercise / assign / expire an option position at expiry (docs: ITM >= $0.01 auto-exercised)."""
        p = self.positions.get(symbol)
        parsed = parse_occ(symbol)
        if not p or not parsed:
            return []
        und, exp, kind, strike = parsed
        spot = self.price(und)
        itm = 0.0 if spot is None else (spot - strike if kind == "call" else strike - spot)
        qty = p.qty
        out = []
        del self.positions[symbol]
        if itm >= 0.01:
            shares = abs(qty) * 100
            long_holder = qty > 0
            # long call / short put -> receive shares; long put / short call -> deliver shares
            gets_shares = (kind == "call") == long_holder
            out.append(self._nta("OPEXC" if long_holder else "OPASN", symbol=symbol, qty=abs(qty), net_amount=0.0))
            self._apply_to_position(und, shares if gets_shares else -shares, strike, "us_equity")
            out.append(self._nta("OPTRD", symbol=und, qty=shares, price=strike, side="buy" if gets_shares else "sell",
                                 net_amount=round(-shares * strike if gets_shares else shares * strike, 2)))
        else:
            out.append(self._nta("OPEXP", symbol=symbol, qty=abs(qty), net_amount=0.0))
        return out

    def _expiry_day_liquidation(self, now: datetime) -> None:
        t = self.behaviour.expiry_day_liquidation_time
        for sym, p in list(self.positions.items()):
            parsed = parse_occ(sym)
            if not parsed:
                continue
            exp = parsed[1]
            when = datetime.combine(exp, t, ET).astimezone(timezone.utc)
            if now >= when and self.calendar.is_open(when):
                q = self.quotes.get(sym)
                px = (q.bid if p.qty > 0 else q.ask) if q else 0.0
                o = self._new_order(symbol=sym, side="sell" if p.qty > 0 else "buy", type="market", tif="day",
                                    qty=abs(p.qty), asset_class="us_option", client_order_id=f"liq-{uuid.uuid4()}")
                o.created_at = when
                self._execute(o, abs(p.qty), px, when)

    # ----------------------------------------------------------- HTTP routing
    def handle(self, request: httpx.Request) -> httpx.Response:
        self._process(self.now())
        if self.behaviour.require_auth and not request.headers.get("APCA-API-KEY-ID"):
            return _err(401, 40110000, "request is not authorized")
        host = request.url.host
        path = request.url.path
        params = dict(request.url.params)
        body = request_json(request) or {}
        m = request.method
        if host in DATA_HOSTS:
            return self._data(m, path, params)
        return self._trading(m, path, params, body)

    def _trading(self, m: str, path: str, params: dict, body: Any) -> httpx.Response:
        if path == "/v2/account" and m == "GET":
            return json_response(self.account_json())
        if path == "/v2/account/configurations":
            if m == "PATCH":
                self.configurations.update(body)
                if "no_shorting" in body:
                    self.shorting_enabled = not body["no_shorting"]
            return json_response(self.configurations)
        if path in ("/v2/clock", "/v3/clock") and m == "GET":
            return json_response(self._clock_json())
        if path == "/v2/calendar" and m == "GET":
            return json_response(self._calendar_json(params))
        if path == "/v2/assets" and m == "GET":
            return json_response([a.to_json() for a in self.assets.values()])
        if path.startswith("/v2/assets/") and m == "GET":
            a = self.assets.get(path.rsplit("/", 1)[1])
            return json_response(a.to_json()) if a else _err(404, 40410000, "asset not found")
        if path == "/v2/orders":
            if m == "POST":
                return self._submit(body)
            if m == "GET":
                return self._list_orders(params)
            if m == "DELETE":
                out = []
                for o in self.open_orders():
                    if o.parent_id:
                        continue
                    self._cancel(o)
                    out.append({"id": o.id, "status": 200})
                return json_response(out, 207)
        if path == "/v2/orders:by_client_order_id" and m == "GET":
            cid = params.get("client_order_id")
            for oid in reversed(self.order_seq):
                if self.orders[oid].client_order_id == cid:
                    return json_response(self.order_json(self.orders[oid]))
            return _err(404, 40410000, "order not found")
        if path.startswith("/v2/orders/"):
            oid = path.split("/")[3]
            o = self.orders.get(oid)
            if o is None:
                return _err(404, 40410000, "order not found")
            if m == "GET":
                return json_response(self.order_json(o))
            if m == "DELETE":
                if not o.is_open:
                    return _err(422, 42210000, f"order is not cancelable (status: {o.status})")
                self._cancel(o)
                return httpx.Response(204)
            if m == "PATCH":
                return self._replace(o, body)
        if path == "/v2/positions":
            if m == "GET":
                return json_response([self.position_json(p) for p in self.positions.values() if p.qty != 0])
            if m == "DELETE":
                out = []
                if params.get("cancel_orders") == "true":
                    for o in self.open_orders():
                        self._cancel(o)
                for sym in list(self.positions):
                    r = self._close_position(sym, {})
                    out.append({"symbol": sym, "status": r.status_code, "body": r.json() if r.content else None})
                return json_response(out, 207)
        if path.startswith("/v2/positions/"):
            parts = path.split("/")
            sym = parts[3]
            if len(parts) == 5 and parts[4] == "exercise" and m == "POST":
                return self._exercise(sym)
            p = self.positions.get(sym)
            if m == "GET":
                return json_response(self.position_json(p)) if p and p.qty else _err(404, 40410000, "position does not exist")
            if m == "DELETE":
                return self._close_position(sym, params)
        if path.startswith("/v2/account/activities") and m == "GET":
            parts = path.split("/")
            types = [parts[4]] if len(parts) > 4 and parts[4] else (params.get("activity_types", "").split(",") if params.get("activity_types") else [])
            return json_response(self._activities(types, params))
        if path == "/v2/account/portfolio/history" and m == "GET":
            return json_response(self._portfolio_history(params))
        if path == "/v2/options/contracts" and m == "GET":
            return json_response(self._list_contracts(params))
        if path.startswith("/v2/options/contracts/") and m == "GET":
            c = self.contracts.get(path.rsplit("/", 1)[1])
            return json_response(c.to_json()) if c else _err(404, 40410000, "contract not found")
        return _err(404, 40410000, f"fake alpaca: no route for {m} {path}")

    # ------------------------------------------------------------ endpoints
    def _clock_json(self) -> dict:
        now = self.now()
        return {
            "timestamp": now.astimezone(ET).isoformat(),
            "is_open": self.calendar.is_open(now),
            "next_open": self.calendar.next_open(now).astimezone(ET).isoformat(),
            "next_close": self.calendar.next_close(now).astimezone(ET).isoformat(),
        }

    def _calendar_json(self, params: dict) -> list[dict]:
        today = self.now().astimezone(ET).date()
        start = date.fromisoformat(params.get("start", today.isoformat())[:10])
        end = date.fromisoformat(params.get("end", (today + timedelta(days=30)).isoformat())[:10])
        out = []
        for s in self.calendar.sessions(start, end):
            out.append({
                "date": s.day.isoformat(), "open": s.open.astimezone(ET).strftime("%H:%M"),
                "close": s.close.astimezone(ET).strftime("%H:%M"),
                "session_open": "0400", "session_close": "2000",
                "settlement_date": self.calendar.settlement_date(s.day).isoformat(),
            })
        return out

    def _list_orders(self, params: dict) -> httpx.Response:
        status = params.get("status", "open")
        limit = min(int(params.get("limit", 50)), 500)
        nested = params.get("nested") in ("true", "True", "1")
        symbols = set(params["symbols"].split(",")) if params.get("symbols") else None
        side = params.get("side")
        out = []
        for oid in self.order_seq:
            o = self.orders[oid]
            if nested and o.parent_id:
                continue
            if status == "open" and not o.is_open:
                continue
            if status == "closed" and o.is_open:
                continue
            if symbols and o.symbol not in symbols:
                continue
            if side and o.side != side:
                continue
            if params.get("after") and _iso(o.created_at) <= params["after"]:
                continue
            if params.get("until") and _iso(o.created_at) >= params["until"]:
                continue
            out.append(self.order_json(o, nested=nested))
        if params.get("direction", "desc") == "desc":
            out.reverse()
        return json_response(out[:limit])

    def _new_order(self, *, symbol, side, type, tif, qty, asset_class, client_order_id, notional=None,
                   limit_price=None, stop_price=None, order_class="simple", extended_hours=False) -> Order:
        o = Order(
            id=str(uuid.uuid4()), client_order_id=client_order_id, symbol=symbol, asset_class=asset_class,
            side=side, type=type, time_in_force=tif, qty=qty, notional=notional, limit_price=limit_price,
            stop_price=stop_price, order_class=order_class, extended_hours=extended_hours, created_at=self.now(),
        )
        self.orders[o.id] = o
        self.order_seq.append(o.id)
        return o

    def _schedule(self, o: Order) -> None:
        """Set status / eligible_at / expires_at for a just-accepted order."""
        b = self.behaviour
        now = self.now()
        cal = self.calendar
        today = now.astimezone(ET).date()
        if cal.is_open(now):
            sess = cal.session(today)
            in_window = (now - sess.open).total_seconds() < b.opening_window_seconds
            o.status = "new"
            o.eligible_at = now + timedelta(seconds=b.opening_fill_delay_seconds if in_window else b.fill_delay_seconds)
            if o.time_in_force == "day":
                o.expires_at = sess.close
        else:
            o.status = "accepted"
            nxt = cal.next_open(now)
            o.eligible_at = nxt + timedelta(seconds=b.opening_fill_delay_seconds)
            if o.time_in_force == "day":
                holiday = today.weekday() < 5 and not cal.is_trading_day(today) and cal.covers(today)
                if holiday and b.holiday_day_order_policy == "cancel_at_close":
                    o.expires_at = datetime.combine(today, time(16), ET).astimezone(timezone.utc)
                else:
                    o.expires_at = cal.next_close(nxt)
        if o.type != "market":
            # resting orders are evaluated as soon as they are eligible; the
            # opening delay models market-order queueing only
            o.eligible_at = now if cal.is_open(now) else cal.next_open(now)
        script = self.fill_scripts.get(o.symbol)
        if script:
            o.fill_plan = script.pop(0)

    def _validate_simple(self, body: dict, *, exclude: str | None = None) -> tuple[httpx.Response | None, dict]:
        b = self.behaviour
        sym = body.get("symbol")
        side = body.get("side")
        typ = body.get("type") or body.get("order_type") or "market"
        tif = body.get("time_in_force", "day")
        info: dict[str, Any] = {}
        if self.trading_blocked or self.configurations.get("suspend_trade"):
            return _err(403, 40310000, "trading is suspended for this account"), info
        if side not in ("buy", "sell"):
            return _err(422, 40010001, "invalid side"), info
        if typ not in ("market", "limit", "stop", "stop_limit", "trailing_stop"):
            return _err(422, 40010001, f"invalid order type {typ!r}"), info
        if tif not in ("day", "gtc", "opg", "cls", "ioc", "fok"):
            return _err(422, 40010001, "invalid time_in_force"), info
        qty = body.get("qty")
        notional = body.get("notional")
        if (qty is None) == (notional is None):
            return _err(422, 40010001, "qty or notional is required (exactly one)"), info
        qty_f = float(qty) if qty is not None else None
        notional = float(notional) if notional is not None else None
        if qty_f is not None and qty_f <= 0:
            return _err(422, 40010001, "qty must be > 0"), info
        if typ in ("limit", "stop_limit") and body.get("limit_price") is None:
            return _err(422, 40010001, "limit_price is required"), info
        if typ in ("stop", "stop_limit") and body.get("stop_price") is None:
            return _err(422, 40010001, "stop_price is required"), info
        opt = parse_occ(sym or "")
        info["asset_class"] = "us_option" if opt else "us_equity"
        if opt:
            if sym not in self.contracts:
                return _err(422, 40010001, f"asset {sym} not found"), info
            if qty_f is None or not float(qty_f).is_integer():
                return _err(422, 40010001, "qty must be a whole number for options"), info
            if tif not in ("day", "gtc"):
                return _err(422, 40010001, "options orders must be day or gtc"), info
            if body.get("extended_hours"):
                return _err(422, 40010001, "extended hours not supported for options"), info
            if typ == "market" and not self.calendar.is_open(self.now()):
                return _err(422, 42210000, "options market orders are only allowed during market hours"), info
            if self.options_level < 1:
                return _err(403, 40310000, "account not eligible to trade options"), info
            p = self.positions.get(sym)
            held_short = p is not None and p.qty < 0
            if side == "sell" and (p is None or p.qty <= 0) and self.options_level < 3 and not held_short:
                return _err(403, 40310000, "account not eligible to trade uncovered option contracts"), info
            return None, info
        asset = self.assets.get(sym)
        if asset is None:
            return _err(422, 40010001, f"asset \"{sym}\" not found"), info
        if not asset.tradable:
            return _err(422, 40010001, f"asset {sym} is not tradable"), info
        fractional = (qty_f is not None and not float(qty_f).is_integer()) or notional is not None
        if fractional:
            if not asset.fractionable:
                return _err(422, 40010001, f"asset {sym} is not fractionable"), info
            if tif != "day":
                return _err(422, 40010001, "fractional orders must be DAY orders"), info
        if body.get("extended_hours") and not (typ == "limit" and tif in ("day", "gtc")):
            return _err(422, 40010001, "extended hours order must be DAY or GTC limit orders"), info
        q = self.quotes.get(sym)
        ref = float(body.get("limit_price") or 0) or (q.ask if q and side == "buy" else (q.last if q else 0.0))
        if side == "sell":
            pos = self.positions.get(sym)
            long_qty = pos.qty if pos and pos.qty > 0 else 0.0
            held = self._held_for_orders(sym, exclude=exclude)
            available = max(long_qty - held, 0.0)
            want = qty_f if qty_f is not None else (notional / ref if ref else 0.0)
            if long_qty > 0:
                if want > available + 1e-9 and (held > 0 or not b.allow_long_to_short_flip):
                    return _err(
                        403, 40310000,
                        f"insufficient qty available for order (requested: {_f(want)}, available: {_f(available)})",
                        available=_f(available), existing_qty=_f(long_qty), held_for_orders=_f(held), symbol=sym,
                        related_orders=[o.id for o in self.open_orders(sym) if o.side == "sell" and o.id != exclude],
                    ), info
                if want > available + 1e-9:
                    info["position_intent"] = "sell_to_open_short"
                else:
                    info["position_intent"] = "sell_to_close"
            else:
                if not self.shorting_enabled or self.configurations.get("no_shorting"):
                    return _err(403, 40310000, b.shorting_disabled_message), info
                if not asset.shortable:
                    return _err(422, 40010001, f"asset {sym} cannot be sold short"), info
                if fractional:
                    return _err(422, 40010001, "fractional orders cannot be sold short"), info
                cost = want * (q.bid if q else ref) * b.short_sale_bp_factor
                if cost > self._buying_power() + 1e-6:
                    return _err(403, 40310000, "insufficient buying power", buying_power=_f(round(self._buying_power(), 2)),
                                cost_basis=_f(round(cost, 2))), info
                info["position_intent"] = "sell_to_open_short"
        else:
            pos = self.positions.get(sym)
            info["position_intent"] = "buy_to_cover" if pos and pos.qty < 0 else "buy_to_open"
            cost = notional if notional is not None else qty_f * ref
            if cost > self._buying_power() + 1e-6:
                return _err(403, 40310000, "insufficient buying power", buying_power=_f(round(self._buying_power(), 2)),
                            cost_basis=_f(round(cost, 2))), info
        return None, info

    def _wash_trade_conflict(self, sym: str, side: str, typ: str, limit: float | None, order_class: str) -> Order | None:
        if not self.behaviour.wash_trade_check or order_class in ("bracket", "oco", "oto") or typ == "trailing_stop":
            return None
        for o in self.open_orders(sym):
            if o.side == side or o.order_class in ("bracket", "oco", "oto") or o.parent_id or o.type == "trailing_stop":
                continue
            if o.type in ("market", "stop"):
                return o
            if typ in ("market", "stop"):
                return o
            if o.type in ("limit", "stop_limit") and limit is not None and o.limit_price is not None:
                buy_px, sell_px = (limit, o.limit_price) if side == "buy" else (o.limit_price, limit)
                if buy_px >= sell_px:
                    return o
        return None

    def _submit(self, body: dict) -> httpx.Response:
        if not isinstance(body, dict):
            return _err(422, 40010001, "request body format is invalid")
        cid = body.get("client_order_id") or str(uuid.uuid4())
        if len(cid) > 128:
            return _err(422, 40010001, "client_order_id must be no more than 128 characters")
        scope = self.behaviour.client_order_id_unique_scope
        for o in self.orders.values():
            if o.client_order_id == cid and (scope == "all" or o.is_open):
                return _err(422, 40010001, "client_order_id must be unique")
        order_class = body.get("order_class") or "simple"
        if order_class == "mleg":
            return self._submit_mleg(body, cid)
        err, info = self._validate_simple(body)
        if err is not None:
            return err
        sym = body["symbol"]
        side = body["side"]
        typ = body.get("type") or body.get("order_type") or "market"
        limit = float(body["limit_price"]) if body.get("limit_price") is not None else None
        stop = float(body["stop_price"]) if body.get("stop_price") is not None else None
        if order_class == "oco":
            stop = float((body.get("stop_loss") or {}).get("stop_price", 0)) or stop
        conflict = self._wash_trade_conflict(sym, side, typ, limit, order_class)
        if conflict is not None:
            reason = "opposite side market/stop order exists" if conflict.type in ("market", "stop") else "opposite side limit order exists"
            return _err(403, 40310000, "potential wash trade detected. use complex orders",
                        reject_reason=reason, existing_order_id=conflict.id)
        o = self._new_order(
            symbol=sym, side=side, type=typ, tif=body.get("time_in_force", "day"),
            qty=float(body["qty"]) if body.get("qty") is not None else None,
            notional=float(body["notional"]) if body.get("notional") is not None else None,
            asset_class=info["asset_class"], client_order_id=cid, limit_price=limit, stop_price=stop,
            order_class=order_class, extended_hours=bool(body.get("extended_hours")),
        )
        o.position_intent = body.get("position_intent") or info.get("position_intent")
        if typ == "trailing_stop":
            o.trail_percent = float(body["trail_percent"]) if body.get("trail_percent") else None
            o.trail_price = float(body["trail_price"]) if body.get("trail_price") else None
        self._schedule(o)
        if order_class in ("bracket", "oto", "oco"):
            self._attach_legs(o, body)
        self._process(self.now())
        return json_response(self.order_json(o))

    def _attach_legs(self, parent: Order, body: dict) -> None:
        exit_side = "sell" if parent.side == "buy" else "buy"
        group = f"grp-{parent.id}"
        legs = []
        tp = body.get("take_profit")
        sl = body.get("stop_loss")
        if parent.order_class == "oco":
            # parent is the take-profit limit; one stop leg
            parent.type = "limit"
            parent.group = group
            parent.stop_price = None
            if sl:
                legs.append(("stop_limit" if sl.get("limit_price") else "stop", None, sl))
        else:
            if tp:
                legs.append(("limit", float(tp["limit_price"]), None))
            if sl:
                legs.append(("stop_limit" if sl.get("limit_price") else "stop", None, sl))
        for typ, lim, slb in legs:
            child = self._new_order(
                symbol=parent.symbol, side=exit_side if parent.order_class != "oco" else parent.side, type=typ,
                tif=parent.time_in_force, qty=parent.qty, asset_class=parent.asset_class,
                client_order_id=str(uuid.uuid4()),
                limit_price=lim if lim is not None else (float(slb["limit_price"]) if slb and slb.get("limit_price") else None),
                stop_price=float(slb["stop_price"]) if slb else None, order_class=parent.order_class,
            )
            child.parent_id = parent.id
            child.group = group
            child.status = "new" if parent.order_class == "oco" else "held"
            child.eligible_at = parent.eligible_at if parent.order_class == "oco" else None
            child.expires_at = parent.expires_at
            parent.leg_ids.append(child.id)

    def _submit_mleg(self, body: dict, cid: str) -> httpx.Response:
        legs = body.get("legs") or []
        if not 2 <= len(legs) <= 4:
            return _err(422, 40010001, "mleg orders require 2 to 4 legs")
        if self.options_level < 3:
            return _err(403, 40310000, "account not eligible for multi-leg options orders")
        ratios = [int(leg.get("ratio_qty", 1)) for leg in legs]
        if math.gcd(*ratios) != 1:
            return _err(422, 40010001, "leg ratio_qty must be in simplest form")
        typ = body.get("type", "market")
        if typ == "market" and not self.calendar.is_open(self.now()):
            return _err(422, 42210000, "options market orders are only allowed during market hours")
        for leg in legs:
            if leg.get("symbol") not in self.contracts:
                return _err(422, 40010001, f"asset {leg.get('symbol')} not found")
        qty = float(body.get("qty", 1))
        limit = float(body["limit_price"]) if body.get("limit_price") is not None else None
        if limit is not None and limit > 0 and limit * qty * 100 > self._options_buying_power():
            return _err(403, 40310000, "insufficient options buying power")
        parent = self._new_order(symbol="", side="buy" if (limit or 0) >= 0 else "sell", type=typ,
                                 tif=body.get("time_in_force", "day"), qty=qty, asset_class="us_option",
                                 client_order_id=cid, limit_price=limit, order_class="mleg")
        for leg in legs:
            child = self._new_order(symbol=leg["symbol"], side=leg["side"], type=typ, tif=parent.time_in_force,
                                    qty=qty * int(leg.get("ratio_qty", 1)), asset_class="us_option",
                                    client_order_id=str(uuid.uuid4()), order_class="mleg")
            child.ratio_qty = float(leg.get("ratio_qty", 1))
            child.position_intent = leg.get("position_intent")
            child.parent_id = parent.id
            parent.leg_ids.append(child.id)
        self._schedule(parent)
        for lid in parent.leg_ids:
            self.orders[lid].status = parent.status
        self._process(self.now())
        return json_response(self.order_json(parent))

    def _cancel(self, o: Order) -> None:
        lat = self.behaviour.cancel_latency_seconds
        if lat > 0:
            o.status = "pending_cancel"
            o.cancel_at = self.now() + timedelta(seconds=lat)
        else:
            self._finish(o, "canceled", self.now())

    def _replace(self, o: Order, body: dict) -> httpx.Response:
        if not o.is_open or o.status == "pending_cancel":
            return _err(422, 42210000, f"order is not replaceable (status: {o.status})")
        if o.order_class in ("oto", "mleg") and not o.parent_id:
            return _err(422, 40010001, "replace is not supported for this order class")
        new_body = {
            "symbol": o.symbol, "side": o.side, "type": o.type, "time_in_force": body.get("time_in_force", o.time_in_force),
            "qty": body.get("qty", _f(o.remaining if o.qty else None)),
            "limit_price": body.get("limit_price", _f(o.limit_price)),
            "stop_price": body.get("stop_price", _f(o.stop_price)),
        }
        new_body = {k: v for k, v in new_body.items() if v is not None}
        err, info = self._validate_simple(new_body, exclude=o.id)
        if err is not None:
            return err
        n = self._new_order(
            symbol=o.symbol, side=o.side, type=o.type, tif=new_body["time_in_force"], qty=float(new_body["qty"]),
            asset_class=o.asset_class, client_order_id=body.get("client_order_id") or str(uuid.uuid4()),
            limit_price=float(new_body["limit_price"]) if "limit_price" in new_body else None,
            stop_price=float(new_body["stop_price"]) if "stop_price" in new_body else None,
            order_class=o.order_class,
        )
        n.replaces = o.id
        n.parent_id = o.parent_id
        n.group = o.group
        n.position_intent = o.position_intent
        o.status = "replaced"
        o.replaced_by = n.id
        o.replaced_at = self.now()
        if o.parent_id:
            par = self.orders[o.parent_id]
            par.leg_ids = [n.id if i == o.id else i for i in par.leg_ids]
        self._schedule(n)
        self._process(self.now())
        return json_response(self.order_json(n))

    def _close_position(self, sym: str, params: dict) -> httpx.Response:
        p = self.positions.get(sym)
        if not p or p.qty == 0:
            return _err(404, 40410000, "position does not exist")
        qty = abs(p.qty)
        if params.get("percentage"):
            qty = round(qty * float(params["percentage"]) / 100, 9)
        elif params.get("qty"):
            qty = float(params["qty"])
        body = {"symbol": sym, "side": "sell" if p.qty > 0 else "buy", "type": "market", "time_in_force": "day",
                "qty": _f(qty)}
        return self._submit(body)

    def _exercise(self, sym: str) -> httpx.Response:
        p = self.positions.get(sym)
        parsed = parse_occ(sym)
        if not p or p.qty <= 0 or not parsed:
            return _err(422, 40010001, "position not found or not a long option position")
        now_et = self.now().astimezone(ET)
        s = self.calendar.session(now_et.date())
        if s and self.now() >= s.close:
            return _err(403, 40310000, "exercise requests are not accepted between market close and midnight")
        und, exp, kind, strike = parsed
        shares = p.qty * 100
        del self.positions[sym]
        self._nta("OPEXC", symbol=sym, qty=p.qty, net_amount=0.0)
        self._apply_to_position(und, shares if kind == "call" else -shares, strike, "us_equity")
        self._nta("OPTRD", symbol=und, qty=shares, price=strike, side="buy" if kind == "call" else "sell",
                  net_amount=round(-shares * strike if kind == "call" else shares * strike, 2))
        return json_response({}, 200)

    def _activities(self, types: list[str], params: dict) -> list[dict]:
        acts = [a for a in self.activities if not types or a["activity_type"] in types]
        if params.get("date"):
            acts = [a for a in acts if a["_at"].astimezone(ET).date().isoformat() == params["date"][:10]]
        if params.get("after"):
            acts = [a for a in acts if _iso(a["_at"]) > params["after"]]
        if params.get("until"):
            acts = [a for a in acts if _iso(a["_at"]) < params["until"]]
        acts.sort(key=lambda a: (a["_at"], a["_seq"]), reverse=params.get("direction", "desc") == "desc")
        if params.get("page_token"):
            ids = [a["id"] for a in acts]
            if params["page_token"] in ids:
                acts = acts[ids.index(params["page_token"]) + 1:]
        size = min(int(params.get("page_size", 100)), 100)
        return [{k: v for k, v in a.items() if not k.startswith("_")} for a in acts[:size]]

    def _portfolio_history(self, params: dict) -> dict:
        pts = list(self.equity_history) + [(self.now(), self.equity())]
        base = pts[0][1] if pts else self.equity()
        return {
            "timestamp": [int(t.timestamp()) for t, _ in pts],
            "equity": [round(e, 2) for _, e in pts],
            "profit_loss": [round(e - base, 2) for _, e in pts],
            "profit_loss_pct": [round((e - base) / base, 6) if base else 0 for _, e in pts],
            "base_value": round(base, 2), "timeframe": params.get("timeframe", "1D"),
        }

    def _list_contracts(self, params: dict) -> dict:
        unds = set(params["underlying_symbols"].split(",")) if params.get("underlying_symbols") else None
        out = []
        for c in self.contracts.values():
            if unds and c.underlying not in unds:
                continue
            if params.get("type") and c.type != params["type"]:
                continue
            if params.get("expiration_date") and c.expiration.isoformat() != params["expiration_date"]:
                continue
            if params.get("expiration_date_gte") and c.expiration.isoformat() < params["expiration_date_gte"]:
                continue
            if params.get("expiration_date_lte") and c.expiration.isoformat() > params["expiration_date_lte"]:
                continue
            if params.get("strike_price_gte") and c.strike < float(params["strike_price_gte"]):
                continue
            if params.get("strike_price_lte") and c.strike > float(params["strike_price_lte"]):
                continue
            out.append(c.to_json())
        limit = min(int(params.get("limit", 100)), 10000)
        start = int(params.get("page_token") or 0)
        page = out[start:start + limit]
        nxt = str(start + limit) if start + limit < len(out) else None
        return {"option_contracts": page, "next_page_token": nxt}

    # ------------------------------------------------------------ market data
    def _quote_json(self, sym: str) -> dict | None:
        q = self.quotes.get(sym)
        if not q:
            return None
        return {"t": _iso(q.at), "ax": "V", "ap": q.ask, "as": q.ask_size, "bx": "V", "bp": q.bid, "bs": q.bid_size,
                "c": ["R"], "z": "C"}

    def _trade_json(self, sym: str) -> dict | None:
        q = self.quotes.get(sym)
        if not q:
            return None
        return {"t": _iso(q.at), "x": "V", "p": q.last, "s": 100, "c": ["@"], "i": 1, "z": "C"}

    def _bars(self, sym: str, params: dict) -> list[dict]:
        hist = self.trades_history.get(sym, [])
        tf = params.get("timeframe", "1Day")
        buckets: dict[datetime, list[tuple[float, int]]] = {}
        for t, px, vol in hist:
            et = t.astimezone(ET)
            if tf.endswith("Day") or tf == "1D":
                key = datetime.combine(et.date(), time(0), ET)
            elif tf.endswith("Hour"):
                key = et.replace(minute=0, second=0, microsecond=0)
            else:
                key = et.replace(second=0, microsecond=0)
            buckets.setdefault(key, []).append((px, vol))
        out = []
        for k in sorted(buckets):
            pxs = [p for p, _ in buckets[k]]
            vols = sum(v for _, v in buckets[k])
            out.append({"t": _iso(k), "o": pxs[0], "h": max(pxs), "l": min(pxs), "c": pxs[-1], "v": vols,
                        "n": len(pxs), "vw": round(sum(pxs) / len(pxs), 4)})
        if params.get("start"):
            out = [b for b in out if b["t"] >= params["start"]]
        if params.get("end"):
            out = [b for b in out if b["t"] <= params["end"]]
        limit = int(params.get("limit", 1000))
        return out[-limit:]

    def _snapshot_json(self, sym: str) -> dict | None:
        if sym not in self.quotes:
            return None
        bars = self._bars(sym, {"timeframe": "1Day"})
        mbars = self._bars(sym, {"timeframe": "1Min"})
        return {
            "latestTrade": self._trade_json(sym), "latestQuote": self._quote_json(sym),
            "minuteBar": mbars[-1] if mbars else None, "dailyBar": bars[-1] if bars else None,
            "prevDailyBar": bars[-2] if len(bars) > 1 else None,
        }

    def _option_snapshot_json(self, sym: str) -> dict | None:
        if sym not in self.quotes:
            return None
        return {"latestQuote": self._quote_json(sym), "latestTrade": self._trade_json(sym),
                "greeks": None, "impliedVolatility": None}

    def _data(self, m: str, path: str, params: dict) -> httpx.Response:
        if m != "GET":
            return _err(405, 40510000, "method not allowed")
        syms = params.get("symbols", "").split(",") if params.get("symbols") else []
        parts = path.strip("/").split("/")
        # /v2/stocks/{sym}/quotes/latest | trades/latest | bars | snapshot
        if parts[:2] == ["v2", "stocks"]:
            if len(parts) >= 3 and parts[2] in ("quotes", "trades", "bars", "snapshots"):
                kind = parts[2]
                if kind == "snapshots":
                    return json_response({s: self._snapshot_json(s) for s in syms if s in self.quotes})
                if kind == "bars":
                    return json_response({"bars": {s: self._bars(s, params) for s in syms}, "next_page_token": None})
                key = "quotes" if kind == "quotes" else "trades"
                fn = self._quote_json if kind == "quotes" else self._trade_json
                return json_response({key: {s: fn(s) for s in syms if s in self.quotes}})
            sym = parts[2]
            rest = parts[3:]
            if rest == ["snapshot"]:
                snap = self._snapshot_json(sym)
                return json_response({"symbol": sym, **snap}) if snap else _err(404, 40410000, "not found")
            if rest == ["quotes", "latest"]:
                return json_response({"symbol": sym, "quote": self._quote_json(sym)})
            if rest == ["trades", "latest"]:
                return json_response({"symbol": sym, "trade": self._trade_json(sym)})
            if rest == ["bars"]:
                return json_response({"symbol": sym, "bars": self._bars(sym, params), "next_page_token": None})
        if parts[:2] == ["v1beta1", "news"]:
            out = self.news
            if syms:
                out = [n for n in out if set(n["symbols"]) & set(syms)]
            out = sorted(out, key=lambda n: n["created_at"], reverse=params.get("sort", "desc") == "desc")
            return json_response({"news": out[: int(params.get("limit", 10))], "next_page_token": None})
        if parts[:2] == ["v1beta1", "options"]:
            if parts[2:4] == ["quotes", "latest"]:
                return json_response({"quotes": {s: self._quote_json(s) for s in syms if s in self.quotes}})
            if parts[2:4] == ["trades", "latest"]:
                return json_response({"trades": {s: self._trade_json(s) for s in syms if s in self.quotes}})
            if parts[2] == "snapshots":
                if len(parts) == 4:
                    und = parts[3]
                    snaps = {s: self._option_snapshot_json(s) for s, c in self.contracts.items()
                             if c.underlying == und and s in self.quotes}
                else:
                    snaps = {s: self._option_snapshot_json(s) for s in syms if s in self.quotes}
                return json_response({"snapshots": snaps, "next_page_token": None})
        return _err(404, 40410000, f"fake alpaca data: no route for {path}")
