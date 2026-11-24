"""A controllable clock and the US equity market calendar, for tests.

Nothing here reads the wall clock. A test creates a `SimClock` at a fixed
Eastern time, moves it with `advance()` / `set()`, and installs it so the bot
code sees the simulated time:

    clock = SimClock.at_et(2026, 11, 25, 9, 31)
    clock.install(monkeypatch)            # scheduler._now_et, datetime.now, asyncio.sleep
    clock.advance(minutes=5)

The calendar holds the NYSE full-day holidays and 13:00 early closes for
2026 to 2028, copied from https://www.nyse.com/markets/hours-calendars
(read 2026-11-23). Days outside that table fall back to "weekday = open
09:30 to 16:00", which is good enough for tests and is flagged by
`MarketCalendar.covers()`.

Daylight saving comes from `zoneinfo` (America/New_York): in 2026 it ends on
Sunday 1 November (02:00 -> 01:00) and starts on Sunday 8 March 2026 and
Sunday 14 March 2027.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Source: https://www.nyse.com/markets/hours-calendars (read 2026-11-23).
NYSE_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King, Jr. Day",
    date(2026, 2, 16): "Washington's Birthday",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King, Jr. Day",
    date(2027, 2, 15): "Washington's Birthday",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed)",
    date(2028, 1, 17): "Martin Luther King, Jr. Day",
    date(2028, 2, 21): "Washington's Birthday",
    date(2028, 4, 14): "Good Friday",
    date(2028, 5, 29): "Memorial Day",
    date(2028, 6, 19): "Juneteenth",
    date(2028, 7, 4): "Independence Day",
    date(2028, 9, 4): "Labor Day",
    date(2028, 11, 23): "Thanksgiving Day",
    date(2028, 12, 25): "Christmas Day",
}

NYSE_EARLY_CLOSES: dict[date, time] = {
    date(2026, 11, 27): EARLY_CLOSE,
    date(2026, 12, 24): EARLY_CLOSE,
    date(2027, 11, 26): EARLY_CLOSE,
    date(2028, 7, 3): EARLY_CLOSE,
    date(2028, 11, 24): EARLY_CLOSE,
}

# Handy named dates for tests.
THANKSGIVING_2026 = date(2026, 11, 26)
DAY_AFTER_THANKSGIVING_2026 = date(2026, 11, 27)
CHRISTMAS_EVE_2026 = date(2026, 12, 24)
GOOD_FRIDAY_2026 = date(2026, 4, 3)
DST_END_2026 = date(2026, 11, 1)  # Sunday; 02:00 EDT -> 01:00 EST
DST_START_2027 = date(2027, 3, 14)  # Sunday; 02:00 EST -> 03:00 EDT


@dataclass(frozen=True)
class Session:
    """One trading day: open and close as aware datetimes in Eastern time."""

    day: date
    open: datetime
    close: datetime

    @property
    def early_close(self) -> bool:
        return self.close.timetz().replace(tzinfo=None) != REGULAR_CLOSE


class MarketCalendar:
    """NYSE trading days, holidays and early closes."""

    def __init__(
        self,
        holidays: dict[date, str] | None = None,
        early_closes: dict[date, time] | None = None,
    ):
        self.holidays = dict(NYSE_HOLIDAYS if holidays is None else holidays)
        self.early_closes = dict(NYSE_EARLY_CLOSES if early_closes is None else early_closes)

    def covers(self, day: date) -> bool:
        """True when `day` is inside the years the holiday table was copied for."""
        return 2026 <= day.year <= 2028

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def session(self, day: date) -> Session | None:
        if not self.is_trading_day(day):
            return None
        close_t = self.early_closes.get(day, REGULAR_CLOSE)
        return Session(
            day=day,
            open=datetime.combine(day, REGULAR_OPEN, tzinfo=ET),
            close=datetime.combine(day, close_t, tzinfo=ET),
        )

    def sessions(self, start: date, end: date) -> list[Session]:
        out = []
        d = start
        while d <= end:
            s = self.session(d)
            if s:
                out.append(s)
            d += timedelta(days=1)
        return out

    def is_open(self, at: datetime) -> bool:
        """Regular session open at `at` (aware). Open is inclusive, close exclusive."""
        et = at.astimezone(ET)
        s = self.session(et.date())
        return bool(s and s.open <= et < s.close)

    def next_open(self, at: datetime) -> datetime:
        """The next session open strictly after `at` (or later today if before open)."""
        et = at.astimezone(ET)
        d = et.date()
        for _ in range(30):
            s = self.session(d)
            if s and s.open > et:
                return s.open
            d += timedelta(days=1)
        raise RuntimeError("no session within 30 days")

    def next_close(self, at: datetime) -> datetime:
        """The close of the current session if open, else of the next session."""
        et = at.astimezone(ET)
        d = et.date()
        for _ in range(30):
            s = self.session(d)
            if s and s.close > et:
                return s.close
            d += timedelta(days=1)
        raise RuntimeError("no session within 30 days")

    def current_or_next_session(self, at: datetime) -> Session:
        et = at.astimezone(ET)
        d = et.date()
        for _ in range(30):
            s = self.session(d)
            if s and s.close > et:
                return s
            d += timedelta(days=1)
        raise RuntimeError("no session within 30 days")

    def settlement_date(self, day: date) -> date:
        """T+1 settlement: the next trading day."""
        d = day + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d


class _FrozenDatetimeMeta(type):
    def __instancecheck__(cls, obj):  # isinstance(x, patched_datetime) still works
        return isinstance(obj, datetime)


class SimClock:
    """A clock that only moves when the test moves it.

    `now()` returns an aware UTC datetime. Listeners registered with
    `on_advance` are called with (old, new) every time the clock moves, which
    is how the fake broker runs its fill engine.
    """

    def __init__(self, start: datetime, calendar: MarketCalendar | None = None):
        if start.tzinfo is None:
            raise ValueError("SimClock needs an aware datetime")
        self._now = start.astimezone(UTC)
        self.calendar = calendar or MarketCalendar()
        self._listeners: list[Callable[[datetime, datetime], None]] = []
        self.sleeps: list[float] = []  # every asyncio.sleep the code asked for

    # construction helpers
    @classmethod
    def at_et(cls, year, month, day, hour=0, minute=0, second=0, **kw) -> "SimClock":
        return cls(datetime(year, month, day, hour, minute, second, tzinfo=ET), **kw)

    # reading
    def now(self) -> datetime:
        return self._now

    def now_et(self) -> datetime:
        return self._now.astimezone(ET)

    def is_market_open(self) -> bool:
        return self.calendar.is_open(self._now)

    # moving
    def on_advance(self, fn: Callable[[datetime, datetime], None]) -> None:
        self._listeners.append(fn)

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("aware datetime required")
        new = when.astimezone(UTC)
        if new < self._now:
            raise ValueError(f"SimClock cannot go backwards ({self._now} -> {new})")
        old, self._now = self._now, new
        for fn in list(self._listeners):
            fn(old, new)

    def set_et(self, year, month, day, hour=0, minute=0, second=0) -> None:
        self.set(datetime(year, month, day, hour, minute, second, tzinfo=ET))

    def advance(self, seconds: float = 0, *, minutes: float = 0, hours: float = 0, days: float = 0) -> None:
        self.set(self._now + timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days))

    def advance_to_open(self) -> None:
        self.set(self.calendar.next_open(self._now))

    # patching
    def datetime_class(self):
        """A `datetime` subclass whose now()/utcnow()/today() read this clock."""
        clock = self

        class SimDatetime(datetime, metaclass=_FrozenDatetimeMeta):
            @classmethod
            def now(cls, tz=None):
                n = clock.now()
                if tz is None:
                    return n.astimezone().replace(tzinfo=None)
                return n.astimezone(tz)

            @classmethod
            def utcnow(cls):
                return clock.now().replace(tzinfo=None)

            @classmethod
            def today(cls):
                return cls.now()

        return SimDatetime

    def install(self, monkeypatch, *, patch_sleep: bool = True, modules_prefix: str = "stock_agent") -> None:
        """Make the bot read this clock.

        - `stock_agent.scheduler._now_et` / `_now_utc` return simulated time.
        - every already-imported `stock_agent.*` module that did
          `from datetime import datetime` gets a `datetime` whose `now()` is
          simulated (other methods are the real ones).
        - `asyncio.sleep(n)` advances the clock by n seconds and yields once,
          so `await asyncio.sleep(3)` in the executor costs no real time and
          lets the fake broker's fill engine run.
        - modules that did `import time` (any alias) see `time.monotonic()` /
          `time.time()` on the simulated clock, so rate limiters waiting with
          asyncio.sleep make progress (otherwise they spin in simulated time).
        """
        import importlib

        sched = importlib.import_module(f"{modules_prefix}.scheduler")
        monkeypatch.setattr(sched, "_now_et", lambda: self.now_et())
        monkeypatch.setattr(sched, "_now_utc", lambda: self.now())

        sim_dt = self.datetime_class()
        sim_time = _SimTime(self)
        for name, mod in list(sys.modules.items()):
            if not (name == modules_prefix or name.startswith(modules_prefix + ".")):
                continue
            if getattr(mod, "datetime", None) is datetime:
                monkeypatch.setattr(mod, "datetime", sim_dt)
            for attr, val in list(vars(mod).items()):
                if val is _time_mod:     # `import time` / `import time as _time`
                    monkeypatch.setattr(mod, attr, sim_time)

        if patch_sleep:
            real_sleep = asyncio.sleep

            async def sim_sleep(delay, result=None):
                d = max(float(delay or 0), 0.0)
                self.sleeps.append(d)
                if d:
                    self.advance(d)
                await real_sleep(0)
                return result

            monkeypatch.setattr(asyncio, "sleep", sim_sleep)


_time_mod = __import__("time")


class _SimTime:
    """Stand-in for the `time` module: monotonic/time/perf_counter follow the
    simulated clock; everything else is the real module."""

    def __init__(self, clock):
        self._clock = clock

    def time(self) -> float:
        return self._clock.now().timestamp()

    monotonic = perf_counter = time

    def __getattr__(self, name):
        return getattr(_time_mod, name)
