"""Trust decision for the drawdown close only (notes/TRUSTED_CLOSE_FIX_BRIEF.md,
valuation rule in notes/trusted_close_gap.md sections 3 and 7).

The reconcile snapshot is "indicative" whenever any holding or option leg lacks a
usable mark. Close equity is the broker's account equity, which already carries
each leg at its broker market value. For the drawdown close only, a snapshot whose
sole invalid marks are option legs is accepted at ``broker equity - haircut``,
where the haircut is never negative, so the recorded close never overstates
today's equity relative to broker equity. An understated close can slightly lower
a later high-water mark and so slightly reduce a later measured fall; that is
accepted (lead ruling) and the mark is labelled a conservative estimate:

* every stock holding (long or short) must be validly priced, else untrusted;
* each invalid leg (unpriced, or crossed by reconcile) is paired with the unique
  leg of the same underlying/expiry/right, opposite sign and same |qty|; two or
  more candidates, or a crossed group of three or more legs, is untrusted;
* debit vertical (long at the more in-the-money strike): every unpriced leg must be
  carried at market value 0; haircut = max(0, mv_long + mv_short);
* credit vertical: untrusted;
* lone long: unpriced needs market value 0 (missing is consistent), haircut 0;
  crossed gives haircut max(0, mv);
* lone short: needs today's valid underlying price (held stock mark, else the
  official session close) more than 10% from the strike (exactly 10% is near the
  money), and a standard contract; haircut = max(0, intrinsic x 100 x |qty| + mv),
  the part of intrinsic not already carried as a liability in broker equity;
* a short leg or paired leg with no reported market value is untrusted;
* the resulting equity must stay positive.

Nothing here changes ``snapshot.quality`` for any other consumer; the scheduler
records an adjusted copy and never stores it as the last snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

ZERO = Decimal(0)

# Stable reason codes for the drawdown close job (scheduler_deploy_engineer formats them).
CLOSE_CLOCK_UNVERIFIED = "close_clock_unverified"
CLOSE_SNAPSHOT_NONPOSITIVE = "close_snapshot_nonpositive"
CLOSE_SNAPSHOT_STALE = "close_snapshot_stale"
CLOSE_SNAPSHOT_PRECLOSE = "close_snapshot_preclose"
CLOSE_TRADING_BLOCK = "close_trading_block"
CLOSE_UNPRICED_STOCK = "close_unpriced_stock"
CLOSE_OPTION_MARKS_CROSSED = "close_option_marks_crossed"
CLOSE_OPTION_UNDERLYING_UNPRICED = "close_option_underlying_unpriced"
CLOSE_SNAPSHOT_INDICATIVE = "close_snapshot_indicative"
CLOSE_SNAPSHOT_QUALITY_UNKNOWN = "close_snapshot_quality_unknown"
CLOSE_HAIRCUT_NONPOSITIVE = "close_haircut_nonpositive"
CLOSE_OPTION_MARK_INCONSISTENT = "close_option_mark_inconsistent"
CLOSE_OPTION_NEAR_MONEY = "close_option_near_money"

CLOSE_OPTION_PAIRING_AMBIGUOUS = "close_option_pairing_ambiguous"
CLOSE_OPTION_CREDIT_VERTICAL = "close_option_credit_vertical"
CLOSE_OPTION_MARK_MISSING = "close_option_mark_missing"
CLOSE_OPTION_UNPARSEABLE = "close_option_unparseable"
CLOSE_OPTION_ADJUSTED_CONTRACT = "close_option_adjusted_contract"

NEAR_MONEY_FRACTION = Decimal("0.10")
STANDARD_MULTIPLIER = 100
KNOWN_QUALITIES = frozenset({"live", "indicative"})
# Detail keys persisted in equity_marks.extra_json for a conservative close (restored on replay).
PERSISTED_KEYS = ("close_basis", "conservative", "summary", "broker_equity", "haircut", "close_equity",
                  "unpriced_option_legs", "legs", "estimated_option_value")
# Display-only estimate for an unpriced/crossed leg: intrinsic value from the underlying's price.
# It never changes the haircut, the close equity, or anything an entry decision reads.
ESTIMATE_BASIS = "intrinsic_from_underlying"


def persisted_detail(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """The conservative-close fields of a recorded mark's extra (empty for an ordinary close)."""
    if not extra or extra.get("conservative") is not True:
        return {}
    return {key: extra[key] for key in PERSISTED_KEYS if key in extra}


@dataclass(frozen=True)
class CloseTrust:
    trusted: bool
    reason: str | None
    broker_equity: Decimal
    equity: Decimal | None = None          # conservative close equity when trusted
    haircut: Decimal = ZERO
    unpriced_option_legs: tuple[str, ...] = ()   # every invalid leg (unpriced or crossed)
    symbols: tuple[str, ...] = ()          # the symbols that caused an untrusted result
    legs: tuple[Mapping[str, Any], ...] = field(default=())   # per-leg valuation detail

    @property
    def conservative(self) -> bool:
        return bool(self.unpriced_option_legs)

    @property
    def basis(self) -> str:
        return "conservative_estimate" if self.conservative else "broker_equity"

    @property
    def summary(self) -> str:
        if not self.conservative:
            return "broker equity, all marks valid"
        return (f"conservative estimate: {len(self.unpriced_option_legs)} option legs unpriced, "
                f"haircut ${self.haircut:,.2f}")

    @property
    def estimated_option_value(self) -> Decimal | None:
        """Sum of the intrinsic estimates of the invalid legs, or None if any leg has none."""
        return _estimate_total(self.legs)

    def detail(self) -> dict[str, Any]:
        est = self.estimated_option_value
        out: dict[str, Any] = {
            "close_basis": self.basis,
            "conservative": self.conservative,
            "summary": self.summary,
            "broker_equity": str(self.broker_equity),
            "unpriced_option_legs": list(self.unpriced_option_legs),
            "haircut": str(self.haircut),
            "legs": [dict(item) for item in self.legs],
        }
        if self.conservative:
            out["estimated_option_value"] = str(est) if est is not None else None
        if self.equity is not None:
            out["close_equity"] = str(self.equity)
        if self.reason is not None:
            out["reason"] = self.reason
            out["symbols"] = list(self.symbols)
        return out

    def close_snapshot(self, snapshot):
        """Snapshot to record as the trusted close (close path only; never stored as last_snapshot)."""
        if not self.trusted or self.equity is None:
            raise ValueError("untrusted close has no recordable snapshot")
        return replace(snapshot, equity=self.equity, quality="live")


class _Untrusted(Exception):
    def __init__(self, reason: str, symbols) -> None:
        super().__init__(reason)
        self.reason, self.symbols = reason, tuple(symbols)


def _d(value) -> Decimal:
    return Decimal(str(value))


def _intrinsic(right: str, strike: Decimal, underlying: Decimal) -> Decimal:
    if right == "call":
        return max(ZERO, underlying - strike)
    if right == "put":
        return max(ZERO, strike - underlying)
    raise ValueError("unknown option right")


def _key(leg) -> tuple:
    return (leg.underlying, leg.expiry, leg.right)


def _crossed(leg) -> bool:
    return not leg.price_ok and leg.price is not None


def _has_unparseable_option(snapshot) -> list[str]:
    return sorted(str(d.symbol) for d in snapshot.diffs
                  if getattr(d, "explanation", "") == "option symbol not parseable")


def _candidates(leg, same) -> list:
    return [o for o in same if o is not leg and _d(o.qty) * _d(leg.qty) < 0
            and abs(_d(o.qty)) == abs(_d(leg.qty))]


def _groups(snapshot) -> list[tuple]:
    """Invalid legs grouped as ("pair", long, short), ("long", leg) or ("short", leg).

    Raises _Untrusted for ambiguous pairing or a crossed group of three or more legs.
    """
    out, done = [], set()
    for leg in snapshot.legs:
        if leg.price_ok or leg.occ_symbol in done:
            continue
        same = [o for o in snapshot.legs if _key(o) == _key(leg)]
        if _crossed(leg) and len(same) >= 3:
            raise _Untrusted(CLOSE_OPTION_MARKS_CROSSED, [o.occ_symbol for o in same])
        partners = _candidates(leg, same)
        if len(partners) > 1:
            raise _Untrusted(CLOSE_OPTION_PAIRING_AMBIGUOUS, [leg.occ_symbol, *(o.occ_symbol for o in partners)])
        if partners:
            other = partners[0]
            back = _candidates(other, same)
            if len(back) != 1 or other.occ_symbol in done:
                # The pairing must be mutual and unique, or the result would depend on traversal order.
                raise _Untrusted(CLOSE_OPTION_PAIRING_AMBIGUOUS,
                                 [leg.occ_symbol, other.occ_symbol, *(o.occ_symbol for o in back)])
            long_leg, short_leg = (leg, other) if _d(leg.qty) > 0 else (other, leg)
            done.update({leg.occ_symbol, other.occ_symbol})
            out.append(("pair", long_leg, short_leg))
        else:
            done.add(leg.occ_symbol)
            out.append(("long" if _d(leg.qty) > 0 else "short", leg))
    return out


BOOK_TOLERANCE = Decimal("0.05")


def _book_adds_up(snapshot) -> bool:
    """Broker equity equals cash plus every carried market value (within rounding)."""
    total = _d(snapshot.cash) + sum((_d(h.market_value) for h in snapshot.holdings), ZERO) \
        + sum((_d(o.market_value) for o in snapshot.legs), ZERO)
    return abs(total - _d(snapshot.equity)) <= BOOK_TOLERANCE


def _standard_root(symbol: str) -> bool:
    # Reconcile hardcodes multiplier 100; adjusted contracts carry a non-alpha root (e.g. "XYZ1").
    return bool(symbol) and symbol.replace(".", "").isalpha()


def _leg_row(leg, group: str, haircut: Decimal, **extra) -> dict[str, Any]:
    row = {"symbol": leg.occ_symbol, "qty": str(leg.qty), "status": "crossed" if _crossed(leg) else
           ("unpriced" if not leg.price_ok else "valid"), "group": group,
           "market_value": str(leg.market_value) if leg.market_value_reported else None,
           "haircut": str(haircut)}
    row.update(extra)
    return row


def _estimate_total(rows) -> Decimal | None:
    vals = [row.get("estimate_value") for row in rows if row.get("status") != "valid"]
    if not vals or any(v is None for v in vals):
        return None
    return sum((_d(v) for v in vals), ZERO)


def _with_estimates(rows: list[dict[str, Any]], snapshot, closes: Mapping[str, Decimal]) -> tuple:
    """Add a flagged intrinsic estimate to every invalid leg row (display only)."""
    by_symbol = {leg.occ_symbol: leg for leg in snapshot.legs}
    out = []
    for row in rows:
        leg = by_symbol.get(row.get("symbol"))
        if leg is not None and row.get("status") != "valid":
            est = leg_estimate(snapshot, leg, closes)
            if row.get("estimate_value") is None or est["estimate_value"] is not None:
                row = {**row, **est}  # never overwrite a known estimate with None
        out.append(row)
    return tuple(out)


def leg_estimate(snapshot, leg, closes: Mapping[str, Decimal]) -> dict[str, Any]:
    """Intrinsic value of one leg (signed by position: a short is a liability), flagged as an
    estimate. Only prices already in hand (held stock mark, mandatory session closes): no I/O.
    Without a valid underlying price, or for a non-standard contract, no estimate."""
    price, source = underlying_price_source(snapshot, leg.underlying, closes)
    if price is None or not _standard_root(leg.underlying):
        why = "no_underlying_price" if price is None else "non_standard_contract"
        return {"estimate_value": None, "estimate_basis": why, "estimated": False}
    intrinsic = _intrinsic(leg.right, _d(leg.strike), price) * STANDARD_MULTIPLIER * _d(leg.qty)
    return {"estimate_value": str(intrinsic), "estimate_basis": ESTIMATE_BASIS, "estimated": True,
            "estimate_underlying_price": str(price), "estimate_underlying_source": source}


def assess_close(snapshot, underlying_closes: Mapping[str, Decimal] | None = None) -> CloseTrust:
    """Decide whether this snapshot's equity may be recorded as the trusted close.

    ``underlying_closes`` holds TODAY's official session closes for underlyings that
    are not held; a held stock's valid mark is preferred.
    """
    broker = _d(snapshot.equity)
    closes = underlying_closes or {}
    invalid = tuple(sorted(leg.occ_symbol for leg in snapshot.legs if not leg.price_ok))

    def untrusted(reason: str, symbols=(), rows=(), haircut=ZERO) -> CloseTrust:
        return CloseTrust(False, reason, broker, haircut=haircut, symbols=tuple(sorted(set(symbols))),
                          unpriced_option_legs=invalid, legs=_with_estimates(list(rows), snapshot, closes))

    if snapshot.quality not in KNOWN_QUALITIES:
        return untrusted(CLOSE_SNAPSHOT_QUALITY_UNKNOWN)
    unpriced_stock = [h.symbol for h in snapshot.holdings
                      if not h.price_ok or h.price is None or _d(h.price) <= 0]
    if unpriced_stock:
        return untrusted(CLOSE_UNPRICED_STOCK, unpriced_stock)
    unparseable = _has_unparseable_option(snapshot)
    if unparseable:
        # Reconcile drops such a row from legs, so it could otherwise be silently ignored.
        return untrusted(CLOSE_OPTION_UNPARSEABLE, unparseable)
    if not invalid:
        if snapshot.quality != "live":
            return untrusted(CLOSE_SNAPSHOT_INDICATIVE)
        return CloseTrust(True, None, broker, equity=broker)
    malformed = [o.occ_symbol for o in snapshot.legs if getattr(o, "market_value_malformed", False)]
    if malformed:
        # A malformed value is not "missing": it may hide a carried value we cannot read.
        return untrusted(CLOSE_OPTION_MARK_INCONSISTENT, malformed)
    rows: list[dict[str, Any]] = []
    haircut = ZERO
    try:
        for group in _groups(snapshot):
            if group[0] == "pair":
                _, long_leg, short_leg = group
                pair = [long_leg.occ_symbol, short_leg.occ_symbol]
                lk, sk = _d(long_leg.strike), _d(short_leg.strike)
                debit = lk < sk if long_leg.right == "call" else lk > sk
                if not debit:
                    raise _Untrusted(CLOSE_OPTION_CREDIT_VERTICAL, pair)
                missing = [leg.occ_symbol for leg in (long_leg, short_leg) if not leg.market_value_reported]
                if missing:
                    raise _Untrusted(CLOSE_OPTION_MARK_MISSING, missing)
                carried = [leg.occ_symbol for leg in (long_leg, short_leg)
                           if not leg.price_ok and leg.price is None and _d(leg.market_value) != 0]
                if carried:
                    raise _Untrusted(CLOSE_OPTION_MARK_INCONSISTENT, carried)
                cut = max(ZERO, _d(long_leg.market_value) + _d(short_leg.market_value))
                haircut += cut
                rows.append(_leg_row(long_leg, "debit_vertical", cut, partner=short_leg.occ_symbol))
                rows.append(_leg_row(short_leg, "debit_vertical", ZERO, partner=long_leg.occ_symbol))
            elif group[0] == "long":
                leg = group[1]
                if _crossed(leg):
                    if not leg.market_value_reported:
                        raise _Untrusted(CLOSE_OPTION_MARK_MISSING, [leg.occ_symbol])
                    cut = max(ZERO, _d(leg.market_value))
                elif _d(leg.market_value) != 0:
                    raise _Untrusted(CLOSE_OPTION_MARK_INCONSISTENT, [leg.occ_symbol])
                elif not leg.market_value_reported and not _book_adds_up(snapshot):
                    # A missing value is "consistent" only if broker equity carries nothing for it.
                    raise _Untrusted(CLOSE_OPTION_MARK_INCONSISTENT, [leg.occ_symbol])
                else:
                    cut = ZERO
                haircut += cut
                rows.append(_leg_row(leg, "lone_long", cut))
            else:
                leg = group[1]
                if not leg.market_value_reported:
                    raise _Untrusted(CLOSE_OPTION_MARK_MISSING, [leg.occ_symbol])
                if not _standard_root(leg.underlying):
                    raise _Untrusted(CLOSE_OPTION_ADJUSTED_CONTRACT, [leg.occ_symbol])
                price, source = underlying_price_source(snapshot, leg.underlying, closes)
                if price is None:
                    raise _Untrusted(CLOSE_OPTION_UNDERLYING_UNPRICED, [leg.occ_symbol, leg.underlying])
                strike = _d(leg.strike)
                if abs(strike - price) <= NEAR_MONEY_FRACTION * price:
                    raise _Untrusted(CLOSE_OPTION_NEAR_MONEY, [leg.occ_symbol])
                intrinsic = _intrinsic(leg.right, strike, price) * STANDARD_MULTIPLIER * abs(_d(leg.qty))
                cut = max(ZERO, intrinsic + _d(leg.market_value))
                haircut += cut
                rows.append(_leg_row(leg, "lone_short", cut, underlying_price=str(price),
                                     underlying_source=source, intrinsic=str(intrinsic)))
    except _Untrusted as exc:
        return untrusted(exc.reason, exc.symbols, rows, haircut)
    equity = broker - haircut
    if equity <= 0:
        return untrusted(CLOSE_HAIRCUT_NONPOSITIVE, (), rows, haircut)
    return CloseTrust(True, None, broker, equity=equity, haircut=haircut,
                      unpriced_option_legs=invalid, legs=_with_estimates(rows, snapshot, closes))


def short_leg_underlyings(snapshot) -> tuple[str, ...]:
    """Underlyings of lone invalid short legs not held with a valid mark (need today's session close)."""
    try:
        groups = _groups(snapshot)
    except _Untrusted:
        return ()
    need = {g[1].underlying for g in groups if g[0] == "short"
            and underlying_price(snapshot, g[1].underlying, {}) is None}
    return tuple(sorted(need))


def underlying_price_source(snapshot, symbol: str, closes: Mapping[str, Decimal]) -> tuple[Decimal | None, str | None]:
    stock = snapshot.holding(symbol)
    if stock is not None and stock.price_ok and stock.price is not None and _d(stock.price) > 0:
        return _d(stock.price), "held_stock_mark"
    close = closes.get(symbol)
    if close is None:
        return None, None
    close = _d(close)
    return (close, "session_close") if close.is_finite() and close > 0 else (None, None)


def underlying_price(snapshot, symbol: str, closes: Mapping[str, Decimal]) -> Decimal | None:
    return underlying_price_source(snapshot, symbol, closes)[0]
