"""Pure options policy: contract choice, pricing, sizing, exits, cool-down.

No I/O here. Everything takes plain data and returns plain data so the rules
can be tested exhaustively. Money is in US dollars per structure unless a name
says ``per_contract`` or ``per_share``. One contract = 100 shares.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from stock_agent.options_settings import OptionsSettings

MULTIPLIER = 100
_OCC = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")


class Strategy(StrEnum):
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    UNKNOWN = "unknown"  # adopted from the broker, shape not recognised

    @property
    def is_spread(self) -> bool:
        return self in (Strategy.BULL_CALL_SPREAD, Strategy.BEAR_PUT_SPREAD)


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


_DIRECTION_ALIASES = {
    "bullish": Direction.BULLISH, "buy": Direction.BULLISH, "long": Direction.BULLISH, "bull": Direction.BULLISH,
    "bearish": Direction.BEARISH, "sell": Direction.BEARISH, "short": Direction.BEARISH, "bear": Direction.BEARISH,
}
# Research directions that contradict an open structure (research_schemas.Direction values).
_BREAKS = {
    Direction.BULLISH: {"bearish", "exit_proposed", "avoid", "sell"},
    Direction.BEARISH: {"buy", "bullish"},
}


def breaks_thesis(structure: Direction | None, research_direction: object) -> bool:
    """True when the current research view contradicts the structure's direction.
    HOLD, INSUFFICIENT_EVIDENCE, a stale or a missing thesis never break it."""
    if structure is None or research_direction is None:
        return False
    return str(getattr(research_direction, "value", research_direction)).strip().lower() in _BREAKS[structure]


def parse_direction(raw: object) -> Direction | None:
    if raw is None:
        return None
    return _DIRECTION_ALIASES.get(str(getattr(raw, "value", raw)).strip().lower())


# --------------------------------------------------------------------- OCC
@dataclass(frozen=True)
class OccSymbol:
    underlying: str
    expiry: date
    type: str  # call | put
    strike: float


def parse_occ(symbol: str) -> OccSymbol | None:
    m = _OCC.match(symbol or "")
    if not m:
        return None
    und, ymd, cp, k = m.groups()
    return OccSymbol(und, date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:])),
                     "call" if cp == "C" else "put", int(k) / 1000)


def occ_symbol(underlying: str, expiry: date, type_: str, strike: float) -> str:
    return f"{underlying}{expiry:%y%m%d}{'C' if type_ == 'call' else 'P'}{round(strike * 1000):08d}"


# ------------------------------------------------------------------ quotes
@dataclass(frozen=True)
class ContractQuote:
    symbol: str
    underlying: str
    expiry: date
    type: str
    strike: float
    bid: float | None
    ask: float | None
    quote_at: datetime | None
    open_interest: int | None = None
    delta: float | None = None
    iv: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2


WORTHLESS_ASK = 0.10


def mark_problem(q: ContractQuote | None, now: datetime, s: OptionsSettings) -> str | None:
    """Why this quote can't be used as a mark for exits or P&L, or None.
    Zero, one-sided and crossed quotes (common before the open) are not marks;
    the only zero bid accepted is a near-worthless contract (ask <= 0.10)."""
    if q is None:
        return "no quote"
    if q.ask is None or q.ask <= 0:
        return "no ask"
    if q.bid is not None and q.ask < q.bid:
        return "crossed quote"
    if (q.bid is None or q.bid <= 0) and q.ask > WORTHLESS_ASK:
        return "no bid"
    if q.quote_at is None:
        return "quote has no timestamp"
    if (now - q.quote_at).total_seconds() > s.max_quote_age_s:
        return "stale quote"
    return None


def quote_problem(q: ContractQuote, now: datetime, s: OptionsSettings, *, need_bid: bool = True) -> str | None:
    """Why this leg's quote is not good enough to trade on, or None."""
    if q.ask is None or q.ask <= 0:
        return "no ask"
    if need_bid and (q.bid is None or q.bid <= 0):
        return "no bid"
    if q.bid is not None and q.ask < q.bid:
        return "crossed quote"
    if q.quote_at is None:
        return "quote has no timestamp"
    age = (now - q.quote_at).total_seconds()
    if age > s.max_quote_age_s:
        return f"quote is {age:.0f}s old"
    mid = q.mid
    if mid is not None and (q.ask - q.bid) / mid > s.max_leg_spread_pct:
        return f"bid-ask spread {(q.ask - q.bid) / mid:.0%} of mid"
    if q.open_interest is not None and q.open_interest < s.min_open_interest:
        return f"open interest {q.open_interest}"
    return None


# ---------------------------------------------------------------- proposal
@dataclass(frozen=True)
class LegSpec:
    symbol: str
    side: str  # buy | sell (opening side)
    type: str
    strike: float
    expiry: date
    ratio: int = 1

    @property
    def open_intent(self) -> str:
        return "buy_to_open" if self.side == "buy" else "sell_to_open"

    @property
    def close_intent(self) -> str:
        return "sell_to_close" if self.side == "buy" else "buy_to_close"


@dataclass(frozen=True)
class Proposal:
    underlying: str
    strategy: Strategy
    direction: Direction
    expiry: date
    legs: tuple[LegSpec, ...]
    limit_debit: float  # per share, positive = debit
    mid_debit: float
    natural_debit: float
    width: float | None  # strike distance for spreads
    spot: float
    delta: float | None = None
    iv: float | None = None
    notes: tuple[str, ...] = ()

    @property
    def max_loss_per_contract(self) -> float:
        return round(self.limit_debit * MULTIPLIER, 2)

    @property
    def max_profit_per_contract(self) -> float | None:
        if self.width is None:
            return None
        return round((self.width - self.limit_debit) * MULTIPLIER, 2)


@dataclass(frozen=True)
class Rejection:
    underlying: str
    reason: str


def _round_up_cent(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def _limit_between(mid: float, natural: float, frac: float) -> float:
    return _round_up_cent(mid + max(natural - mid, 0.0) * frac)


def pick_expiry(expiries: Sequence[date], today: date, s: OptionsSettings,
                trading_days: Callable[[date, date], int | None]) -> date | None:
    best: tuple[int, date] | None = None
    for exp in sorted(set(expiries)):
        dte = (exp - today).days
        if dte < s.min_dte or dte > s.max_dte:
            continue
        td = trading_days(today, exp)
        if td is None or td <= s.exit_trading_days_before_expiry + 5:
            continue
        key = (abs(dte - s.target_dte), exp)
        if best is None or key < best:
            best = key
    return best[1] if best else None


def build_proposal(
    *, underlying: str, direction: Direction, conviction: int, spot: float,
    chain: Sequence[ContractQuote], today: date, now: datetime, s: OptionsSettings,
    trading_days: Callable[[date, date], int | None],
    shapes: Sequence[str] = ("single", "spread"),
) -> Proposal | Rejection:
    """Choose a structure for a view. `shapes` limits what the account level allows. Conviction >= single_option_min_conviction
    prefers a single long option; otherwise a vertical debit spread. If the
    preferred shape cannot be built from good quotes the other is tried."""
    if spot <= 0:
        return Rejection(underlying, "no underlying price")
    kind = "call" if direction is Direction.BULLISH else "put"
    exp = pick_expiry([q.expiry for q in chain if q.type == kind], today, s, trading_days)
    if exp is None:
        return Rejection(underlying, f"no expiry between {s.min_dte} and {s.max_dte} days")
    legs = sorted((q for q in chain if q.type == kind and q.expiry == exp), key=lambda q: q.strike)
    if not legs:
        return Rejection(underlying, "empty chain")
    order = ("single", "spread") if conviction >= s.single_option_min_conviction else ("spread", "single")
    order = tuple(x for x in order if x in shapes)
    reasons = []
    for shape in order:
        out = (_spread(underlying, direction, spot, legs, now, s) if shape == "spread"
               else _single(underlying, direction, spot, legs, now, s))
        if isinstance(out, Proposal):
            return out
        reasons.append(f"{shape}: {out.reason}")
    return Rejection(underlying, "; ".join(reasons))


def _atm(legs: Sequence[ContractQuote], spot: float) -> ContractQuote:
    return min(legs, key=lambda q: (abs(q.strike - spot), q.strike))


def _single(und, direction, spot, legs, now, s) -> Proposal | Rejection:
    q = _atm(legs, spot)
    bad = quote_problem(q, now, s)
    if bad:
        return Rejection(und, f"{q.symbol} {bad}")
    limit = _limit_between(q.mid, q.ask, s.limit_slippage_frac)
    strat = Strategy.LONG_CALL if direction is Direction.BULLISH else Strategy.LONG_PUT
    return Proposal(und, strat, direction, q.expiry, (LegSpec(q.symbol, "buy", q.type, q.strike, q.expiry),),
                    limit, round(q.mid, 4), q.ask, None, spot, q.delta, q.iv)


def _spread(und, direction, spot, legs, now, s) -> Proposal | Rejection:
    long_q = _atm(legs, spot)
    min_w, max_w = spot * s.min_width_pct, spot * s.max_width_pct
    if direction is Direction.BULLISH:
        shorts = [q for q in legs if q.strike - long_q.strike >= min_w - 1e-9 and q.strike - long_q.strike <= max_w]
        short_q = min(shorts, key=lambda q: q.strike) if shorts else None
        strat = Strategy.BULL_CALL_SPREAD
    else:
        shorts = [q for q in legs if long_q.strike - q.strike >= min_w - 1e-9 and long_q.strike - q.strike <= max_w]
        short_q = max(shorts, key=lambda q: q.strike) if shorts else None
        strat = Strategy.BEAR_PUT_SPREAD
    if short_q is None:
        return Rejection(und, f"no listed strike gives a width of {s.min_width_pct:.0%}-{s.max_width_pct:.0%} of spot")
    for q in (long_q, short_q):
        bad = quote_problem(q, now, s)
        if bad:
            return Rejection(und, f"{q.symbol} {bad}")
    width = round(abs(short_q.strike - long_q.strike), 4)
    mid = long_q.mid - short_q.mid
    natural = long_q.ask - short_q.bid
    if mid <= 0:
        return Rejection(und, "spread has no positive debit")
    limit = min(_limit_between(mid, natural, s.limit_slippage_frac), round(width, 2))
    rr = (width - limit) / limit
    if rr < s.min_reward_risk:
        return Rejection(und, f"reward/risk {rr:.2f} below {s.min_reward_risk:.2f}")
    d = None if long_q.delta is None or short_q.delta is None else round(long_q.delta - short_q.delta, 4)
    return Proposal(
        und, strat, direction, long_q.expiry,
        (LegSpec(long_q.symbol, "buy", long_q.type, long_q.strike, long_q.expiry),
         LegSpec(short_q.symbol, "sell", short_q.type, short_q.strike, short_q.expiry)),
        limit, round(mid, 4), round(natural, 4), width, spot, d, long_q.iv,
    )


# ------------------------------------------------------------------ sizing
@dataclass(frozen=True)
class Sizing:
    qty: int
    max_loss: float
    debit_cash: float
    reason: str = ""


def size_structure(max_loss_per_contract: float, *, equity: float, open_max_loss: float, open_count: int,
                   s: OptionsSettings, drawdown_halt: bool = False) -> Sizing:
    """Contracts that fit every option budget. qty 0 carries the reason."""
    if drawdown_halt:
        return Sizing(0, 0.0, 0.0, "drawdown control stops new option entries")
    if equity <= 0 or max_loss_per_contract <= 0:
        return Sizing(0, 0.0, 0.0, "no equity or no price")
    if open_count >= s.max_open_structures:
        return Sizing(0, 0.0, 0.0, f"{open_count} structures open (max {s.max_open_structures})")
    room = min(equity * s.max_loss_per_structure_pct, equity * s.max_loss_total_pct - open_max_loss)
    qty = int(math.floor(room / max_loss_per_contract + 1e-9))
    if qty < 1:
        return Sizing(0, 0.0, 0.0, f"one contract risks {max_loss_per_contract:.0f}, room is {max(room, 0):.0f}")
    ml = round(qty * max_loss_per_contract, 2)
    return Sizing(qty, ml, ml)


# ------------------------------------------------------------------- exits
class ExitReason(StrEnum):
    PROFIT_TARGET = "profit_target"
    LOSS_LIMIT = "loss_limit"
    TIME_EXIT = "time_exit"
    EX_DIVIDEND = "ex_dividend"
    THESIS_BREAK = "thesis_break"
    REPAIR = "repair"
    ADOPTED_EXPIRY = "adopted_near_expiry"
    MANUAL = "manual"


@dataclass(frozen=True)
class ExitInputs:
    strategy: Strategy
    qty: int
    entry_debit: float  # per share
    width: float | None
    value_now: float | None  # per share, bid-side value to close (long bid - short ask)
    trading_days_left: int | None
    thesis_broken: bool = False
    short_call_strike: float | None = None
    short_call_extrinsic: float | None = None
    spot: float | None = None
    ex_dividend: tuple[date, float] | None = None
    ex_dividend_known: bool = True
    next_session: date | None = None


def exit_reason(x: ExitInputs, s: OptionsSettings) -> ExitReason | None:
    """First exit rule that fires, in priority order. Time exit first: nothing is
    ever carried into expiry day, and an unknown expiry distance counts as due."""
    if x.trading_days_left is None or x.trading_days_left <= s.exit_trading_days_before_expiry:
        return ExitReason.TIME_EXIT
    if x.short_call_strike is not None and x.spot is not None and x.spot > x.short_call_strike:
        # an in-the-money short call can be assigned the day before an ex-date
        if not x.ex_dividend_known:
            return ExitReason.EX_DIVIDEND
        if x.ex_dividend and x.next_session and x.ex_dividend[0] <= x.next_session:
            if x.short_call_extrinsic is None or x.short_call_extrinsic < x.ex_dividend[1]:
                return ExitReason.EX_DIVIDEND
    if x.value_now is not None and x.entry_debit > 0:
        if x.width is not None:
            max_profit = x.width - x.entry_debit
            if max_profit > 0 and x.value_now - x.entry_debit >= s.profit_target_frac * max_profit:
                return ExitReason.PROFIT_TARGET
        elif x.value_now - x.entry_debit >= s.profit_target_frac * x.entry_debit:
            return ExitReason.PROFIT_TARGET
        if x.entry_debit - x.value_now >= s.loss_limit_frac * x.entry_debit:
            return ExitReason.LOSS_LIMIT
    if x.thesis_broken:
        return ExitReason.THESIS_BREAK
    return None


# ---------------------------------------------------------------- cool-down
@dataclass
class Cooldown:
    underlying: str
    consecutive_losses: int = 0
    until: date | None = None
    history: list[str] = field(default_factory=list)


def apply_result(cd: Cooldown, pnl: float, closed_on: date, s: OptionsSettings,
                 add_trading_days: Callable[[date, int], date]) -> Cooldown:
    """Update the cool-down after a structure on this underlying closes."""
    if pnl < 0:
        cd.consecutive_losses += 1
        days = s.cooldown_after_two_losses if cd.consecutive_losses >= 2 else s.cooldown_after_loss
        cd.until = add_trading_days(closed_on, days)
    else:
        cd.consecutive_losses = 0
        cd.until = None
    cd.history = (cd.history + [f"{closed_on.isoformat()}:{pnl:+.2f}"])[-10:]
    return cd


def in_cooldown(cd: Cooldown | None, today: date) -> bool:
    return bool(cd and cd.until and today < cd.until)


def close_limit(value_bid: float, value_mid: float, frac: float) -> float:
    """Limit for a closing credit per share: from mid towards the natural price, never negative."""
    px = value_mid - max(value_mid - value_bid, 0.0) * frac
    return max(math.floor(round(px * 100, 6)) / 100, 0.0)


def _floor_cent(px: float) -> float:
    return math.floor(round(px * 100, 6)) / 100


def leg_intrinsic(type_: str, strike: float, spot: float) -> float:
    return max(spot - strike, 0.0) if type_ == "call" else max(strike - spot, 0.0)


@dataclass(frozen=True)
class NoQuoteClose:
    """Fallback limit for closing without usable option quotes. `credit` is per share,
    signed like a closing value: + we receive, - we pay (only ever when a short leg makes
    the close cost money)."""
    credit: float
    intrinsic: float
    deep_otm: bool


def fallback_close_limit(legs: Sequence[object], spot: float, *, haircut: float, cushion_pct: float,
                         cushion_min: float, deep_otm_pct: float, debit_vertical: bool) -> NoQuoteClose:
    """A defensible marketable limit from the underlying alone.

    - net intrinsic = sum(sign * ratio * intrinsic) per share;
    - no short legs, or a debit vertical (whose value is never below 0): receive at least
      intrinsic less the haircut; a lone long never below 0.01, a spread never below 0.00,
      so the close can only improve on zero and never pays;
    - otherwise (a short leg can make the close cost money): pay at most net intrinsic
      plus a cushion of max(cushion_min, cushion_pct * spot) per short contract.
    deep_otm: every leg has 0 intrinsic and every short is at least deep_otm_pct out of the money.
    """
    net = 0.0
    shorts = 0
    deep = True
    for lg in legs:
        intr = leg_intrinsic(lg.type, lg.strike, spot)
        net += lg.sign * lg.ratio * intr
        if intr > 0:
            deep = False
        if lg.side == "sell":
            shorts += lg.ratio
            gap = (lg.strike - spot) if lg.type == "call" else (spot - lg.strike)
            if gap < deep_otm_pct * spot:
                deep = False
    if shorts == 0 or debit_vertical:
        floor_px = 0.01 if len(legs) == 1 else 0.0
        credit = max(_floor_cent(max(net, 0.0) * (1 - haircut)), floor_px)
    else:
        cushion = max(cushion_min, cushion_pct * spot) * shorts
        credit = _floor_cent(net - cushion)
        if credit > 0:  # the longs out-weigh the shorts: still haircut, never ask for more
            credit = _floor_cent(max(net, 0.0) * (1 - haircut))
    return NoQuoteClose(round(credit, 2), round(net, 4), deep)


# ---------------------------------------------------------------- calendar fallback
# Used only beyond the broker calendar's window. Rule-based NYSE full-day closures
# (https://www.nyse.com/markets/hours-calendars); special closures are not known here,
# so near-expiry decisions always rely on the broker calendar instead.

def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = (h + m - 7 * n + 114) % 31 + 1
    return date(y, month, day)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    d = date(y, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, month: int, weekday: int) -> date:
    d = date(y + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(y: int) -> frozenset[date]:
    days = {
        _nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3), _easter(y) - timedelta(days=2),
        _last_weekday(y, 5, 0), _observed(date(y, 7, 4)), _nth_weekday(y, 9, 0, 1),
        _nth_weekday(y, 11, 3, 4), _observed(date(y, 12, 25)),
    }
    if date(y, 1, 1).weekday() != 5:  # NYSE does not observe a Saturday New Year on Dec 31
        days.add(_observed(date(y, 1, 1)))
    if y >= 2022:
        days.add(_observed(date(y, 6, 19)))
    return frozenset(days)


def is_probable_session(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def estimate_trading_days(a: date, b: date) -> int | None:
    """Sessions after ``a`` through ``b`` by weekday and holiday rules."""
    if b < a:
        return None
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        n += is_probable_session(cur)
    return n
