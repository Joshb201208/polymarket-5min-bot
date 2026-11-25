"""The bot's books: one facade over the state store, the journal and reconcile.

Ownership rule: only ``Ledger.refresh`` (reconcile against the broker) writes
quantities, average cost and cash. Everything else here is metadata the
broker cannot know: thesis links, entry dates, sectors, stops, order intents,
job markers, equity close marks and risk state.

Numbers are ``Decimal`` (parsed from the broker's strings). Quantities are
signed: a short is negative, fractions are kept.

Consumers:

* risk_limits: ``snapshot()`` -> ``BookSnapshot``; ``risk_state_get/set``;
  ``equity_marks()``; ``cash_flows()``.
* broker_execution: ``refresh()``, ``enqueue_intents/claim_intent/ack_intent``,
  ``set_stop``, ``open_position_meta``.
* scheduler_deploy: ``start_job/finish_job/job_status``, ``list_intents``,
  ``trading_block``.
* research_pipeline: thesis API (``record_thesis``, ``set_current_thesis``,
  ``current_thesis``, ``entry_thesis``, ``last_filled_thesis``).
* alerts_reporting: ``snapshot()``, ``last_reconcile()``, ``equity_marks()``,
  ``cash_flows()``, ``journal``.
* options: ``store.register_migration(1, "options", fn)`` for its own tables;
  ``snapshot().legs``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from stock_agent.journal import Journal
from stock_agent.state_store import StateStore, StateUnavailable

if TYPE_CHECKING:
    from stock_agent.research_gate import OwnerEntryProof

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


# =========================================================================
# Errors
# =========================================================================
class StaleBook(Exception):
    """The last reconciled snapshot is too old (or there is none)."""


class LedgerError(Exception):
    """Misuse of the ledger API (bad role, unknown intent, illegal transition)."""


class TopUpAlreadyToday(LedgerError):
    """Another top-up for this position filled or is still unresolved this session."""


class NewStockEntryRefused(LedgerError):
    """A new stock position could not reserve a common entry slot."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# =========================================================================
# Book snapshot (broker truth + local metadata)
# =========================================================================
class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DiffKind(StrEnum):
    IMPORTED = "imported"                 # broker holds it, ledger did not
    CLOSED = "closed"                     # ledger had it, broker no longer holds it
    QTY_CHANGED = "qty_changed"
    AVG_COST_CHANGED = "avg_cost_changed"
    SHORT_STOCK = "short_stock"           # always critical (A1, D3)
    FRACTIONAL = "fractional"
    CASH_CHANGED = "cash_changed"
    RENAMED = "renamed"                   # NC / SC activity moved metadata
    OPTION_LEG_UNGROUPED = "option_leg_ungrouped"
    PRICE_INVALID = "price_invalid"
    UNEXPLAINED = "unexplained"
    MASS_CLOSE = "mass_close"
    ENTRY_DATE_CORRECTED = "entry_date_corrected"   # migrated/unknown entry date replaced from broker fills


@dataclass(frozen=True)
class Diff:
    kind: DiffKind
    symbol: str | None
    severity: Severity
    ledger: Any = None          # what the ledger believed (qty, cash, ...)
    broker: Any = None          # what the broker says
    explanation: str = ""       # e.g. "stop fill 2026-05-01", "assignment", "no activity found"
    action: str = ""            # what reconcile did ("imported", "closed meta", "blocked buys")


@dataclass(frozen=True)
class PositionMeta:
    position_id: str
    symbol: str
    asset_class: str = "us_equity"
    opened_at: datetime | None = None
    opened_source: str = "bot"            # bot | import | assignment | exercise | manual | migration
    sector: str | None = None
    stop_price: Decimal | None = None
    stop_order_id: str | None = None
    stop_expires_at: datetime | None = None
    entry_thesis_id: str | None = None
    entry_intent_id: str | None = None
    notes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Holding:
    """One stock position at the broker (shorts negative, fractions kept)."""

    symbol: str
    qty: Decimal
    qty_available: Decimal
    avg_cost: Decimal
    price: Decimal | None          # None when the broker mark is invalid
    price_ok: bool
    market_value: Decimal
    lastday_price: Decimal | None
    asset_id: str | None = None
    meta: PositionMeta | None = None

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def is_fractional(self) -> bool:
        return self.qty != self.qty.to_integral_value()


@dataclass(frozen=True)
class OptionLeg:
    occ_symbol: str
    underlying: str
    right: str                     # "call" | "put"
    strike: Decimal
    expiry: date
    qty: Decimal                   # signed; short legs negative
    multiplier: int
    avg_cost: Decimal              # per share premium
    price: Decimal | None
    price_ok: bool
    market_value: Decimal
    # False when the broker omitted market_value (reconcile then carries 0). Read only by the
    # drawdown close trust check: a missing short-leg liability must not be read as 0.
    market_value_reported: bool = True
    # True when the broker sent a market_value that is not a finite number (not merely absent).
    market_value_malformed: bool = False

    @property
    def side(self) -> str:
        return "short" if self.qty < 0 else "long"


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    client_order_id: str | None
    symbol: str
    side: str                      # buy | sell
    type: str
    qty: Decimal
    filled_qty: Decimal
    remaining_qty: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    time_in_force: str
    asset_class: str
    submitted_at: datetime | None
    expires_at: datetime | None = None
    order_class: str = ""
    position_intent: str | None = None   # buy_to_open | buy_to_close | sell_to_open | sell_to_close
    legs: tuple["OpenOrder", ...] = ()


@dataclass(frozen=True)
class BookSnapshot:
    """Broker truth at ``as_of`` joined with local metadata. Immutable."""

    as_of: datetime
    run_id: str
    equity: Decimal
    cash: Decimal
    long_mv: Decimal
    short_mv: Decimal              # negative or zero (broker convention)
    options_mv: Decimal            # net of long and short legs
    last_equity: Decimal | None    # broker field; UNTRUSTED (omits positions on this account)
    buying_power: Decimal | None
    holdings: tuple[Holding, ...]
    legs: tuple[OptionLeg, ...]
    open_orders: tuple[OpenOrder, ...]
    diffs: tuple[Diff, ...]
    quality: str = "live"          # live | indicative (pre/after-hours, invalid marks)
    blocked_symbols: frozenset[str] = frozenset()   # no new buys (shorts, unexplained diffs)
    account_flags: Mapping[str, Any] = field(default_factory=dict)
    # Margin figures from the broker account (None when not reported). Cash may be
    # negative: that is a debit balance (borrowed money), not an error.
    regt_buying_power: Decimal | None = None
    maintenance_margin: Decimal | None = None
    initial_margin: Decimal | None = None
    options_buying_power: Decimal | None = None
    non_marginable_buying_power: Decimal | None = None
    margin_call: bool | None = None

    @property
    def borrowed(self) -> Decimal:
        """Debit balance: money borrowed from the broker (0 when cash >= 0)."""
        return max(ZERO, -self.cash)

    @property
    def leverage(self) -> Decimal | None:
        """Gross long stock market value / equity (None when equity <= 0)."""
        return (self.long_mv / self.equity) if self.equity > 0 else None

    @property
    def margin_excess(self) -> Decimal | None:
        """Equity above the broker's maintenance requirement (None if not reported)."""
        return None if self.maintenance_margin is None else self.equity - self.maintenance_margin

    @property
    def margin_excess_pct(self) -> Decimal | None:
        ex = self.margin_excess
        return (ex / self.equity) if ex is not None and self.equity > 0 else None

    def holding(self, symbol: str) -> Holding | None:
        for h in self.holdings:
            if h.symbol == symbol:
                return h
        return None

    def legs_for(self, underlying: str) -> tuple[OptionLeg, ...]:
        return tuple(leg for leg in self.legs if leg.underlying == underlying)

    def open_orders_for(self, symbol: str) -> tuple[OpenOrder, ...]:
        return tuple(o for o in self.open_orders if o.symbol == symbol)

    @property
    def gross_exposure_pct(self) -> Decimal:
        if self.equity <= 0:
            return ZERO
        return (self.long_mv + abs(self.short_mv)) / self.equity

    def age_seconds(self, now: datetime) -> float:
        return (now - self.as_of).total_seconds()


# =========================================================================
# Theses
# =========================================================================
class ThesisRole(StrEnum):
    ENTRY_CANDIDATE = "entry_candidate"
    HELD_REVIEW = "held_review"
    EXIT_CONFIRMATION = "exit_confirmation"
    LEGACY_UNVERIFIED = "legacy_unverified"


NON_ACTIONABLE_ROLES = frozenset({ThesisRole.EXIT_CONFIRMATION, ThesisRole.LEGACY_UNVERIFIED})


@dataclass(frozen=True)
class ThesisRecord:
    thesis_id: str
    symbol: str
    role: ThesisRole
    created_at: datetime
    generated_at: datetime | None
    source: str
    decision_id: str | None
    supersedes: str | None
    payload: Mapping[str, Any]

    @property
    def actionable(self) -> bool:
        return self.role not in NON_ACTIONABLE_ROLES

    def age(self, now: datetime) -> timedelta:
        return now - (self.generated_at or self.created_at)


def filled_thesis_grade(thesis: ThesisRecord | None) -> int | None:
    """Read a documented grade, including a verified v0 entry, never a placeholder."""
    if thesis is None or not thesis.actionable:
        return None
    if thesis.source == "v0" or thesis.payload.get("legacy_v0") is True:
        if thesis.source != "v0" or thesis.role != ThesisRole.ENTRY_CANDIDATE or (
            thesis.payload.get("legacy_v0") is not True
        ):
            return None
        grade = thesis.payload.get("conviction")
    else:
        grade = thesis.payload.get(
            "raw_conviction" if (thesis.payload.get("adjustment_reason") == "owner_directive"
                                 or "owner_directive_added" in thesis.payload)
            else "effective_conviction",
        )
    return grade if type(grade) is int and 1 <= grade <= 10 else None


# =========================================================================
# Order intents and job runs
# =========================================================================
class IntentStatus(StrEnum):
    QUEUED = "queued"
    SKIPPED = "skipped"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    AMBIGUOUS = "ambiguous"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_INTENT = frozenset({IntentStatus.SKIPPED, IntentStatus.FILLED, IntentStatus.REJECTED,
                             IntentStatus.EXPIRED, IntentStatus.CANCELLED})

# allowed transitions (from -> set of to)
INTENT_TRANSITIONS: dict[IntentStatus, frozenset[IntentStatus]] = {
    IntentStatus.QUEUED: frozenset({IntentStatus.SUBMITTING, IntentStatus.SKIPPED, IntentStatus.EXPIRED,
                                    IntentStatus.CANCELLED, IntentStatus.QUEUED}),
    IntentStatus.SUBMITTING: frozenset({IntentStatus.SUBMITTED, IntentStatus.AMBIGUOUS, IntentStatus.REJECTED,
                                        IntentStatus.FILLED, IntentStatus.PARTIALLY_FILLED, IntentStatus.QUEUED}),
    IntentStatus.AMBIGUOUS: frozenset({IntentStatus.SUBMITTED, IntentStatus.QUEUED, IntentStatus.FILLED,
                                       IntentStatus.PARTIALLY_FILLED, IntentStatus.CANCELLED, IntentStatus.EXPIRED,
                                       IntentStatus.REJECTED}),
    IntentStatus.SUBMITTED: frozenset({IntentStatus.PARTIALLY_FILLED, IntentStatus.FILLED, IntentStatus.CANCELLED,
                                       IntentStatus.EXPIRED, IntentStatus.REJECTED}),
    IntentStatus.PARTIALLY_FILLED: frozenset({IntentStatus.PARTIALLY_FILLED, IntentStatus.FILLED,
                                              IntentStatus.CANCELLED, IntentStatus.EXPIRED}),
}


@dataclass(frozen=True)
class OrderIntent:
    """A decision to trade, persisted before any order is sent. No quantity:
    it is sized at execution time from a fresh snapshot."""

    intent_id: str
    client_order_id: str           # stable; reused on any resubmission
    dedupe_key: str                # unique (job, window, symbol, side) or caller-supplied
    symbol: str
    side: str                      # buy | sell
    status: IntentStatus
    created_at: datetime
    kind: str = "equity"           # equity | option_structure | cover | trim | stop
    decision_id: str | None = None
    job: str | None = None
    window_key: str | None = None
    position_id: str | None = None # required for exits
    thesis_ids: tuple[str, ...] = ()
    reference_price: Decimal | None = None
    evidence_ids: tuple[str, ...] = ()
    origin: str | None = None      # scheduled | news | repair | legacy
    sleeve: str | None = None      # CORE_EQUITY | NEWS_EQUITY | OPTIONS
    session_date: date | None = None
    expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    broker_order_id: str | None = None
    attempts: int = 0
    last_error: str | None = None
    updated_at: datetime | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_INTENT


@dataclass(frozen=True)
class NewIntent:
    """Input to ``enqueue_intents``."""

    symbol: str
    side: str
    dedupe_key: str
    kind: str = "equity"
    decision_id: str | None = None
    position_id: str | None = None
    thesis_ids: tuple[str, ...] = ()
    reference_price: Decimal | None = None
    evidence_ids: tuple[str, ...] = ()
    origin: str | None = None
    sleeve: str | None = None
    session_date: date | None = None
    expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    status: IntentStatus = IntentStatus.QUEUED   # QUEUED or SKIPPED (record a refused opportunity)
    last_error: str | None = None                # reason when SKIPPED
    meta: Mapping[str, Any] = field(default_factory=dict)


class JobStatus(StrEnum):
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class JobRun:
    job: str
    window_key: str
    status: JobStatus
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    eligible_retry_at: datetime | None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EquityMark:
    session_date: date
    quality: str                   # close | indicative | legacy
    equity: Decimal
    cash: Decimal | None
    long_mv: Decimal | None
    short_mv: Decimal | None
    options_mv: Decimal | None
    taken_at: datetime
    extra: Mapping[str, Any] = field(default_factory=dict)   # e.g. maintenance_margin at the mark

    @property
    def borrowed(self) -> Decimal | None:
        return None if self.cash is None else max(ZERO, -self.cash)

    @property
    def leverage(self) -> Decimal | None:
        return (self.long_mv / self.equity) if self.long_mv is not None and self.equity > 0 else None


@dataclass(frozen=True)
class CashFlow:
    activity_id: str
    date: date
    amount: Decimal                # + deposit, - withdrawal
    kind: str                      # JNLC | CSD | CSW | JNL | ...


# =========================================================================
# helpers
# =========================================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | date | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime) and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def _json_obj(raw: Any) -> dict[str, Any]:
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _dec(x: Any) -> Decimal | None:
    if x is None or x == "":
        return None
    return Decimal(str(x))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def client_order_id_for(intent_id: str) -> str:
    """Stable client order id (<= 40 chars). Same intent -> same id, always."""
    h = hashlib.sha1(intent_id.encode()).hexdigest()[:24]
    return f"sa-{h}"


# =========================================================================
# Ledger facade
# =========================================================================
class Ledger:
    """Facade used by every other area. Construct once in the scheduler."""

    def __init__(self, store: StateStore, journal: Journal, *, broker: Any = None,
                 max_age_s: int = 120, clock=None, alert=None):
        self.store = store
        self.journal = journal
        self.broker = broker
        self.max_age_s = max_age_s
        self._clock = clock or _now
        self._alert = alert              # callable(severity, title, body, key=None)
        self._snapshot: BookSnapshot | None = None
        self._refresh_lock: asyncio.Lock | None = None
        self._refresh_starts = 0
        self._alert_tasks: set = set()
        self._listeners: list[Callable[[BookSnapshot], Any]] = []
        self._exit_fill_handler: Callable[..., Any] | None = None
        self._snapshot_start_seq = -1

    def now(self) -> datetime:
        return self._clock()

    # --------------------------------------------------------------- book
    async def refresh(self, reason: str, *, rebuild: bool = False) -> BookSnapshot:
        """Reconcile with the broker now. Raises on any broker read failure
        (the ledger is left untouched); never treats a failure as empty."""
        from stock_agent.reconcile import reconcile

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        arrival = self._refresh_starts
        async with self._refresh_lock:
            # coalesce: a refresh that STARTED after we asked and succeeded is as fresh as ours
            if not rebuild and self._snapshot is not None and self._snapshot_start_seq >= arrival:
                return self._snapshot
            seq = self._refresh_starts
            self._refresh_starts += 1
            snap = await reconcile(self, reason=reason, rebuild=rebuild)
            self._snapshot = snap
            self._snapshot_start_seq = seq
            for fn in list(self._listeners):
                try:
                    fn(snap)
                except Exception as e:  # a view must never break the books
                    logger.error("snapshot listener failed: %s", e)
            return snap

    def register_symbol_alias(self, old: str, new: str, *, source: str, effective: date | None = None) -> None:
        """Record a VERIFIED rename (e.g. AssetRegistry corporate action BK -> BNY).

        The next reconcile moves the open position's metadata from ``old`` to
        ``new`` only if: ``old`` is no longer held at the broker, ``new`` is
        held, ``new`` has no open metadata of its own, and the quantity carried
        over exactly. Otherwise nothing moves (normal import/close rules apply).
        """
        old, new = old.strip().upper(), new.strip().upper()
        if not old or not new or old == new:
            raise LedgerError("alias needs two different symbols")
        aliases = dict(self.store.kv_get("symbol_aliases", {}) or {})
        aliases[old] = {"new": new, "source": source, "effective": effective.isoformat() if effective else None,
                        "registered_at": _iso(self.now())}
        self.store.kv_set("symbol_aliases", aliases)
        self.journal.append("state_event", event="symbol_alias", old=old, new=new, source=source,
                            effective=effective)

    def set_exit_fill_handler(self, fn: Callable[..., Any] | None) -> None:
        """Trade alert for broker stop fills the bot did not submit in this process.

        Called as ``fn(symbol, qty, fill_price, *, realized_pnl, hold_days, order_id)``,
        the shape of ``AlertService.alert_stop_filled``. Without a handler the
        generic ledger alert path is used (WARNING, key ``stop_filled:SYM:order``); other
        broker-side exits (orders placed outside the bot) always use the generic path."""
        self._exit_fill_handler = fn

    def _raise_exit_fill(self, *, symbol: str, qty: Decimal, fill_price: Decimal, kind: str,
                         realized_pnl: Decimal | None, hold_days: int | None, order_id: str | None) -> None:
        self.journal.append("exit.broker_fill", symbol=symbol, qty=qty, fill_price=fill_price, exit_kind=kind,
                            realized_pnl=realized_pnl, hold_days=hold_days, order_id=order_id)
        q = int(qty) if qty == qty.to_integral_value() else float(qty)
        pnl = None if realized_pnl is None else float(realized_pnl)
        if self._exit_fill_handler is not None and kind == "stop":
            try:
                res = self._exit_fill_handler(symbol, q, float(fill_price), realized_pnl=pnl, hold_days=hold_days,
                                              order_id=order_id)
                if inspect.isawaitable(res):
                    task = asyncio.get_running_loop().create_task(res)
                    self._alert_tasks.add(task)
                    task.add_done_callback(self._alert_tasks.discard)
                return
            except Exception as e:  # fall back to the generic path
                logger.error("exit fill alert failed: %s", e)
        what = "stop filled" if kind == "stop" else "broker-side exit filled"
        body = f"{what}: sold {q} {symbol} at {float(fill_price):.2f}."
        if pnl is not None:
            body += f" Realised P&L {pnl:+.2f}."
        if hold_days is not None:
            body += f" Held {hold_days} days."
        self._raise_alert(Severity.WARNING, f"Stop-loss triggered on {symbol}" if kind == "stop"
                          else f"Position reduced at the broker: {symbol}", body,
                          key=f"stop_filled:{symbol}:{order_id}" if order_id else None)

    def add_snapshot_listener(self, fn: Callable[[BookSnapshot], Any]) -> None:
        """Call ``fn(snapshot)`` after a successful refresh; log listener exceptions."""
        self._listeners.append(fn)

    def snapshot(self, max_age_s: int | None = None) -> BookSnapshot:
        """The last reconciled snapshot, if fresh enough. Raises ``StaleBook``."""
        if self._snapshot is None:
            raise StaleBook("no reconciled snapshot yet")
        limit = self.max_age_s if max_age_s is None else max_age_s
        age = self._snapshot.age_seconds(self.now())
        if age > limit:
            raise StaleBook(f"snapshot is {age:.0f}s old (limit {limit}s)")
        return self._snapshot

    def last_snapshot(self) -> BookSnapshot | None:
        """Last snapshot regardless of age (for reports only, never for orders)."""
        return self._snapshot

    def trading_block(self) -> str | None:
        return self.store.trading_block()

    def last_reconcile(self) -> Mapping[str, Any] | None:
        rows = self.store.query("SELECT * FROM reconcile_runs ORDER BY at DESC, rowid DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        report = json.loads(r["report_json"])
        # ok = the broker read succeeded; clean = no critical diff (other than per-symbol shorts)
        return {"run_id": r["run_id"], "at": _dt(r["at"]), "reason": r["reason"], "ok": bool(r["ok"]),
                "clean": bool(report.get("clean", True)), "n_diffs": r["n_diffs"], "report": report}

    # ------------------------------------------------------ position meta
    def _row_to_meta(self, r) -> PositionMeta:
        return PositionMeta(
            position_id=r["position_id"], symbol=r["symbol"], asset_class=r["asset_class"],
            opened_at=_dt(r["opened_at"]), opened_source=r["opened_source"], sector=r["sector"],
            stop_price=_dec(r["stop_price"]), stop_order_id=r["stop_order_id"],
            stop_expires_at=_dt(r["stop_expires_at"]), entry_thesis_id=r["entry_thesis_id"],
            entry_intent_id=r["entry_intent_id"], notes=json.loads(r["notes_json"] or "{}"),
        )

    def open_position_meta(self, symbol: str) -> PositionMeta | None:
        rows = self.store.query(
            "SELECT * FROM positions_meta WHERE symbol=? AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1",
            (symbol,))
        return self._row_to_meta(rows[0]) if rows else None

    def position_meta(self, position_id: str) -> PositionMeta | None:
        rows = self.store.query("SELECT * FROM positions_meta WHERE position_id=?", (position_id,))
        return self._row_to_meta(rows[0]) if rows else None

    def all_open_meta(self) -> list[PositionMeta]:
        rows = self.store.query("SELECT * FROM positions_meta WHERE closed_at IS NULL ORDER BY symbol")
        return [self._row_to_meta(r) for r in rows]

    def _update_meta(self, position_id: str, **cols: Any) -> None:
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = [(_iso(v) if isinstance(v, (datetime, date)) else (str(v) if isinstance(v, Decimal) else v))
                for v in cols.values()]
        with self.store.transaction() as c:
            cur = c.execute(f"UPDATE positions_meta SET {sets} WHERE position_id=?", (*vals, position_id))
            if cur.rowcount != 1:
                raise LedgerError(f"unknown position_id {position_id}")

    def set_stop(self, position_id: str, stop_price: Decimal | float | None, stop_order_id: str | None,
                 stop_expires_at: datetime | None = None) -> None:
        """broker_execution: record the live broker stop for a position."""
        self._update_meta(position_id, stop_price=None if stop_price is None else float(stop_price),
                          stop_order_id=stop_order_id, stop_expires_at=stop_expires_at)
        self.journal.append("state_event", event="stop_set", position_id=position_id,
                            stop_price=stop_price, stop_order_id=stop_order_id)

    def set_sector(self, position_id: str, sector: str) -> None:
        self._update_meta(position_id, sector=sector)

    def set_entry_thesis(self, position_id: str, thesis_id: str | None) -> None:
        self._update_meta(position_id, entry_thesis_id=thesis_id)

    def update_position_notes(self, position_id: str, **notes: Any) -> None:
        meta = self.position_meta(position_id)
        if meta is None:
            raise LedgerError(f"unknown position_id {position_id}")
        merged = dict(meta.notes)
        merged.update(notes)
        self._update_meta(position_id, notes_json=json.dumps(merged, default=str))

    # -------------------------------------------------------------- theses
    def record_thesis(self, symbol: str, payload: Mapping[str, Any], role: ThesisRole | str, *,
                      source: str = "", decision_id: str | None = None,
                      generated_at: datetime | None = None, activate: bool = False) -> str:
        """Append a thesis (never modified later). Returns thesis_id.
        ``activate=True`` also makes it the current thesis (same transaction)."""
        role = ThesisRole(role)
        if activate and role in NON_ACTIONABLE_ROLES:
            raise LedgerError(f"a {role.value} thesis cannot be made current")
        tid = new_id("th")
        now = self.now()
        with self.store.transaction() as c:
            prev = c.execute("SELECT thesis_id FROM current_thesis WHERE symbol=?", (symbol,)).fetchone()
            c.execute(
                "INSERT INTO theses(thesis_id, symbol, role, created_at, generated_at, source, decision_id,"
                " supersedes, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, symbol, role.value, _iso(now), _iso(generated_at), source, decision_id,
                 prev[0] if (prev and activate) else None, json.dumps(payload, default=str)))
            if activate:
                c.execute("INSERT OR REPLACE INTO current_thesis(symbol, thesis_id, set_at) VALUES (?,?,?)",
                          (symbol, tid, _iso(now)))
        self.journal.append("thesis", thesis_id=tid, symbol=symbol, role=role.value, source=source,
                            decision_id=decision_id, activated=activate, generated_at=generated_at,
                            payload=payload)
        return tid

    def set_current_thesis(self, symbol: str, thesis_id: str) -> None:
        th = self.get_thesis(thesis_id)
        if th is None or th.symbol != symbol:
            raise LedgerError(f"thesis {thesis_id} not found for {symbol}")
        if not th.actionable:
            raise LedgerError(f"a {th.role.value} thesis cannot be made current")
        with self.store.transaction() as c:
            c.execute("INSERT OR REPLACE INTO current_thesis(symbol, thesis_id, set_at) VALUES (?,?,?)",
                      (symbol, thesis_id, _iso(self.now())))
        self.journal.append("thesis", event="set_current", symbol=symbol, thesis_id=thesis_id)

    def _row_to_thesis(self, r) -> ThesisRecord:
        return ThesisRecord(
            thesis_id=r["thesis_id"], symbol=r["symbol"], role=ThesisRole(r["role"]),
            created_at=_dt(r["created_at"]), generated_at=_dt(r["generated_at"]), source=r["source"],
            decision_id=r["decision_id"], supersedes=r["supersedes"], payload=json.loads(r["payload_json"]))

    def get_thesis(self, thesis_id: str) -> ThesisRecord | None:
        rows = self.store.query("SELECT * FROM theses WHERE thesis_id=?", (thesis_id,))
        return self._row_to_thesis(rows[0]) if rows else None

    def current_thesis(self, symbol: str, max_age: timedelta | None = None) -> ThesisRecord | None:
        """The actionable current thesis, or None (also None when older than max_age)."""
        rows = self.store.query(
            "SELECT t.* FROM current_thesis c JOIN theses t ON t.thesis_id=c.thesis_id WHERE c.symbol=?",
            (symbol,))
        if not rows:
            return None
        th = self._row_to_thesis(rows[0])
        if max_age is not None and th.age(self.now()) > max_age:
            return None
        return th

    def entry_thesis(self, position_id: str) -> ThesisRecord | None:
        meta = self.position_meta(position_id)
        if meta is None or not meta.entry_thesis_id:
            return None
        return self.get_thesis(meta.entry_thesis_id)

    def _last_filled_buy(self, position_id: str):
        rows = self.store.query(
            "SELECT thesis_ids_json FROM order_intents WHERE position_id=? AND kind='equity' AND side='buy'"
            " AND status IN ('filled','partially_filled') ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (position_id,))
        return rows[0] if rows else None

    def last_filled_thesis(self, position_id: str) -> ThesisRecord | None:
        """Thesis of the most recent filled buy intent for this position."""
        fill = self._last_filled_buy(position_id)
        if fill is None:
            return self.entry_thesis(position_id)
        ids = json.loads(fill[0] or "[]")
        return self.get_thesis(ids[0]) if ids else None

    def top_up_baseline(self, position_id: str) -> tuple[int | None, bool]:
        """Return the actual fill grade and whether an adopted holding may initialize one."""
        fill = self._last_filled_buy(position_id)
        if fill is not None:
            ids = json.loads(fill[0] or "[]")
            return filled_thesis_grade(self.get_thesis(ids[0]) if ids else None), False
        meta = self.position_meta(position_id)
        entry = self.get_thesis(meta.entry_thesis_id) if meta and meta.entry_thesis_id else None
        grade = filled_thesis_grade(entry)
        may_initialize = (
            grade is None and meta is not None and meta.entry_intent_id is None
            # Reconcile may verify a migrated entry date from broker fills;
            # that does not supply the legacy research conviction.
            and meta.opened_source in {"v0", "v0_verified", "import", "assignment"}
            and (entry is None or entry.source == "v0")
        )
        return grade, may_initialize

    def theses_for(self, symbol: str, limit: int = 20) -> list[ThesisRecord]:
        rows = self.store.query("SELECT * FROM theses WHERE symbol=? ORDER BY created_at DESC LIMIT ?",
                                (symbol, limit))
        return [self._row_to_thesis(r) for r in rows]

    # -------------------------------------------------------------- intents
    def _row_to_intent(self, r) -> OrderIntent:
        return OrderIntent(
            intent_id=r["intent_id"], client_order_id=r["client_order_id"], dedupe_key=r["dedupe_key"],
            symbol=r["symbol"], side=r["side"], status=IntentStatus(r["status"]),
            created_at=_dt(r["created_at"]), kind=r["kind"], decision_id=r["decision_id"], job=r["job"],
            window_key=r["window_key"], position_id=r["position_id"],
            thesis_ids=tuple(json.loads(r["thesis_ids_json"] or "[]")),
            reference_price=_dec(r["reference_price"]),
            evidence_ids=tuple(json.loads(r["evidence_ids_json"] or "[]")), origin=r["origin"],
            sleeve=r["sleeve"], session_date=_d(r["session_date"]), expires_at=_dt(r["expires_at"]),
            next_attempt_at=_dt(r["next_attempt_at"]), broker_order_id=r["broker_order_id"],
            attempts=r["attempts"], last_error=r["last_error"], updated_at=_dt(r["updated_at"]),
            meta=json.loads(r["meta_json"] or "{}"))

    def _insert_intents(self, c, items: Sequence[NewIntent], job: str | None, window_key: str | None
                        ) -> list[str]:
        ids: list[str] = []
        now = _iso(self.now())
        for it in items:
            if it.status not in (IntentStatus.QUEUED, IntentStatus.SKIPPED):
                raise LedgerError("new intents must be queued or skipped")
            if it.side not in ("buy", "sell"):
                raise LedgerError(f"bad side {it.side!r}")
            existing = c.execute("SELECT intent_id FROM order_intents WHERE dedupe_key=?",
                                 (it.dedupe_key,)).fetchone()
            if existing:
                ids.append(existing[0])
                continue
            iid = new_id("in")
            c.execute(
                "INSERT INTO order_intents(intent_id, client_order_id, dedupe_key, decision_id, job, window_key,"
                " symbol, side, kind, position_id, thesis_ids_json, reference_price, evidence_ids_json, origin,"
                " sleeve, created_at, session_date, expires_at, next_attempt_at, status, attempts, last_error,"
                " updated_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
                (iid, client_order_id_for(iid), it.dedupe_key, it.decision_id, job, window_key, it.symbol,
                 it.side, it.kind, it.position_id, json.dumps(list(it.thesis_ids)),
                 None if it.reference_price is None else float(it.reference_price),
                 json.dumps(list(it.evidence_ids)), it.origin, it.sleeve, now, _iso(it.session_date),
                 _iso(it.expires_at), _iso(it.next_attempt_at), it.status.value, it.last_error, now,
                 json.dumps(dict(it.meta), default=str)))
            ids.append(iid)
        return ids

    def enqueue_intents(self, items: Sequence[NewIntent], *, job: str | None = None,
                        window_key: str | None = None) -> list[str]:
        """Persist intents atomically. Idempotent on ``dedupe_key`` (returns the existing id)."""
        with self.store.transaction() as c:
            ids = self._insert_intents(c, items, job, window_key)
        for iid, it in zip(ids, items):
            self.journal.append("intent", event="enqueued", intent_id=iid, symbol=it.symbol, side=it.side,
                                status=it.status.value, decision_id=it.decision_id, dedupe_key=it.dedupe_key,
                                thesis_ids=it.thesis_ids, origin=it.origin, sleeve=it.sleeve,
                                reason=it.last_error, meta=it.meta)
        return ids

    def get_intent(self, intent_id: str) -> OrderIntent | None:
        rows = self.store.query("SELECT * FROM order_intents WHERE intent_id=?", (intent_id,))
        return self._row_to_intent(rows[0]) if rows else None

    def intent_by_client_order_id(self, client_order_id: str) -> OrderIntent | None:
        rows = self.store.query("SELECT * FROM order_intents WHERE client_order_id=?", (client_order_id,))
        return self._row_to_intent(rows[0]) if rows else None

    def list_intents(self, statuses: Iterable[IntentStatus | str] | None = None, *,
                     symbol: str | None = None, origin: str | None = None) -> list[OrderIntent]:
        sql, params = "SELECT * FROM order_intents WHERE 1=1", []
        if statuses is not None:
            st = [IntentStatus(s).value for s in statuses]
            sql += f" AND status IN ({','.join('?' * len(st))})"
            params += st
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if origin is not None:
            sql += " AND origin=?"
            params.append(origin)
        sql += " ORDER BY created_at"
        return [self._row_to_intent(r) for r in self.store.query(sql, tuple(params))]

    def due_intents(self, now: datetime | None = None) -> list[OrderIntent]:
        """Queued intents whose next_attempt_at has passed; expired ones are marked EXPIRED first."""
        now = now or self.now()
        self.expire_intents(now)
        out = []
        for it in self.list_intents([IntentStatus.QUEUED]):
            if it.next_attempt_at is None or it.next_attempt_at <= now:
                out.append(it)
        return out

    def expire_intents(self, now: datetime | None = None) -> int:
        now = now or self.now()
        n = 0
        for it in self.list_intents([IntentStatus.QUEUED]):
            if it.expires_at is not None and it.expires_at <= now:
                self._transition(it.intent_id, IntentStatus.EXPIRED, last_error="expired before submission")
                n += 1
        return n

    def _transition(self, intent_id: str, to: IntentStatus, *, broker_order_id: str | None = None,
                    last_error: str | None = None, next_attempt_at: datetime | None = None,
                    bump_attempts: bool = False, meta: Mapping[str, Any] | None = None) -> OrderIntent:
        with self.store.transaction() as c:
            r = c.execute("SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if r is None:
                raise LedgerError(f"unknown intent {intent_id}")
            cur = IntentStatus(r["status"])
            if to not in INTENT_TRANSITIONS.get(cur, frozenset()):
                raise LedgerError(f"illegal intent transition {cur.value} -> {to.value}")
            m = json.loads(r["meta_json"] or "{}")
            if meta:
                m.update(meta)
            c.execute(
                "UPDATE order_intents SET status=?, broker_order_id=COALESCE(?, broker_order_id),"
                " last_error=COALESCE(?, last_error), next_attempt_at=?, attempts=attempts+?, updated_at=?,"
                " meta_json=? WHERE intent_id=?",
                (to.value, broker_order_id, last_error, _iso(next_attempt_at), 1 if bump_attempts else 0,
                 _iso(self.now()), json.dumps(m, default=str), intent_id))
            r2 = c.execute("SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
        it = self._row_to_intent(r2)
        self.journal.append("intent", event="status", intent_id=intent_id, symbol=it.symbol, side=it.side,
                            status_from=cur.value, status=to.value, broker_order_id=broker_order_id,
                            error=last_error, attempts=it.attempts)
        return it

    @staticmethod
    def _owner_key(symbol: str, added: date, lane: str) -> str:
        return f"owner_directive.{lane}.{symbol.strip().upper()}.{added.isoformat()}"

    def owner_directive_vetoed(self, symbol: str, added: date) -> bool:
        return bool(self.store.query("SELECT 1 FROM kv WHERE key=?", (self._owner_key(symbol, added, "veto"),)))

    def _owner_vetoed_in(self, c, symbol: str, added: date) -> bool:
        return c.execute("SELECT 1 FROM kv WHERE key=?",
                         (self._owner_key(symbol, added, "veto"),)).fetchone() is not None

    def latch_owner_directive_veto(self, symbol: str, added: date) -> bool:
        with self.store.transaction() as c:
            return c.execute(
                "INSERT OR IGNORE INTO kv(key,value_json,updated_at) VALUES (?,'true',?)",
                (self._owner_key(symbol, added, "veto"), _iso(self.now())),
            ).rowcount == 1

    def mark_owner_stock_uncertainty_notified(self, symbol: str, added: date) -> bool:
        with self.store.transaction() as c:
            return c.execute(
                "INSERT OR IGNORE INTO kv(key,value_json,updated_at) VALUES (?,'true',?)",
                (self._owner_key(symbol, added, "stock_uncertainty_notified"), _iso(self.now())),
            ).rowcount == 1

    @staticmethod
    def _owner_stock_used(c, symbol: str, added: date, reservation_id: str | None) -> bool:
        return c.execute(
            """SELECT 1 FROM order_intents WHERE symbol=? AND kind='equity' AND side='buy'
               AND json_extract(meta_json, '$.owner_stock_claim')=?
               AND (status='filled' OR CAST(json_extract(meta_json, '$.filled_qty') AS REAL)>0
                    OR status IN ('submitted','ambiguous','partially_filled')
                    OR (status IN ('queued','submitting') AND intent_id<>COALESCE(?,''))) LIMIT 1""",
            (symbol, added.isoformat(), reservation_id),
        ).fetchone() is not None

    def owner_directive_used(self, symbol: str, added: date, *, lane: str = "stock",
                             reservation_id: str | None = None) -> bool:
        with self.store.transaction() as c:
            if lane == "stock":
                return self._owner_stock_used(c, symbol.strip().upper(), added, reservation_id)
            if lane != "option":
                return True
            row = c.execute("SELECT value_json FROM kv WHERE key=?",
                            (self._owner_key(symbol, added, lane),)).fetchone()
            return row is not None and (reservation_id is None or row[0] != json.dumps(reservation_id))

    def claim_owner_option(self, symbol: str, added: date, structure_id: str) -> bool:
        if not structure_id:
            return False
        with self.store.transaction() as c:
            if self._owner_vetoed_in(c, symbol, added):
                return False
            key = self._owner_key(symbol, added, "option")
            value = json.dumps(structure_id)
            c.execute("INSERT OR IGNORE INTO kv(key,value_json,updated_at) VALUES (?,?,?)",
                      (key, value, _iso(self.now())))
            row = c.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
            return row is not None and row[0] == value

    def release_owner_option(self, symbol: str, added: date, structure_id: str) -> bool:
        """Release the exact claim after the caller proves a terminal zero-fill."""
        if not structure_id:
            return False
        with self.store.transaction() as c:
            if self._owner_vetoed_in(c, symbol, added):
                return False
            return c.execute("DELETE FROM kv WHERE key=? AND value_json=?",
                             (self._owner_key(symbol, added, "option"), json.dumps(structure_id))).rowcount == 1

    def claim_intent(self, intent_id: str, *, check_thesis: bool = True,
                     top_up_session: date | None = None, top_up_baseline_grade: int | None = None,
                     new_stock_session: date | None = None, new_stock_limit: int | None = None,
                     owner_entry: OwnerEntryProof | None = None) -> OrderIntent:
        """Mark SUBMITTING before the HTTP call. Raises ``LedgerError`` when the
        intent is not QUEUED, is expired, or (buys) its thesis was superseded."""
        it = self.get_intent(intent_id)
        if it is None:
            raise LedgerError(f"unknown intent {intent_id}")
        if it.status != IntentStatus.QUEUED:
            raise LedgerError(f"intent {intent_id} is {it.status.value}, not queued")
        owner_marked = (it.meta or {}).get("adjustment_reason") == "owner_directive" or (
            "owner_directive_added" in (it.meta or {}) or "owner_stock_claim" in (it.meta or {})
        )
        if (owner_marked or owner_entry is not None) and (not owner_marked or owner_entry is None
                or new_stock_session is None or top_up_session is not None or not check_thesis):
            raise NewStockEntryRefused("owner_directive_invalid", "owner directive requires a verified opening buy")
        if it.expires_at is not None and it.expires_at <= self.now():
            self._transition(intent_id, IntentStatus.EXPIRED, last_error="expired before submission")
            raise LedgerError(f"intent {intent_id} expired")
        if check_thesis and it.side == "buy" and it.thesis_ids:
            cur = self.current_thesis(it.symbol)
            if cur is None or cur.thesis_id not in it.thesis_ids:
                self._transition(intent_id, IntentStatus.CANCELLED, last_error="thesis superseded")
                raise LedgerError(f"intent {intent_id}: thesis superseded")
            if owner_entry is None and (cur.payload.get("adjustment_reason") == "owner_directive"
                                        or "owner_directive_added" in cur.payload):
                raise NewStockEntryRefused("owner_directive_invalid", "owner thesis requires verified owner proof")
        if it.position_id:
            row = self.store.query("SELECT closed_at FROM positions_meta WHERE position_id=?", (it.position_id,))
            if not row or row[0][0] is not None:
                self._transition(intent_id, IntentStatus.CANCELLED, last_error="position closed or replaced")
                raise LedgerError(f"intent {intent_id}: position {it.position_id} is no longer open")
        if top_up_session is not None:
            return self._claim_top_up(it, top_up_session, top_up_baseline_grade)
        if new_stock_session is not None:
            if new_stock_limit is None or new_stock_limit < 1:
                raise LedgerError("a new-stock reservation requires a positive session limit")
            return self._claim_new_stock(it, new_stock_session, new_stock_limit, owner_entry=owner_entry)
        return self._transition(intent_id, IntentStatus.SUBMITTING, bump_attempts=True)

    def _claim_new_stock(self, it: OrderIntent, session: date, limit: int, *,
                         owner_entry: OwnerEntryProof | None = None) -> OrderIntent:
        from stock_agent.research_gate import OwnerEntryProof
        from stock_agent.risk_manager import classify_symbol

        if it.position_id or it.side != "buy" or it.kind != "equity":
            raise LedgerError("only an opening stock buy can reserve a new-stock slot")
        day = session.isoformat()
        issuer = classify_symbol(it.symbol).issuer
        with self.store.transaction() as c:
            owner_meta = {}
            if owner_entry is not None:
                cur = self.current_thesis(it.symbol)
                if (not isinstance(owner_entry, OwnerEntryProof) or owner_entry.lane != "stock"
                        or owner_entry.symbol != it.symbol or cur is None or owner_entry.thesis_id != cur.thesis_id
                        or cur.thesis_id not in it.thesis_ids or it.origin != "prescan"
                        or cur.payload.get("adjustment_reason") != "owner_directive"
                        or cur.payload.get("owner_directive_added") != owner_entry.added.isoformat()
                        or (it.meta or {}).get("owner_directive_added") != owner_entry.added.isoformat()
                        or cur.payload.get("raw_conviction") != owner_entry.raw_conviction
                        or cur.payload.get("effective_conviction") != owner_entry.effective_conviction
                        or cur.generated_at != owner_entry.generated_at):
                    raise NewStockEntryRefused("owner_directive_invalid", "owner proof no longer matches the buy")
                if self._owner_vetoed_in(c, it.symbol, owner_entry.added):
                    raise NewStockEntryRefused("owner_directive_vetoed", "owner directive received a model veto")
                if self._owner_stock_used(c, it.symbol, owner_entry.added, it.intent_id):
                    raise NewStockEntryRefused("owner_directive_used", "owner directive already reserved or filled")
                owner_meta["owner_stock_claim"] = owner_entry.added.isoformat()
            rows = c.execute(
                """SELECT symbol, session_date, status, meta_json FROM order_intents
                   WHERE kind='equity' AND side='buy' AND position_id IS NULL AND intent_id<>?
                     AND (status IN ('submitting','submitted','ambiguous','partially_filled')
                          OR (status='queued' AND json_extract(meta_json,'$.new_stock_session') IS NOT NULL)
                          OR (COALESCE(json_extract(meta_json,'$.new_stock_fill_session'),
                                       json_extract(meta_json,'$.new_stock_session'), session_date)=?
                              AND (status='filled' OR CAST(json_extract(meta_json,'$.filled_qty') AS REAL)>0)))""",
                (it.intent_id, day),
            )
            occupied = set()
            for row in rows:
                meta = json.loads(row["meta_json"] or "{}")
                if row["status"] == IntentStatus.QUEUED and meta.get("new_stock_session") != day:
                    # A confirmed unsent retry from a previous day does not reserve
                    # today; if used again it goes through this claim again.
                    continue
                occupied.add(meta.get("new_stock_issuer") or classify_symbol(row["symbol"]).issuer)
            if issuer in occupied:
                raise NewStockEntryRefused("new_stock_issuer_already_admitted",
                                           "this issuer filled today or already has an active entry reservation")
            if len(occupied) >= limit:
                raise NewStockEntryRefused("new_stock_session_cap",
                                           f"new-stock limit ({limit}) reached for this NY session (fills/reservations)")
            return self._transition(it.intent_id, IntentStatus.SUBMITTING, bump_attempts=True,
                                    meta={"new_stock_session": day, "new_stock_issuer": issuer, **owner_meta})

    def _claim_top_up(self, it: OrderIntent, session: date, baseline_grade: int | None) -> OrderIntent:
        if not it.position_id or it.side != "buy" or it.kind != "equity":
            raise LedgerError("only a buy matched to an open stock position can claim a top-up")
        session_key = session.isoformat()
        with self.store.transaction() as c:
            rows = c.execute(
                "SELECT intent_id, status, session_date, meta_json FROM order_intents"
                " WHERE position_id=? AND kind='equity' AND side='buy' AND intent_id<>?",
                (it.position_id, it.intent_id),
            )
            for row in rows:
                meta = json.loads(row["meta_json"] or "{}")
                status = IntentStatus(row["status"])
                if status in (IntentStatus.SUBMITTING, IntentStatus.SUBMITTED, IntentStatus.AMBIGUOUS,
                              IntentStatus.PARTIALLY_FILLED):
                    # A working/uncertain buy may still fill. Do not place a
                    # second order, even if the unresolved state crosses midnight.
                    raise TopUpAlreadyToday("a previous top-up still has an unresolved order")
                if meta.get("top_up_session", row["session_date"]) == session_key and (
                    status == IntentStatus.FILLED or (_dec(meta.get("filled_qty")) or Decimal(0)) > 0
                ):
                    raise TopUpAlreadyToday("this position already had a top-up fill this session")
            # The existing intent metadata is backward compatible. Claim and
            # tag in one write, before contacting the broker or releasing the stop.
            meta = {"top_up_session": session_key}
            if baseline_grade is not None:
                meta.update(top_up_baseline_provenance="first_fresh_reaffirmation",
                            top_up_baseline_grade=baseline_grade)
            return self._transition(it.intent_id, IntentStatus.SUBMITTING, bump_attempts=True, meta=meta)

    def ack_intent(self, intent_id: str, status: IntentStatus | str, *, broker_order_id: str | None = None,
                   error: str | None = None, next_attempt_at: datetime | None = None,
                   meta: Mapping[str, Any] | None = None) -> OrderIntent:
        """Record the broker outcome. Use QUEUED + next_attempt_at for a safe retry
        (only when the broker has established the order does not exist)."""
        return self._transition(intent_id, IntentStatus(status), broker_order_id=broker_order_id,
                                last_error=error, next_attempt_at=next_attempt_at, meta=meta)

    def mark_submitting_ambiguous(self) -> list[OrderIntent]:
        """Start-up: any SUBMITTING intent (crash mid-request) becomes AMBIGUOUS.
        It must be resolved with ``orders:by_client_order_id`` before any resubmission."""
        out = []
        for it in self.list_intents([IntentStatus.SUBMITTING]):
            out.append(self._transition(it.intent_id, IntentStatus.AMBIGUOUS,
                                        last_error="process stopped during submission"))
        return out

    # ----------------------------------------------------------------- jobs
    def _row_to_job(self, r) -> JobRun:
        return JobRun(job=r["job"], window_key=r["window_key"], status=JobStatus(r["status"]),
                      attempts=r["attempts"], started_at=_dt(r["started_at"]), finished_at=_dt(r["finished_at"]),
                      eligible_retry_at=_dt(r["eligible_retry_at"]), detail=json.loads(r["detail_json"] or "{}"))

    def job_status(self, job: str, window_key: str) -> JobRun | None:
        rows = self.store.query("SELECT * FROM job_runs WHERE job=? AND window_key=?", (job, window_key))
        return self._row_to_job(rows[0]) if rows else None

    def start_job(self, job: str, window_key: str) -> JobRun:
        """Record an attempt. Raises ``LedgerError`` if the window is already DONE/SKIPPED
        or a FAILED run is not yet eligible for retry."""
        now = self.now()
        with self.store.transaction() as c:
            r = c.execute("SELECT * FROM job_runs WHERE job=? AND window_key=?", (job, window_key)).fetchone()
            if r is not None:
                st = JobStatus(r["status"])
                if st in (JobStatus.DONE, JobStatus.SKIPPED):
                    raise LedgerError(f"job {job}/{window_key} already {st.value}")
                er = _dt(r["eligible_retry_at"])
                if st == JobStatus.FAILED and er is not None and er > now:
                    raise LedgerError(f"job {job}/{window_key} not eligible for retry until {er.isoformat()}")
                c.execute("UPDATE job_runs SET status='started', attempts=attempts+1, started_at=?,"
                          " finished_at=NULL WHERE job=? AND window_key=?", (_iso(now), job, window_key))
            else:
                c.execute("INSERT INTO job_runs(job, window_key, status, attempts, started_at) VALUES"
                          " (?,?,'started',1,?)", (job, window_key, _iso(now)))
            r2 = c.execute("SELECT * FROM job_runs WHERE job=? AND window_key=?", (job, window_key)).fetchone()
        return self._row_to_job(r2)

    def finish_job(self, job: str, window_key: str, status: JobStatus | str, *,
                   intents: Sequence[NewIntent] = (), detail: Mapping[str, Any] | None = None,
                   eligible_retry_at: datetime | None = None) -> list[str]:
        """Atomically finish a job and enqueue its intents. Returns intent ids."""
        status = JobStatus(status)
        with self.store.transaction() as c:
            cur = c.execute("UPDATE job_runs SET status=?, finished_at=?, eligible_retry_at=?, detail_json=?"
                            " WHERE job=? AND window_key=?",
                            (status.value, _iso(self.now()), _iso(eligible_retry_at),
                             json.dumps(dict(detail or {}), default=str), job, window_key))
            if cur.rowcount != 1:
                raise LedgerError(f"finish_job without start_job: {job}/{window_key}")
            ids = self._insert_intents(c, intents, job, window_key)
        self.journal.append("decision", event="job_finished", job=job, window_key=window_key,
                            status=status.value, intent_ids=ids, detail=detail)
        return ids

    # ------------------------------------------------- equity, flows, risk
    def record_equity_mark(self, session_date: date, quality: str, snapshot: BookSnapshot,
                           extra: Mapping[str, Any] | None = None) -> None:
        with self.store.transaction() as c:
            c.execute(
                "INSERT OR REPLACE INTO equity_marks(session_date, quality, equity, cash, long_mv, short_mv,"
                " options_mv, taken_at, extra_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (session_date.isoformat(), quality, str(snapshot.equity), str(snapshot.cash),
                 str(snapshot.long_mv), str(snapshot.short_mv), str(snapshot.options_mv),
                 _iso(snapshot.as_of), json.dumps(
                     {"maintenance_margin": snapshot.maintenance_margin,
                      "regt_buying_power": snapshot.regt_buying_power} | dict(extra or {}), default=str)))
        self.journal.append("state_event", event="equity_mark", session_date=session_date, quality=quality,
                            run_id=snapshot.run_id)

    def equity_marks(self, quality: str | None = "close", since: date | None = None) -> list[EquityMark]:
        sql, p = "SELECT * FROM equity_marks WHERE 1=1", []
        if quality:
            sql += " AND quality=?"
            p.append(quality)
        if since:
            sql += " AND session_date>=?"
            p.append(since.isoformat())
        sql += " ORDER BY session_date"
        return [EquityMark(session_date=_d(r["session_date"]), quality=r["quality"], equity=Decimal(str(r["equity"])),
                           cash=_dec(r["cash"]), long_mv=_dec(r["long_mv"]), short_mv=_dec(r["short_mv"]),
                           options_mv=_dec(r["options_mv"]), taken_at=_dt(r["taken_at"]),
                           extra=_json_obj(r["extra_json"]))
                for r in self.store.query(sql, tuple(p))]

    def cash_flows(self) -> list[CashFlow]:
        return [CashFlow(activity_id=r["activity_id"], date=_d(r["date"]), amount=Decimal(str(r["amount"])),
                         kind=r["kind"])
                for r in self.store.query("SELECT * FROM cash_flows ORDER BY date")]

    def risk_state_get(self, key: str, default: Any = None) -> Any:
        rows = self.store.query("SELECT value_json FROM risk_state WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def risk_state_set(self, key: str, value: Any) -> None:
        with self.store.transaction() as c:
            c.execute("INSERT OR REPLACE INTO risk_state(key, value_json, updated_at) VALUES (?,?,?)",
                      (key, json.dumps(value, default=str), _iso(self.now())))

    # ------------------------------------------------------------- alerts
    def _raise_alert(self, severity: Severity, title: str, body: str, key: str | None = None) -> None:
        self.journal.append("alert", severity=severity.value, title=title, body=body, key=key)
        if self._alert is not None:
            try:
                res = self._alert(severity.value, title, body, key=key)
                if inspect.isawaitable(res):
                    # an async alert callable is scheduled, never left un-awaited
                    try:
                        task = asyncio.get_running_loop().create_task(res)
                        self._alert_tasks.add(task)
                        task.add_done_callback(self._alert_tasks.discard)
                    except RuntimeError:
                        res.close() if hasattr(res, "close") else None
                        logger.error("async alert dropped (no running loop): %s", title)
            except Exception as e:  # alerting must not break the books
                logger.error("alert delivery failed: %s", e)


__all__ = [
    "BookSnapshot", "CashFlow", "Diff", "DiffKind", "EquityMark", "Holding", "IntentStatus", "JobRun",
    "JobStatus", "Ledger", "LedgerError", "NewIntent", "NON_ACTIONABLE_ROLES", "OpenOrder", "OptionLeg",
    "OrderIntent", "PositionMeta", "Severity", "StaleBook", "StateUnavailable", "TERMINAL_INTENT",
    "ThesisRecord", "ThesisRole", "TopUpAlreadyToday", "client_order_id_for", "filled_thesis_grade", "new_id", "replace",
]
