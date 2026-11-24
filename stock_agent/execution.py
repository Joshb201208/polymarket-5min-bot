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
import asyncio
import contextlib
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
from stock_agent.ledger import IntentStatus, LedgerError, NewIntent, OrderIntent, ThesisRecord, ThesisRole, TopUpAlreadyToday

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
                                                     "risk_limit", "margin_buffer"})


@dataclass
class ExecResult:
    intent_id: str | None
    symbol: str
    status: IntentStatus
    filled_qty: float = 0.0
    avg_price: float | None = None
    reason: str = ""
    order_id: str | None = None
    code: str = ""  # machine-readable skip/hold reason for the entry funnel (A14d)


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


def _D(x: Any) -> Decimal:
    return Decimal(repr(float(x or 0)))


def _is_own(o: Any) -> bool:
    """Orders this bot placed carry its `sa-` client order id prefix."""
    return str(getattr(o, "client_order_id", "") or "").startswith("sa-")


def _floor(x: Any) -> int:
    from decimal import ROUND_FLOOR

    return int(_D(x).to_integral_value(rounding=ROUND_FLOOR))


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
        capital_lock: Any = None,
        research_gate: Any = None,
        notifier: Any = None,
        margin_controls: Callable[[Any], Any] | None = None,
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
        self._capital_lock = capital_lock
        self._research_gate = research_gate
        self._margin_controls = margin_controls  # scheduler: snapshot -> RiskControls (held convictions)
        self._notifier = notifier  # AlertService-like: send_normalise_plan / send_normalise_result
        self.config = config
        self.s = settings or ExecSettings()
        self._alert_fn = alert
        self._now = now
        self._draining = False
        self._in_flight: set[str] = set()
        self._busy = 0  # whole order workflows (submit -> fill -> stop) still running; drain waits on these
        self._last_atr: dict[str, tuple[Any, Any]] = {}

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
            dd = from_record(self.ledger.risk_state_get(key), config=self.config)
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
        actual = str(getattr(q, "symbol", "") or symbol).upper()
        if actual != symbol.upper():
            # renamed ticker: never size an order for the old symbol from the new one's quote
            raise ExecutionError(f"symbol changed: {symbol} now trades as {actual}; fresh thesis required")
        return q

    async def _stop_distance(self, symbol: str, price: float):
        from stock_agent.risk_manager import atr14, stop_fraction

        atr = None
        self._last_atr.pop(symbol, None)
        if self.bars is not None:
            try:
                bars = await self.bars(symbol)
                atr = atr14(bars) if bars else None
            except Exception as e:
                logger.warning("bars for %s unavailable (%s); using the minimum stop distance", symbol, e)
        dist = stop_fraction(Decimal(str(price)), atr)
        self._last_atr[symbol] = (atr, dist)
        return dist

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
            self._mark_accepted(req.client_order_id, order.id)
            final = await self.broker.wait_terminal(order.id, timeout_s=wait_s or self.s.fill_wait_seconds,
                                                    poll_s=self.s.fill_poll_seconds)
            if not final.is_terminal:
                final = await self._cancel_confirm(final)
            return final
        finally:
            self._in_flight.discard(req.client_order_id)

    def _mark_accepted(self, cid: str, order_id: str) -> None:
        """Persist SUBMITTED as soon as the broker accepts, so a later poll or
        cancel failure can never be recorded as 'nothing happened'."""
        base = cid[:-2] if cid.endswith(("-m", "-f")) else cid
        try:
            it = self.ledger.intent_by_client_order_id(base) or self.ledger.intent_by_client_order_id(cid)
            if it is not None and it.status == IntentStatus.SUBMITTING:
                self.ledger.ack_intent(it.intent_id, IntentStatus.SUBMITTED, broker_order_id=order_id)
                self._funnel(it, "ordered", order_id=order_id)
        except Exception:
            logger.exception("could not mark intent for %s submitted", cid)

    async def resolve_pending(self) -> list[str]:
        """Resolve submitted/ambiguous intents that are not in flight in this process."""
        done: list[str] = []
        for it in self.ledger.list_intents([IntentStatus.AMBIGUOUS, IntentStatus.SUBMITTED,
                                            IntentStatus.PARTIALLY_FILLED]):
            if any(c in self._in_flight for c in self._order_ids_for(it)):
                continue
            try:
                await self._resolve(it)
                done.append(it.intent_id)
            except BrokerError as e:
                await self.alert("critical", f"{it.symbol}: order still unresolved",
                                 f"Order {it.client_order_id} could not be resolved ({e}); retrying next tick.",
                                 key=f"pending-{it.intent_id}")
        return done

    # --------------------------------------------------------------- resume
    async def resume(self) -> None:
        """Start-up: resolve intents whose outcome is unknown (crash mid-request)."""
        try:
            self.ledger.mark_submitting_ambiguous()
        except Exception as e:
            await self.alert("critical", "Order recovery failed", f"Could not read the intent queue: {e}")
            return
        await self._bind_recovery_order_ids()
        for it in self.ledger.list_intents([IntentStatus.AMBIGUOUS, IntentStatus.SUBMITTED,
                                            IntentStatus.PARTIALLY_FILLED]):
            try:
                await self._resolve(it)
            except BrokerError as e:
                await self.alert("critical", f"{it.symbol}: order state unknown",
                                 f"Could not look up order {it.client_order_id} at the broker ({e}). "
                                 "No new order for this intent until it is resolved.",
                                 key=f"resume-{it.intent_id}")

    async def _bind_recovery_order_ids(self) -> int:
        """Pass 1 of start-up recovery: bind every AMBIGUOUS intent to the broker order
        id(s) found under its client ids, without waiting or refreshing, so the first
        reconcile links all of the bot's own fills (R-03). A failed read leaves the
        intent AMBIGUOUS (fail closed); pass 2 alerts on it."""
        bound = 0
        for it in self.ledger.list_intents([IntentStatus.AMBIGUOUS]):
            try:
                for cid in self._order_ids_for(it):
                    o = await self.broker.get_order_by_client_id(cid)
                    if o is not None:
                        self.ledger.ack_intent(it.intent_id, IntentStatus.SUBMITTED, broker_order_id=o.id)
                        bound += 1
                        break
            except BrokerError as e:
                logger.warning("recovery bind for %s failed: %s", it.intent_id, e)
        return bound

    @staticmethod
    def _order_ids_for(it: OrderIntent) -> list[str]:
        """Every client_order_id one intent can use: base, market fallback, fraction."""
        base = it.client_order_id
        return [base, f"{base}-m"[:128], f"{base}-f"[:128]]

    async def _resolve(self, it: OrderIntent) -> None:
        async with self._workflow():
            await self._resolve_inner(it)

    @contextlib.asynccontextmanager
    async def _workflow(self):
        """Marks an order workflow as running until its fill is booked and protected,
        so drain() never reports idle between a fill and its stop placement."""
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1

    async def _resolve_inner(self, it: OrderIntent) -> None:
        found: list[BrokerOrder] = []
        for cid in self._order_ids_for(it):
            o = await self.broker.get_order_by_client_id(cid)
            if o is not None:
                if not o.is_terminal:
                    o = await self.broker.wait_terminal(o.id, timeout_s=self.s.fill_wait_seconds,
                                                        poll_s=self.s.fill_poll_seconds)
                    if not o.is_terminal:
                        o = await self._cancel_confirm(o)
                found.append(o)
        if not found:
            if it.status == IntentStatus.AMBIGUOUS:
                # The broker confirms no order exists under any of its ids: safe to retry later.
                self.ledger.ack_intent(it.intent_id, IntentStatus.QUEUED, error="not found at broker after restart")
            else:
                await self.alert("critical", f"{it.symbol}: submitted order missing",
                                 f"Intent {it.intent_id} was submitted but the broker has no order "
                                 f"{it.client_order_id}.", key=f"missing-{it.intent_id}")
            return
        if it.status == IntentStatus.AMBIGUOUS:
            it = self.ledger.ack_intent(it.intent_id, IntentStatus.SUBMITTED, broker_order_id=found[0].id)
        filled = sum(_f(o.filled_qty) for o in found)
        notional = sum(_f(o.filled_qty) * _f(o.filled_avg_price) for o in found)
        last = found[-1]
        if it.side == "sell":
            await self._finish_sell(it, last, filled, notional / filled if filled else None)
            await self._protect_symbol(it.symbol)
        else:
            await self._finish(it, last, extra_filled=filled - _f(last.filled_qty),
                               avg=notional / filled if filled else None)

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
        meta = {"filled_qty": filled, "avg_price": price, "order_status": order.status}
        code = ""
        if status == IntentStatus.REJECTED:
            code = "broker_rejected"
        elif status != IntentStatus.FILLED and it.side == "buy":
            code = "unfilled_cancelled"
        if code:
            meta.update(self._code_meta(code, f"order ended {order.status} with nothing filled"))
        if cur is not None and cur.status == IntentStatus.SUBMITTING:
            self._funnel(it, "ordered", order_id=order.id)
        self.ledger.ack_intent(it.intent_id, status, broker_order_id=order.id, meta=meta)
        if status == IntentStatus.FILLED:
            provenance = {
                key: it.meta[key] for key in ("top_up_baseline_provenance", "top_up_baseline_grade") if key in it.meta
            }
            self._funnel(it, "filled", qty=filled, avg_price=price, order_id=order.id, **provenance)
        elif code:
            self._funnel(it, "skipped", code, f"order ended {order.status}")
        if status == IntentStatus.REJECTED:
            # Accepted (HTTP 200) but rejected afterwards by the broker: as loud as a refused submit.
            await self.alert("warning" if it.side == "buy" and it.kind != "cover" else "critical",
                             f"{it.symbol}: {it.side} rejected by the broker",
                             f"Order {order.id} for {it.symbol} was accepted, then rejected (status "
                             f"{order.status}); nothing filled.", key=f"rejected-{it.intent_id}")
        if it.side == "buy" and filled > 0 and it.kind != "cover":
            try:  # books match the broker before the stop is recorded against the position
                await self.ledger.refresh(f"entry fill {it.symbol}")
            except Exception as e:
                logger.warning("ledger refresh after %s fill failed: %s", it.symbol, e)
            await self._protect_symbol(it.symbol, fill_price=price)
            self._journal_entry_fill(it, filled, price, order.id)
        return ExecResult(it.intent_id, it.symbol, status, filled, price, order.status, order.id, code=code)

    def _journal_entry_fill(self, it: OrderIntent, filled: float, price: float | None,
                           fill_id: str | None = None) -> None:
        """A3 shadow-stop anchor. Never raises."""
        try:
            meta = self.ledger.open_position_meta(it.symbol)
            atr, dist = self._last_atr.get(it.symbol, (None, None))
            self.ledger.journal.append(
                "entry.filled", position_id=getattr(meta, "position_id", None), symbol=it.symbol,
                session_date=self.session_date().isoformat(),
                intent_id=it.intent_id, fill_id=fill_id, fill_price=price, qty=filled,
                atr=None if atr is None else str(atr),
                stop_fraction=None if dist is None else str(dist.fraction),
                stop_fallback=None if dist is None else dist.fallback,
                stop_price=None if meta is None or meta.stop_price is None else str(meta.stop_price),
                source=it.origin, sleeve=it.sleeve, thesis_ids=list(it.thesis_ids))
        except Exception:
            logger.exception("entry.filled journal failed")

    # --------------------------------------------------------------- dispatch
    async def execute_due(self) -> list[ExecResult]:
        out: list[ExecResult] = []
        if self._draining:
            return out
        due = self.ledger.due_intents(self.now())
        def urgency(i: OrderIntent) -> int:
            if i.kind == "cover":
                return 0  # an accidental short first
            if i.side == "sell" and (i.origin == "margin_buffer" or (i.meta or {}).get("rule") in (
                    "hard_stop", "margin_buffer")):
                return 1  # A13e: margin trims are not delayed by older ordinary exits
            return 2 if i.side == "sell" else 3

        due.sort(key=lambda i: (urgency(i), i.created_at))
        for it in due:
            if it.kind == "option_structure":
                continue  # the options module executes its own structures
            async with self._workflow():
                try:
                    out.append(await self.execute_intent(it))
                except Exception as e:  # one lane's bug must not stop the other intents or protection
                    logger.exception("intent %s crashed", it.intent_id)
                    await self.alert("critical", f"{it.symbol}: order step crashed",
                                     f"{it.side} {it.symbol}: {type(e).__name__}: {e}",
                                     key=f"crash-{it.intent_id}")
                    try:
                        await self._protect_symbol(it.symbol)
                    except Exception:
                        logger.exception("protect after crash failed")
        return out

    async def execute_intent(self, intent: OrderIntent) -> ExecResult:
        if self._draining:
            return ExecResult(intent.intent_id, intent.symbol, intent.status, reason="draining")
        async with self._workflow():
            return await self._execute_intent(intent)

    async def _execute_intent(self, intent: OrderIntent) -> ExecResult:
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

    # A14d entry-funnel codes (agreed with alerts_reporting). Persisted as intent
    # meta {"skip_code", "skip_reason"} on every skipped/held/refused intent.
    SKIP_CODES = frozenset({
        "cleanup_pending", "books_untrusted", "market_closed", "price_ran_away", "quote_unavailable",
        "symbol_blocked", "short_open", "research_stale", "position_changed", "risk_size_zero",
        "risk_refused", "risk_controls_unavailable", "insufficient_buying_power", "broker_rejected",
        "intent_expired", "duplicate", "top_up_already_today", "top_up_thesis_direction",
        "nothing_to_sell", "exit_cap", "other"})

    def _funnel(self, it: OrderIntent, stage: str, code: str = "", reason: str = "", **extra: Any) -> None:
        """Journal event `execution.buy_funnel` for the daily entry funnel (A14d). Never raises."""
        if it.side != "buy" or it.kind == "cover":
            return
        try:
            self.ledger.journal.append("execution.buy_funnel", intent_id=it.intent_id, symbol=it.symbol,
                                       origin=it.origin, stage=stage, code=code or None,
                                       reason=reason[:500] or None,
                                       session_date=self.session_date().isoformat(), **extra)
        except Exception:
            logger.exception("buy funnel journal failed")

    @staticmethod
    def _code_meta(code: str, reason: str, **extra: Any) -> dict:
        return {"skip_code": code, "skip_reason": reason[:500], **extra}

    @staticmethod
    def _error_code(reason: str) -> str:
        r = reason.lower()
        if "quote" in r or "price service" in r or "symbol changed" in r:
            return "quote_unavailable"
        if "risk controls" in r or "drawdown state" in r:
            return "risk_controls_unavailable"
        return "other"

    def _skip(self, intent: OrderIntent, reason: str, code: str = "other", **extra: Any) -> ExecResult:
        cur = self.ledger.get_intent(intent.intent_id)
        meta = self._code_meta(code, reason, **extra)
        if cur is not None and cur.status == IntentStatus.QUEUED:
            self.ledger.ack_intent(intent.intent_id, IntentStatus.SKIPPED, error=reason, meta=meta)
        elif cur is not None and cur.status == IntentStatus.SUBMITTING:
            self.ledger.ack_intent(intent.intent_id, IntentStatus.REJECTED, error=reason, meta=meta)
        logger.info("intent %s %s %s skipped [%s]: %s", intent.intent_id, intent.side, intent.symbol, code, reason)
        self._funnel(intent, "skipped", code, reason)
        return ExecResult(intent.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=reason, code=code)

    async def _defer_or_skip(self, intent: OrderIntent, reason: str) -> ExecResult:
        code = self._error_code(reason)
        cur = self.ledger.get_intent(intent.intent_id)
        if cur is not None and cur.status == IntentStatus.SUBMITTING:
            # nothing was sent: back to the queue for one more try in 5 minutes
            self.ledger.ack_intent(intent.intent_id, IntentStatus.QUEUED, error=reason,
                                   next_attempt_at=self.now() + timedelta(minutes=5),
                                   meta=self._code_meta(code, reason))
            self._funnel(intent, "held", code, reason)
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.QUEUED, reason=reason, code=code)
        return self._skip(intent, reason, code)

    async def _broker_failure(self, intent: OrderIntent, e: BrokerError) -> ExecResult:
        cur = self.ledger.get_intent(intent.intent_id)
        if cur is not None and cur.status in (IntentStatus.SUBMITTED, IntentStatus.PARTIALLY_FILLED):
            # The broker accepted the order; a later poll/cancel failed. Shares may be
            # held. Protect what is confirmed now and resolve the remainder each tick.
            # A stop cannot rest beside our own working buy (Alpaca's wash-trade
            # check), so first retry cancelling the remainder, then resolve and protect.
            try:
                await self._resolve(cur)
                cur2 = self.ledger.get_intent(intent.intent_id)
                if cur2 is not None and cur2.status not in (IntentStatus.SUBMITTED, IntentStatus.PARTIALLY_FILLED):
                    meta = cur2.meta or {}
                    await self.alert("warning", f"{intent.symbol}: order recovered after a broker error",
                                     f"{e}; the remainder was cancelled on retry and the filled shares "
                                     "are protected.", key=f"accepted-{intent.intent_id}")
                    return ExecResult(intent.intent_id, intent.symbol, cur2.status,
                                      _f(meta.get("filled_qty")), meta.get("avg_price"), reason=str(e))
            except BrokerError as re:
                logger.warning("retry resolve for %s failed: %s", intent.symbol, re)
            try:
                await self._protect_symbol(intent.symbol)
            except Exception as pe:
                logger.warning("protect after failure for %s: %s", intent.symbol, pe)
            await self.alert("critical", f"{intent.symbol}: filled shares may be unprotected",
                             f"{intent.side} {intent.symbol}: {e}. The broker would not cancel the rest of order "
                             f"{intent.client_order_id}, and a stop cannot rest beside it. Retrying the cancel "
                             "and the stop on every monitoring tick.", key=f"accepted-{intent.intent_id}")
            return ExecResult(intent.intent_id, intent.symbol, cur.status, reason=str(e))
        if e.uncertain:
            if cur is not None and cur.status == IntentStatus.SUBMITTING:
                self.ledger.ack_intent(intent.intent_id, IntentStatus.AMBIGUOUS, error=str(e))
            await self.alert("critical", f"{intent.symbol}: order outcome unknown",
                             f"{intent.side} {intent.symbol}: {e}. Not resubmitting until the broker "
                             f"confirms order {intent.client_order_id}.", key=f"ambiguous-{intent.intent_id}")
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.AMBIGUOUS, reason=str(e))
        if (intent.side == "buy" and intent.kind != "cover" and e.insufficient_buying_power
                and cur is not None and cur.status == IntentStatus.SUBMITTING):
            # The broker says the overnight funds are not there: a clean, final skip for
            # this intent (no retry loop). The next research/risk cycle sizes afresh.
            why = f"broker: insufficient buying power ({e.message})"
            self.ledger.ack_intent(intent.intent_id, IntentStatus.REJECTED, error=why,
                                   meta=self._code_meta("insufficient_buying_power", why))
            self._funnel(intent, "skipped", "insufficient_buying_power", why)
            await self.alert("info", f"{intent.symbol}: buy skipped, not enough buying power",
                             f"The broker refused the buy for lack of buying power; not retrying this intent.",
                             key=f"nobp-{self.session_date().isoformat()}")
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=why,
                              code="insufficient_buying_power")
        if cur is not None and cur.status == IntentStatus.SUBMITTING:
            if e.retryable:
                self.ledger.ack_intent(intent.intent_id, IntentStatus.QUEUED, error=str(e),
                                       next_attempt_at=self.now() + timedelta(minutes=5))
            else:
                self.ledger.ack_intent(intent.intent_id, IntentStatus.REJECTED, error=str(e),
                                       meta=self._code_meta("broker_rejected", str(e)))
                self._funnel(intent, "skipped", "broker_rejected", str(e))
        await self.alert("warning" if intent.side == "buy" else "critical",
                         f"{intent.symbol}: {intent.side} failed", f"Broker refused or failed: {e}",
                         key=f"fail-{intent.intent_id}")
        return ExecResult(intent.intent_id, intent.symbol, IntentStatus.REJECTED, reason=str(e),
                          code="broker_rejected")

    # -------------------------------------------------------------------- buy
    @property
    def normalised(self) -> bool:
        return (self._kv_get(self.NORMALISE_KEY, {}) or {}).get("state") == "done"

    async def buy(self, intent: OrderIntent) -> ExecResult:
        if not self.normalised:
            return await self._hold(intent, "account clean-up has not finished; entries are held",
                                    code="cleanup_pending")
        block = self.ledger.trading_block()
        if block:
            return await self._hold(intent, f"trading blocked: {block}", code="books_untrusted")
        gate = await self._gate("entry")
        if not gate.allowed:
            return await self._hold(intent, f"market gate: {gate.reason}", gate.next_open, code="market_closed")
        q = await self._quote(intent.symbol)
        ask = _f(q.ask) or _f(q.price)
        ref = _f(intent.reference_price)
        if ref > 0 and ask > ref * (1 + self.s.max_price_drift_pct):
            return self._skip(intent, f"price {ask:.2f} ran more than {self.s.max_price_drift_pct:.0%} above "
                                      f"the analysed {ref:.2f}", "price_ran_away")
        limit = round_price(ask * (1 + self.s.buy_limit_offset_pct))
        try:
            async with (self._capital_lock or contextlib.nullcontext()):
                # Held only from the fresh risk snapshot through submit/confirm, so the
                # options engine cannot authorise against the same free cash meanwhile.
                got = await self._authorise_and_submit_buy(intent, q, limit, ask)
        except BaseException as e:
            sym = getattr(e, "_exec_reprotect", None)
            if sym:
                await self._protect_symbol(sym)
            raise
        if isinstance(got, ExecResult):
            return got
        claimed, order, released = got
        res = await self._finish(claimed, order)
        if released and res.filled_qty <= 0:
            await self._protect_symbol(intent.symbol)
        if res.filled_qty > 0:
            await self.alert("info", f"Bought {intent.symbol}",
                             f"{res.filled_qty:g} shares at {res.avg_price or 0:.2f}", key=None)
        return res

    async def _authorise_and_submit_buy(self, intent: OrderIntent, q: Any, limit: float, ask: float):
        snap = await self._snapshot("pre-buy")
        if intent.symbol in getattr(snap, "blocked_symbols", frozenset()):
            return self._skip(intent, "symbol blocked by reconcile", "symbol_blocked")
        h = snap.holding(intent.symbol)
        if h is not None and h.qty < 0:
            return self._skip(intent, "account is short this symbol; cover first", "short_open")
        add = h is not None and h.qty > 0
        if (add or intent.position_id) and (
            not add or h.meta is None or h.meta.position_id != intent.position_id
        ):
            return self._skip(intent, "top-up no longer matches the live position", "position_changed")
        why = await self._research_recheck(intent, h)
        if why:
            code = "position_changed" if why.startswith("top-up") else "research_stale"
            return self._skip(intent, why, code)
        thesis = None
        last_filled_conviction = None
        initial_reaffirmation = False
        if add:
            thesis = self.ledger.get_thesis(intent.thesis_ids[0]) if len(intent.thesis_ids) == 1 else None
            draft = thesis.payload.get("draft") if thesis is not None else None
            if thesis is None or not thesis.actionable or thesis.symbol != intent.symbol or not isinstance(draft, dict) or (
                draft.get("direction") != "BUY"
            ):
                return self._skip(intent, "top-up requires a linked BUY thesis", "top_up_thesis_direction")
            last_filled_conviction, may_initialize = self.ledger.top_up_baseline(intent.position_id)
            initial_reaffirmation = (
                may_initialize and thesis.role == ThesisRole.HELD_REVIEW
                and "/" in thesis.source and thesis.payload.get("legacy_v0") is not True
            )
        dist = await self._stop_distance(intent.symbol, ask)
        ti = self._trade_intent(intent, limit, q, dist, add_thesis=thesis,
                                last_filled_conviction=last_filled_conviction,
                                initial_reaffirmation=initial_reaffirmation)
        controls = self._risk_controls()
        qty = int(self.risk.propose_equity_qty(snap, ti, now=self.now(), controls=controls))
        from dataclasses import replace

        if qty < 1:
            why = self.risk.evaluate_trade(snap, replace(ti, qty=Decimal(1)), now=self.now(), controls=controls)
            return self._skip(intent, "risk sizing gave less than one share"
                              + (": " + "; ".join(why.reasons) if why.reasons else ""), "risk_size_zero",
                              risk_reasons=list(why.reasons or ()))

        ti = replace(ti, qty=Decimal(qty))
        verdict = self.risk.evaluate_trade(snap, ti, now=self.now(), controls=controls)
        if not verdict.ok:
            return self._skip(intent, "risk: " + "; ".join(verdict.reasons), "risk_refused",
                              risk_reasons=list(verdict.reasons or ()))
        try:  # A14d: record that risk passed before anything can fail at the broker
            self.ledger.ack_intent(intent.intent_id, IntentStatus.QUEUED, meta={"passed_risk": True})
        except LedgerError:
            pass  # not QUEUED any more: claim below reports it
        self._funnel(intent, "passed_risk", qty=qty)
        try:
            claimed = self.ledger.claim_intent(
                intent.intent_id, top_up_session=self.session_date() if add else None,
                top_up_baseline_grade=ti.effective_conviction if initial_reaffirmation else None,
            )
        except TopUpAlreadyToday as e:
            return self._skip(intent, str(e), "top_up_already_today")
        except LedgerError as e:
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=str(e), code="duplicate")
        # Sizing takes bar/quote/book round trips; the entry window can close meanwhile
        # (e.g. a 12:50 half-day cutoff). Re-check right before the order goes out.
        gate = await self._gate("entry")
        if not gate.allowed:
            nxt = gate.next_open or (self.now() + timedelta(minutes=5))
            why = f"market gate at submit: {gate.reason}"
            self.ledger.ack_intent(claimed.intent_id, IntentStatus.QUEUED, error=why, next_attempt_at=nxt,
                                   meta=self._code_meta("market_closed", why))
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.QUEUED, reason=why, code="market_closed")
        if verdict.allowed_existing_breaches if hasattr(verdict, "allowed_existing_breaches") else ():
            self.ledger.journal.append("risk.allowed_existing_breaches", intent_id=claimed.intent_id,
                                       symbol=intent.symbol, breaches=[
                                           {"kind": b.kind, "key": b.key, "value": str(b.value), "cap": str(b.cap)}
                                           for b in verdict.allowed_existing_breaches])
        req = OrderRequest(symbol=intent.symbol, side="buy", qty=qty, type="limit", time_in_force="day",
                           limit_price=limit, client_order_id=claimed.client_order_id,
                           position_intent="buy_to_open")
        # A top-up cannot rest next to our own sell stop (Alpaca rejects a
        # potential wash trade), so the stop is cancelled and confirmed first
        # and re-placed for the whole position straight after.
        released = False
        try:
            for o in await self._open_orders(intent.symbol):
                if o.side == "sell":
                    await self._cancel_confirm(o)
                    released = True
            order = await self._place_and_wait(req)
        except BaseException as e:
            if released:
                e._exec_reprotect = intent.symbol  # re-protected by buy(), outside the capital lock
            raise
        return claimed, order, released

    RESEARCH_ORIGINS = frozenset({"weekly", "midweek", "prescan", "earnings", "news"})

    async def _research_recheck(self, intent: OrderIntent, holding: Any) -> str | None:
        """At submission, not just at proposal: the research gate must still say BUY
        for this intent's thesis, and a top-up must target the position it was proven for."""
        if holding is not None and holding.qty > 0 and intent.position_id:
            meta = self.ledger.open_position_meta(intent.symbol)
            if meta is None or meta.position_id != intent.position_id:
                return "top-up was approved for a position that has since closed or reopened"
        if self._research_gate is None or intent.origin not in self.RESEARCH_ORIGINS:
            return None
        try:
            g = self._research_gate.is_entry_eligible(intent.symbol, self.now())
            if hasattr(g, "__await__"):
                g = await g
        except Exception as e:
            # A14a: a broken recheck must not block every buy. Degrade to the ledger's
            # current thesis: the intent still has to come from the newest thesis.
            logger.warning("research gate unavailable at submit (%s); using the ledger thesis", e)
            try:
                cur = self.ledger.current_thesis(intent.symbol)
            except Exception as le:
                return f"research gate and ledger thesis both unavailable: {e}; {le}"
            if cur is None or cur.thesis_id not in set(intent.thesis_ids or ()):
                return "research gate unavailable and the ledger has a newer thesis"
            return None
        if not g or getattr(g, "direction", None) != "BUY":
            return "research gate no longer has a BUY for this symbol"
        if getattr(g, "thesis_id", None) not in set(intent.thesis_ids or ()):
            return "research gate's current thesis is not the one this intent was made from"
        return None

    async def _hold(self, intent: OrderIntent, reason: str, until: datetime | None = None,
                    code: str = "other") -> ExecResult:
        nxt = until or (self.now() + timedelta(minutes=5))
        if intent.expires_at is not None and nxt >= intent.expires_at:
            return self._skip(intent, reason + " (intent would expire)", "intent_expired", held_by=code)
        self.ledger.ack_intent(intent.intent_id, IntentStatus.QUEUED, error=reason, next_attempt_at=nxt,
                               meta=self._code_meta(code, reason))
        self._funnel(intent, "held", code, reason)
        return ExecResult(intent.intent_id, intent.symbol, IntentStatus.QUEUED, reason=reason, code=code)

    @staticmethod
    def _signal_at(intent: OrderIntent) -> datetime:
        """When the thesis behind the intent was generated (not when it was queued)."""
        raw = (intent.meta or {}).get("signal_at") or (intent.meta or {}).get("generated_at")
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                pass
        return intent.created_at

    def _trade_intent(self, intent: OrderIntent, limit: float, q: Any, dist: Any, *,
                      add_thesis: ThesisRecord | None = None, last_filled_conviction: int | None = None,
                      initial_reaffirmation: bool = False):
        from stock_agent.risk_manager import TradeIntent, TradeKind

        m = dict(intent.meta or {})
        add = add_thesis is not None
        if add:
            # The thesis/grade used for approval comes from the ledger, not
            # a stale or optimistic copy in a queued order's metadata.
            m.update(raw_conviction=add_thesis.payload.get("raw_conviction"),
                     effective_conviction=add_thesis.payload.get("effective_conviction"),
                     thesis_change=add_thesis.payload["draft"].get("thesis_change"))
        kw: dict[str, Any] = dict(
            kind=TradeKind.ADD if add else TradeKind.BUY,
            symbol=intent.symbol,
            qty=Decimal(0),
            price=Decimal(str(limit)),
            sector=m.get("sector"),
            raw_conviction=m.get("raw_conviction"),
            effective_conviction=m.get("effective_conviction", m.get("conviction")),
            last_filled_conviction=last_filled_conviction,
            thesis_change=m.get("thesis_change") if add else None,
            initial_reaffirmation=initial_reaffirmation,
            stop_distance=dist.fraction,
            analysis_price=intent.reference_price,
            signal_at=add_thesis.generated_at if add else self._signal_at(intent),
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

    def _count_model_exit(self) -> None:
        self._kv_set(self._model_exit_key(), int(self._kv_get(self._model_exit_key(), 0) or 0) + 1)

    async def _replan_trim(self, intent: OrderIntent) -> float | str:
        """A queued risk trim is re-planned from a fresh, trusted book right before it
        sells: still the recommended symbol, and never more than the fresh quantity."""
        from stock_agent.risk_manager import TradeKind

        m = intent.meta or {}
        counters: dict[tuple[str, str], int] = {}
        for row in m.get("consecutive_closes") or []:
            try:
                kind, key, n = row
                counters[(str(kind), str(key))] = int(n)
            except (TypeError, ValueError):
                continue
        snap = await self._snapshot(f"trim replan {intent.symbol}")
        if not counters and intent.origin == "limit_drift":
            # Drift was confirmed at the close that queued this trim; re-check only that
            # the breach is still beyond its band on today's fresh book.
            need = int(getattr(self.risk.config, "RISK_DRIFT_CONFIRM_SESSIONS", 2))
            counters = {(b.kind, b.key): need
                        for b in self.risk.evaluate_portfolio(snap, controls=self._risk_controls())}
        delivery = intent.origin == "option_delivery"
        plan = self.risk.plan_remediation(snap, consecutive_closes=counters, broker_action=delivery,
                                          broker_symbol=m.get("delivery_symbol") if delivery else None,
                                          controls=self._risk_controls())
        if not plan:
            return "risk trim no longer needed on the fresh book"
        rec = plan[0]
        if rec.kind != TradeKind.SELL or str(rec.symbol).upper() != intent.symbol.upper():
            return f"fresh risk plan now recommends {rec.kind} {rec.symbol}, not this trim"
        qty = float(rec.qty)
        if qty <= 0:
            return "fresh risk plan sells nothing"
        return qty

    MARGIN_EPISODE_KEY = "a13.margin_trim_episode"  # ledger risk_state, written by the scheduler

    async def _replan_margin_trim(self, intent: OrderIntent) -> float | str:
        """A13e: a queued margin-buffer trim is re-planned on a fresh book right before it
        sells (risk.plan_margin_trim, force=True): same symbol, never more than the fresh
        or the queued quantity, never shares backing a short call."""
        from stock_agent.risk_manager import TradeKind

        planner = getattr(self.risk, "plan_margin_trim", None)
        if planner is None:
            return "risk has no margin trim planner"
        want_ep = (intent.meta or {}).get("margin_episode")
        if want_ep is not None:
            try:  # the scheduler persists the episode in the ledger's risk_state table
                cur_ep = (self.ledger.risk_state_get(self.MARGIN_EPISODE_KEY, {}) or {}).get("opened_at")
            except Exception as e:
                return f"margin episode unreadable: {e}"
            if cur_ep != want_ep:
                return "margin trim belongs to an earlier margin episode"
        snap = await self._snapshot(f"margin trim replan {intent.symbol}")
        try:
            controls = self._margin_controls(snap) if self._margin_controls else self._risk_controls()
        except Exception as e:
            logger.warning("margin controls unavailable, using entry controls: %s", e)
            controls = self._risk_controls()
        plan = tuple(planner(snap, controls=controls, force=True) or ())
        if not plan:
            return "margin trim no longer needed on the fresh book"
        rec = plan[0]  # weakest first: only the fresh plan's FIRST leg may sell now
        if rec.kind != TradeKind.SELL or str(rec.symbol).upper() != intent.symbol.upper():
            return f"fresh margin plan now trims {rec.symbol} first, not this leg"
        qty = float(rec.qty)
        return qty if qty > 0 else "fresh margin plan sells nothing of this symbol"

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
        if rule == "model" and intent.origin in self.RESEARCH_ORIGINS:
            # Research exits are re-checked at submission (not protective or risk exits).
            if intent.position_id and meta is None:
                return self._skip(intent, "position metadata missing; the research exit is stale")
            if intent.thesis_ids:
                cur = self.ledger.current_thesis(intent.symbol)
                if cur is None or cur.thesis_id != intent.thesis_ids[0]:
                    return self._skip(intent, "a newer thesis replaced the one this exit was made from")
        want = (intent.meta or {}).get("qty")
        want_qty = float(want) if want is not None else pos.qty
        want_qty = min(want_qty, pos.qty)
        if intent.kind == "trim" and intent.origin in ("limit_drift", "option_delivery", "margin_buffer"):
            block = self.ledger.trading_block()
            if block:
                return await self._hold(intent, f"risk trim waits for trusted books: {block}", code="books_untrusted")
            fresh = await (self._replan_margin_trim(intent) if intent.origin == "margin_buffer"
                           else self._replan_trim(intent))
            if isinstance(fresh, str):
                return self._skip(intent, fresh)
            want_qty = min(want_qty, fresh)
        margin_trim = intent.origin == "margin_buffer"
        if margin_trim:
            # A13e: sized on the broker position read AFTER the fresh replan; never the
            # shares backing a short call, never shares reserved by someone else's order.
            pos = await self.broker.get_position(intent.symbol)
            if pos is None or not pos.is_long:
                return self._skip(intent, "no long position at the broker", "nothing_to_sell")
            want_qty = min(want_qty, pos.qty, float(await self._free_shares(intent.symbol, pos, own_only=True)))
        if want_qty <= 0:
            return self._skip(intent, "nothing to sell", "nothing_to_sell")
        if not margin_trim:  # a margin trim never closes a short call to free shares
            await self._release_short_calls(intent.symbol, pos, want_qty)
        try:
            claimed = self.ledger.claim_intent(intent.intent_id, check_thesis=False)
        except LedgerError as e:
            return ExecResult(intent.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=str(e))
        capped = rule not in self.s.uncapped_exit_rules
        try:
            filled, avg, last = await self._sell_qty(intent.symbol, want_qty, claimed.client_order_id, reason=rule,
                                                     own_only=margin_trim)
        except BaseException:
            if capped:  # an order may have reached the broker: count it, never under-count
                self._count_model_exit()
            # the stop was cancelled for this exit: put it back before reporting the failure
            await self._protect_symbol(intent.symbol)
            raise
        if last is None:
            # No sell order was placed, so no model-exit slot is used.
            await self._protect_symbol(intent.symbol)
            after = await self.broker.get_position(intent.symbol)
            if after is None or not after.is_long:
                # The resting stop filled while we were cancelling it: the exit is moot.
                why = "position closed by its stop while the exit was cancelling it"
                self.ledger.ack_intent(claimed.intent_id, IntentStatus.REJECTED, error=why)
                return ExecResult(claimed.intent_id, intent.symbol, IntentStatus.SKIPPED, reason=why)
            self.ledger.ack_intent(claimed.intent_id, IntentStatus.REJECTED, error="nothing sellable after cancel")
            await self.alert("warning" if capped else "critical", f"{intent.symbol}: exit not placed",
                             f"Exit ({rule}) placed no order (no usable quote or no free shares); "
                             "the stop has been re-placed.", key=f"exit-noorder-{intent.intent_id}")
            return ExecResult(claimed.intent_id, intent.symbol, IntentStatus.REJECTED, reason="nothing sellable")
        if capped:
            self._count_model_exit()
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

    async def _free_shares(self, symbol: str, pos: BrokerPosition, *, own_only: bool = False) -> Decimal:
        """Shares we could sell after cancelling sell orders (only the bot's own when
        own_only); excludes shares the broker holds as cover for short calls."""
        free = pos.qty_available + sum(_f(o.remaining_qty) for o in await self._open_orders(symbol)
                                       if o.side == "sell" and not _is_option(o.symbol)
                                       and (not own_only or _is_own(o)))
        return _D(min(free, pos.qty))

    async def _release_short_calls(self, symbol: str, pos: BrokerPosition, want_qty: float) -> None:
        """A6: shares backing a short call are never sold; close the call first if needed."""
        free = await self._free_shares(symbol, pos)
        if _D(want_qty) <= free:
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

    async def _sell_qty(self, symbol: str, qty: float, cid: str, *, reason: str, own_only: bool = False
                        ) -> tuple[float, float | None, BrokerOrder | None]:
        """Cancel our sell orders, confirm, then sell up to `qty` (capped at free shares)."""
        for o in await self._open_orders(symbol):
            if o.side == "sell" and (not own_only or _is_own(o)):
                await self._cancel_confirm(o)
        pos = await self.broker.get_position(symbol)
        if pos is None or not pos.is_long:
            return 0.0, None, None
        whole = min(_floor(qty), pos.free_whole_qty)
        frac = 0.0
        if _D(qty) >= _D(pos.qty):  # selling everything: include any fraction
            frac = float(_D(pos.qty) - pos.whole_qty)
            if _D(frac) > _D(pos.qty_available) - whole:
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
        # A13i: covering an accidental short always reduces risk; it no longer has to be
        # paid from cash (margin may fund it). Only the broker can refuse it.
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
        # Only OUR working exit defers protection (it re-protects when it finishes).
        # Someone else's resting sell holds its own shares (excluded from qty_available);
        # the free rest is still protected beside it.
        other_sells = [o for o in orders if o.side == "sell" and not o.is_sell_stop and _is_own(o)]
        if other_sells:
            rep.kept.append(sym)
            return
        stop_held = sum(_f(o.remaining_qty) for o in stops)
        coverable = _floor(_D(pos.qty_available) + _D(stop_held))
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
                price = await self._split_adjusted(sym, pos, float(meta.stop_price))
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
        self._kv_set(f"exec.stop_basis.{sym}", {"qty": str(pos2.qty), "avg": str(pos2.avg_entry_price)})
        exp = order.expires_at or (now + timedelta(days=self.s.gtc_life_days))
        if meta is not None:
            self.ledger.set_stop(meta.position_id, order.stop_price or price, order.id, exp)

    async def _split_adjusted(self, sym: str, pos: BrokerPosition, recorded: float) -> float:
        """The broker cancels resting stops on a split; our recorded stop is then
        in pre-split dollars. Detect a split (qty up by k, average cost down by k)
        and scale the stop, rather than reading the old price as a breach."""
        basis = self._kv_get(f"exec.stop_basis.{sym}", None) or {}
        try:
            q0, a0 = float(basis["qty"]), float(basis["avg"])
        except (KeyError, TypeError, ValueError):
            return recorded
        q1, a1 = _f(pos.qty), _f(pos.avg_entry_price)
        if q0 <= 0 or a0 <= 0 or q1 <= 0 or a1 <= 0:
            return recorded
        k_qty, k_px = q1 / q0, a0 / a1
        if abs(k_qty - 1) < 0.01 or abs(k_qty / k_px - 1) > 0.01:
            return recorded  # no split (or a partial sale / top-up, which leaves avg cost alone)
        adj = recorded / k_qty
        await self.alert("warning", f"{sym}: corporate action detected",
                         f"Shares changed by a factor of {k_qty:g} and cost basis by 1/{k_px:g}; "
                         f"re-placing the stop at {adj:.2f} (was {recorded:.2f}).", key=f"split-{sym}")
        self.ledger.journal.append("stop.split_adjusted", symbol=sym, factor=round(k_qty, 6),
                                   old_stop=round(recorded, 4), new_stop=round(adj, 4))
        return adj

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
        if self.normalised:
            key = f"exec.no_shorting_checked.{self.session_date().isoformat()}"
            last = self._kv_get(key, None)
            due = last is None or (last != "ok" and self.now() - datetime.fromisoformat(last) >= timedelta(hours=1))
            if due:
                # daily check (hourly retry after a failure) that shorting is off at the broker
                ok = await self._ensure_no_shorting([])
                self._kv_set(key, "ok" if ok else self.now().isoformat())
        try:
            await self.resolve_pending()
            # Until clean-up is done, normalise owns short covering (it sets
            # no_shorting and reads it back first); afterwards any short is repaired here.
            positions = await self.broker.get_positions() if self.normalised else []
            for p in positions:
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
        while (self._in_flight or self._busy) and waited < timeout_s:
            await asyncio.sleep(1.0)
            waited += 1.0
        return not (self._in_flight or self._busy)

    # -------------------------------------------------------------- normalise
    def _norm_state(self) -> dict:
        return dict(self._kv_get(self.NORMALISE_KEY, {}) or {})

    async def plan_normalise(self) -> list[str]:
        """Describe, from fresh broker state, what normalise would do."""
        steps: list[str] = []
        # Same order as _run_normalise (B3): buy back shorts FIRST, then turn shorting off.
        positions = await self.broker.get_positions()
        for p in positions:
            if not p.is_option and p.is_short:
                steps.append(f"buy back the short in {p.symbol} ({abs(p.qty):g} shares)")
        cfg = await self.broker.get_account_config()
        if not cfg.no_shorting:
            steps.append("turn shorting off at the broker (no_shorting) and read it back")
        acct = await self.broker.get_account()
        equity = _f(acct.equity)
        cap = float(getattr(self.config, "MAX_POSITION_PCT", 0.15))
        for p in positions:
            if p.is_option or not p.is_long:
                continue
            needs_trim = equity > 0 and _f(p.market_value) > cap * equity
            if needs_trim and await self._short_calls(p.symbol):
                steps.append(f"close the short call on {p.symbol}")
            if p.fractional_qty > 0:
                steps.append(f"sell the fractional {p.fractional_qty:g} share of {p.symbol}")
            if needs_trim:
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
            corrective = [x for x in steps if not x.startswith(("place one broker stop", "rebuild the ledger",
                                                                 "hand option position"))]
            if not corrective:
                # Already clean (new account, or state rebuilt after a clean run): protect and go.
                self._kv_set(self.NORMALISE_KEY, {"state": "planned", "planned_at": self.now().isoformat(),
                                                  "steps": steps})
                return await self.run_normalise()
            if not await self._notify("send_normalise_plan", steps):
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
        """Run every clean-up step; any failure re-protects and leaves entries held."""
        done: list[str] = []
        try:
            return await self._run_normalise(done)
        except (BrokerError, ExecutionError) as e:
            try:
                await self.ensure_protection()
            except BrokerError:
                pass
            await self.alert("critical", "Account clean-up stopped", f"{e}. Entries stay held; it will retry.",
                             key="normalise-failed")
            return await self._norm_fail(str(e), done)

    async def _ensure_no_shorting(self, done: list[str]) -> bool:
        """Set no_shorting=true and read it back. A failure alerts and continues:
        the submit-time sell guard still refuses any sell that could open a short."""
        try:
            cfg = await self.broker.get_account_config()
            if cfg.no_shorting:
                return True
            await self.broker.set_account_config(no_shorting=True)
            back = await self.broker.get_account_config()
            if back.no_shorting:
                done.append("shorting off (read back)")
                return True
            why = "read-back still shows shorting allowed"
        except BrokerError as e:
            why = str(e)
        await self.alert("critical", "Could not turn shorting off at the broker",
                         f"{why}. Continuing: every sell is still checked against shares held, "
                         "so no order can open a short. Will retry on the next clean-up.",
                         key="no-shorting-config")
        done.append("shorting off FAILED (sell guard still active)")
        return False

    async def _run_normalise(self, done: list[str]) -> NormaliseStatus:
        st = self._norm_state()
        st["state"] = "running"
        self._kv_set(self.NORMALISE_KEY, st)
        # B3 order: buy back existing shorts FIRST, then turn shorting off.
        for p in await self.broker.get_positions():
            if not p.is_option and p.is_short:
                o = await self.cover_short(p.symbol, reason="clean-up")
                if o is None or _f(o.filled_qty) + 1e-9 < abs(p.qty):
                    return await self._norm_fail(f"short {p.symbol} not covered", done)
                done.append(f"covered {p.symbol}")
                await self.ledger.refresh("normalise: cover")
        await self._ensure_no_shorting(done)
        acct = await self.broker.get_account()
        equity = _f(acct.equity)
        cap = float(getattr(self.config, "MAX_POSITION_PCT", 0.15))
        for p0 in await self.broker.get_positions():
            if p0.is_option or not p0.is_long:
                continue
            sym = p0.symbol
            needs_frac = p0.fractional_qty > 0
            needs_trim = equity > 0 and _f(p0.market_value) > cap * equity
            if not (needs_frac or needs_trim):
                continue
            # Every step is derived from fresh broker state, so a rerun after a
            # crash or a cancelled child order simply does what is still left.
            for o in await self._open_orders(sym):
                if o.side == "sell":
                    await self._cancel_confirm(o)
            if needs_trim:
                for leg in await self._short_calls(sym):
                    if self.options is None:
                        return await self._norm_fail(f"{sym}: short call open and no options manager", done)
                    r = await self.options.close_short_call(leg.symbol)
                    if not getattr(r, "filled", False):
                        return await self._norm_fail(f"short call {leg.symbol} not closed", done)
                    done.append(f"closed short call on {sym}")
            p = await self.broker.get_position(sym)
            if p is None or not p.is_long:
                continue
            if p.fractional_qty > 0:
                cid = f"sa-{self.session_date():%Y%m%d}-{sym}-frac-{uuid.uuid4().hex[:10]}"
                req = OrderRequest(symbol=sym, side="sell", qty=p.fractional_qty, type="market",
                                   time_in_force="day", client_order_id=cid, position_intent="sell_to_close")
                o = await self._place_and_wait(req)
                if _D(o.filled_qty) < _D(p.fractional_qty):
                    await self._protect_symbol(sym)
                    return await self._norm_fail(f"fraction of {sym} not sold ({o.status})", done)
                done.append(f"sold fractional {sym}")
                await self.ledger.refresh("normalise: fraction")
                p = await self.broker.get_position(sym)
                if p is None or not p.is_long:
                    continue
            if equity > 0 and _f(p.market_value) > cap * equity and _f(p.current_price) > 0:
                trim = math.ceil((_f(p.market_value) - cap * equity) / _f(p.current_price))
                cid = f"sa-{self.session_date():%Y%m%d}-{sym}-trim-{uuid.uuid4().hex[:10]}"
                try:
                    filled, _, _ = await self._sell_qty(sym, float(trim), cid, reason="normalise")
                finally:
                    await self._protect_symbol(sym)
                if filled < trim:
                    return await self._norm_fail(f"trim of {sym} filled {filled:g} of {trim}", done)
                done.append(f"trimmed {sym} by {trim}")
                await self.ledger.refresh("normalise: trim")
            else:
                await self._protect_symbol(sym)
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
        acct = await self.broker.get_account()
        equity = _f(acct.equity)
        for p in await self.broker.get_positions():
            if p.is_option:
                continue
            if p.is_short:
                return await self._norm_fail(f"{p.symbol} is still short", done)
            if p.fractional_qty > 0:
                return await self._norm_fail(f"{p.symbol} still has a fractional share", done)
            if equity > 0 and _f(p.market_value) - _f(p.current_price) > cap * equity:
                return await self._norm_fail(f"{p.symbol} is still above {cap:.0%} of equity", done)
        snap = await self.ledger.refresh("normalise", rebuild=True)
        bad = [d for d in getattr(snap, "diffs", ()) if str(getattr(d, "severity", "")) in ("error", "critical")]
        if bad or any(h.qty < 0 for h in snap.holdings):
            return await self._norm_fail("reconcile after clean-up was not clean: " + "; ".join(
                f"{d.kind} {d.symbol} {d.explanation}" for d in bad), done)
        st = self._norm_state()
        st.update(state="done", done_at=self.now().isoformat(), done=done)
        self._kv_set(self.NORMALISE_KEY, st)
        await self._notify("send_normalise_result", list(done), all_clean=True)
        return NormaliseStatus("done", steps=done)

    async def _notify(self, method: str, *args: Any, **kw: Any) -> bool:
        """Call the optional notifier; never lets a reporting failure touch trading."""
        fn = getattr(self._notifier, method, None) if self._notifier is not None else None
        if fn is None:
            return False
        try:
            r = fn(*args, **kw)
            if hasattr(r, "__await__"):
                r = await r
            return r is not False
        except Exception:
            logger.exception("notifier %s failed", method)
            return False

    async def _norm_fail(self, why: str, done: list[str]) -> NormaliseStatus:
        st = self._norm_state()
        st.update(state="planned", last_error=why, partial=done)
        self._kv_set(self.NORMALISE_KEY, st)
        logger.error("normalise: %s", why)
        await self._notify("send_normalise_result", [*done, f"stopped: {why}"], all_clean=False)
        return NormaliseStatus("failed", why, done)
