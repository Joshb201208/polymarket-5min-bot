"""The options book: structures, their legs, cool-downs, and the two-way
reconcile against the broker.

Rules that hold here:
- A failed broker request changes nothing (the caller gets the exception).
- A structure the broker no longer shows is only closed with evidence (a fill,
  an expiry, an exercise or an assignment activity). Without evidence it becomes
  ``unknown`` and new opens are blocked until it is resolved. P&L is never
  invented.
- Broker option positions that belong to no structure are adopted, so nothing
  the broker holds is invisible to the exit manager.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from stock_agent.options_policy import (
    MULTIPLIER,
    Cooldown,
    Direction,
    Strategy,
    parse_occ,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class StructState(StrEnum):
    PENDING = "pending"  # opening order sent, not terminal
    OPEN = "open"
    CLOSING = "closing"  # closing order sent, not terminal
    CLOSED = "closed"
    VOID = "void"  # opening order ended with no fill
    UNKNOWN = "unknown"  # broker and book disagree with no evidence; blocks opens
    REPAIR = "repair"  # short stock or an orphan short leg needs fixing

    @property
    def live(self) -> bool:
        return self in (StructState.PENDING, StructState.OPEN, StructState.CLOSING,
                        StructState.UNKNOWN, StructState.REPAIR)


@dataclass
class Leg:
    symbol: str
    side: str  # opening side: buy | sell
    type: str
    strike: float
    expiry: str  # ISO date
    ratio: int = 1

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1

    @property
    def close_intent(self) -> str:
        return "sell_to_close" if self.side == "buy" else "buy_to_close"


@dataclass
class Structure:
    id: str
    underlying: str
    strategy: str
    direction: str | None
    qty: int
    expiry: str
    legs: list[Leg]
    state: str = StructState.PENDING.value
    source: str = "scan"  # scan | news | research | owner | adopted
    thesis_id: str | None = None
    owner_directive_added: str | None = None  # owner-backed: the directive generation ("added" date)
    zero_fill_confirmed: bool = False  # VOID because the broker confirmed a terminal open with no fill
    owner_claim_released: bool = False  # owner-backed: durable one-shot claim handed back
    conviction: int | None = None
    story_id: str | None = None
    opened_at: str | None = None
    entry_debit: float | None = None  # per share, from fills
    width: float | None = None
    max_loss: float | None = None  # dollars, whole structure
    limit_debit: float | None = None
    open_client_id: str | None = None
    open_order_id: str | None = None
    close_client_id: str | None = None
    close_order_id: str | None = None
    close_attempts: int = 0
    close_session: str | None = None  # session (ISO date) the try counters below belong to
    close_tries_session: int = 0  # unfilled or rejected close tries this session
    close_retry_after: str | None = None  # back-off: no close order before this time
    close_zero_form: str | None = None  # "0.00" rejected -> "credit_0.01" tried once -> "stopped"
    exit_reason: str | None = None
    exit_credit: float | None = None  # per share, from fills
    realized_pnl: float | None = None
    partial_pnl: float = 0.0  # realised on contracts closed before the final close
    partial_qty: int = 0
    closed_at: str | None = None
    entry_greeks: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def st(self) -> StructState:
        return StructState(self.state)

    @property
    def strat(self) -> Strategy:
        try:
            return Strategy(self.strategy)
        except ValueError:
            return Strategy.UNKNOWN

    @property
    def dir(self) -> Direction | None:
        try:
            return Direction(self.direction) if self.direction else None
        except ValueError:
            return None

    @property
    def expiry_date(self) -> date:
        return date.fromisoformat(self.expiry)

    def expected_positions(self) -> dict[str, int]:
        """Signed contracts per leg symbol while the structure is open."""
        return {leg.symbol: leg.sign * leg.ratio * self.qty for leg in self.legs}

    def note(self, text: str) -> None:
        self.notes = (self.notes + [text])[-20:]

    def close_part(self, qty: int, credit: float | None) -> None:
        """`qty` contracts closed at `credit` per share; the rest stays open. Max loss
        shrinks pro rata; the part's P&L is kept for the final result."""
        if qty <= 0:
            return
        if credit is not None and self.entry_debit is not None:
            self.partial_pnl = round(self.partial_pnl + (credit - self.entry_debit) * 100 * qty, 2)
        else:
            self.note("partial close without fill prices; its P&L is not recorded")
        if self.max_loss is not None and self.qty > 0:
            self.max_loss = round(self.max_loss * (self.qty - qty) / self.qty, 2)
        self.qty -= qty
        self.partial_qty += qty

    def final_pnl(self, credit: float | None) -> float | None:
        """Total realised P&L when the remaining `qty` closes at `credit` per share."""
        if credit is None or self.entry_debit is None:
            return None
        return round(self.partial_pnl + (credit - self.entry_debit) * 100 * self.qty, 2)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Structure:
        d = dict(d)
        d["legs"] = [Leg(**leg) for leg in d.get("legs", [])]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def structure_mark(st: Structure, marks: Mapping[str, float | None]) -> float | None:
    """Per-share value of the open structure from per-share leg marks, signed like
    `entry_debit` (long legs add, short legs subtract). None if any leg is unmarked."""
    total = 0.0
    for leg in st.legs:
        px = marks.get(leg.symbol)
        if px is None:
            return None
        total += leg.sign * leg.ratio * float(px)
    return round(total, 4)


def open_pnl(st: Structure, mark: float | None) -> float | None:
    """Unrealised dollars on the contracts still open: (value - entry) x 100 x qty.
    Debit structures gain as value rises; credit structures (negative entry and value)
    gain as the value moves back toward zero."""
    if mark is None or st.entry_debit is None:
        return None
    return round((mark - st.entry_debit) * 100 * st.qty, 2)


def new_structure_id() -> str:
    return f"os-{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------- storage
class OptionsStore:
    """JSON file with atomic replace. One writer (the options manager)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> tuple[list[Structure], dict[str, Cooldown], dict[str, Any]]:
        if not self.path.exists():
            return [], {}, {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            # Unreadable state: keep the file for inspection; the caller rebuilds from the broker.
            bad = self.path.with_suffix(f".corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
            try:
                self.path.replace(bad)
            except OSError:
                pass
            raise OptionsStateError(f"options book unreadable ({e}); quarantined to {bad.name}") from e
        try:
            return _decode(raw)
        except (TypeError, ValueError, KeyError) as e:
            raise OptionsStateError(f"options book malformed ({e})") from e

    def save(self, structures: Iterable[Structure], cooldowns: Mapping[str, Cooldown], meta: Mapping[str, Any]) -> None:
        data = _encode(structures, cooldowns, meta)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class OptionsStateError(RuntimeError):
    pass


class OptionsStoreUnavailable(RuntimeError):
    """The backing store could not be read at all (e.g. not opened yet). Nothing is
    rebuilt or overwritten; the caller retries later."""


class KvOptionsStore:
    """The same book kept as one JSON value in the shared SQLite store (state_ledger)."""

    KEY = "options.book.v1"

    def __init__(self, store: Any, key: str | None = None):
        self.store = store
        self.key = key or self.KEY

    def load(self) -> tuple[list[Structure], dict[str, Cooldown], dict[str, Any]]:
        try:
            raw = self.store.kv_get(self.key, None)
        except Exception as e:  # the store itself is unavailable: not a corrupt book
            raise OptionsStoreUnavailable(f"options store unavailable ({e})") from e
        if not raw:
            return [], {}, {}
        try:
            return _decode(raw)
        except (TypeError, ValueError, KeyError) as e:
            raise OptionsStateError(f"options book malformed ({e})") from e

    def save(self, structures: Iterable[Structure], cooldowns: Mapping[str, Cooldown], meta: Mapping[str, Any]) -> None:
        self.store.kv_set(self.key, _encode(structures, cooldowns, meta))


def _encode(structures, cooldowns, meta) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "structures": [asdict(s) for s in structures],
        "cooldowns": {k: {**asdict(v), "until": v.until.isoformat() if v.until else None}
                      for k, v in cooldowns.items()},
        "meta": dict(meta),
    }


def _decode(raw: Mapping[str, Any]):
    structs = [Structure.from_dict(s) for s in raw.get("structures", [])]
    cds = {}
    for k, v in raw.get("cooldowns", {}).items():
        v = dict(v)
        v["until"] = date.fromisoformat(v["until"]) if v.get("until") else None
        cds[k] = Cooldown(**v)
    return structs, cds, dict(raw.get("meta", {}))


# ----------------------------------------------------------------- reconcile
@dataclass
class ReconcileReport:
    adopted: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    repair: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    short_stock: dict[str, float] = field(default_factory=dict)
    deliveries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.unknown or self.repair or self.mismatched or self.short_stock)


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _ts(a: Mapping[str, Any]) -> str:
    return str(a.get("transaction_time") or a.get("date") or "")


def evidence_by_symbol(activities: Iterable[Mapping[str, Any]], since: str | None) -> dict[str, list[Mapping]]:
    out: dict[str, list[Mapping]] = defaultdict(list)
    for a in activities:
        sym = a.get("symbol")
        if not sym:
            continue
        if since and _ts(a) and _ts(a)[:10] < since[:10]:
            continue
        out[str(sym)].append(a)
    return out


def leg_close_value(leg: Leg, acts: list[Mapping[str, Any]]) -> tuple[float, str] | None:
    """Per-share value this leg was closed at, from activities, or None if no evidence.

    - a closing FILL: its price
    - OPEXP: 0 (expired worthless)
    - OPEXC or OPXRC (both names appear in Alpaca's docs) / OPASN: intrinsic value at the strike is realised through the share
      trade (OPTRD); the option itself closes at 0 and the shares are reported as
      a delivery for repair/trim.
    """
    closing_side = "sell" if leg.side == "buy" else "buy"
    fills = [a for a in acts if a.get("activity_type") == "FILL" and a.get("side") == closing_side]
    if fills:
        qty = sum(_f(a.get("qty")) for a in fills)
        if qty > 0:
            return sum(_f(a.get("price")) * _f(a.get("qty")) for a in fills) / qty, "fill"
    kinds = {a.get("activity_type") for a in acts}
    if "OPEXP" in kinds:
        return 0.0, "expired"
    if kinds & {"OPEXC", "OPXRC"}:
        return 0.0, "exercised"
    if "OPASN" in kinds:
        return 0.0, "assigned"
    return None


def infer_strategy(legs: list[Leg]) -> tuple[Strategy, Direction | None, float | None]:
    if len(legs) == 1 and legs[0].side == "buy":
        return ((Strategy.LONG_CALL, Direction.BULLISH, None) if legs[0].type == "call"
                else (Strategy.LONG_PUT, Direction.BEARISH, None))
    if len(legs) == 2 and {lg.side for lg in legs} == {"buy", "sell"} and legs[0].type == legs[1].type:
        long_leg = next(lg for lg in legs if lg.side == "buy")
        short_leg = next(lg for lg in legs if lg.side == "sell")
        width = round(abs(short_leg.strike - long_leg.strike), 4)
        if long_leg.type == "call" and short_leg.strike > long_leg.strike:
            return Strategy.BULL_CALL_SPREAD, Direction.BULLISH, width
        if long_leg.type == "put" and short_leg.strike < long_leg.strike:
            return Strategy.BEAR_PUT_SPREAD, Direction.BEARISH, width
        return Strategy.UNKNOWN, None, width  # a credit vertical: defined risk, width known
    return Strategy.UNKNOWN, None, None


def adopt_positions(option_positions: list[Any], now: datetime,
                    hints: Mapping[str, Mapping[str, Any]] | None = None) -> list[Structure]:
    """Group unowned broker option positions into structures: same underlying,
    expiry, type and contract count with one long and one short -> a vertical;
    a long alone -> a long option; anything else -> its own leg, strategy
    unknown (a naked short becomes REPAIR). The broker is the truth for legs,
    quantities and prices; `hints` (legacy rows by contract symbol) only steer
    pairing towards the legacy group and are recorded in the notes."""
    hints = hints or {}
    groups: dict[tuple[str, str, str], list[tuple[Leg, int, float]]] = defaultdict(list)
    for p in option_positions:
        occ = parse_occ(p.symbol)
        if occ is None:
            continue
        qty = int(round(p.qty))
        if qty == 0:
            continue
        leg = Leg(p.symbol, "buy" if qty > 0 else "sell", occ.type, occ.strike, occ.expiry.isoformat())
        groups[(occ.underlying, occ.expiry.isoformat(), occ.type)].append((leg, abs(qty), float(p.avg_entry_price or 0)))
    out: list[Structure] = []
    for (und, exp, _t), items in groups.items():
        longs = sorted([i for i in items if i[0].side == "buy"], key=lambda i: i[0].strike)
        shorts = sorted([i for i in items if i[0].side == "sell"], key=lambda i: i[0].strike)
        while longs and shorts:
            lg, lq, lp = longs.pop(0)
            # pair with the short from the same legacy group, else the same count, else the first
            grp = (hints.get(lg.symbol) or {}).get("group")
            idx = next((k for k, s in enumerate(shorts) if grp and (hints.get(s[0].symbol) or {}).get("group") == grp),
                       next((k for k, s in enumerate(shorts) if s[1] == lq), 0))
            sg, sq, sp = shorts.pop(idx)
            q = min(lq, sq)
            out.append(_adopted(und, exp, [lg, sg], q, round(lp - sp, 4), now))
            if lq > q:
                longs.insert(0, (lg, lq - q, lp))
            if sq > q:
                shorts.insert(idx, (sg, sq - q, sp))
        for lg, q, px in longs:
            out.append(_adopted(und, exp, [lg], q, round(px, 4), now))
        for sg, q, px in shorts:
            st = _adopted(und, exp, [sg], q, round(-px, 4), now)
            st.state = StructState.REPAIR.value
            st.note("adopted a short option without a long leg")
            out.append(st)
    for st in out:
        hs = [hints[lg.symbol] for lg in st.legs if lg.symbol in hints]
        if hs:
            h = hs[0]
            st.note(f"legacy group {h.get('group')} ({h.get('strategy')}), legacy entry {h.get('entry_price')}")
    return out


def _adopted_max_loss(legs: list[Leg], debit: float, width: float | None, qty: int) -> float | None:
    """Defined max loss of an adopted structure in dollars: the debit paid, or for a
    credit vertical the width less the credit. None when it is not defined (a lone
    short leg), which keeps it a REPAIR item."""
    if len(legs) == 2 and width is not None:
        per = debit if debit > 0 else width + debit  # debit < 0 is a credit
        return round(max(per, 0.0) * MULTIPLIER * qty, 2)
    if len(legs) == 1 and legs[0].side == "buy":
        return round(max(debit, 0.0) * MULTIPLIER * qty, 2)
    return None


def _adopted(und: str, exp: str, legs: list[Leg], qty: int, debit: float, now: datetime) -> Structure:
    strat, direction, width = infer_strategy(legs)
    st = Structure(
        id=new_structure_id(), underlying=und, strategy=strat.value,
        direction=direction.value if direction else None, qty=qty, expiry=exp, legs=legs,
        state=StructState.OPEN.value, source="adopted", opened_at=now.isoformat(),
        entry_debit=debit, width=width,
        max_loss=_adopted_max_loss(legs, debit, width, qty),
    )
    st.note("adopted from broker positions; entry from broker average prices")
    return st


# ------------------------------------------------------------ legacy import
LEGACY_HINTS_KEY = "legacy_hints"


def legacy_hints_from_rows(open_rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Contract symbol -> legacy group facts, from options_portfolio.json open rows."""
    hints: dict[str, dict[str, Any]] = {}
    for row in open_rows:
        if not isinstance(row, Mapping):
            continue
        group = str(row.get("position_id") or "") or None
        base = {"group": group, "strategy": row.get("strategy"), "entry_price": row.get("entry_price"),
                "opened_at": row.get("entry_date") or row.get("opened_at")}
        for key, side_key in (("contract_symbol", "side"), ("leg2_contract_symbol", "leg2_side")):
            sym = row.get(key)
            if sym and parse_occ(str(sym)) is not None:
                hints[str(sym)] = {**base, "side": row.get(side_key)}
    return hints


def import_legacy_options(conn: Any, open_rows: list[dict], key: str = KvOptionsStore.KEY) -> int:
    """state_ledger's migrate_v0 options hook: `(conn, open_rows) -> rows imported`.

    Legacy rows are not trusted as positions. They are stored as pairing hints in
    the options book; the first reconcile adopts whatever the broker actually holds
    (spreads paired by legacy group, lone longs as long options, lone shorts as
    REPAIR) and the exit manager closes adopted structures at T-2. Runs inside the
    migration transaction, so it writes through `conn`.
    """
    hints = legacy_hints_from_rows(open_rows)
    if not hints:
        return 0
    row = conn.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
    book = json.loads(row[0]) if row else {"schema": SCHEMA_VERSION, "structures": [], "cooldowns": {}, "meta": {}}
    meta = dict(book.get("meta") or {})
    meta[LEGACY_HINTS_KEY] = {**dict(meta.get(LEGACY_HINTS_KEY) or {}), **hints}
    book["meta"] = meta
    conn.execute("INSERT OR REPLACE INTO kv(key, value_json, updated_at) VALUES (?,?,?)",
                 (key, json.dumps(book, default=str), datetime.now(timezone.utc).isoformat()))
    return sum(1 for r in open_rows if isinstance(r, Mapping) and parse_occ(str(r.get("contract_symbol") or "")))
