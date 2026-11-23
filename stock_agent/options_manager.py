"""Options manager: scan for entries, monitor and exit, reconcile, repair.

One instance, built by the scheduler with the shared services. Every broker
call goes through `stock_agent.broker.AlpacaClient` (raises on failure).

Mechanics this module guarantees:
- Spreads are one atomic multi-leg limit order (positive limit = debit), legs
  with `position_intent`; single options are simple limit orders.
- Fills are read back from the broker; entry debit and exit credit come from
  fills, never from the quote.
- The book is written before the order is sent (write-ahead with a
  deterministic client order id), so a crash cannot leave an invisible order.
- A failed request changes nothing; an unexplained broker difference marks the
  structure `unknown` and blocks new opens.
- Nothing is carried into expiry day: time exit at T-2 trading days; a bot
  started inside T-2 closes at the next session open + 5 minutes.
- Short stock found after an assignment is repaired by the execution engine's
  `cover_short` (one owner for covers); orphan short legs are bought back here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from stock_agent.broker import BrokerError, BrokerOrder, BrokerPosition
from stock_agent.options_book import (
    Leg,
    OptionsStateError,
    ReconcileReport,
    Structure,
    StructState,
    adopt_positions,
    evidence_by_symbol,
    leg_close_value,
    new_structure_id,
)
from stock_agent.options_policy import (
    MULTIPLIER,
    ContractQuote,
    Cooldown,
    Direction,
    ExitInputs,
    ExitReason,
    Proposal,
    estimate_trading_days,
    is_probable_session,
    mark_problem,
    Rejection,
    apply_result,
    breaks_thesis,
    build_proposal,
    close_limit,
    exit_reason,
    in_cooldown,
    parse_direction,
    parse_occ,
    pick_expiry,
    size_structure,
)
from stock_agent.options_settings import OptionsSettings, load_options_settings
from stock_agent.risk_manager import OptionCommitment, RiskControls, TradeIntent, TradeKind

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# estimated counts are trusted only this many sessions past the exit threshold
_FALLBACK_MARGIN = 5
_EXPIRY_DRIVEN = frozenset({ExitReason.TIME_EXIT, ExitReason.ADOPTED_EXPIRY, ExitReason.EX_DIVIDEND})
OPTION_ACTIVITY_TYPES = ("FILL", "OPEXC", "OPXRC", "OPASN", "OPEXP", "OPTRD")
_FILLED = "filled"
_DEAD = {"canceled", "expired", "rejected", "done_for_day", "replaced", "suspended", "stopped"}


class OptionsError(RuntimeError):
    """An options action whose outcome is not known. Always loud."""


@dataclass(frozen=True)
class OptionCandidate:
    """A symbol handed to `scan` mid-session (news reactions, A5)."""

    symbol: str
    source: str = "news"
    story_id: str | None = None
    direction_hint: str | None = None  # bullish | bearish | BUY | BEARISH


@dataclass(frozen=True)
class CloseResult:
    filled: bool
    filled_qty: int
    avg_price: float | None
    order_id: str | None
    status: str
    reason: str = ""


@dataclass
class ScanOutcome:
    symbol: str
    opened: str | None = None  # structure id
    reason: str = ""
    # False when the answer may change soon (market or data gate, broker error, research
    # gate health); the caller should keep a news idea and offer it again later.
    final: bool = True
    story_id: str | None = None
    story_handled: bool = False  # the story id is durably recorded as handled


@dataclass
class MonitorOutcome:
    reconcile: ReconcileReport | None = None
    exits: list[tuple[str, str, str]] = field(default_factory=list)  # (structure id, reason, result)
    repairs: list[str] = field(default_factory=list)
    skipped: str = ""


async def _maybe(x: Any) -> Any:
    return await x if inspect.isawaitable(x) else x


def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _parse_ts(x: Any) -> datetime | None:
    if not x:
        return None
    try:
        dt = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cid(kind: str, structure_id: str, attempt: int = 0) -> str:
    h = hashlib.sha1(f"{structure_id}:{kind}:{attempt}".encode()).hexdigest()[:10]
    return f"sa-opt-{kind}-{structure_id[-12:]}-{attempt}-{h}"


class OptionsManager:
    def __init__(
        self,
        *,
        broker: Any,
        store: Any,
        gate: Any,
        risk: Any,
        snapshot: Callable[[], Any],
        calendar: Any,
        market_gate: Callable[[str], Awaitable[Any]],
        spot: Callable[[str], Any],
        cover_short: Callable[..., Awaitable[Any]] | None = None,
        universe: Callable[[], Iterable[str]] | None = None,
        risk_controls: Callable[[], RiskControls] | None = None,
        capital_lock: asyncio.Lock | None = None,
        defer_load: bool = False,
        ex_dividend: Callable[[str], Any] | None = None,
        journal: Any = None,
        alerts: Any = None,
        config: Any = None,
        settings: OptionsSettings | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.broker = broker
        self.store = store
        self.gate = gate
        self.risk = risk
        self._snapshot = snapshot
        self.calendar = calendar
        self._market_gate = market_gate
        self._spot = spot
        self._cover_short = cover_short
        self._universe = universe or (lambda: ())
        self._risk_controls = risk_controls or (lambda: RiskControls())
        self._bad_marks: set[str] = set()
        self._capital_lock = capital_lock
        self._closed_queue: list[str] = []
        self._ex_dividend = ex_dividend
        self.journal = journal
        self.alerts = alerts
        self.settings = settings or load_options_settings(config)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self.structures: list[Structure] = []
        self.cooldowns: dict[str, Cooldown] = {}
        self.meta: dict[str, Any] = {}
        self.load_error: str | None = None
        self._loaded = False
        if not defer_load:
            self.load_state()

    # ---------------------------------------------------------------- state
    def load_state(self) -> None:
        """Read the book (call after the shared store is opened when built with
        defer_load=True). Every public entry point also loads lazily."""
        self._load()
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            self.load_state()
        except OptionsStateError:
            raise
        except Exception as e:  # store not open yet: fail loudly, never report "nothing committed"
            raise OptionsError(f"options book not loaded: {e}") from e

    def _load(self) -> None:
        try:
            self.structures, self.cooldowns, self.meta = self.store.load()
        except OptionsStateError as e:
            # rebuilt from the broker at the next reconcile (adoption); opens blocked until then
            self.load_error = str(e)
            self.structures, self.cooldowns, self.meta = [], {}, {"rebuild": True}
            logger.error("%s", e)

    def _save(self) -> None:
        self.store.save(self.structures, self.cooldowns, self.meta)

    @property
    def mode(self):
        return self.settings.mode

    def live(self) -> list[Structure]:
        return [s for s in self.structures if s.st.live]

    def get(self, structure_id: str) -> Structure | None:
        return next((s for s in self.structures if s.id == structure_id), None)

    @contextlib.asynccontextmanager
    async def _capital_lock_cm(self):
        if self._capital_lock is None:
            yield
        else:
            async with self._capital_lock:
                yield

    def risk_commitments(self) -> tuple[OptionCommitment, ...]:
        """Public alias for the equity lane's projection: every open and pending structure."""
        return self.commitments()

    def commitments(self) -> tuple[OptionCommitment, ...]:
        """Open and pending structures for risk: max loss for all; for pending ones the
        debit is also reserved as cash (identified by order ids) until the fill shows
        up in the broker's cash. Raises OptionsError if the book can't be loaded."""
        self._ensure_loaded()
        out = []
        for s in self.live():
            ml = s.max_loss if s.max_loss is not None else 0.0
            ids = tuple(i for i in (s.open_order_id, s.open_client_id) if i) if s.st is StructState.PENDING else ()
            cash = Decimal(str(round(ml, 2))) if s.st is StructState.PENDING else Decimal(0)
            kw: dict[str, Any] = {}
            if ids:
                kw["order_ids"] = ids
            try:
                out.append(OptionCommitment(s.id, Decimal(str(round(ml, 2))), cash, **kw))
            except TypeError:  # older risk interface without order_ids
                out.append(OptionCommitment(s.id, Decimal(str(round(ml, 2))), cash))
        return tuple(out)

    def open_max_loss(self) -> float:
        return round(sum(s.max_loss or 0.0 for s in self.live()), 2)

    def open_blocker(self) -> str | None:
        """Why no new structure may open right now, or None."""
        if not self._loaded:
            return "options book not loaded"
        if not self.mode.may_open:
            return f"OPTIONS_MODE={self.mode.value}"
        if self.load_error or self.meta.get("rebuild"):
            return "options book is being rebuilt from the broker"
        bad = [s for s in self.structures if s.st in (StructState.UNKNOWN, StructState.REPAIR)]
        if bad:
            return f"{len(bad)} structure(s) need attention ({', '.join(sorted({s.st.value for s in bad}))})"
        return None

    # -------------------------------------------------------------- helpers
    def _j(self, kind: str, **fields: Any) -> None:
        if self.journal is None:
            return
        try:
            self.journal.append(kind, **fields)
        except Exception:  # journal never blocks trading
            logger.exception("journal append failed for %s", kind)

    async def _notify(self, method: str, **kw: Any) -> bool:
        """Use AlertService's typed hook when it has one. True if delivered."""
        fn = getattr(self.alerts, method, None) if self.alerts is not None else None
        if fn is None:
            return False
        try:
            await _maybe(fn(**kw))
            return True
        except Exception:
            logger.exception("alert hook %s failed", method)
            return False

    def _legs_summary(self, st: Structure) -> str:
        return " / ".join(f"{'+' if lg.side == 'buy' else '-'}{lg.ratio} {lg.type} {lg.strike:g}" for lg in st.legs) \
            + f" exp {st.expiry}"

    async def _rejected(self, st_or_symbol: Any, side: str, qty: int, reason: str, title: str, key: str) -> None:
        sym = st_or_symbol if isinstance(st_or_symbol, str) else st_or_symbol.underlying
        if not await self._notify("alert_order_rejected", symbol=sym, side=side, qty=qty, reason=reason,
                                  asset_class="option"):
            await self._alert("critical", title, reason, key=key)

    async def _flush_closed(self, now: datetime) -> None:
        while self._closed_queue:
            st = self.get(self._closed_queue.pop(0))
            if st is not None:
                await self._closed_alert(st, now)

    async def _closed_alert(self, st: Structure, now: datetime) -> None:
        held = (now - (_parse_ts(st.opened_at) or now)).days if st.opened_at else None
        await self._notify("send_option_structure_closed", underlying=st.underlying, strategy=st.strategy,
                           legs_summary=self._legs_summary(st), realized_pnl=st.realized_pnl,
                           exit_reason=st.exit_reason or "", held_days=held)

    async def _alert(self, severity: str, title: str, body: str, key: str | None = None) -> None:
        logger.log({"critical": logging.ERROR, "error": logging.ERROR, "warning": logging.WARNING}.get(severity, logging.INFO), "%s: %s", title, body)
        if self.alerts is None:
            return
        try:
            fn = getattr(self.alerts, "alert", self.alerts)
            await _maybe(fn(severity, title, body, key=key))
        except Exception:
            logger.exception("alert failed: %s", title)

    async def _gate(self, purpose: str) -> tuple[bool, str]:
        try:
            d = await _maybe(self._market_gate(purpose))
        except Exception as e:  # fail closed
            return False, f"market gate failed: {e}"
        if isinstance(d, bool):
            return d, ""
        return bool(getattr(d, "allowed", getattr(d, "ok", False))), str(getattr(d, "reason", ""))

    def _today(self, now: datetime) -> date:
        return now.astimezone(ET).date()

    def _calendar_days(self, a: date, b: date) -> int | None:
        """Broker-calendar count only; new entries never use an estimate."""
        try:
            return self.calendar.trading_days_between(a, b)
        except Exception:
            return None

    def _trading_days(self, a: date, b: date) -> int | None:
        """For managing held structures: falls back to an estimate for distant dates so a
        calendar outage does not force-close them; near expiry unknown stays None (close)."""
        n = self._calendar_days(a, b)
        if n is not None or b < a:
            return n
        # Beyond the broker calendar's window: use the weekday/holiday estimate, but only
        # for clearly distant dates. Near expiry an unknown count stays None (close).
        est = estimate_trading_days(a, b)
        if est is not None and est > self.settings.exit_trading_days_before_expiry + _FALLBACK_MARGIN:
            return est
        return None

    def _add_trading_days(self, d: date, n: int) -> date:
        cur = d
        for _ in range(n):
            nxt = None
            try:
                nxt = self.calendar.next_session(cur)
            except Exception:
                nxt = None
            if nxt is None:
                # calendar not covered that far: weekdays minus rule-based NYSE holidays
                cur = cur + timedelta(days=1)
                while not is_probable_session(cur):
                    cur += timedelta(days=1)
            else:
                cur = getattr(nxt, "date", None) or getattr(nxt, "day")
        return cur

    def _next_session_date(self, d: date) -> date | None:
        try:
            nxt = self.calendar.next_session(d)
        except Exception:
            return None
        return None if nxt is None else (getattr(nxt, "date", None) or getattr(nxt, "day", None))

    async def _spot_now(self, symbol: str, now: datetime) -> tuple[float | None, str]:
        try:
            r = await _maybe(self._spot(symbol))
        except Exception as e:
            return None, f"quote failed: {e}"
        if r is None:
            return None, "no quote"
        if isinstance(r, tuple):
            px, at = r
        else:
            actual = getattr(r, "symbol", None)
            if isinstance(actual, str) and actual and actual.upper() != symbol.upper():
                # the market-data layer resolved a rename: never trade options on a stale symbol
                return None, f"symbol now trades as {actual}"
            px, at = getattr(r, "price", None), getattr(r, "at", None) or getattr(r, "as_of", None)
        px = _f(px)
        at = _parse_ts(at) if not isinstance(at, datetime) else at
        if px is None or px <= 0:
            return None, "no quote"
        if at is None or (now - at).total_seconds() > self.settings.max_quote_age_s:
            return None, "stale quote"
        return px, ""

    async def _quotes(self, symbols: Sequence[str], contracts: dict[str, dict] | None = None) -> dict[str, ContractQuote]:
        snaps = await self.broker.get_option_snapshots(list(symbols))
        out: dict[str, ContractQuote] = {}
        for sym in symbols:
            occ = parse_occ(sym)
            snap = snaps.get(sym)
            if occ is None or not snap:
                continue
            q = snap.get("latestQuote") or {}
            g = snap.get("greeks") or {}
            c = (contracts or {}).get(sym) or {}
            oi = c.get("open_interest")
            out[sym] = ContractQuote(
                sym, occ.underlying, occ.expiry, occ.type, occ.strike,
                _f(q.get("bp")), _f(q.get("ap")), _parse_ts(q.get("t")),
                int(float(oi)) if oi not in (None, "") else None,
                _f(g.get("delta")), _f(snap.get("impliedVolatility")),
            )
        return out

    # ===================================================================== scan
    async def scan(self, now: datetime | None = None,
                   candidates: Sequence[OptionCandidate] | None = None) -> list[ScanOutcome]:
        """Look for entries. With `candidates` only those symbols are tried
        (news reactions); otherwise the research universe."""
        now = now or self._now()
        async with self._lock:
            self._ensure_loaded()
            blocker = self.open_blocker()
            if blocker:
                syms = [c.symbol for c in candidates] if candidates else []
                return [ScanOutcome(s, None, blocker, final=False) for s in syms] or [ScanOutcome("*", None, blocker, final=False)]
            try:
                await self._reconcile_locked(now)
            except BrokerError as e:
                return [ScanOutcome("*", None, f"reconcile failed: {e}", final=False)]
            blocker = self.open_blocker()
            if blocker:
                return [ScanOutcome("*", None, blocker, final=False)]
            ok, why = await self._gate("entry")
            if not ok:
                return [ScanOutcome("*", None, f"market gate: {why}", final=False)]
            if candidates is None:
                cands = [OptionCandidate(s, source="scan") for s in dict.fromkeys(self._universe())]
            else:
                cands = list(candidates)
            out = []
            for c in cands:
                err: BrokerError | None = None
                try:
                    o = await self._try_open(c, now)
                except BrokerError as e:
                    err = e
                    o = ScanOutcome(c.symbol, None, f"broker: {e}", final=False)
                if c.story_id:
                    o.story_id = c.story_id
                    if o.final or o.opened:
                        self._mark_story(c.story_id)
                    o.story_handled = self.is_story_handled(c.story_id)
                out.append(o)
                if err is not None:
                    if err.uncertain:
                        await self._alert("critical", "Options order outcome unknown", f"{c.symbol}: {err}",
                                          key=f"opt-uncertain-{c.symbol}")
                        break
                    if err.kind in ("auth", "network", "timeout", "server"):
                        break
            for o in out:
                if not o.opened:
                    self._j("options.scan_skip", symbol=o.symbol, reason=o.reason)
            return out

    def is_story_handled(self, story_id: str) -> bool:
        """True once a news story has had a final answer or an order was sent for it.
        Persisted in the book, so replays after a crash are answered the same way."""
        return story_id in set(self.meta.get("stories", [])) or any(
            x.story_id == story_id for x in self.structures)

    def handled_story_ids(self, story_ids: Iterable[str]) -> set[str]:
        return {sid for sid in story_ids if sid and self.is_story_handled(sid)}

    def _mark_story(self, story_id: str) -> None:
        if story_id in set(self.meta.get("stories", [])):
            return
        self.meta["stories"] = (list(self.meta.get("stories", [])) + [story_id])[-500:]
        self._save()

    def _news_opens_today(self, today: date) -> int:
        return sum(1 for s in self.structures
                   if s.source == "news" and s.opened_at and s.opened_at[:10] == today.isoformat()
                   and s.st is not StructState.VOID)

    async def _try_open(self, c: OptionCandidate, now: datetime) -> ScanOutcome:
        s = self.settings
        sym = c.symbol.upper()
        today = self._today(now)
        if any(x.underlying == sym for x in self.live()):
            return ScanOutcome(sym, None, "already holds a structure on this underlying")
        live = self.live()
        if len(live) >= s.max_open_structures:
            return ScanOutcome(sym, None, f"{len(live)} structures open")
        if in_cooldown(self.cooldowns.get(sym), today):
            return ScanOutcome(sym, None, f"cool-down until {self.cooldowns[sym].until}")
        if c.source == "news":
            if c.story_id and self.is_story_handled(c.story_id):
                return ScanOutcome(sym, None, "story already handled")
            if self._news_opens_today(today) >= s.max_news_opens_per_session:
                return ScanOutcome(sym, None, "news-driven option opens for this session used up")
        # ---- fresh underlying quote first: it also refreshes the shared price
        # health the research gate checks, so a prescan thesis isn't blocked by staleness
        spot, why = await self._spot_now(sym, now)
        if spot is None:
            return ScanOutcome(sym, None, f"underlying {why}", final=False)
        # ---- research gate (at a time no earlier than the quote just fetched)
        now = max(now, self._now())
        el = await _maybe(self.gate.is_entry_eligible(sym, now))
        if not el.ok:
            return ScanOutcome(sym, None, "research gate: " + ",".join(el.reasons), final=False)
        direction = parse_direction(el.direction)
        if direction is None:
            return ScanOutcome(sym, None, f"thesis direction {el.direction} is not tradeable with options")
        if c.direction_hint and parse_direction(c.direction_hint) not in (None, direction):
            return ScanOutcome(sym, None, "news direction disagrees with the thesis")
        conv = el.effective_conviction
        if conv is None or conv < s.min_effective_conviction or (el.raw_conviction or 0) < 7:
            return ScanOutcome(sym, None, f"conviction {conv} below {s.min_effective_conviction}")
        account = await self.broker.get_account()
        level = account.options_trading_level
        kind = "call" if direction is Direction.BULLISH else "put"
        contracts = await self.broker.get_option_contracts(
            sym, type=kind, expiration_gte=today + timedelta(days=s.min_dte),
            expiration_lte=today + timedelta(days=s.max_dte),
            strike_gte=round(spot * 0.85, 2), strike_lte=round(spot * 1.15, 2))
        contracts = [x for x in contracts if x.get("tradable", True) and x.get("symbol")]
        if not contracts:
            return ScanOutcome(sym, None, "no listed contracts in the window")
        expiries = [date.fromisoformat(x["expiration_date"]) for x in contracts if x.get("expiration_date")]
        exp = pick_expiry(expiries, today, s, self._calendar_days)
        if exp is None:
            return ScanOutcome(sym, None, "no expiry fits the window")
        chosen = {x["symbol"]: x for x in contracts if x.get("expiration_date") == exp.isoformat()}
        quotes = await self._quotes(sorted(chosen), chosen)
        prop = build_proposal(underlying=sym, direction=direction, conviction=conv, spot=spot,
                              chain=list(quotes.values()), today=today, now=now, s=s,
                              trading_days=self._calendar_days)
        if isinstance(prop, Rejection):
            return ScanOutcome(sym, None, prop.reason)
        if prop.strategy.is_spread and level is not None and level < 3:
            return ScanOutcome(sym, None, f"options level {level} does not allow spreads")
        # ---- sizing and risk
        if el.generated_at is None:
            return ScanOutcome(sym, None, "thesis has no generation time")
        try:
            controls = await _maybe(self._risk_controls())
        except Exception as e:  # e.g. an invalid drawdown record: refuse entries
            await self._alert("critical", "Options entries refused: risk state unreadable", str(e),
                              key="opt-risk-state")
            return ScanOutcome(sym, None, f"risk state unavailable: {e}", final=False)
        halted = bool(getattr(getattr(controls, "drawdown", None), "throttled", False))
        ladder = self._open_ladder(prop)
        if not ladder:
            return ScanOutcome(sym, None, "no limit price meets the reward/risk floor")
        worst = round(ladder[-1] * MULTIPLIER, 2)  # budgets hold even at the last rung
        sizing = size_structure(worst, equity=float(account.equity),
                                open_max_loss=self.open_max_loss(), open_count=len(self.live()), s=s,
                                drawdown_halt=halted)
        if sizing.qty < 1:
            return ScanOutcome(sym, None, sizing.reason)
        sid = new_structure_id()
        intent = TradeIntent(
            kind=TradeKind.OPTION_OPEN, symbol=sym, qty=Decimal(sizing.qty),
            raw_conviction=el.raw_conviction, effective_conviction=conv, minimum_conviction=s.min_effective_conviction,
            option_max_loss=Decimal(str(sizing.max_loss)), option_cash_debit=Decimal(str(sizing.debit_cash)),
            structure_id=sid, signal_at=el.generated_at, quote_at=now, analysis_price=Decimal(str(spot)),
        )
        # final risk recheck -> write-ahead reservation under the shared capital lock, so an
        # equity lane can't size from the same free cash at the same moment
        async with self._capital_lock_cm():
            full_controls = RiskControls(
                pause_new_buys=getattr(controls, "pause_new_buys", False),
                drawdown=getattr(controls, "drawdown", RiskControls().drawdown),
                option_commitments=self.commitments(),
            )
            try:
                snap = await _maybe(self._snapshot())
                verdict = self.risk.evaluate_trade(snap, intent, now=now, controls=full_controls)
            except BrokerError:
                raise
            except Exception as e:  # risk unavailable -> no entry
                return ScanOutcome(sym, None, f"risk check unavailable: {e}", final=False)
            if not verdict.ok:
                return ScanOutcome(sym, None, "risk: " + "; ".join(verdict.reasons))
            existing = getattr(verdict, "allowed_existing_breaches", ()) or ()
            if existing:
                self._j("options.risk_existing_breaches", symbol=sym, structure_id=sid,
                        breaches=[{"kind": getattr(b, "kind", None), "key": getattr(b, "key", None),
                                   "value": str(getattr(b, "value", "")), "cap": str(getattr(b, "cap", ""))}
                                  for b in existing])
            ok, why = await self._gate("entry")
            if not ok:
                return ScanOutcome(sym, None, f"market gate: {why}", final=False)
            st = self._new_structure(sid, prop, sizing.qty, sizing.max_loss, c, el, now)
            st.open_client_id = _cid("open", st.id)
            self.structures.append(st)
            self._save()  # write-ahead: from here the commitment is visible to every lane
        return await self._submit_open(st, prop, now)

    def _new_structure(self, sid, prop: Proposal, qty, max_loss, c: OptionCandidate, el, now) -> Structure:
        return Structure(
            id=sid, underlying=prop.underlying, strategy=prop.strategy.value, direction=prop.direction.value,
            qty=qty, expiry=prop.expiry.isoformat(),
            legs=[Leg(lg.symbol, lg.side, lg.type, lg.strike, lg.expiry.isoformat(), lg.ratio) for lg in prop.legs],
            state=StructState.PENDING.value, source=c.source if c.source in ("news", "scan") else "scan",
            thesis_id=el.thesis_id, conviction=el.effective_conviction, story_id=c.story_id,
            opened_at=now.isoformat(), width=prop.width, max_loss=max_loss, limit_debit=prop.limit_debit,
            entry_greeks={"delta": prop.delta, "iv": prop.iv, "spot": prop.spot, "mid_debit": prop.mid_debit,
                          "natural_debit": prop.natural_debit},
        )

    def _open_body(self, st: Structure, limit: float) -> dict:
        cid = st.open_client_id
        if len(st.legs) == 1:
            lg = st.legs[0]
            return {"symbol": lg.symbol, "side": "buy", "qty": str(st.qty), "type": "limit",
                    "time_in_force": "day", "limit_price": f"{limit:.2f}", "position_intent": "buy_to_open",
                    "client_order_id": cid}
        return {
            "order_class": "mleg", "qty": str(st.qty), "type": "limit", "time_in_force": "day",
            "limit_price": f"{limit:.2f}", "client_order_id": cid,
            "legs": [{"symbol": lg.symbol, "side": lg.side, "ratio_qty": str(lg.ratio),
                      "position_intent": "buy_to_open" if lg.side == "buy" else "sell_to_open"} for lg in st.legs],
        }

    def _open_ladder(self, prop: Proposal) -> list[float]:
        """Limit debits to try, from near mid towards the natural price, never above
        the price that still meets the reward/risk floor for a spread."""
        s = self.settings
        cap = prop.natural_debit
        if prop.width is not None:
            cap = min(cap, prop.width / (1.0 + s.min_reward_risk))
        out: list[float] = []
        for frac in (s.limit_slippage_frac, 0.6, 1.0):
            px = prop.mid_debit + max(prop.natural_debit - prop.mid_debit, 0.0) * frac
            px = min(round(px + 0.004999, 2), round(cap, 2))
            if px > 0 and (not out or px > out[-1]):
                out.append(px)
        if prop.limit_debit not in out and prop.limit_debit <= cap:
            out = sorted(set(out) | {prop.limit_debit})
        return out

    async def _submit_open(self, st: Structure, prop: Proposal, now: datetime) -> ScanOutcome:
        """Send the open ladder for a structure already reserved (write-ahead) in the book."""
        self._j("options.open_submitted", structure_id=st.id, underlying=st.underlying, strategy=st.strategy,
                direction=st.direction, qty=st.qty, expiry=st.expiry, legs=[lg.symbol for lg in st.legs],
                limit_debit=prop.limit_debit, max_loss=st.max_loss, thesis_id=st.thesis_id,
                conviction=st.conviction, source=st.source, story_id=st.story_id, greeks=st.entry_greeks)
        order = None
        for attempt, limit in enumerate(self._open_ladder(prop)):  # same ladder as sized
            if attempt:
                ok, why = await self._gate("entry")
                if not ok:
                    break
                st.open_client_id = _cid("open", st.id, attempt)
                st.state = StructState.PENDING.value
                self._save()
            # the reservation stays at the sized ceiling (top rung) until the order is
            # terminal; _apply_open_order then sets max loss from the actual fill
            st.limit_debit = limit
            try:
                order = await self.broker.submit_raw_order(self._open_body(st, limit))
            except BrokerError as e:
                if e.uncertain:
                    st.note(f"open submit uncertain: {e.kind}")
                    self._save()
                    raise
                st.state = StructState.VOID.value
                st.note(f"open rejected: {e.message}")
                self._save()
                await self._rejected(st, "buy", st.qty, e.message, f"Options open rejected: {st.underlying}",
                                     key=f"opt-open-{st.id}")
                return ScanOutcome(st.underlying, None, f"order rejected: {e.message}")
            st.open_order_id = order.id
            self._save()
            order = await self._wait_or_cancel(order)
            self._apply_open_order(st, order)
            self._save()
            if st.st is not StructState.VOID:
                break
        if st.st is StructState.OPEN:
            self._j("options.opened", structure_id=st.id, underlying=st.underlying, strategy=st.strategy,
                    direction=st.direction, qty=st.qty, entry_debit=st.entry_debit, max_loss=st.max_loss,
                    width=st.width, expiry=st.expiry, thesis_id=st.thesis_id, greeks=st.entry_greeks,
                    conviction=st.conviction, source=st.source)
            max_profit = (round((st.width - st.entry_debit) * MULTIPLIER * st.qty, 2)
                          if st.width is not None and st.entry_debit is not None else None)
            if not await self._notify(
                    "send_option_structure_opened", underlying=st.underlying, strategy=st.strategy,
                    legs_summary=self._legs_summary(st), debit_total=round((st.entry_debit or 0) * MULTIPLIER * st.qty, 2),
                    max_loss=st.max_loss, max_profit=max_profit, expiry=st.expiry, conviction=st.conviction,
                    origin="NEWS" if st.source == "news" else "SCHEDULED", reason=st.thesis_id or ""):
                await self._alert("info", f"Options opened: {st.underlying} {st.strategy}",
                                  f"{st.qty} x {st.expiry}, debit {st.entry_debit:.2f}, max loss {st.max_loss:.0f}",
                                  key=f"opt-open-{st.id}")
            return ScanOutcome(st.underlying, st.id, "opened")
        if st.st is StructState.PENDING:
            return ScanOutcome(st.underlying, None, "open order still working", final=False)
        return ScanOutcome(st.underlying, None, f"not filled ({order.status if order else 'not sent'})")

    async def _wait_or_cancel(self, order: BrokerOrder) -> BrokerOrder:
        s = self.settings
        order = await self.broker.wait_terminal(order.id, timeout_s=s.fill_wait_seconds, poll_s=s.fill_poll_seconds)
        if order.is_terminal:
            return order
        await self.broker.cancel_order(order.id)
        order = await self.broker.wait_terminal(order.id, timeout_s=30, poll_s=s.fill_poll_seconds)
        if not order.is_terminal:
            raise OptionsError(f"cancel of order {order.id} not confirmed (status {order.status})")
        return order

    @staticmethod
    def _net_fill(order: BrokerOrder, legs: list[Leg], closing: bool) -> tuple[int, float | None]:
        """(filled structures, net per-share price: + paid / - received from our side's view).
        For an opening order the result is the debit paid; for a closing order the
        credit received is returned as a positive number."""
        if order.legs:
            by_sym = {lg.symbol: lg for lg in order.legs}
            parent_qty = order.qty or 0
            filled = int(round(order.filled_qty or 0))
            if not filled:
                leg_q = [by_sym[lg.symbol].filled_qty / max(lg.ratio, 1) for lg in legs if lg.symbol in by_sym]
                filled = int(round(min(leg_q))) if leg_q and len(leg_q) == len(legs) else 0
            if filled <= 0:
                return 0, None
            net = 0.0
            for lg in legs:
                bo = by_sym.get(lg.symbol)
                if bo is None or bo.filled_avg_price is None:
                    return filled, None
                px = bo.filled_avg_price * lg.ratio
                net += px if bo.side == "buy" else -px
            _ = parent_qty
            return filled, round(-net if closing else net, 4)
        filled = int(round(order.filled_qty or 0))
        if filled <= 0 or order.filled_avg_price is None:
            return filled, None
        return filled, round(order.filled_avg_price, 4)

    def _apply_open_order(self, st: Structure, order: BrokerOrder) -> None:
        st.open_order_id = order.id
        if order.status == _FILLED or (order.is_terminal and (order.filled_qty or 0) > 0):
            filled, debit = self._net_fill(order, st.legs, closing=False)
            if filled <= 0:
                st.state = StructState.VOID.value
                return
            st.qty = filled
            st.entry_debit = debit if debit is not None else st.limit_debit
            if debit is None:
                st.note("leg fill prices missing; entry debit from the limit price")
            st.max_loss = round((st.entry_debit or 0) * MULTIPLIER * filled, 2)
            st.state = StructState.OPEN.value
        elif order.is_terminal:
            st.state = StructState.VOID.value
            st.note(f"open order ended {order.status} with no fill")

    # ============================================================== reconcile
    async def reconcile(self, now: datetime | None = None) -> ReconcileReport:
        async with self._lock:
            self._ensure_loaded()
            return await self._reconcile_locked(now or self._now())

    async def _reconcile_locked(self, now: datetime) -> ReconcileReport:
        """Two-way: book -> broker and broker -> book. Reads everything first;
        a failed read raises before anything is changed."""
        positions: list[BrokerPosition] = await self.broker.get_positions()
        pending_orders: dict[str, BrokerOrder | None] = {}
        for st in self.structures:
            if st.st is StructState.PENDING and st.open_client_id:
                pending_orders[st.id] = await self.broker.get_order_by_client_id(st.open_client_id)
            elif st.st is StructState.CLOSING and st.close_client_id:
                pending_orders[st.id] = await self.broker.get_order_by_client_id(st.close_client_id)
        live_dates = [st.opened_at for st in self.live() if st.opened_at]
        since = min(live_dates) if live_dates else None
        after = (_parse_ts(since) - timedelta(days=1)) if since else now - timedelta(days=10)
        activities = await self.broker.get_activities(OPTION_ACTIVITY_TYPES, after=after)

        rep = ReconcileReport()
        # 1. orders in flight
        for st in self.structures:
            if st.id not in pending_orders:
                continue
            o = pending_orders[st.id]
            if st.st is StructState.PENDING:
                if o is None:
                    st.state = StructState.VOID.value
                    st.note("no broker order under the client id; voided")
                else:
                    self._apply_open_order(st, o)
            elif st.st is StructState.CLOSING:
                if o is None:
                    st.state = StructState.OPEN.value
                    st.note("closing order not found at broker; back to open")
                elif o.is_terminal:
                    self._apply_close_order(st, o, now)
                    if st.st is StructState.CLOSED:
                        rep.closed.append(st.id)

        # 2. positions vs book
        opt_pos = {p.symbol: p for p in positions if p.is_option}
        broker_qty = {sym: int(round(p.qty)) for sym, p in opt_pos.items()}
        expected: dict[str, int] = {}
        holders: dict[str, list[Structure]] = {}
        for st in self.structures:
            if st.st in (StructState.OPEN, StructState.CLOSING, StructState.UNKNOWN, StructState.REPAIR):
                for sym, q in st.expected_positions().items():
                    expected[sym] = expected.get(sym, 0) + q
                    holders.setdefault(sym, []).append(st)
        ev = evidence_by_symbol(activities, None)
        touched: set[str] = set()
        for sym in sorted(set(expected) | set(broker_qty)):
            e, b = expected.get(sym, 0), broker_qty.get(sym, 0)
            if e == b:
                continue
            if e == 0:
                continue  # extra broker legs are adopted below
            if b == 0:
                for st in holders.get(sym, []):
                    touched.add(st.id)
            else:
                for st in holders.get(sym, []):
                    if st.st is not StructState.UNKNOWN:
                        st.state = StructState.UNKNOWN.value
                        st.note(f"{sym}: book {e}, broker {b}")
                    rep.mismatched.append(st.id)
        for st in self.structures:
            if st.id not in touched:
                if st.st is StructState.UNKNOWN and st.id not in rep.mismatched and all(
                        broker_qty.get(s, 0) == q for s, q in st.expected_positions().items()):
                    st.state = StructState.OPEN.value
                    st.note("broker matches again; back to open")
                continue
            self._resolve_missing(st, broker_qty, ev, rep, now)

        # 3. adopt broker legs no structure claims (never legs of an order still in flight)
        in_flight = {lg.symbol for st in self.structures if st.st is StructState.PENDING for lg in st.legs}
        claimed: set[str] = set()
        for st in self.structures:
            if st.st in (StructState.OPEN, StructState.CLOSING, StructState.UNKNOWN, StructState.REPAIR):
                claimed.update(lg.symbol for lg in st.legs)
        extras = [_Pos(sym, broker_qty[sym], p.avg_entry_price) for sym, p in opt_pos.items()
                  if sym not in claimed and sym not in in_flight and broker_qty[sym] != 0]
        if extras:
            for st in adopt_positions(extras, now, self.meta.get("legacy_hints")):
                self.structures.append(st)
                rep.adopted.append(st.id)
                if st.st is StructState.REPAIR:
                    rep.repair.append(st.id)
                self._j("options.adopted", structure_id=st.id, underlying=st.underlying, strategy=st.strategy,
                        qty=st.qty, expiry=st.expiry, legs=[lg.symbol for lg in st.legs], state=st.state)

        # 4. short stock anywhere is a repair item
        for p in positions:
            if p.is_equity and p.is_short:
                rep.short_stock[p.symbol] = p.qty
        rep.unknown = [st.id for st in self.structures if st.st is StructState.UNKNOWN]
        rep.repair = sorted(set(rep.repair) | {st.id for st in self.structures if st.st is StructState.REPAIR})
        if self.meta.pop("rebuild", None) is not None:
            self.load_error = None
        self.meta["last_reconcile"] = now.isoformat()
        self.meta["last_reconcile_clean"] = rep.clean
        self._save()
        if rep.unknown:
            await self._alert("critical", "Options book disagrees with the broker",
                              f"{len(rep.unknown)} structure(s) unknown; new option entries blocked",
                              key="opt-unknown")
        for sid in rep.unknown:
            if sid not in self.meta.get("journaled_unknown", []):
                self._j("options.reconcile_unknown", structure_id=sid)
        self.meta["journaled_unknown"] = rep.unknown
        await self._flush_closed(now)
        return rep

    def _resolve_missing(self, st: Structure, broker_qty: dict[str, int], ev: dict, rep: ReconcileReport,
                         now: datetime) -> None:
        """Some legs of `st` are gone at the broker. Close them only with evidence."""
        gone, remaining, values = [], [], {}
        opened = _parse_ts(st.opened_at) if st.opened_at else None
        for lg in st.legs:
            if broker_qty.get(lg.symbol, 0) == 0:
                acts = [a for a in ev.get(lg.symbol, []) if _after_open(a, opened)]
                v = leg_close_value(lg, acts)
                if v is None:
                    if any(broker_qty.get(x.symbol, 0) != 0 for x in st.legs):
                        # other legs are still held: never wait on a possibly naked leg,
                        # close what is left under the repair rule
                        v = (0.0, "unexplained")
                        st.note(f"{lg.symbol} gone at broker with no record; repairing the rest")
                    else:
                        st.state = StructState.UNKNOWN.value
                        st.note(f"{lg.symbol} gone at broker with no fill/expiry/exercise/assignment record")
                        return
                values[lg.symbol] = v
                gone.append(lg)
            else:
                remaining.append(lg)
        kinds = {v[1] for v in values.values()}
        if kinds & {"exercised", "assigned"}:
            rep.deliveries.append({"structure_id": st.id, "underlying": st.underlying,
                                   "legs": {k: v[1] for k, v in values.items()}})
        if remaining:
            # Legs went separately (early assignment, broker liquidation of one leg ...).
            st.legs = remaining
            short_left = any(lg.side == "sell" for lg in remaining)
            st.state = StructState.REPAIR.value
            st.note(f"legs closed separately ({', '.join(f'{k}:{v[1]}' for k, v in values.items())}); "
                    f"{'orphan short leg' if short_left else 'orphan long leg'} to close")
            st.exit_reason = st.exit_reason or ExitReason.REPAIR.value
            rep.repair.append(st.id)
            self._j("options.repair", structure_id=st.id, underlying=st.underlying,
                    gone={k: v[1] for k, v in values.items()}, remaining=[lg.symbol for lg in remaining])
            return
        st.closed_at = now.isoformat()
        st.state = StructState.CLOSED.value
        if kinds <= {"fill", "expired"} and st.entry_debit is not None:
            credit = sum(lg.sign * lg.ratio * values[lg.symbol][0] for lg in st.legs)
            st.exit_credit = round(credit, 4)
            st.realized_pnl = st.final_pnl(credit)
            self._record_result(st, now)
        else:
            st.realized_pnl = None
            st.note("closed by exercise/assignment; P&L completes in the stock book")
        st.exit_reason = st.exit_reason or ("expired" if "expired" in kinds else
                                            "delivered" if kinds & {"exercised", "assigned"} else "closed_at_broker")
        rep.closed.append(st.id)
        self._j("options.closed", structure_id=st.id, underlying=st.underlying, strategy=st.strategy,
                reason=st.exit_reason, realized_pnl=st.realized_pnl, exit_credit=st.exit_credit,
                entry_debit=st.entry_debit, evidence={k: v[1] for k, v in values.items()},
                thesis_id=st.thesis_id)
        self._closed_queue.append(st.id)

    def _record_result(self, st: Structure, now: datetime) -> None:
        if st.realized_pnl is None:
            return
        cd = self.cooldowns.get(st.underlying) or Cooldown(st.underlying)
        self.cooldowns[st.underlying] = apply_result(cd, st.realized_pnl, self._today(now), self.settings,
                                                     self._add_trading_days)

    # ================================================================ monitor
    async def monitor(self, now: datetime | None = None) -> MonitorOutcome:
        """Reconcile, repair, then apply the exit rules. Runs in every mode
        except that `off` only repairs."""
        now = now or self._now()
        out = MonitorOutcome()
        async with self._lock:
            self._ensure_loaded()
            try:
                out.reconcile = await self._reconcile_locked(now)
            except BrokerError as e:
                out.skipped = f"reconcile failed: {e}"
                await self._alert("critical", "Options reconcile failed", str(e), key="opt-reconcile")
                return out
            ok, why = await self._gate("exit")
            if not ok:
                out.skipped = f"market gate: {why}"
                return out
            await self._repair(now, out)
            if not self.mode.may_manage:
                return out
            for st in list(self.structures):
                if st.st is not StructState.OPEN:
                    continue
                try:
                    reason = await self._exit_due(st, now)
                    if reason is None:
                        continue
                    res = await self._close(st, reason, now)
                    out.exits.append((st.id, reason.value, res))
                except BrokerError as e:
                    out.exits.append((st.id, "error", str(e)))
                    await self._alert("critical", f"Options exit failed: {st.underlying}", str(e),
                                      key=f"opt-exit-{st.id}")
                    if e.uncertain:
                        break
            self._save()
        return out

    async def _repair(self, now: datetime, out: MonitorOutcome) -> None:
        rep = out.reconcile
        if rep is None:
            return
        for sym, qty in rep.short_stock.items():
            if self._cover_short is None:
                await self._alert("critical", f"Short stock {sym}", f"{qty} shares short and no cover service",
                                  key=f"opt-short-{sym}")
                continue
            try:
                await self._cover_short(sym, reason="assignment")
                out.repairs.append(f"cover {sym}")
                self._j("options.repair", action="cover_short", symbol=sym, qty=qty)
            except Exception as e:
                await self._alert("critical", f"Short stock cover failed: {sym}", str(e), key=f"opt-short-{sym}")
        for st in [s for s in self.structures if s.st is StructState.REPAIR]:
            try:
                res = await self._close(st, ExitReason.REPAIR, now)
                out.repairs.append(f"close {st.id}: {res}")
            except BrokerError as e:
                await self._alert("critical", f"Options repair failed: {st.underlying}", str(e),
                                  key=f"opt-repair-{st.id}")
        for d in rep.deliveries:
            await self._alert("critical", f"Option delivery on {d['underlying']}",
                              f"exercise/assignment moved shares ({d['legs']}); trim policy applies",
                              key=f"opt-delivery-{d['structure_id']}")

    async def _exit_due(self, st: Structure, now: datetime) -> ExitReason | None:
        s = self.settings
        today = self._today(now)
        days_left = self._trading_days(today, st.expiry_date)
        if days_left is None and st.expiry_date <= today:
            days_left = 0
        try:
            quotes = await self._quotes([lg.symbol for lg in st.legs])
        except BrokerError as e:  # no marks: the time exit and repairs still run
            if e.uncertain or e.kind in ("auth",):
                raise
            quotes = {}
        bad = {lg.symbol: mark_problem(quotes.get(lg.symbol), now, s) for lg in st.legs}
        bad = {k: v for k, v in bad.items() if v}
        value_mid = None if bad else self._value(st, quotes, "mid")
        if bad and st.id not in self._bad_marks:
            self._bad_marks.add(st.id)
            self._j("options.mark_rejected", structure_id=st.id, problems=bad)
        elif not bad:
            self._bad_marks.discard(st.id)
        spot = None
        short_call = next((lg for lg in st.legs if lg.side == "sell" and lg.type == "call"), None)
        extrinsic = None
        exdiv, exdiv_known = None, True
        if short_call is not None:
            spot, _ = await self._spot_now(st.underlying, now)
            q = quotes.get(short_call.symbol)
            if spot is not None and q is not None and q.mid is not None:
                extrinsic = q.mid - max(spot - short_call.strike, 0.0)
            if self._ex_dividend is None:
                exdiv_known = False
            else:
                try:
                    exdiv = await _maybe(self._ex_dividend(st.underlying))
                except Exception:
                    exdiv_known = False
        broken = False
        if st.dir is not None:
            try:
                el = await _maybe(self.gate.is_entry_eligible(st.underlying, max(now, self._now())))
                # only a fresh, healthy opposite view breaks the thesis; stale or
                # unhealthy research never closes a position (price and time exits still run)
                reasons = " ".join(str(r) for r in (getattr(el, "reasons", ()) or ())).lower()
                trusted = getattr(el, "generated_at", None) is not None and not any(
                    w in reasons for w in ("stale", "health", "unavailable", "missing", "invalid", "insufficient"))
                broken = trusted and breaks_thesis(st.dir, getattr(el, "direction", None))
            except Exception:
                broken = False
        x = ExitInputs(
            strategy=st.strat, qty=st.qty, entry_debit=st.entry_debit or 0.0, width=st.width,
            value_now=value_mid, trading_days_left=days_left, thesis_broken=broken,
            short_call_strike=short_call.strike if short_call else None, short_call_extrinsic=extrinsic,
            spot=spot, ex_dividend=exdiv, ex_dividend_known=exdiv_known, next_session=self._next_session_date(today),
        )
        if st.source == "adopted" and (days_left is None or days_left <= s.exit_trading_days_before_expiry):
            return ExitReason.ADOPTED_EXPIRY
        return exit_reason(x, s)

    @staticmethod
    def _value(st: Structure, quotes: dict[str, ContractQuote], how: str) -> float | None:
        """Per-share value of closing the structure now: 'mid' or 'natural' (sell longs at bid, buy shorts at ask)."""
        total = 0.0
        for lg in st.legs:
            q = quotes.get(lg.symbol)
            if q is None:
                return None
            if how == "mid":
                px = q.mid
                if px is None:
                    # a worthless long can have bid 0: value it at 0
                    if lg.side == "buy" and (q.bid or 0) == 0 and q.ask is not None:
                        px = 0.0
                    else:
                        return None
            else:
                px = (q.bid or 0.0) if lg.side == "buy" else q.ask
                if px is None:
                    return None
            total += lg.sign * lg.ratio * px
        return round(total, 4)

    def _close_body(self, st: Structure, limit: float | None) -> dict:
        cid = st.close_client_id
        if len(st.legs) == 1:
            lg = st.legs[0]
            side = "sell" if lg.side == "buy" else "buy"
            body = {"symbol": lg.symbol, "side": side, "qty": str(st.qty * lg.ratio),
                    "time_in_force": "day", "position_intent": lg.close_intent, "client_order_id": cid}
            if limit is None:
                body["type"] = "market"
            else:
                body.update(type="limit", limit_price=f"{max(limit, 0.01):.2f}")
            return body
        body = {
            "order_class": "mleg", "qty": str(st.qty), "time_in_force": "day", "client_order_id": cid,
            "legs": [{"symbol": lg.symbol, "side": "sell" if lg.side == "buy" else "buy",
                      "ratio_qty": str(lg.ratio), "position_intent": lg.close_intent} for lg in st.legs],
        }
        if limit is None:
            body["type"] = "market"
        else:
            # mleg: positive = debit, negative = credit. A closing credit is sent negative.
            body.update(type="limit", limit_price=f"{-limit:.2f}")
        return body

    async def _close(self, st: Structure, reason: ExitReason, now: datetime) -> str:
        """Close a structure: limit rungs from mid towards the natural price, then
        market on later attempts or when getting out is urgent."""
        today = self._today(now)
        days_left = self._trading_days(today, st.expiry_date)
        # a lone short leg is closed at market straight away; time and ex-dividend exits
        # escalate to market close to expiry or after two unfilled attempts
        urgent = reason is ExitReason.REPAIR or (
            reason in (ExitReason.TIME_EXIT, ExitReason.ADOPTED_EXPIRY, ExitReason.EX_DIVIDEND)
            and (days_left is None or days_left <= 1 or st.close_attempts >= 2))
        if urgent or st.close_attempts >= 3:
            fracs: list[float | None] = [None]
        elif st.close_attempts == 0:
            fracs = [self.settings.limit_slippage_frac, 0.6, 1.0]
        else:
            fracs = [1.0]
        fallback = StructState.REPAIR if reason is ExitReason.REPAIR else StructState.OPEN
        status = "not sent"
        for i, frac in enumerate(fracs):
            if i:
                ok, _ = await self._gate("exit")
                if not ok:
                    break
            limit: float | None = None
            natural = mid = None
            if frac is not None:
                try:
                    quotes = await self._quotes([lg.symbol for lg in st.legs])
                except BrokerError as e:
                    if e.uncertain or e.kind == "auth":
                        raise
                    quotes = {}
                natural = self._value(st, quotes, "natural")
                mid = self._value(st, quotes, "mid")
                if natural is not None and mid is not None:
                    limit = close_limit(natural, mid, frac)
                if limit is None or limit <= 0:
                    if reason in _EXPIRY_DRIVEN:
                        # nothing may be carried into expiry: without a usable quote, go market
                        st.note("no usable quote for an expiry-driven exit; closing at market")
                        limit = None
                    else:
                        # no usable quote: don't send a market order on a bad mark
                        st.note("close skipped: no usable quote")
                        self._save()
                        return "no usable quote"
            st.close_client_id = _cid("close", st.id, st.close_attempts * 10 + i)
            st.exit_reason = reason.value
            st.state = StructState.CLOSING.value
            self._save()  # write-ahead
            self._j("options.exit_submitted", structure_id=st.id, underlying=st.underlying, reason=reason.value,
                    attempt=st.close_attempts, rung=i, limit_credit=limit, natural=natural, mid=mid)
            try:
                order = await self.broker.submit_raw_order(self._close_body(st, limit))
            except BrokerError as e:
                if e.uncertain:
                    self._save()
                    raise
                st.state = fallback.value
                st.close_attempts += 1
                st.note(f"close rejected: {e.message}")
                self._save()
                await self._rejected(st, "close", st.qty, e.message, f"Options close rejected: {st.underlying}",
                                     key=f"opt-close-{st.id}")
                return f"rejected: {e.message}"
            st.close_order_id = order.id
            self._save()
            order = await self._wait_or_cancel(order)
            self._apply_close_order(st, order, now, fallback_state=fallback, count_attempt=False)
            self._save()
            await self._flush_closed(now)
            status = order.status
            if st.st is StructState.CLOSED:
                return status
        st.close_attempts += 1
        self._save()
        return status

    def _apply_close_order(self, st: Structure, order: BrokerOrder, now: datetime,
                           fallback_state: StructState | None = None, count_attempt: bool = True) -> None:
        st.close_order_id = order.id
        filled, credit = self._net_fill(order, st.legs, closing=True)
        if order.status == _FILLED and filled >= st.qty:
            st.exit_credit = credit
            st.closed_at = now.isoformat()
            st.state = StructState.CLOSED.value
            if credit is not None and st.entry_debit is not None:
                st.realized_pnl = st.final_pnl(credit)
                self._record_result(st, now)
            else:
                st.note("fill prices missing; P&L not recorded")
            self._j("options.closed", structure_id=st.id, underlying=st.underlying, strategy=st.strategy,
                    reason=st.exit_reason, entry_debit=st.entry_debit, exit_credit=st.exit_credit,
                    realized_pnl=st.realized_pnl, qty=st.qty, thesis_id=st.thesis_id,
                    held_days=(now - (_parse_ts(st.opened_at) or now)).days)
            self._closed_queue.append(st.id)
            return
        if filled > 0:
            # part of the structures closed: keep the rest open with the same entry debit
            st.close_part(filled, credit)
            st.note(f"closing order {order.status}: {filled} closed, {st.qty} left")
        if count_attempt:
            st.close_attempts += 1
        st.state = (fallback_state or StructState.OPEN).value

    # ======================================================== B3 normalise
    async def close_short_call(self, contract: str) -> CloseResult:
        """Buy back one short call (used by broker_execution's first-run normalise
        before it trims the shares). Marketable limit at the ask; one wait; cancel
        and confirm if not filled. Raises OptionsError if the outcome is unknown."""
        now = self._now()
        occ = parse_occ(contract)
        if occ is None or occ.type != "call":
            return CloseResult(False, 0, None, None, "refused", "not a call contract")
        async with self._lock:
            self._ensure_loaded()
            pos = await self.broker.get_position(contract)
            if pos is None or not pos.is_short:
                return CloseResult(False, 0, None, None, "no_position", "no short position in this contract")
            qty = int(round(abs(pos.qty)))
            ok, why = await self._gate("exit")
            if not ok:
                return CloseResult(False, 0, None, None, "market_closed", why)
            quotes = await self._quotes([contract])
            q = quotes.get(contract)
            limit = q.ask if q is not None and q.ask else None
            cid = _cid("bsc", f"{contract}-{self._today(now).isoformat()}", int(self.meta.get("bsc_attempts", 0)))
            self.meta["bsc_attempts"] = int(self.meta.get("bsc_attempts", 0)) + 1
            self._save()
            body = {"symbol": contract, "side": "buy", "qty": str(qty), "time_in_force": "day",
                    "position_intent": "buy_to_close", "client_order_id": cid}
            if limit is None:
                body["type"] = "market"
            else:
                body.update(type="limit", limit_price=f"{limit:.2f}")
            self._j("options.exit_submitted", contract=contract, reason="normalise_short_call", qty=qty, limit=limit)
            try:
                order = await self.broker.submit_raw_order(body)
            except BrokerError as e:
                if e.uncertain:
                    raise OptionsError(f"buy to close {contract}: outcome unknown ({e})") from e
                await self._rejected(contract, "buy", qty, e.message, f"Options buy-to-close rejected: {contract}",
                                     key=f"opt-bsc-{contract}")
                return CloseResult(False, 0, None, None, "rejected", e.message)
            try:
                order = await self._wait_or_cancel(order)
            except OptionsError:
                raise
            filled = int(round(order.filled_qty or 0))
            done = order.status == _FILLED and filled >= qty
            # the short call may belong to a book structure (adopted): mark it
            for st in self.live():
                if [lg.symbol for lg in st.legs] == [contract] and done:
                    st.state = StructState.CLOSED.value
                    st.exit_reason = "normalise"
                    st.closed_at = now.isoformat()
                    st.exit_credit = -(order.filled_avg_price or 0.0)
                    if st.entry_debit is not None and order.filled_avg_price is not None:
                        st.realized_pnl = st.final_pnl(st.exit_credit)
                    self._closed_queue.append(st.id)
            self._save()
            self._j("options.closed", contract=contract, reason="normalise_short_call", filled_qty=filled,
                    avg_price=order.filled_avg_price, status=order.status)
            await self._flush_closed(now)
            return CloseResult(done, filled, order.filled_avg_price, order.id, order.status,
                               "" if done else f"order ended {order.status}")

    # ================================================================ reporting
    def brief_lines(self, now: datetime | None = None) -> list[str]:
        now = now or self._now()
        lines = [f"Options: mode {self.mode.value}, {len(self.live())} live structure(s), "
                 f"max loss committed {self.open_max_loss():.0f}"]
        blocker = self.open_blocker()
        if blocker and self.mode.may_open:
            lines.append(f"Options entries blocked: {blocker}")
        for st in self.live():
            lines.append(f"- {st.underlying} {st.strategy} x{st.qty} exp {st.expiry} [{st.state}]"
                         + (f" debit {st.entry_debit:.2f}" if st.entry_debit is not None else ""))
        today = self._today(now).isoformat()
        for st in self.structures:
            if st.st is StructState.CLOSED and (st.closed_at or "")[:10] == today:
                pnl = "n/a" if st.realized_pnl is None else f"{st.realized_pnl:+.0f}"
                lines.append(f"- closed {st.underlying} {st.strategy}: {st.exit_reason}, P&L {pnl}")
        return lines


def _after_open(activity: Mapping[str, Any], opened: datetime | None) -> bool:
    """Evidence must postdate the structure: fills by their timestamp; date-only
    non-trade activities (expiry, exercise, assignment) by their date."""
    if opened is None:
        return True
    tt = activity.get("transaction_time")
    if tt:
        t = _parse_ts(tt)
        return t is not None and t >= opened
    d = str(activity.get("date") or "")[:10]
    return bool(d) and d >= opened.astimezone(timezone.utc).date().isoformat()


@dataclass
class _Pos:
    symbol: str
    qty: float
    avg_entry_price: float
