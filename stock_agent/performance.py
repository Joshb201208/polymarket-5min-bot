"""Performance from broker truth (pure helpers + the daily close step).

Headline numbers come from live broker equity against the money actually put
in (cash flows), never from the legacy JSON curve or legacy realised P&L.

* Day P&L       = equity - previous trusted close mark (not Alpaca ``last_equity``,
                  which can omit positions).
* Total return  = (equity - net deposits) / net deposits.
* TWR           = chained daily returns between close marks, each adjusted for
                  the cash flows dated inside that interval.
* Drawdown      = equity / high-water mark of close marks - 1 (starts fresh at
                  cut-over: legacy marks are never used).
* Realised P&L  = FIFO over broker FILL activities (stop fills included), with
                  options expiry/assignment/exercise closing the option at zero;
                  dividends and fees reported separately.

``close_session`` is the only function with side effects: it records the close
mark and journals ``performance.close`` and ``shadow.stop`` rows. It never
places orders and never blocks trading; shadow-stop problems become warnings.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .ledger import BookSnapshot, CashFlow, EquityMark, Ledger

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
DEPOSIT_KINDS = frozenset({"JNLC", "CSD", "CSW", "JNL"})
OPTION_CLOSE_AT_ZERO = frozenset({"OPEXP", "OPASN", "OPEXC", "OPXRC"})
SHADOW_RECENT_DAYS = 30

DailyBars = Callable[[str, date, date], Awaitable[Sequence[Any]]]


def _d(x: Any) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        v = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None
    return v if v.is_finite() else None


def _pct(num: Decimal, den: Decimal) -> Decimal | None:
    return None if not den else (num / den * 100).quantize(Decimal("0.0001"))


# ------------------------------------------------------------------ pure
def previous_close(marks: Sequence[EquityMark], before: date) -> EquityMark | None:
    prior = [m for m in marks if m.quality == "close" and m.session_date < before]
    return max(prior, key=lambda m: m.session_date) if prior else None


def day_pnl(marks: Sequence[EquityMark], equity: Decimal, session_date: date) -> tuple[Decimal | None, Decimal | None]:
    """(P&L, %) versus the previous trusted close mark; (None, None) if there is none."""
    prev = previous_close(marks, session_date)
    if prev is None:
        return None, None
    pnl = equity - prev.equity
    return pnl, _pct(pnl, prev.equity)


def net_deposits(flows: Iterable[CashFlow], through: date | None = None) -> Decimal:
    return sum((f.amount for f in flows if f.kind in DEPOSIT_KINDS and (through is None or f.date <= through)),
               ZERO)


def twr(marks: Sequence[EquityMark], flows: Sequence[CashFlow]) -> Decimal | None:
    """Time-weighted return in % over close marks, flows assumed at the interval end."""
    closes = sorted((m for m in marks if m.quality == "close"), key=lambda m: m.session_date)
    if len(closes) < 2:
        return None
    growth = Decimal(1)
    for a, b in zip(closes, closes[1:], strict=False):
        f = sum((x.amount for x in flows if x.kind in DEPOSIT_KINDS and a.session_date < x.date <= b.session_date),
                ZERO)
        if a.equity <= 0:
            return None
        growth *= (b.equity - f) / a.equity
    return ((growth - 1) * 100).quantize(Decimal("0.0001"))


def drawdown(marks: Sequence[EquityMark], equity: Decimal | None = None) -> tuple[Decimal | None, Decimal | None]:
    """(drawdown %, high-water mark) from close marks (and today's equity if given)."""
    vals = [m.equity for m in marks if m.quality == "close"]
    if equity is not None:
        vals.append(equity)
    if not vals:
        return None, None
    hwm = max(vals)
    cur = vals[-1] if equity is None else equity
    return (_pct(cur - hwm, hwm) if hwm > 0 else None), hwm


def _is_option(symbol: str) -> bool:
    return len(symbol) > 10 and symbol[-9] in "CP" and symbol[-8:].isdigit()


@dataclass
class Realised:
    by_symbol: dict[str, Decimal] = field(default_factory=dict)
    total: Decimal = ZERO
    dividends: Decimal = ZERO
    fees: Decimal = ZERO
    unmatched: list[str] = field(default_factory=list)   # closes with no opening lot in the history


def realised_fifo(activities: Iterable[dict]) -> Realised:
    """FIFO realised P&L from Alpaca activities (oldest first is not required).

    Long and short lots are both supported; options use a multiplier of 100.
    OPEXP/OPASN/OPEXC close the option leg at zero (the stock side of an
    assignment arrives as its own OPTRD/FILL and is handled as a trade).
    """
    acts = sorted(activities, key=lambda a: (str(a.get("transaction_time") or a.get("date") or ""),
                                             str(a.get("id") or "")))
    lots: dict[str, deque[list[Decimal]]] = defaultdict(deque)   # symbol -> [qty(signed), price]
    out = Realised()

    def trade(sym: str, qty: Decimal, price: Decimal) -> None:
        mult = Decimal(100) if _is_option(sym) else Decimal(1)
        q = lots[sym]
        remaining = qty
        while remaining != 0 and q and (q[0][0] > 0) != (remaining > 0):
            lot = q[0]
            take = min(abs(remaining), abs(lot[0]))
            sign = Decimal(1) if lot[0] > 0 else Decimal(-1)          # closing a long (+) or a short (-)
            pnl = (price - lot[1]) * take * sign * mult
            out.by_symbol[sym] = out.by_symbol.get(sym, ZERO) + pnl
            out.total += pnl
            lot[0] -= take * sign
            remaining += take * sign
            if lot[0] == 0:
                q.popleft()
        if remaining != 0:
            q.append([remaining, price])

    for a in acts:
        t = a.get("activity_type")
        sym = str(a.get("symbol") or "")
        if t in ("FILL", "OPTRD"):
            qty, price = _d(a.get("qty")), _d(a.get("price"))
            if not sym or qty is None or price is None:
                continue
            side = str(a.get("side") or "")
            signed = (-abs(qty) if side.startswith("sell") else abs(qty)) if t == "FILL" or side else qty
            trade(sym, signed, price)
        elif t in OPTION_CLOSE_AT_ZERO and sym:
            held = sum((lot[0] for lot in lots[sym]), ZERO)
            if held == 0:
                out.unmatched.append(f"{t} {sym}")
                continue
            trade(sym, -held, ZERO)
        elif t == "DIV":
            out.dividends += _d(a.get("net_amount")) or ZERO
        elif t == "FEE":
            out.fees += _d(a.get("net_amount")) or ZERO
    return out


# ------------------------------------------------------------ shadow stops
def _anchor_key(r: dict) -> str:
    return str(r.get("fill_id") or r.get("order_id") or
               f"{r.get('position_id')}:{r.get('session_date')}:{r.get('fill_price')}:{r.get('qty')}")


def _as_date(x: Any) -> date | None:
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    if isinstance(x, datetime):
        return x.date()
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _bar(b: Any):
    from .risk_manager import ShadowDailyBar

    g = b.get if isinstance(b, dict) else (lambda k, d=None: getattr(b, k, d))
    sd = _as_date(g("session_date") or g("date") or g("timestamp") or g("t"))
    vals = [_d(g(k) if g(k) is not None else g(k[0])) for k in ("open", "high", "low", "close")]
    if sd is None or any(v is None for v in vals):
        return None
    return ShadowDailyBar(session_date=sd, open=vals[0], high=vals[1], low=vals[2], close=vals[3])


async def shadow_stops(ledger: Ledger, session_date: date, daily_bars: DailyBars,
                       warnings: list[str]) -> int:
    """Journal one ``shadow.stop`` row per anchor per completed session (idempotent)."""
    from .risk_manager import ShadowEntryAnchor, shadow_stop_results

    done: dict = ledger.store.kv_get("shadow.stop.done", {}) or {}
    open_ids = {m.position_id for m in ledger.all_open_meta()}
    since = (session_date - timedelta(days=SHADOW_RECENT_DAYS)).isoformat()
    closed = {r["position_id"]: _as_date(r["closed_at"]) for r in ledger.store.query(
        "SELECT position_id, closed_at FROM positions_meta WHERE closed_at IS NOT NULL AND closed_at>=?", (since,))}
    rows = 0
    for r in ledger.journal.read(kinds={"entry.filled"}):
        pid = r.get("position_id")
        if not pid and r.get("intent_id"):
            hit = ledger.store.query("SELECT position_id FROM positions_meta WHERE entry_intent_id=?"
                                     " ORDER BY opened_at DESC LIMIT 1", (r["intent_id"],))
            pid = hit[0][0] if hit else None
        if pid not in open_ids and pid not in closed:
            continue
        key = _anchor_key(r)
        end = min(session_date, closed.get(pid) or session_date)
        if done.get(key) and done[key] >= end.isoformat():
            continue
        try:
            anchor = ShadowEntryAnchor(
                position_id=str(pid), symbol=str(r["symbol"]), session_date=_as_date(r["session_date"]),
                fill_price=Decimal(str(r["fill_price"])), qty=Decimal(str(r["qty"])),
                stop_fraction=Decimal(str(r["stop_fraction"])),
                stop_price=_d(r.get("stop_price")), atr=_d(r.get("atr")), source=str(r.get("source") or ""))
            if anchor.session_date is None or anchor.session_date >= end:
                continue
            raw = await daily_bars(anchor.symbol, anchor.session_date, end)
            bars = sorted((b for b in (_bar(x) for x in raw or ()) if b and b.session_date <= end),
                          key=lambda b: b.session_date)
            if not bars or bars[-1].session_date != end:
                # the provider has not published the bar for ``end`` yet: retry at a later close
                warnings.append(f"shadow stop {anchor.symbol}: no completed bar for {end.isoformat()} yet")
                continue
            results = shadow_stop_results(anchor, bars)
        except Exception as e:  # never let analysis break the close job
            warnings.append(f"shadow stop {r.get('symbol')}: {type(e).__name__}: {e}")
            continue
        if not results:
            continue
        ledger.journal.append("shadow.stop", anchor=key, position_id=pid, symbol=anchor.symbol,
                              entry_session=anchor.session_date, through_session=end,
                              closed=pid in closed, results=[{
                                  "variant": x.variant, "stopped": x.stopped, "exit_session": x.exit_session,
                                  "price": x.price, "return_fraction": x.return_fraction, "pnl": x.pnl,
                                  "through_session": x.through_session} for x in results])
        done[key] = end.isoformat()
        rows += 1
    ledger.store.kv_set("shadow.stop.done", done)
    return rows


# --------------------------------------------------------------- close job
@dataclass
class CloseReport:
    session_date: date
    equity: Decimal
    day_pnl: Decimal | None
    day_pnl_pct: Decimal | None
    total_return_pct: Decimal | None
    twr_pct: Decimal | None
    drawdown_pct: Decimal | None
    hwm: Decimal | None
    deposits: Decimal
    realised_pnl: Decimal
    dividends: Decimal
    fees: Decimal
    shadow_rows: int = 0
    warnings: list[str] = field(default_factory=list)


async def close_session(ledger: Ledger, snapshot: BookSnapshot, session_date: date, *,
                        daily_bars: DailyBars | None = None) -> CloseReport:
    """Daily close: record the trusted close mark, compute and journal performance."""
    warnings: list[str] = []
    marks_before = ledger.equity_marks("close")
    equity = Decimal(str(snapshot.equity))
    pnl, pnl_pct = day_pnl(marks_before, equity, session_date)
    quality = "close" if snapshot.quality == "live" else "indicative"
    if quality != "close":
        warnings.append(f"snapshot quality {snapshot.quality}: mark stored as indicative")
    ledger.record_equity_mark(session_date, quality, snapshot)
    marks = ledger.equity_marks("close")
    flows = ledger.cash_flows()
    dep = net_deposits(flows, session_date)
    dd, hwm = drawdown(marks)
    acts = [json.loads(r[0]) for r in ledger.store.query("SELECT raw_json FROM broker_activities")]
    real = realised_fifo(acts)
    if real.unmatched:
        warnings.append(f"{len(real.unmatched)} option close events without an opening lot in history")
    rep = CloseReport(session_date=session_date, equity=equity, day_pnl=pnl, day_pnl_pct=pnl_pct,
                      total_return_pct=_pct(equity - dep, dep) if dep > 0 else None,
                      twr_pct=twr(marks, flows), drawdown_pct=dd, hwm=hwm, deposits=dep,
                      realised_pnl=real.total, dividends=real.dividends, fees=real.fees, warnings=warnings)
    if daily_bars is not None:
        try:
            rep.shadow_rows = await shadow_stops(ledger, session_date, daily_bars, warnings)
        except Exception as e:
            warnings.append(f"shadow stops skipped: {type(e).__name__}: {e}")
    ledger.journal.append("performance.close", session_date=session_date, equity=equity, day_pnl=pnl,
                          day_pnl_pct=pnl_pct, total_return_pct=rep.total_return_pct, twr_pct=rep.twr_pct,
                          drawdown_pct=dd, hwm=hwm, deposits=dep, realised_pnl=real.total,
                          dividends=real.dividends, fees=real.fees, quality=quality,
                          shadow_rows=rep.shadow_rows, warnings=warnings)
    return rep


__all__ = ["CloseReport", "Realised", "close_session", "day_pnl", "drawdown", "net_deposits", "previous_close",
           "realised_fifo", "shadow_stops", "twr"]
