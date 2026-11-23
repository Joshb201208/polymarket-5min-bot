"""Shared wiring for the options tests: the real AlpacaClient against FakeAlpaca,
a calendar adapter over the fake's calendar, and simple stand-ins for the
research gate and the risk manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from stock_agent.broker import AlpacaClient
from stock_agent.options_book import OptionsStore
from stock_agent.options_manager import OptionsManager
from stock_agent.options_settings import OptionsMode, OptionsSettings
from stock_agent.research_gate import Eligibility
from stock_agent.research_schemas import Direction
from stock_agent.risk_manager import RiskVerdict
from tests.fakes import occ_symbol

EXP = date(2026, 12, 24)  # 31 days after Mon 23 Nov 2026 (Christmas Eve, an early close)
EXP2 = date(2026, 12, 31)


@dataclass
class _Sess:
    date: date
    opens_at: datetime
    closes_at: datetime


class CalendarAdapter:
    """The scheduler's calendar interface over tests.fakes.clock.MarketCalendar."""

    def __init__(self, cal, clock):
        self.cal = cal
        self.clock = clock

    def session_on(self, day: date):
        s = self.cal.session(day)
        return _Sess(s.day, s.open, s.close) if s else None

    def next_session(self, day: date):
        d = day + timedelta(days=1)
        for _ in range(30):
            s = self.cal.session(d)
            if s:
                return _Sess(s.day, s.open, s.close)
            d += timedelta(days=1)
        return None

    def trading_days_between(self, a: date, b: date):
        if b < a:
            return None
        return sum(1 for s in self.cal.sessions(a + timedelta(days=1), b))

    def is_open(self, now: datetime) -> bool:
        return self.cal.is_open(now)

    async def market_gate(self, purpose: str):
        now = self.clock.now()
        if not self.cal.is_open(now):
            return Gate(False, "closed")
        s = self.cal.current_or_next_session(now)
        if now < s.open + timedelta(minutes=5):
            return Gate(False, "opening buffer")
        if purpose == "entry" and now >= s.close - timedelta(minutes=10):
            return Gate(False, "entry closing buffer")
        return Gate(True, "open")


@dataclass
class Gate:
    allowed: bool
    reason: str


class StubGate:
    """Research gate: per-symbol Eligibility, default BUY 8 fresh."""

    def __init__(self):
        self.views: dict[str, Eligibility] = {}

    def set(self, symbol, direction=Direction.BUY, conviction=8, ok=True, raw=None, reasons=()):
        self.views[symbol] = Eligibility(ok, tuple(reasons), f"th-{symbol}", raw if raw is not None else conviction,
                                         conviction, direction, None)

    def is_entry_eligible(self, symbol, now):
        return self.views.get(symbol) or Eligibility(False, ("missing_thesis",))


@dataclass
class StubRisk:
    ok: bool = True
    reasons: tuple = ()
    intents: list = field(default_factory=list)
    controls: list = field(default_factory=list)

    def evaluate_trade(self, snapshot, intent, *, now=None, controls=None):
        self.intents.append(intent)
        self.controls.append(controls)
        return RiskVerdict(self.ok, self.reasons)


class Journal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def append(self, kind, **fields):
        self.events.append((kind, fields))
        return len(self.events)

    def kinds(self):
        return [k for k, _ in self.events]


class Alerts:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def alert(self, severity, title, body, key=None):
        self.sent.append((severity, title, body))


def make_manager(world, tmp_path: Path, *, mode=OptionsMode.DEFINED_RISK, risk=None, gate=None,
                 covers=None, universe=("AAA",), ex_dividend=None, **settings):
    a = world.alpaca
    broker = AlpacaClient("https://paper-api.alpaca.markets", "k", "s")
    broker.lookup_attempts = 1
    cal = CalendarAdapter(a.calendar, world.clock)
    gate = gate or StubGate()
    risk = risk or StubRisk()
    covered = covers if covers is not None else []

    async def cover_short(symbol, reason=""):
        covered.append((symbol, reason))

    def spot(sym):
        q = a.quotes.get(sym)
        return None if q is None else (q.last, q.at)

    m = OptionsManager(
        broker=broker, store=OptionsStore(tmp_path / "options_book.json"), gate=gate, risk=risk,
        snapshot=lambda: object(), calendar=cal, market_gate=cal.market_gate, spot=spot,
        cover_short=cover_short, universe=lambda: list(universe), journal=Journal(), alerts=Alerts(),
        ex_dividend=ex_dividend, settings=OptionsSettings(mode=mode, fill_poll_seconds=1.0, **settings),
        now=world.clock.now,
    )
    m.covered = covered
    return m


def seed_chain(world, und="AAA", spot=100.0, exp=EXP, strikes=None, *, width_quotes=None, oi=1000):
    """A call and put chain with sensible quotes: intrinsic + time value, 0.10 wide."""
    a = world.alpaca
    world.price(und, spot, spread=0.02)
    strikes = strikes or [90, 95, 100, 105, 110, 115]
    for k in strikes:
        for kind in ("call", "put"):
            a.add_option_contract(und, exp, kind, k, open_interest=oi)
            intrinsic = max(spot - k, 0) if kind == "call" else max(k - spot, 0)
            mid = round(intrinsic + 3.0 * max(0.2, 1 - abs(spot - k) / 25), 2)
            a.set_option_quote(occ_symbol(und, exp, kind, k), round(mid - 0.05, 2), round(mid + 0.05, 2))
    return a


def requote(world, sym, mid, half=0.05):
    world.alpaca.set_option_quote(sym, round(max(mid - half, 0.0), 2), round(mid + half, 2))
