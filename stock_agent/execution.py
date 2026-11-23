"""Order execution: the only module that turns intents into broker orders.

Rules this module enforces (DECISIONS A1, A3, A6, A8, B2, B3):

* Long only. A sell is always ``sell_to_close`` for at most the whole free
  shares the broker reports right now (``qty_available`` after our own stop
  has been cancelled and the cancel confirmed). A short found at the broker
  is bought back (``cover_short``), never added to.
* One protective order per long: a broker GTC stop for the whole coverable
  quantity, placed after a confirmed fill. There is no software duplicate
  sell. Stops are renewed before Alpaca's 90-day GTC expiry and re-placed if
  the broker cancels them (corporate actions).
* Entries are marketable limit DAY orders, not before open+5 min and not in
  the last 10 min (``MarketCalendar.market_gate``). Fills are confirmed by
  polling; an unfilled remainder is cancelled and the cancel confirmed.
* Every order carries a client_order_id written to the ledger before the HTTP
  call; after any ambiguous failure the order is looked up by that id.
* Any failure is loud (alert) and leaves the intent in a state that blocks a
  duplicate submission.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable
from zoneinfo import ZoneInfo

from stock_agent.broker import AlpacaClient, BrokerError, BrokerOrder, BrokerPosition, OrderRequest, round_price
from stock_agent.config import Config
from stock_agent.ledger import IntentStatus, LedgerError, NewIntent, OrderIntent

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
_OCC_LEN = 15  # option symbols are longer than any equity ticker plus date/strike


@dataclass(frozen=True)
class ExecSettings:
    """Execution defaults. These are what run; nothing needs an env var."""

    buy_limit_offset_pct: float = 0.005     # marketable limit: ask * (1 + offset)
    sell_limit_offset_pct: float = 0.005    # marketable limit: bid * (1 - offset)
    max_price_drift_pct: float = 0.03       # skip a buy if price ran this far above the analysed price
    fill_wait_seconds: float = 120.0
    fill_poll_seconds: float = 2.0
    cancel_confirm_seconds: float = 30.0
    stop_renew_days: int = 7                # renew GTC stops this close to their expiry
    gtc_life_days: int = 90                 # Alpaca cancels GTC orders after 90 days
    max_model_exits_per_session: int = 4    # A8; hard stops / covers / normalise are uncapped
    uncapped_exit_rules: frozenset[str] = frozenset({"hard_stop", "cover_short", "normalise", "assignment",
                                                     "risk_limit"})


@dataclass
class ExecResult:
    intent_id: str | None
    symbol: str
    status: IntentStatus
    filled_qty: float = 0.0
    avg_price: float | None = None
    reason: str = ""
    order_id: str | None = None


@dataclass
class ProtectionReport:
    placed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    renewed: list[str] = field(default_factory=list)
    cancelled_extra: list[str] = field(default_factory=list)
    uncoverable: list[str] = field(default_factory=list)
    breached: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class NormaliseStatus:
    state: str          # planned | waiting | running | done | failed
    detail: str = ""
    steps: list[str] = field(default_factory=list)


@dataclass
class MonitorReport:
    normalise: NormaliseStatus | None = None
    covered: list[str] = field(default_factory=list)
    protection: ProtectionReport | None = None
    executed: list[ExecResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ExecutionError(Exception):
    pass


def _f(x: Any) -> float:
    return float(x) if x is not None else 0.0


def _is_option(symbol: str) -> bool:
    return len(symbol) >= _OCC_LEN and any(c.isdigit() for c in symbol)


def _hash8(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:8]


class ExecutionEngine:
    NORMALISE_KEY = "exec.normalise"

    def __init__(
        self,
        broker: AlpacaClient,
        ledger: Any,
        risk: Any,
        calendar: Any,
        prices: Any = None,
        *,
        alert: Callable[..., Any] | None = None,
        options: Any = None,
        bars: Callable[[str], Awaitable[Any]] | None = None,
        controls: Callable[[], Any] | None = None,
        market_gate: Callable[..., Awaitable[Any]] | None = None,
        config: Any = Config,
        settings: ExecSettings | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.broker = broker
        self.ledger = ledger
        self.risk = risk
        self.calendar = calendar
        self.prices = prices
        self.options = options
        self.bars = bars
        self.controls = controls
        self._market_gate = market_gate
        self.config = config
        self.s = settings or ExecSettings()
        self._alert_fn = alert
        self._now = now
        self._draining = False
        self._in_flight: set[str] = set()

    # ------------------------------------------------------------------ utils
    def now(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(timezone.utc)

    def session_date(self) -> date:
        return self.now().astimezone(ET).date()

    async def alert(self, severity: str, title: str, body: str, key: str | None = None) -> None:
        logger.log(logging.ERROR if severity == "critical" else logging.WARNING if severity == "warning"
                   else logging.INFO, "%s: %s", title, body)
        if self._alert_fn is None:
            return
        try:
            r = self._alert_fn(severity, title, body, key=key)
            if inspect.isawaitable(r):
                await r
        except Exception:  # an alert failure must not stop order handling
            logger.exception("alert delivery failed")

    def _kv_get(self, key: str, default: Any = None) -> Any:
        return self.ledger.store.kv_get(key, default)

    def _kv_set(self, key: str, value: Any) -> None:
        self.ledger.store.kv_set(key, value)

    def _risk_controls(self) -> Any:
        """RiskControls for entries. Raises ExecutionError when the shared
        state is unreadable: new risk is refused, exits are unaffected."""
        if self.controls is not None:
            try:
                return self.controls()
            except Exception as e:
                raise ExecutionError(f"risk controls unavailable: {e}") from e
        from stock_agent import risk_manager as rm

        key = getattr(rm, "DRAWDOWN_STATE_KEY", None)
        from_record = getattr(rm.DrawdownState, "from_record", None)
        if key is None or from_record is None:
            return rm.RiskControls()
        try:
            dd = from_record(self.ledger.risk_state_get(key))
        except ValueError as e:
            raise ExecutionError(f"drawdown state unreadable: {e}") from e
        return rm.RiskControls(drawdown=dd)

    async def _gate(self, purpose: str, rule: str | None = None):
        if self._market_gate is not None:
            return await self._market_gate(purpose, rule=rule)
        return await self.calendar.market_gate(purpose, rule=rule)

    async def _snapshot(self, reason: str):
        return await self.ledger.refresh(reason)

    async def _quote(self, symbol: str, purpose: str = "order_sizing"):
        if self.prices is None:
            raise ExecutionError("no price service")
        res = await self.prices.get_quotes([symbol], purpose=purpose)
        q = res.quotes.get(symbol)
        if q is None:
            raise ExecutionError(f"no fresh quote for {symbol}: {res.missing.get(symbol, 'missing')}")
        return q

    async def _stop_distance(self, symbol: str, price: float):
        from stock_agent.risk_manager import atr14, stop_fraction

        atr = None
        if self.bars is not None:
            try:
                bars = await self.bars(symbol)
                atr = atr14(bars) if bars else None
            except Exception as e:
                logger.warning("bars for %s unavailable (%s); using the minimum stop distance", symbol, e)
        return stop_fraction(Decimal(str(price)), atr)

    async def _stop_price_for(self, symbol: str, price: float) -> float:
        from stock_agent.risk_manager import stop_price

        dist = await self._stop_distance(symbol, price)
        return float(stop_price(Decimal(str(price)), dist))

    # --------------------------------------------------------------- orders
    async def _open_orders(self, symbol: str) -> list[BrokerOrder]:
        orders = await self.broker.list_orders("open", symbols=[symbol])
        return [o for o in orders if o.symbol == symbol]

    async def _cancel_confirm(self, order: BrokerOrder) -> BrokerOrder:
        """Cancel and wait until the broker reports a terminal state."""
        if order.is_terminal:
            return order
        try:
            await self.broker.cancel_order(order.id)
        except BrokerError as e:
            if e.kind != "not_found":
                raise
        final = await self.broker.wait_terminal(order.id, timeout_s=self.s.cancel_confirm_seconds,
                                                poll_s=self.s.fill_poll_seconds)
        if not final.is_terminal:
            raise BrokerError("conflict", f"cancel of {order.symbol} order not confirmed (status {final.status})",
                              uncertain=True)
        return final

    async def _place_and_wait(self, req: OrderRequest, *, wait_s: float | None = None) -> BrokerOrder:
        """Submit, poll for a fill, cancel-confirm any remainder. Returns the terminal order."""
        self._in_flight.add(req.client_order_id)
        try:
            order = await self.broker.submit_order(req)
            final = await self.broker.wait_terminal(order.id, timeout_s=wait_s or self.s.fill_wait_seconds,
                                                    poll_s=self.s.fill_poll_seconds)
            if not final.is_terminal:
                final = await self._cancel_confirm(final)
            return final
        finally:
            self._in_flight.discard(req.client_order_id)

    # --------------------------------------------------------------- resume
    async def resume(self) -> None:
        """Start-up: resolve intents whose outcome is unknown (crash mid-request)."""
        try:
            self.ledger.mark_submitting_ambiguous()
        except Exception as e:
            await self.alert("critical", "Order recovery failed", f"Could not read the intent queue: {e}")
            return
        for it in self.ledger.list_intents([IntentStatus.AMBIGUOUS, IntentStatus.SUBMITTED,
                                            IntentStatus.PARTIALLY_FILLED]):
            try:
                await self._resolve(it)
            except BrokerError as e:
                await self.alert("critical", f"{it.symbol}: order state unknown",
                                 f"Could not look up order {it.client_order_id} at the broker ({e}). "
                                 "No new order for this intent until it is resolved.",
                                 key=f"resume-{it.intent_id}")

    async def _resolve(self, it: OrderIntent) -> None:
        order = await self.broker.get_order_by_client_id(it.client_order_id)
        if order is None:
            if it.status == IntentStatus.AMBIGUOUS:
                # The broker confirms the order does not exist: safe to retry later.
                self.ledger.ack_intent(it.intent_id, IntentStatus.QUEUED, error="not found at broker after restart")
            else:
                await self.alert("critical", f"{it.symbol}: submitted order missing",
                                 f"Intent {it.intent_id} was submitted but the broker has no order "
                                 f"{it.client_order_id}.", key=f"missing-{it.intent_id}")
            return
        if it.status == IntentStatus.AMBIGUOUS:
            it = self.ledger.ack_intent(it.intent_id, IntentStatus.SUBMITTED, broker_order_id=order.id)
        if not order.is_terminal:
            order = await self.broker.wait_terminal(order.id, timeout_s=self.s.fill_wait_seconds,
                                                    poll_s=self.s.fill_poll_seconds)
            if not order.is_terminal:
                order = await self._cancel_confirm(order)
        await self._finish(it, order)

    async def _finish(self, it: OrderIntent, order: BrokerOrder, extra_filled: float = 0.0,
                      avg: float | None = None) -> ExecResult:
        filled = _f(order.filled_qty) + extra_filled
        price = avg if avg is not None else (_f(order.filled_avg_price) or None)
        if filled > 0:
            status = IntentStatus.FILLED
        elif order.status in ("rejected",):
            status = IntentStatus.REJECTED
        elif order.status == "expired":
            status = IntentStatus.EXPIRED
        else:
            status = IntentStatus.CANCELLED
        cur = self.ledger.get_intent(it.intent_id)
        if cur is not None and cur.status == IntentStatus.SUBMITTING:
            self.ledger.ack_intent(it.intent_id, IntentStatus.SUBMITTED, broker_order_id=order.id)
        self.ledger.ack_intent(it.intent_id, status, broker_order_id=order.id,
                               meta={"filled_qty": filled, "avg_price": price, "order_status": order.status})
        if it.side == "buy" and filled > 0 and it.kind != "cover":
            await self._protect_symbol(it.symbol, fill_price=price)
        return ExecResult(it.intent_id, it.symbol, status, filled, price, order.status, order.id)

    # --------------------------------------------------------------- dispatch
    async def execute_due(self) -> list[ExecResult]:
        out: list[ExecResult] = []
        if self._draining:
            return out
        due = self.ledger.due_intents(self.now())
        due.sort(key=lambda i: (0 if i.side == "sell" or i.kind == "cover" else 1, i.created_at))
        for it in due:
            if it.kind == "option_structure":
                continue  # the options module executes its own structures
            out.append(await self.execute_intent(it))
        return out

    async def execute_intent(self, intent: OrderIntent) -> ExecResult:
        if self._draining:
            return ExecResult(intent.intent_id, intent.symbol, intent.status, reason="draining")
        try:
            if intent.kind == "cover":
                return await self._execute_cover_intent(intent)
            if intent.side == "buy":
                return await self.buy(intent)
            if intent.side == "sell":
                return await self.exit(intent)
            return self._skip(intent, f"unknown side {intent.side}")
        except BrokerError as e:
            return await self._broker_failure(intent, e)
        except ExecutionError as e:
            return await self._defer_or_skip(intent, str(e))

    def _skip(self, intent: OrderIntent, reason: str) -> ExecResult:
        cur = self.ledger.get_intent(intent.intent_id)
        if cur is not None and cur.status == IntentStatus.QUEUED:
            self.ledger.ack_intent(intent.intent_id, IntentStatus.SKIPPED, error=reason)
        elif cur is not None and cur.status == IntentStatus.SUBMITTING:
            self.ledger.ack_intent(intent.intent_id, IntentStatus.REJECTED, error=reason)
        logger.info("intent %s %s %s skipped: %s", intent.intent_id, intent.side, intent.symbol, reason)
        return ExecResult(intent.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=reason)

    async def _defer_or_skip(self, intent: OrderIntent, reason: str) -> ExecResult:
        cur = self.ledger.get_intent(intent.intent_id)
        if cur is not None and cur.status == IntentStatus.SUBMITTING:
            # nothing was sent: back to the queue for one more try in 5 minutes
            self.ledger.ack_intent(intent.intent_id, IntentStatus.QUEUED, error=reason,
                                   next_attempt_at=self.now() + timedelta(minutes=5))
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.QUEUED, reason=reason)
        return self._skip(intent, reason)

    async def _broker_failure(self, intent: OrderIntent, e: BrokerError) -> ExecResult:
        cur = self.ledger.get_intent(intent.intent_id)
        if e.uncertain:
            if cur is not None and cur.status == IntentStatus.SUBMITTING:
                self.ledger.ack_intent(intent.intent_id, IntentStatus.AMBIGUOUS, error=str(e))
            await self.alert("critical", f"{intent.symbol}: order outcome unknown",
                             f"{intent.side} {intent.symbol}: {e}. Not resubmitting until the broker "
                             f"confirms order {intent.client_order_id}.", key=f"ambiguous-{intent.intent_id}")
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.AMBIGUOUS, reason=str(e))
        if cur is not None and cur.status == IntentStatus.SUBMITTING:
            if e.retryable:
                self.ledger.ack_intent(intent.intent_id, IntentStatus.QUEUED, error=str(e),
                                       next_attempt_at=self.now() + timedelta(minutes=5))
            else:
                self.ledger.ack_intent(intent.intent_id, IntentStatus.REJECTED, error=str(e))
        await self.alert("warning" if intent.side == "buy" else "critical",
                         f"{intent.symbol}: {intent.side} failed", f"Broker refused or failed: {e}",
                         key=f"fail-{intent.intent_id}")
        return ExecResult(intent.intent_id, intent.symbol, IntentStatus.REJECTED, reason=str(e))

    # -------------------------------------------------------------------- buy
    @property
    def normalised(self) -> bool:
        return (self._kv_get(self.NORMALISE_KEY, {}) or {}).get("state") == "done"

    async def buy(self, intent: OrderIntent) -> ExecResult:
        if not self.normalised:
            return await self._hold(intent, "account clean-up has not finished; entries are held")
        block = self.ledger.trading_block()
        if block:
            return await self._hold(intent, f"trading blocked: {block}")
        gate = await self._gate("entry")
        if not gate.allowed:
            return await self._hold(intent, f"market gate: {gate.reason}", gate.next_open)
        q = await self._quote(intent.symbol)
        ask = _f(q.ask) or _f(q.price)
        ref = _f(intent.reference_price)
        if ref > 0 and ask > ref * (1 + self.s.max_price_drift_pct):
            return self._skip(intent, f"price {ask:.2f} ran more than {self.s.max_price_drift_pct:.0%} above "
                                      f"the analysed {ref:.2f}")
        limit = round_price(ask * (1 + self.s.buy_limit_offset_pct))
        snap = await self._snapshot("pre-buy")
        if intent.symbol in getattr(snap, "blocked_symbols", frozenset()):
            return self._skip(intent, "symbol blocked by reconcile")
        h = snap.holding(intent.symbol)
        if h is not None and h.qty < 0:
            return self._skip(intent, "account is short this symbol; cover first")
        dist = await self._stop_distance(intent.symbol, ask)
        ti = self._trade_intent(intent, snap, limit, q, dist, add=h is not None and h.qty > 0)
        controls = self._risk_controls()
        qty = int(self.risk.propose_equity_qty(snap, ti, now=self.now(), controls=controls))
        if qty < 1:
            return self._skip(intent, "risk sizing gave less than one share")
        from dataclasses import replace

        ti = replace(ti, qty=Decimal(qty))
        verdict = self.risk.evaluate_trade(snap, ti, now=self.now(), controls=controls)
        if not verdict.ok:
            return self._skip(intent, "risk: " + "; ".join(verdict.reasons))
        try:
            claimed = self.ledger.claim_intent(intent.intent_id)
        except LedgerError as e:
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=str(e))
        req = OrderRequest(symbol=intent.symbol, side="buy", qty=qty, type="limit", time_in_force="day",
                           limit_price=limit, client_order_id=claimed.client_order_id,
                           position_intent="buy_to_open")
        order = await self._place_and_wait(req)
        res = await self._finish(claimed, order)
        if res.filled_qty > 0:
            await self.alert("info", f"Bought {intent.symbol}",
                             f"{res.filled_qty:g} shares at {res.avg_price or 0:.2f}", key=None)
        return res

    async def _hold(self, intent: OrderIntent, reason: str, until: datetime | None = None) -> ExecResult:
        nxt = until or (self.now() + timedelta(minutes=5))
        if intent.expires_at is not None and nxt >= intent.expires_at:
            return self._skip(intent, reason + " (intent would expire)")
        self.ledger.ack_intent(intent.intent_id, IntentStatus.QUEUED, error=reason, next_attempt_at=nxt)
        return ExecResult(intent.intent_id, intent.symbol, IntentStatus.QUEUED, reason=reason)

    def _trade_intent(self, intent: OrderIntent, snap: Any, limit: float, q: Any, dist: Any, *, add: bool):
        from stock_agent.risk_manager import TradeIntent, TradeKind

        m = dict(intent.meta or {})
        kw: dict[str, Any] = dict(
            kind=TradeKind.ADD if add else TradeKind.BUY,
            symbol=intent.symbol,
            qty=Decimal(0),
            price=Decimal(str(limit)),
            sector=m.get("sector"),
            raw_conviction=m.get("raw_conviction"),
            effective_conviction=m.get("effective_conviction", m.get("conviction")),
            last_filled_conviction=m.get("last_filled_conviction"),
            stop_distance=dist.fraction,
            analysis_price=intent.reference_price,
            signal_at=intent.created_at,
            quote_at=getattr(q, "as_of", None),
        )
        if m.get("minimum_conviction") is not None:
            kw["minimum_conviction"] = int(m["minimum_conviction"])
        try:
            return TradeIntent(**kw, mark_price=Decimal(str(_f(q.price))))
        except TypeError:
            return TradeIntent(**kw)

    # ------------------------------------------------------------------- exit
    def _model_exit_key(self) -> str:
        return f"exec.model_exits.{self.session_date().isoformat()}"

    async def exit(self, intent: OrderIntent) -> ExecResult:
        """Sell (part of) a long. Cancel our stop, confirm, sell, re-protect any rest."""
        rule = str((intent.meta or {}).get("rule") or "model")
        gate = await self._gate("exit", rule=rule)
        if not gate.allowed:
            return await self._hold(intent, f"market gate: {gate.reason}", gate.next_open)
        if rule not in self.s.uncapped_exit_rules:
            n = int(self._kv_get(self._model_exit_key(), 0) or 0)
            if n >= self.s.max_model_exits_per_session:
                return await self._hold(intent, f"model exit cap ({self.s.max_model_exits_per_session}) reached "
                                                "this session")
        pos = await self.broker.get_position(intent.symbol)
        if pos is None or not pos.is_long:
            return self._skip(intent, "no long position at the broker")
        meta = self.ledger.open_position_meta(intent.symbol)
        if intent.position_id and meta is not None and meta.position_id != intent.position_id:
            return self._skip(intent, "position changed since the decision (position_id mismatch)")
        want = (intent.meta or {}).get("qty")
        want_qty = float(want) if want is not None else pos.qty
        want_qty = min(want_qty, pos.qty)
        if want_qty <= 0:
            return self._skip(intent, "nothing to sell")
        await self._release_short_calls(intent.symbol, pos, want_qty)
        try:
            claimed = self.ledger.claim_intent(intent.intent_id, check_thesis=False)
        except LedgerError as e:
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=str(e))
        if rule not in self.s.uncapped_exit_rules:
            self._kv_set(self._model_exit_key(), int(self._kv_get(self._model_exit_key(), 0) or 0) + 1)
        filled, avg, last = await self._sell_qty(intent.symbol, want_qty, claimed.client_order_id, reason=rule)
        if last is None:
            self.ledger.ack_intent(claimed.intent_id, IntentStatus.REJECTED, error="nothing sellable after cancel")
            await self._protect_symbol(intent.symbol)
            return ExecResult(claimed.intent_id, intent.symbol, IntentStatus.REJECTED, reason="nothing sellable")
        res = await self._finish_sell(claimed, last, filled, avg)
        await self._protect_symbol(intent.symbol)
        if filled > 0:
            await self.alert("info", f"Sold {intent.symbol}", f"{filled:g} shares at {avg or 0:.2f} ({rule})")
        else:
            await self.alert("critical", f"{intent.symbol}: exit did not fill",
                             f"Exit ({rule}) did not fill; the stop has been re-placed.",
                             key=f"exit-nofill-{intent.intent_id}")
        return res

    async def _finish_sell(self, it: OrderIntent, last: BrokerOrder, filled: float, avg: float | None) -> ExecResult:
        status = IntentStatus.FILLED if filled > 0 else IntentStatus.CANCELLED
        cur = self.ledger.get_intent(it.intent_id)
        if cur is not None and cur.status == IntentStatus.SUBMITTING:
            self.ledger.ack_intent(it.intent_id, IntentStatus.SUBMITTED, broker_order_id=last.id)
        self.ledger.ack_intent(it.intent_id, status, broker_order_id=last.id,
                               meta={"filled_qty": filled, "avg_price": avg})
        return ExecResult(it.intent_id, it.symbol, status, filled, avg, last.status, last.id)

    async def _release_short_calls(self, symbol: str, pos: BrokerPosition, want_qty: float) -> None:
        """A6: shares backing a short call are never sold; close the call first if needed."""
        free = pos.qty_available + sum(_f(o.remaining_qty) for o in await self._open_orders(symbol)
                                       if o.side == "sell" and not _is_option(o.symbol))
        if want_qty <= free + 1e-9:
            return
        if self.options is None:
            raise ExecutionError("shares are locked by a short call and no options manager is available")
        for leg in await self._short_calls(symbol):
            result = await self.options.close_short_call(leg.symbol)
            if not getattr(result, "filled", False):
                raise ExecutionError(f"short call {leg.symbol} did not close: {getattr(result, 'reason', '')}")

    async def _short_calls(self, symbol: str) -> list[BrokerPosition]:
        out = []
        for p in await self.broker.get_positions():
            if p.is_option and p.qty < 0 and p.symbol.startswith(symbol) and "C" in p.symbol[len(symbol):]:
                rest = p.symbol[len(symbol):]
                if len(rest) == 15 and rest[6] == "C":
                    out.append(p)
        return out

    async def _sell_qty(self, symbol: str, qty: float, cid: str, *, reason: str
                        ) -> tuple[float, float | None, BrokerOrder | None]:
        """Cancel our sell orders, confirm, then sell up to `qty` (capped at free shares)."""
        for o in await self._open_orders(symbol):
            if o.side == "sell":
                await self._cancel_confirm(o)
        pos = await self.broker.get_position(symbol)
        if pos is None or not pos.is_long:
            return 0.0, None, None
        whole = min(math.floor(qty + 1e-9), pos.free_whole_qty)
        frac = 0.0
        if qty >= pos.qty - 1e-9:  # selling everything: include any fraction
            frac = round(pos.qty - math.floor(pos.qty + 1e-9), 9)
            if frac > pos.qty_available - whole + 1e-9:
                frac = 0.0
        filled, notional, last = 0.0, 0.0, None
        if whole >= 1:
            q = None
            try:
                q = await self._quote(symbol, purpose="order_sizing")
            except ExecutionError:
                q = None
            if q is not None:
                bid = _f(q.bid) or _f(q.price)
                req = OrderRequest(symbol=symbol, side="sell", qty=whole, type="limit", time_in_force="day",
                                   limit_price=round_price(bid * (1 - self.s.sell_limit_offset_pct)),
                                   client_order_id=cid, position_intent="sell_to_close")
                last = await self._place_and_wait(req)
                filled += _f(last.filled_qty)
                notional += _f(last.filled_qty) * _f(last.filled_avg_price)
            remaining = whole - (_f(last.filled_qty) if last else 0)
            if remaining >= 1:
                pos = await self.broker.get_position(symbol)
                rem = min(int(remaining), pos.free_whole_qty if pos and pos.is_long else 0)
                if rem >= 1:
                    req = OrderRequest(symbol=symbol, side="sell", qty=rem, type="market", time_in_force="day",
                                       client_order_id=f"{cid}-m"[:128], position_intent="sell_to_close")
                    last = await self._place_and_wait(req)
                    filled += _f(last.filled_qty)
                    notional += _f(last.filled_qty) * _f(last.filled_avg_price)
        if frac > 0:
            req = OrderRequest(symbol=symbol, side="sell", qty=frac, type="market", time_in_force="day",
                               client_order_id=f"{cid}-f"[:128], position_intent="sell_to_close")
            o = await self._place_and_wait(req)
            last = last or o
            filled += _f(o.filled_qty)
            notional += _f(o.filled_qty) * _f(o.filled_avg_price)
        avg = notional / filled if filled > 0 else None
        return filled, avg, last

    # ------------------------------------------------------------------ cover
    def _cover_cid(self, symbol: str) -> str:
        return f"sa-{self.session_date():%Y%m%d}-{symbol}-cover-{_hash8(symbol, self.session_date())}"

    async def cover_short(self, symbol: str, reason: str = "short found") -> BrokerOrder | None:
        """Buy back exactly the short quantity. The only code path that buys to close stock."""
        pos = await self.broker.get_position(symbol)
        if pos is None or not pos.is_short:
            return None
        gate = await self._gate("exit", rule="cover_short")
        if not gate.allowed:
            return None
        open_buys = [o for o in await self._open_orders(symbol) if o.side == "buy"]
        if open_buys:
            o = open_buys[0]
            return await self.broker.wait_terminal(o.id, timeout_s=self.s.fill_wait_seconds,
                                                   poll_s=self.s.fill_poll_seconds)
        base = self._cover_cid(symbol)
        attempt = int(self._kv_get(f"exec.cover_attempt.{base}", 0) or 0)
        cid = base if attempt == 0 else f"{base}-{attempt}"
        prior = await self.broker.get_order_by_client_id(cid)
        if prior is not None:
            if prior.is_open:
                return await self.broker.wait_terminal(prior.id, timeout_s=self.s.fill_wait_seconds,
                                                       poll_s=self.s.fill_poll_seconds)
            attempt += 1
            cid = f"{base}-{attempt}"
        self._kv_set(f"exec.cover_attempt.{base}", attempt)
        qty = abs(pos.qty)
        req = OrderRequest(symbol=symbol, side="buy", qty=qty, type="market", time_in_force="day",
                           client_order_id=cid, position_intent="buy_to_close")
        try:
            final = await self._place_and_wait(req)
        except BrokerError as e:
            await self.alert("critical", f"{symbol}: could not buy back short",
                             f"Buy-to-cover of {qty:g} ({reason}) failed: {e}", key=f"cover-{symbol}")
            raise
        if _f(final.filled_qty) + 1e-9 >= qty:
            await self.alert("warning", f"{symbol}: short covered", f"Bought back {qty:g} shares ({reason}).")
        else:
            await self.alert("critical", f"{symbol}: short not fully covered",
                             f"Filled {_f(final.filled_qty):g} of {qty:g} ({final.status}).", key=f"cover-{symbol}")
        return final

    async def _execute_cover_intent(self, intent: OrderIntent) -> ExecResult:
        try:
            claimed = self.ledger.claim_intent(intent.intent_id, check_thesis=False)
        except LedgerError as e:
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=str(e))
        order = await self.cover_short(intent.symbol, reason=str((intent.meta or {}).get("rule") or "intent"))
        if order is None:
            self.ledger.ack_intent(claimed.intent_id, IntentStatus.QUEUED, error="no short or gate closed",
                                   next_attempt_at=self.now() + timedelta(minutes=5))
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.QUEUED, reason="not now")
        return await self._finish(claimed, order)

    # ------------------------------------------------------------- protection
    async def ensure_protection(self, symbols: Iterable[str] | None = None) -> ProtectionReport:
        """One broker GTC stop per long for its whole coverable quantity."""
        rep = ProtectionReport()
        positions = await self.broker.get_positions()
        orders = await self.broker.list_orders("open")
        only = set(symbols) if symbols is not None else None
        for pos in positions:
            if pos.is_option or not pos.is_long or (only is not None and pos.symbol not in only):
                continue
            try:
                await self._protect_position(pos, [o for o in orders if o.symbol == pos.symbol], rep)
            except (BrokerError, ExecutionError) as e:
                rep.errors.append(f"{pos.symbol}: {e}")
                await self.alert("critical", f"{pos.symbol}: stop not in place", f"Could not place or check the "
                                 f"protective stop: {e}", key=f"protect-{pos.symbol}")
        return rep

    async def _protect_symbol(self, symbol: str, *, fill_price: float | None = None) -> None:
        pos = await self.broker.get_position(symbol)
        if pos is None or not pos.is_long:
            return
        rep = ProtectionReport()
        try:
            await self._protect_position(pos, await self._open_orders(symbol), rep, fill_price=fill_price)
        except (BrokerError, ExecutionError) as e:
            await self.alert("critical", f"{symbol}: stop not in place",
                             f"Filled, but the protective stop could not be placed: {e}", key=f"protect-{symbol}")

    async def _protect_position(self, pos: BrokerPosition, orders: list[BrokerOrder], rep: ProtectionReport,
                                *, fill_price: float | None = None, fresh_price: bool = False) -> None:
        sym = pos.symbol
        stops = [o for o in orders if o.is_sell_stop]
        other_sells = [o for o in orders if o.side == "sell" and not o.is_sell_stop]
        if other_sells:
            rep.kept.append(sym)  # an exit is working; it re-protects when it finishes
            return
        stop_held = sum(_f(o.remaining_qty) for o in stops)
        coverable = math.floor(pos.qty_available + stop_held + 1e-9)
        coverable = min(coverable, pos.whole_qty)
        if coverable < 1:
            if pos.whole_qty >= 1:
                rep.uncoverable.append(sym)
            for o in stops:
                await self._cancel_confirm(o)
            return
        meta = self.ledger.open_position_meta(sym)
        now = self.now()
        renew_by = now + timedelta(days=self.s.stop_renew_days)
        if len(stops) == 1 and not fresh_price:
            s = stops[0]
            exp = s.expires_at
            if exp is None and s.created_at is not None:
                exp = s.created_at + timedelta(days=self.s.gtc_life_days)
            if abs(_f(s.remaining_qty) - coverable) < 1e-9 and s.time_in_force == "gtc" and (
                    exp is None or exp > renew_by):
                if meta is not None and meta.stop_order_id != s.id:
                    self.ledger.set_stop(meta.position_id, s.stop_price, s.id, exp)
                rep.kept.append(sym)
                return
        # decide the stop price: keep the recorded/active one unless asked for a fresh one
        price = None
        if not fresh_price:
            if stops and stops[0].stop_price:
                price = _f(stops[0].stop_price)
            elif meta is not None and meta.stop_price is not None and fill_price is None:
                price = float(meta.stop_price)
        if price is None:
            ref = fill_price or _f(pos.current_price)
            if ref <= 0:
                raise ExecutionError("no valid price to set a stop from")
            price = await self._stop_price_for(sym, ref)
        if _f(pos.current_price) > 0 and price >= _f(pos.current_price):
            rep.breached.append(sym)
            await self.alert("critical", f"{sym}: price is below its stop",
                             f"Stop {price:.2f} is at or above the price {_f(pos.current_price):.2f}; selling.",
                             key=f"breach-{sym}")
            self._enqueue_hard_stop(sym, meta)
            return
        for o in stops:
            await self._cancel_confirm(o)
        if stops:
            rep.cancelled_extra.extend(o.id for o in stops[1:])
            rep.renewed.append(sym)
        else:
            rep.placed.append(sym)
        pos2 = await self.broker.get_position(sym)
        if pos2 is None or not pos2.is_long:
            return
        qty = min(coverable, pos2.free_whole_qty)
        if qty < 1:
            rep.uncoverable.append(sym)
            return
        cid = f"sa-stop-{sym}-{now.astimezone(ET):%Y%m%d}-{uuid.uuid4().hex[:12]}"
        req = OrderRequest(symbol=sym, side="sell", qty=qty, type="stop", time_in_force="gtc",
                           stop_price=round_price(price), client_order_id=cid, position_intent="sell_to_close")
        order = await self.broker.submit_order(req)
        exp = order.expires_at or (now + timedelta(days=self.s.gtc_life_days))
        if meta is not None:
            self.ledger.set_stop(meta.position_id, order.stop_price or price, order.id, exp)

    def _enqueue_hard_stop(self, symbol: str, meta: Any) -> None:
        self.ledger.enqueue_intents([NewIntent(
            symbol=symbol, side="sell", dedupe_key=f"hard_stop:{symbol}:{self.session_date().isoformat()}",
            position_id=getattr(meta, "position_id", None), origin="repair",
            meta={"rule": "hard_stop"}, session_date=self.session_date())])

    # ------------------------------------------------------------- monitoring
    async def monitor_tick(self) -> MonitorReport:
        rep = MonitorReport()
        try:
            rep.normalise = await self.normalise_step()
        except Exception as e:  # normalise failures are loud but must not stop protection
            rep.errors.append(f"normalise: {e}")
            await self.alert("critical", "Account clean-up failed", str(e), key="normalise-failed")
        try:
            for p in await self.broker.get_positions():
                if not p.is_option and p.is_short:
                    o = await self.cover_short(p.symbol, reason="short found at broker")
                    if o is not None:
                        rep.covered.append(p.symbol)
            rep.protection = await self.ensure_protection()
            rep.executed = await self.execute_due()
        except BrokerError as e:
            rep.errors.append(str(e))
            await self.alert("critical", "Broker unavailable during monitoring", str(e), key="monitor-broker")
        return rep

    async def drain(self, timeout_s: float = 60.0) -> bool:
        """Stop new submissions; return True once nothing is in flight."""
        import asyncio

        self._draining = True
        waited = 0.0
        while self._in_flight and waited < timeout_s:
            await asyncio.sleep(1.0)
            waited += 1.0
        return not self._in_flight

    # -------------------------------------------------------------- normalise
    def _norm_state(self) -> dict:
        return dict(self._kv_get(self.NORMALISE_KEY, {}) or {})

    async def plan_normalise(self) -> list[str]:
        """Describe, from fresh broker state, what normalise would do."""
        steps: list[str] = []
        cfg = await self.broker.get_account_config()
        if not cfg.no_shorting:
            steps.append("turn shorting off at the broker (no_shorting) and read it back")
        positions = await self.broker.get_positions()
        acct = await self.broker.get_account()
        equity = _f(acct.equity)
        cap = float(getattr(self.config, "MAX_POSITION_PCT", 0.15))
        for p in positions:
            if p.is_option:
                continue
            if p.is_short:
                steps.append(f"buy back the short in {p.symbol} ({abs(p.qty):g} shares)")
                continue
            if p.fractional_qty > 0:
                steps.append(f"sell the fractional {p.fractional_qty:g} share of {p.symbol}")
            if equity > 0 and _f(p.market_value) > cap * equity:
                if await self._short_calls(p.symbol):
                    steps.append(f"close the short call on {p.symbol}")
                steps.append(f"trim {p.symbol} to {cap:.0%} of equity")
        for p in positions:
            if p.is_option and p.qty != 0:
                steps.append(f"hand option position {p.symbol} to the options exit manager")
        steps.append("place one broker stop on every long from the current price")
        steps.append("rebuild the ledger and confirm one clean reconcile, then allow new entries")
        return steps

    async def normalise_step(self) -> NormaliseStatus:
        st = self._norm_state()
        state = st.get("state")
        if state == "done":
            return NormaliseStatus("done")
        if state is None:
            steps = await self.plan_normalise()
            await self.alert("warning", "Account clean-up plan",
                             "Before new trades, at the next regular session open + 5 min the bot will:\n- "
                             + "\n- ".join(steps), key="normalise-plan")
            self._kv_set(self.NORMALISE_KEY, {"state": "planned", "planned_at": self.now().isoformat(),
                                              "steps": steps})
            return NormaliseStatus("planned", steps=steps)
        planned_at = datetime.fromisoformat(st["planned_at"])
        gate = await self._gate("exit")
        if not gate.allowed:
            return NormaliseStatus("waiting", gate.reason)
        session = self.calendar.session_on(self.now().astimezone(ET).date()) if hasattr(
            self.calendar, "session_on") else None
        if session is not None and planned_at > session.opens_at:
            return NormaliseStatus("waiting", "plan was posted during this session; runs next session")
        return await self.run_normalise()

    async def run_normalise(self) -> NormaliseStatus:
        done: list[str] = []
        st = self._norm_state()
        st["state"] = "running"
        self._kv_set(self.NORMALISE_KEY, st)
        cfg = await self.broker.get_account_config()
        if not cfg.no_shorting:
            await self.broker.set_account_config(no_shorting=True)
            done.append("shorting off (read back)")
        for p in await self.broker.get_positions():
            if not p.is_option and p.is_short:
                o = await self.cover_short(p.symbol, reason="clean-up")
                if o is None or _f(o.filled_qty) + 1e-9 < abs(p.qty):
                    return self._norm_fail(f"short {p.symbol} not covered", done)
                done.append(f"covered {p.symbol}")
        acct = await self.broker.get_account()
        equity = _f(acct.equity)
        cap = float(getattr(self.config, "MAX_POSITION_PCT", 0.15))
        for p in await self.broker.get_positions():
            if p.is_option or not p.is_long:
                continue
            if p.fractional_qty > 0:
                cid = f"sa-{self.session_date():%Y%m%d}-{p.symbol}-frac-{_hash8(p.symbol, 'frac')}"
                prior = await self.broker.get_order_by_client_id(cid)
                if prior is None:
                    req = OrderRequest(symbol=p.symbol, side="sell", qty=p.fractional_qty, type="market",
                                       time_in_force="day", client_order_id=cid, position_intent="sell_to_close")
                    try:
                        await self._place_and_wait(req)
                    except BrokerError as e:
                        if e.kind != "guard":
                            raise
                        # the fraction is held by a stop: release it first
                        for o in await self._open_orders(p.symbol):
                            if o.side == "sell":
                                await self._cancel_confirm(o)
                        await self._place_and_wait(req)
                done.append(f"sold fractional {p.symbol}")
            if equity > 0 and _f(p.market_value) > cap * equity and _f(p.current_price) > 0:
                excess = _f(p.market_value) - cap * equity
                trim = math.ceil(excess / _f(p.current_price))
                calls = await self._short_calls(p.symbol)
                for leg in calls:
                    if self.options is None:
                        return self._norm_fail(f"{p.symbol}: short call open and no options manager", done)
                    r = await self.options.close_short_call(leg.symbol)
                    if not getattr(r, "filled", False):
                        return self._norm_fail(f"short call {leg.symbol} not closed", done)
                    done.append(f"closed short call on {p.symbol}")
                cid = f"sa-{self.session_date():%Y%m%d}-{p.symbol}-trim-{_hash8(p.symbol, 'trim')}"
                if await self.broker.get_order_by_client_id(cid) is None:
                    filled, _, _ = await self._sell_qty(p.symbol, float(trim), cid, reason="normalise")
                    if filled + 1e-9 < trim:
                        return self._norm_fail(f"trim of {p.symbol} filled {filled:g} of {trim}", done)
                done.append(f"trimmed {p.symbol} by {trim}")
        # stops from CURRENT price on every long
        orders = await self.broker.list_orders("open")
        rep = ProtectionReport()
        for p in await self.broker.get_positions():
            if p.is_option or not p.is_long:
                continue
            await self._protect_position(p, [o for o in orders if o.symbol == p.symbol], rep, fresh_price=True)
        done.append(f"stops placed on {len(rep.placed) + len(rep.renewed)} longs")
        if rep.uncoverable:
            done.append("not coverable (shares locked): " + ", ".join(rep.uncoverable))
        snap = await self.ledger.refresh("normalise", rebuild=True)
        bad = [d for d in getattr(snap, "diffs", ()) if str(getattr(d, "severity", "")) in ("error", "critical")]
        if bad or any(h.qty < 0 for h in snap.holdings):
            return self._norm_fail("reconcile after clean-up was not clean", done)
        st = self._norm_state()
        st.update(state="done", done_at=self.now().isoformat(), done=done)
        self._kv_set(self.NORMALISE_KEY, st)
        return NormaliseStatus("done", steps=done)

    def _norm_fail(self, why: str, done: list[str]) -> NormaliseStatus:
        st = self._norm_state()
        st.update(state="planned", last_error=why, partial=done)
        self._kv_set(self.NORMALISE_KEY, st)
        logger.error("normalise: %s", why)
        return NormaliseStatus("failed", why, done)
