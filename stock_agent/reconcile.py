"""Reconcile the ledger with the broker (the broker is the source of truth).

``reconcile(ledger, reason=..., rebuild=False) -> BookSnapshot``

* Reads account, positions (stock and option rows), open orders and new
  account activities through ``ledger.broker`` (``stock_agent.broker.AlpacaClient``
  or anything with the same async methods). ANY read failure raises
  ``ReconcileError`` and leaves the ledger untouched; a failure is never
  read as "the account is empty".
* Broker quantities, average cost and cash always win. Shorts and fractions
  are kept as they are (signed ``Decimal``).
* Local metadata (position_id, thesis link, entry date, sector, stop) is
  preserved across quantity changes; a symbol that goes flat and reopens
  gets a new position_id.
* Every difference is a typed ``Diff``, journaled, stored in
  ``reconcile_runs`` and alerted when severity >= warning.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from stock_agent.ledger import (
    BookSnapshot,
    Diff,
    DiffKind,
    Holding,
    Ledger,
    OpenOrder,
    OptionLeg,
    PositionMeta,
    Severity,
    new_id,
)


class ReconcileError(Exception):
    """A broker read failed; nothing was changed."""


class BrokerReader(Protocol):
    async def get_account(self) -> Any: ...
    async def get_positions(self) -> list[Any]: ...
    async def list_orders(self, status: str = "open", **kw: Any) -> list[Any]: ...
    async def get_activities(self, activity_types: Any = ("FILL",), *, after: Any = None,
                             page_size: int = 100, max_pages: int = 20) -> list[dict]: ...


# Activity types ingested into broker_activities (OPXRC is the documented
# spelling of exercise; OPEXC is what the live account returns).
ACTIVITY_TYPES = ("FILL", "DIV", "FEE", "OPEXP", "OPASN", "OPEXC", "OPXRC", "OPTRD", "NC", "SC",
                  "SSP", "SPLIT", "SSO", "MA", "REORG", "JNLC", "JNLS", "JNL", "CSD", "CSW", "INT",
                  "INTNRA", "INTTW")
CASH_FLOW_TYPES = ("JNLC", "CSD", "CSW", "JNL")
# Stock splits: "SPLIT" (current; a remove entry with negative qty and an add entry
# with the new qty, same group_id) and "SSP" (former code). Per
# https://docs.alpaca.markets/us/docs/sse-events and trading-api.json ActivityType.
SPLIT_TYPES = ("SPLIT", "SSP")

# a run that would close at least this share of >= 2 open positions re-reads once
MASS_CLOSE_FRACTION = 0.5
REOPEN_TOLERANCE = timedelta(seconds=120)   # bot-recorded open time vs broker fill time




logger = logging.getLogger(__name__)
ZERO = Decimal("0")
OCC_RE = re.compile(r"^([A-Z][A-Z0-9.]{0,5})(\d{6})([CP])(\d{8})$")


def _g(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _dec(x: Any) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        if isinstance(x, float) and not math.isfinite(x):
            return None
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _price(x: Any) -> tuple[Decimal | None, bool]:
    d = _dec(x)
    if d is None or d <= 0:
        return None, False
    return d, True


def _dt(x: Any) -> datetime | None:
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    s = str(x)
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_occ(symbol: str) -> tuple[str, date, str, Decimal] | None:
    m = OCC_RE.match(symbol or "")
    if not m:
        return None
    und, ymd, cp, strike = m.groups()
    exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return und, exp, "call" if cp == "C" else "put", Decimal(strike) / Decimal(1000)


def is_option_row(p: Any) -> bool:
    return _g(p, "asset_class") == "us_option" or bool(OCC_RE.match(str(_g(p, "symbol", ""))))


def _activity_ts(a: dict) -> str:
    return str(a.get("transaction_time") or a.get("date") or a.get("created_at") or "")


def _signed_qty(a: dict) -> Decimal:
    q = _dec(a.get("qty")) or ZERO
    side = str(a.get("side") or "")
    if a.get("activity_type") == "FILL":
        return -abs(q) if side.startswith("sell") else abs(q)
    return q


def _order(o: Any) -> OpenOrder:
    qty = _dec(_g(o, "qty")) or ZERO
    filled = _dec(_g(o, "filled_qty")) or ZERO
    legs = tuple(_order(x) for x in (_g(o, "legs") or ()))
    return OpenOrder(
        order_id=str(_g(o, "id", "")), client_order_id=_g(o, "client_order_id"),
        symbol=str(_g(o, "symbol") or ""), side=str(_g(o, "side") or ""),
        type=str(_g(o, "type") or _g(o, "order_type") or ""), qty=qty, filled_qty=filled,
        remaining_qty=max(qty - filled, ZERO), limit_price=_dec(_g(o, "limit_price")),
        stop_price=_dec(_g(o, "stop_price")), time_in_force=str(_g(o, "time_in_force") or ""),
        asset_class=str(_g(o, "asset_class") or ""), submitted_at=_dt(_g(o, "submitted_at")),
        expires_at=_dt(_g(o, "expires_at")), order_class=str(_g(o, "order_class") or ""),
        position_intent=_g(o, "position_intent") or None, legs=legs)


def _crossed_verticals(legs: list[OptionLeg]) -> set[str]:
    """Legs whose marks are impossible: within one underlying/expiry/right, a
    call with a higher strike (or a put with a lower strike) priced above the
    other. Seen on the live account before the open."""
    bad: set[str] = set()
    groups: dict[tuple, list[OptionLeg]] = {}
    for leg in legs:
        groups.setdefault((leg.underlying, leg.expiry, leg.right), []).append(leg)
    for (_, _, right), grp in groups.items():
        grp = sorted(grp, key=lambda x: x.strike)
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                lo, hi = grp[i], grp[j]
                if lo.price is None or hi.price is None:
                    continue
                if (right == "call" and hi.price > lo.price) or (right == "put" and lo.price > hi.price):
                    bad.update({lo.occ_symbol, hi.occ_symbol})
    return bad


async def _read_broker(broker: Any, after: datetime | None) -> tuple[Any, list, list, list]:
    try:
        account = await broker.get_account()
        positions = await broker.get_positions()
        orders = await broker.list_orders("open", nested=True)
        acts = await broker.get_activities(None, after=after)
    except Exception as e:
        raise ReconcileError(f"broker read failed: {type(e).__name__}: {e}") from e
    if account is None or positions is None or orders is None or acts is None:
        raise ReconcileError("broker read returned nothing")
    return account, list(positions), list(orders), list(acts)


def _ingest_activities(ledger: Ledger, acts: list[dict]) -> list[dict]:
    new: list[dict] = []
    with ledger.store.transaction() as c:
        for a in acts:
            aid = a.get("id")
            if not aid:
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO broker_activities(activity_id, activity_type, ts, symbol, qty, price,"
                " side, order_id, net_amount, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, a.get("activity_type", ""), _activity_ts(a), a.get("symbol"),
                 float(_dec(a.get("qty")) or 0), float(_dec(a.get("price")) or 0) if a.get("price") else None,
                 a.get("side"), a.get("order_id"),
                 float(_dec(a.get("net_amount")) or 0) if a.get("net_amount") is not None else None,
                 json.dumps(a, default=str)))
            if cur.rowcount:
                new.append(a)
                ledger.journal.append("fill" if a.get("activity_type") == "FILL" else "broker_activity",
                                      activity_id=aid, activity_type=a.get("activity_type"),
                                      symbol=a.get("symbol"), qty=a.get("qty"), price=a.get("price"),
                                      side=a.get("side"), order_id=a.get("order_id"), ts=_activity_ts(a),
                                      net_amount=a.get("net_amount"))
            if a.get("activity_type") in CASH_FLOW_TYPES and a.get("net_amount") not in (None, ""):
                c.execute("INSERT OR IGNORE INTO cash_flows(activity_id, date, amount, kind) VALUES (?,?,?,?)",
                          (aid, _activity_ts(a)[:10], float(_dec(a.get("net_amount")) or 0),
                           a.get("activity_type")))
    return new


def _alert_broker_exits(ledger: Ledger, new_acts: list[dict], open_meta: dict, now: datetime) -> int:
    """Trade alert for newly seen exit fills of open stock positions that the bot
    did not submit as an intent (a resting stop, or an order placed elsewhere).
    Only new activities are considered, so a replayed reconcile never re-alerts."""
    by_order: dict[str, list[dict]] = {}
    for a in new_acts:
        if a.get("activity_type") != "FILL" or not str(a.get("side", "")).startswith("sell"):
            continue
        sym = a.get("symbol")
        meta = open_meta.get(sym)
        when = _dt(_activity_ts(a))
        if meta is None or meta.asset_class != "us_equity" or when is None or now - when > timedelta(days=3):
            continue
        by_order.setdefault(str(a.get("order_id") or a.get("id")), []).append(a)
    prev = ledger.last_snapshot()
    n = 0
    for oid, rows in by_order.items():
        if ledger.store.query("SELECT 1 FROM order_intents WHERE broker_order_id=?", (oid,)):
            continue        # the bot's own exit: execution reports it
        sym = str(rows[0]["symbol"])
        meta = open_meta[sym]
        qty = sum((abs(_dec(r.get("qty")) or ZERO) for r in rows), ZERO)
        if qty <= 0:
            continue
        price = sum((abs(_dec(r.get("qty")) or ZERO) * (_dec(r.get("price")) or ZERO) for r in rows), ZERO) / qty
        held = prev.holding(sym) if prev is not None else None
        pnl = ((price - held.avg_cost) * qty).quantize(Decimal("0.01")) if held is not None and held.qty > 0 else None
        last = max((_dt(_activity_ts(r)) for r in rows), default=None)
        days = (last.date() - meta.opened_at.date()).days if last and meta.opened_at else None
        kind = "stop" if meta.stop_order_id and oid == str(meta.stop_order_id) else "broker_exit"
        ledger._raise_exit_fill(symbol=sym, qty=qty, fill_price=price.quantize(Decimal("0.0001")), kind=kind,
                                realized_pnl=pnl, hold_days=days, order_id=oid)
        n += 1
    return n


def _rescale_stops_for_splits(ledger: Ledger, new_acts: list[dict]) -> list[Diff]:
    """Recorded stops are in pre-split dollars after a split. Rescale them for
    newly seen split activity, unless the execution engine keeps its own basis
    (``exec.stop_basis.<sym>``; it rescales on re-placement). Unknown ratio ->
    clear the recorded stop so it is never read as a breach."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for a in new_acts:
        if a.get("activity_type") in SPLIT_TYPES and a.get("symbol"):
            key = (str(a["symbol"]), str(a.get("group_id") or a.get("id")))
            groups.setdefault(key, []).append(a)
    diffs: list[Diff] = []
    for (sym, gid), rows in groups.items():
        meta = ledger.open_position_meta(sym)
        if meta is None or meta.stop_price is None or ledger.store.kv_get(f"exec.stop_basis.{sym}") is not None:
            continue
        when = _dt(_activity_ts(rows[0]))
        if meta.opened_at and when and when < meta.opened_at:
            continue
        removed = sum((-q for q in (_dec(r.get("qty")) or ZERO for r in rows) if q < 0), ZERO)
        added = sum((q for q in (_dec(r.get("qty")) or ZERO for r in rows) if q > 0), ZERO)
        old_stop = Decimal(str(meta.stop_price))
        new_stop = (old_stop * removed / added).quantize(Decimal("0.0001")) if removed > 0 and added > 0 else None
        with ledger.store.transaction() as c:
            c.execute("UPDATE positions_meta SET stop_price=? WHERE position_id=?",
                      (float(new_stop) if new_stop is not None else None, meta.position_id))
        ledger.journal.append("stop.split_adjusted", symbol=sym, group_id=gid, source="reconcile",
                              old_stop=old_stop, new_stop=new_stop,
                              factor=(added / removed) if removed > 0 and added > 0 else None)
        diffs.append(Diff(DiffKind.QTY_CHANGED, sym, Severity.INFO, ledger=str(old_stop),
                          broker=str(new_stop) if new_stop is not None else None,
                          explanation="stock split", action=("recorded stop rescaled" if new_stop is not None
                                                             else "recorded stop cleared: split ratio unknown")))
    return diffs


def _symbol_activities(ledger: Ledger, symbol: str, since: str | None = None) -> list[dict]:
    sql = "SELECT raw_json FROM broker_activities WHERE symbol=?"
    p: list = [symbol]
    if since:
        sql += " AND ts>=?"
        p.append(since)
    sql += " ORDER BY ts, activity_id"
    return [json.loads(r[0]) for r in ledger.store.query(sql, tuple(p))]


def _open_event(ledger: Ledger, symbol: str, qty: Decimal) -> tuple[datetime | None, str, str | None]:
    """Replay stored fills/OPTRD for a symbol; return when it last went from flat
    to non-flat, how (fill | assignment_or_exercise) and that activity's id, if
    the replay ends at ``qty``. Otherwise (None, "import", None)."""
    running = ZERO
    opened: datetime | None = None
    how = "import"
    aid: str | None = None
    for a in _symbol_activities(ledger, symbol):
        t = a.get("activity_type")
        if t in SPLIT_TYPES:
            running += _signed_qty(a)   # a split changes the count, never the opening date
            continue
        if t not in ("FILL", "OPTRD"):
            continue
        before = running
        running += _signed_qty(a)
        if before == 0 and running != 0:
            opened = _dt(_activity_ts(a))
            how = "fill" if t == "FILL" else "assignment_or_exercise"
            aid = str(a.get("id") or "") or None
    if running == qty and opened is not None:
        return opened, how, aid
    return None, "import", None


def _opened_from_history(ledger: Ledger, symbol: str, qty: Decimal) -> tuple[datetime | None, str]:
    opened, how, _ = _open_event(ledger, symbol, qty)
    return opened, how


def _explain_close(ledger: Ledger, symbol: str, since: datetime | None) -> str:
    acts = _symbol_activities(ledger, symbol, since.isoformat()[:10] if since else None)
    for a in reversed(acts):
        t = a.get("activity_type")
        when = _activity_ts(a)[:10]
        if t == "FILL" and str(a.get("side", "")).startswith("sell"):
            return f"sell fill {a.get('qty')} @ {a.get('price')} on {when}"
        if t == "FILL" and str(a.get("side", "")).startswith("buy"):
            return f"buy fill {a.get('qty')} @ {a.get('price')} on {when}"
        if t == "OPTRD":
            return f"option assignment/exercise delivery on {when}"
        if t in ("OPASN",):
            return f"assigned on {when}"
        if t in ("OPEXC", "OPXRC"):
            return f"exercised on {when}"
        if t == "OPEXP":
            return f"expired on {when}"
        if t in ("NC", "SC"):
            return f"name/symbol change on {when}"
        if t in SPLIT_TYPES:
            return f"stock split on {when}"
        if t in ("MA", "REORG", "SSO"):
            return f"corporate action {t} on {when}"
    return ""


def _renames(ledger: Ledger) -> dict[str, str]:
    """old symbol -> new symbol from NC/SC activity pairs (same group_id)."""
    groups: dict[str, dict[str, str]] = {}
    for r in ledger.store.query("SELECT raw_json FROM broker_activities WHERE activity_type IN ('NC','SC')"):
        a = json.loads(r[0])
        g = groups.setdefault(str(a.get("group_id") or a.get("id")), {})
        q = _dec(a.get("qty")) or ZERO
        g["new" if q > 0 else "old"] = a.get("symbol")
    out = {g["old"]: g["new"] for g in groups.values() if g.get("old") and g.get("new")}
    for old, a in (ledger.store.kv_get("symbol_aliases", {}) or {}).items():
        out.setdefault(old, a["new"])
    return out


async def _link_own_orders(ledger: Ledger, broker: Any, acts: list[dict]) -> int:
    """Attach broker order ids to our intents by ``client_order_id`` (R-03).

    After a lost submit reply and a restart, reconcile can run before the
    execution engine re-acks the intent, so the fill's ``order_id`` is unknown.
    For each FILL whose order is unknown while an un-acked intent exists for the
    same symbol, ask the broker for that order and match its client_order_id.
    Read-only at the broker; failures are ignored (the next run retries)."""
    pending = {}
    for r in ledger.store.query("SELECT intent_id, client_order_id, symbol, status FROM order_intents"
                                " WHERE broker_order_id IS NULL"):
        if r["status"] in ("submitting", "submitted", "ambiguous", "partially_filled", "filled"):
            pending.setdefault(str(r["symbol"]), {})[str(r["client_order_id"])] = r["intent_id"]
    if not pending:
        return 0
    seen: set[str] = set()
    linked = 0
    for a in acts:
        oid, sym = a.get("order_id"), a.get("symbol")
        if a.get("activity_type") != "FILL" or not oid or oid in seen or sym not in pending:
            continue
        seen.add(oid)
        if ledger.store.query("SELECT 1 FROM order_intents WHERE broker_order_id=?", (oid,)):
            continue
        try:
            order = await broker.get_order(oid)
        except Exception:
            continue
        intent_id = pending[sym].get(str(_g(order, "client_order_id") or ""))
        if not intent_id:
            continue
        with ledger.store.transaction() as c:
            c.execute("UPDATE order_intents SET broker_order_id=? WHERE intent_id=? AND broker_order_id IS NULL",
                      (oid, intent_id))
        ledger.journal.append("state_event", event="intent_linked", intent_id=intent_id, broker_order_id=oid,
                              via="client_order_id")
        linked += 1
    return linked


def _own_intent_for(ledger: Ledger, open_activity_id: str | None) -> dict | None:
    """The bot's intent whose broker order produced this opening fill, if any."""
    if not open_activity_id:
        return None
    rows = ledger.store.query("SELECT order_id FROM broker_activities WHERE activity_id=?", (open_activity_id,))
    if not rows or not rows[0][0]:
        return None
    hit = ledger.store.query("SELECT intent_id, thesis_ids_json, meta_json, position_id FROM order_intents"
                             " WHERE broker_order_id=?", (rows[0][0],))
    if not hit:
        return None
    r = hit[0]
    meta = json.loads(r["meta_json"] or "{}")
    tids = json.loads(r["thesis_ids_json"] or "[]")
    return {"intent_id": r["intent_id"], "thesis_id": tids[0] if tids else None, "sector": meta.get("sector")}


def _new_meta(c, symbol: str, asset_class: str, opened_at: datetime | None, source: str,
              sector: str | None = None, open_activity_id: str | None = None) -> str:
    pid = new_id("pos")
    notes = {"open_activity_id": open_activity_id} if open_activity_id else {}
    c.execute("INSERT INTO positions_meta(position_id, symbol, asset_class, opened_at, opened_source, sector,"
              " notes_json) VALUES (?,?,?,?,?,?,?)",
              (pid, symbol, asset_class, opened_at.isoformat() if opened_at else None, source, sector,
               json.dumps(notes)))
    return pid


def _validate_read(account: Any, positions: list) -> None:
    """A successful HTTP read can still be unusable. Anything we cannot trust
    aborts the reconcile (ledger untouched) instead of being read as zero/flat."""
    for k in ("equity", "cash"):
        if _dec(_g(account, k)) is None:
            raise ReconcileError(f"account {k} missing or not a finite number")
    seen: set[str] = set()
    for p in positions:
        sym = _g(p, "symbol")
        if not sym or not str(sym).strip():
            raise ReconcileError("position row without a symbol")
        sym = str(sym)
        if sym in seen:
            raise ReconcileError(f"duplicate position row for {sym}")
        seen.add(sym)
        if _dec(_g(p, "qty")) is None:
            raise ReconcileError(f"position {sym}: qty missing or not a finite number")


async def reconcile(ledger: Ledger, *, reason: str, rebuild: bool = False) -> BookSnapshot:
    store, journal = ledger.store, ledger.journal
    broker = ledger.broker
    if broker is None:
        raise ReconcileError("ledger has no broker client")
    now = ledger.now()
    cursor = _dt(store.kv_get("activities_cursor"))
    after = (cursor - timedelta(days=3)) if cursor else None
    try:
        account, positions, orders, acts = await _read_broker(broker, after)
    except ReconcileError as e:
        journal.append("reconcile", ok=False, reason=reason, error=str(e))
        raise
    try:
        _validate_read(account, positions)
    except ReconcileError as e:
        journal.append("reconcile", ok=False, reason=reason, error=str(e))
        # A14c: name the exact cause; the read is retried on every reconcile and the lock
        # clears by itself on the first usable read (key dedupe is the alert sink's job).
        ledger._raise_alert(Severity.CRITICAL, "Broker read unusable; entries locked",
                            f"Cause: {e}. Clears automatically on the next reconcile whose broker read is "
                            "complete (numeric equity/cash, one row per symbol, numeric qty).",
                            key="state:read_unusable")
        raise

    open_meta = {m.symbol: m for m in ledger.all_open_meta()}
    broker_syms = {str(_g(p, "symbol")) for p in positions
                   if (_dec(_g(p, "qty")) or ZERO) != 0}
    to_close = [s for s in open_meta if s not in broker_syms]
    diffs: list[Diff] = []
    # broker activity is fact even if the rest of this run aborts; ingest first so closes can be explained
    new_acts = _ingest_activities(ledger, acts)
    diffs.extend(_rescale_stops_for_splits(ledger, new_acts))
    await _link_own_orders(ledger, broker, acts)
    if ledger.last_reconcile() is not None:      # never alert history on the very first reconcile
        _alert_broker_exits(ledger, new_acts, open_meta, now)
    if len(open_meta) >= 2 and len(to_close) / len(open_meta) >= MASS_CLOSE_FRACTION:
        try:
            positions = list(await broker.get_positions())
            _validate_read(account, positions)
        except Exception as e:
            journal.append("reconcile", ok=False, reason=reason, error=f"re-read failed: {e}")
            raise ReconcileError(f"positions re-read failed: {e}") from e
        broker_syms = {str(_g(p, "symbol")) for p in positions if (_dec(_g(p, "qty")) or ZERO) != 0}
        to_close = [s for s in open_meta if s not in broker_syms]
        if len(to_close) / len(open_meta) >= MASS_CLOSE_FRACTION:
            unexplained = [s for s in to_close if not _explain_close(ledger, s, open_meta[s].opened_at)]
            critical = len(open_meta) >= 3 and len(unexplained) / len(open_meta) >= MASS_CLOSE_FRACTION
            if critical or (len(open_meta) >= 3 and not unexplained):
                diffs.append(Diff(DiffKind.MASS_CLOSE, None, Severity.CRITICAL if critical else Severity.WARNING,
                                  ledger=len(open_meta), broker=len(open_meta) - len(to_close),
                                  explanation=(f"{len(to_close)} of {len(open_meta)} positions gone (confirmed twice),"
                                               f" {len(unexplained)} without broker activity"),
                                  action="applied"))

    if acts:
        newest = max((_activity_ts(a) for a in acts), default="")
        if newest and (cursor is None or newest > cursor.isoformat()):
            store.kv_set("activities_cursor", newest)

    prev_book: dict = store.kv_get("last_book", {}) or {}
    prev_qty: dict = prev_book.get("qty", {})
    renames = _renames(ledger)

    holdings: list[Holding] = []
    legs: list[OptionLeg] = []
    blocked: set[str] = set()
    seen_meta: set[str] = set()

    with store.transaction() as c:
        # metadata follows a name change before anything else
        aliases = ledger.store.kv_get("symbol_aliases", {}) or {}
        broker_qty = {str(_g(p, "symbol")): _dec(_g(p, "qty")) for p in positions}
        for old, new in renames.items():
            if old in aliases and old in prev_qty and broker_qty.get(new) != Decimal(str(prev_qty[old])):
                continue        # a registered alias moves metadata only when the quantity carried over exactly
            if old in open_meta and new in broker_syms and new not in open_meta and old not in broker_syms:
                m = open_meta.pop(old)
                c.execute("UPDATE positions_meta SET symbol=? WHERE position_id=?", (new, m.position_id))
                open_meta[new] = PositionMeta(**{**m.__dict__, "symbol": new})
                diffs.append(Diff(DiffKind.RENAMED, new, Severity.INFO, ledger=old, broker=new,
                                  explanation=("verified corporate action" if old in aliases else "NC/SC activity"),
                                  action="metadata moved"))

        for p in positions:
            sym = str(_g(p, "symbol"))
            qty = _dec(_g(p, "qty")) or ZERO
            if qty == 0:
                continue
            avg = _dec(_g(p, "avg_entry_price")) or ZERO
            price, ok = _price(_g(p, "current_price"))
            mv = _dec(_g(p, "market_value")) or ZERO
            option = is_option_row(p)
            meta = open_meta.get(sym)
            ev_open, how, open_aid = _open_event(ledger, sym, qty)
            source = {"fill": "import", "assignment_or_exercise": "assignment"}.get(how, "import")
            asset_class = "us_option" if option else "us_equity"
            own = _own_intent_for(ledger, open_aid) if meta is None else None
            if meta is None and own is not None:
                # the bot's own filled order: open the position with its intent, thesis and sector
                pid = _new_meta(c, sym, asset_class, ev_open, "bot", sector=own["sector"], open_activity_id=open_aid)
                c.execute("UPDATE positions_meta SET entry_intent_id=?, entry_thesis_id=? WHERE position_id=?",
                          (own["intent_id"], own["thesis_id"], pid))
                meta = PositionMeta(position_id=pid, symbol=sym, asset_class=asset_class, opened_at=ev_open,
                                    opened_source="bot", sector=own["sector"], entry_intent_id=own["intent_id"],
                                    entry_thesis_id=own["thesis_id"], notes={"open_activity_id": open_aid})
                open_meta[sym] = meta
            elif meta is None:
                pid = _new_meta(c, sym, asset_class, ev_open, source, open_activity_id=open_aid)
                meta = PositionMeta(position_id=pid, symbol=sym, asset_class=asset_class, opened_at=ev_open,
                                    opened_source=source, notes={"open_activity_id": open_aid} if open_aid else {})
                open_meta[sym] = meta
                diffs.append(Diff(DiffKind.IMPORTED, sym, Severity.INFO if rebuild else Severity.WARNING,
                                  ledger=None, broker=str(qty),
                                  explanation=_explain_close(ledger, sym, None) or "no matching activity",
                                  action=f"imported ({source})"))
            else:
                known = meta.notes.get("open_activity_id")
                reopened = False
                if ev_open is not None:
                    if known:
                        reopened = open_aid is not None and open_aid != known
                    elif meta.opened_at is not None and meta.opened_source != "v0":
                        reopened = ev_open > meta.opened_at + REOPEN_TOLERANCE
                if reopened:
                    # sold to flat and bought again between refreshes: a new position, not the old one
                    c.execute("UPDATE positions_meta SET closed_at=?, close_cause=? WHERE position_id=?",
                              (ev_open.isoformat(), "flat_then_reopened", meta.position_id))
                    diffs.append(Diff(DiffKind.CLOSED, sym, Severity.INFO, ledger=meta.position_id, broker=str(qty),
                                      explanation="went flat and was reopened between refreshes",
                                      action="old position closed; new position opened"))
                    pid = _new_meta(c, sym, meta.asset_class, ev_open, source, open_activity_id=open_aid)
                    meta = PositionMeta(position_id=pid, symbol=sym, asset_class=meta.asset_class,
                                        opened_at=ev_open, opened_source=source,
                                        notes={"open_activity_id": open_aid} if open_aid else {})
                    open_meta[sym] = meta
                    diffs.append(Diff(DiffKind.IMPORTED, sym, Severity.WARNING, broker=str(qty),
                                      explanation=f"reopened by {how}", action=f"imported ({source})"))
                else:
                    notes = dict(meta.notes)
                    opened_at, src = meta.opened_at, meta.opened_source
                    if open_aid and not known:
                        notes["open_activity_id"] = open_aid
                    if ev_open is not None and (opened_at is None or src == "v0"):
                        # migrated (or unknown) entry date: broker fills are the truth (F-07-03)
                        if opened_at is None or opened_at.date() != ev_open.date():
                            diffs.append(Diff(DiffKind.ENTRY_DATE_CORRECTED, sym, Severity.INFO,
                                              ledger=opened_at.isoformat() if opened_at else None,
                                              broker=ev_open.isoformat(), explanation=f"first {how} after flat",
                                              action="entry date taken from broker fills"))
                        opened_at, src = ev_open, ("v0_verified" if src == "v0" else src)
                    if notes != dict(meta.notes) or opened_at != meta.opened_at or src != meta.opened_source:
                        c.execute("UPDATE positions_meta SET opened_at=?, opened_source=?, notes_json=?"
                                  " WHERE position_id=?",
                                  (opened_at.isoformat() if opened_at else None, src,
                                   json.dumps(notes, default=str), meta.position_id))
                        meta = PositionMeta(**{**meta.__dict__, "opened_at": opened_at, "opened_source": src,
                                               "notes": notes})
                        open_meta[sym] = meta
            seen_meta.add(sym)
            pq = prev_qty.get(sym)
            if pq is not None and Decimal(str(pq)) != qty:
                diffs.append(Diff(DiffKind.QTY_CHANGED, sym, Severity.INFO, ledger=str(pq), broker=str(qty),
                                  explanation=_explain_close(ledger, sym, meta.opened_at) or "",
                                  action="broker quantity adopted"))
            if option:
                parsed = parse_occ(sym)
                if parsed is None:
                    diffs.append(Diff(DiffKind.UNEXPLAINED, sym, Severity.WARNING, broker=str(qty),
                                      explanation="option symbol not parseable"))
                    continue
                und, exp, right, strike = parsed
                if not ok:
                    diffs.append(Diff(DiffKind.PRICE_INVALID, sym, Severity.INFO, broker=_g(p, "current_price"),
                                      action="mark not used"))
                legs.append(OptionLeg(occ_symbol=sym, underlying=und, right=right, strike=strike, expiry=exp,
                                      qty=qty, multiplier=100, avg_cost=avg, price=price, price_ok=ok,
                                      market_value=mv))
                continue
            if qty < 0:
                blocked.add(sym)
                diffs.append(Diff(DiffKind.SHORT_STOCK, sym, Severity.CRITICAL, ledger=None, broker=str(qty),
                                  explanation=_explain_close(ledger, sym, meta.opened_at) or "",
                                  action="new buys blocked until flat; cover via broker_execution"))
            if qty != qty.to_integral_value() and sym not in prev_book.get("fractional", []):
                diffs.append(Diff(DiffKind.FRACTIONAL, sym, Severity.WARNING, broker=str(qty),
                                  action="kept as is"))
            if not ok:
                diffs.append(Diff(DiffKind.PRICE_INVALID, sym, Severity.WARNING, broker=_g(p, "current_price"),
                                  action="mark not used for decisions"))
            holdings.append(Holding(symbol=sym, qty=qty, qty_available=_dec(_g(p, "qty_available")) or ZERO,
                                    avg_cost=avg, price=price, price_ok=ok, market_value=mv,
                                    lastday_price=_dec(_g(p, "lastday_price")), asset_id=_g(p, "asset_id"),
                                    meta=meta))

        for sym, meta in list(open_meta.items()):
            if sym in seen_meta:
                continue
            expl = _explain_close(ledger, sym, meta.opened_at)
            c.execute("UPDATE positions_meta SET closed_at=?, close_cause=? WHERE position_id=?",
                      (now.isoformat(), expl or "unexplained", meta.position_id))
            diffs.append(Diff(DiffKind.CLOSED, sym, Severity.INFO if expl else Severity.CRITICAL,
                              ledger=prev_qty.get(sym), broker="0",
                              explanation=expl or "no activity explains it", action="position closed in ledger"))

    bad = _crossed_verticals(legs)
    if bad:
        legs = [OptionLeg(**{**leg.__dict__, "price_ok": False}) if leg.occ_symbol in bad else leg for leg in legs]
        diffs.append(Diff(DiffKind.PRICE_INVALID, None, Severity.INFO, broker=sorted(bad),
                          explanation="crossed option marks", action="marks not used"))
    grouper = getattr(ledger, "option_leg_grouper", None)
    if grouper is not None:
        for leg in legs:
            try:
                sid = grouper(leg.occ_symbol)
            except Exception:
                sid = None
            if not sid:
                diffs.append(Diff(DiffKind.OPTION_LEG_UNGROUPED, leg.occ_symbol, Severity.WARNING,
                                  broker=str(leg.qty), action="handed to options repair"))

    cash = _dec(_g(account, "cash")) or ZERO
    equity = _dec(_g(account, "equity")) or ZERO
    pc = prev_book.get("cash")
    if pc is not None and Decimal(str(pc)) != cash:
        diffs.append(Diff(DiffKind.CASH_CHANGED, None, Severity.INFO, ledger=str(pc), broker=str(cash)))
    for sym in prev_book.get("blocked", []):
        if sym not in blocked and any(h.symbol == sym and h.qty < 0 for h in holdings):
            blocked.add(sym)

    options_mv = sum((leg.market_value for leg in legs), ZERO)
    quality = "live"
    if any(not h.price_ok for h in holdings) or any(not leg.price_ok for leg in legs):
        quality = "indicative"
    run_id = new_id("rc")
    snap = BookSnapshot(
        as_of=now, run_id=run_id, equity=equity, cash=cash,
        long_mv=_dec(_g(account, "long_market_value")) or ZERO,
        short_mv=_dec(_g(account, "short_market_value")) or ZERO,
        options_mv=options_mv, last_equity=_dec(_g(account, "last_equity")),
        buying_power=_dec(_g(account, "buying_power")),
        holdings=tuple(sorted(holdings, key=lambda h: h.symbol)),
        legs=tuple(sorted(legs, key=lambda x: x.occ_symbol)),
        open_orders=tuple(_order(o) for o in orders), diffs=tuple(diffs), quality=quality,
        blocked_symbols=frozenset(blocked),
        account_flags={k: _g(account, k) for k in ("shorting_enabled", "trading_blocked", "account_blocked",
                                                  "multiplier", "options_trading_level", "status", "sma", "trade_suspended_by_user",
                                                  "last_maintenance_margin")},
        regt_buying_power=_dec(_g(account, "regt_buying_power")),
        maintenance_margin=_dec(_g(account, "maintenance_margin")),
        initial_margin=_dec(_g(account, "initial_margin")),
        options_buying_power=_dec(_g(account, "options_buying_power")),
        non_marginable_buying_power=_dec(_g(account, "non_marginable_buying_power")),
        margin_call=(bool(_g(account, "margin_call")) if _g(account, "margin_call") is not None else None),
    )
    # B1: resume only after a CLEAN reconcile. Shorts are blocked per symbol, not globally.
    blocking = [d for d in diffs if d.severity == Severity.CRITICAL and d.kind != DiffKind.SHORT_STOCK]
    clean = not blocking
    report = {"clean": clean, "diffs": [{"kind": d.kind.value, "symbol": d.symbol, "severity": d.severity.value,
                         "ledger": d.ledger, "broker": d.broker, "explanation": d.explanation,
                         "action": d.action} for d in diffs],
              "n_holdings": len(holdings), "n_legs": len(legs), "quality": quality}
    with store.transaction() as c:
        c.execute("INSERT INTO reconcile_runs(run_id, at, reason, ok, n_diffs, report_json) VALUES (?,?,?,?,?,?)",
                  (run_id, now.isoformat(), reason, 1, len(diffs), json.dumps(report, default=str)))
    store.kv_set("last_book", {
        "qty": {h.symbol: str(h.qty) for h in holdings} | {leg.occ_symbol: str(leg.qty) for leg in legs},
        "cash": str(cash), "blocked": sorted(blocked),
        "fractional": sorted(h.symbol for h in holdings if h.is_fractional), "at": now.isoformat()})
    for d in diffs:
        journal.append("reconcile_diff", run_id=run_id, diff_kind=d.kind.value, symbol=d.symbol,
                       severity=d.severity.value, ledger=d.ledger, broker=d.broker,
                       explanation=d.explanation, action=d.action)
        if d.severity != Severity.INFO:
            ledger._raise_alert(d.severity, f"Books: {d.kind.value} {d.symbol or ''}".strip(),
                                f"ledger={d.ledger} broker={d.broker}. {d.explanation} {d.action}".strip(),
                                key=f"reconcile:{d.kind.value}:{d.symbol}")
    journal.append("reconcile", ok=True, clean=clean, run_id=run_id, reason=reason, n_diffs=len(diffs),
                   quality=quality,
                   equity=str(equity), cash=str(cash), n_holdings=len(holdings), n_legs=len(legs))
    if store.trading_block() and blocking:
        ledger._raise_alert(Severity.CRITICAL, "Books not clean; trading stays blocked",
                            "Cause: " + "; ".join(f"{d.kind.value} {d.symbol or ''}".strip() for d in blocking[:10])
                            + f". Block: {store.trading_block()}. These positions are now closed in the ledger, so"
                            " the next reconcile is clean and clears the block unless the broker book changes"
                            " again without activity.",
                            key="state:still_blocked")
    elif store.trading_block():
        old = store.trading_block()
        store.clear_trading_block(by=f"reconcile {run_id}")
        ledger._raise_alert(Severity.WARNING, "Books rebuilt from broker",
                            f"Trading resumed after a clean reconcile. Previous block: {old}",
                            key="state:rebuilt")
    return snap
