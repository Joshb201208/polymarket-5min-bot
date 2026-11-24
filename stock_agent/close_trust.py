"""Trust decision for the drawdown close only (notes/TRUSTED_CLOSE_FIX_BRIEF.md,
valuation rule in notes/trusted_close_gap.md sections 3 and 7).

The reconcile snapshot is "indicative" whenever any holding or option leg lacks a
usable mark. Close equity is the broker's account equity, which already carries
each leg at its broker market value. For the drawdown close only, a snapshot whose
sole invalid marks are option legs is accepted at ``broker equity - haircut``,
where the haircut can only lower equity (the safe direction for every brake):

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
  official session close) at least 10% from the strike, and a standard contract;
  haircut = max(0, intrinsic x 100 x |qty| + mv);
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

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "close_basis": self.basis,
            "conservative": self.conservative,
            "summary": self.summary,
            "broker_equity": str(self.broker_equity),
            "unpriced_option_legs": list(self.unpriced_option_legs),
            "haircut": str(self.haircut),
            "legs": [dict(item) for item in self.legs],
        }
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
        partners = [o for o in same if o is not leg and _d(o.qty) * _d(leg.qty) < 0
                    and abs(_d(o.qty)) == abs(_d(leg.qty))]
        if len(partners) > 1:
            raise _Untrusted(CLOSE_OPTION_PAIRING_AMBIGUOUS, [leg.occ_symbol, *(o.occ_symbol for o in partners)])
        if partners:
            other = partners[0]
            long_leg, short_leg = (leg, other) if _d(leg.qty) > 0 else (other, leg)
            done.update({leg.occ_symbol, other.occ_symbol})
            out.append(("pair", long_leg, short_leg))
        else:
            done.add(leg.occ_symbol)
            out.append(("long" if _d(leg.qty) > 0 else "short", leg))
    return out


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
                          unpriced_option_legs=invalid, legs=tuple(rows))

    unpriced_stock = [h.symbol for h in snapshot.holdings
                      if not h.price_ok or h.price is None or _d(h.price) <= 0]
    if unpriced_stock:
        return untrusted(CLOSE_UNPRICED_STOCK, unpriced_stock)
    if not invalid:
        if snapshot.quality != "live":
            return untrusted(CLOSE_SNAPSHOT_INDICATIVE)
        return CloseTrust(True, None, broker, equity=broker)
    unparseable = _has_unparseable_option(snapshot)
    if unparseable:
        return untrusted(CLOSE_OPTION_UNPARSEABLE, unparseable)
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
                if abs(strike - price) < NEAR_MONEY_FRACTION * price:
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
                      unpriced_option_legs=invalid, legs=tuple(rows))


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
