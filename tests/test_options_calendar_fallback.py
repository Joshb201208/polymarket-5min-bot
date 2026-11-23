"""Trading-day counts beyond the broker calendar's short window (R-04)."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

from stock_agent.options_policy import estimate_trading_days, nyse_holidays
from stock_agent.research_schemas import Direction
from tests.options_support import StubGate, make_manager, seed_chain


def run(coro):
    return asyncio.run(coro)

# https://www.nyse.com/markets/hours-calendars (checked 2026-11-23)
NYSE = {
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
           "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
           "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
    2028: ["2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04",
           "2028-09-04", "2028-11-23", "2028-12-25"],
}


def test_holiday_rules_match_published_nyse_calendar():
    for y, days in NYSE.items():
        assert nyse_holidays(y) == frozenset(date.fromisoformat(d) for d in days), y


def test_estimate_counts_sessions_after_start_through_end():
    assert estimate_trading_days(date(2026, 11, 23), date(2026, 11, 27)) == 3  # Thanksgiving off
    assert estimate_trading_days(date(2026, 11, 23), date(2026, 12, 24)) == 22
    assert estimate_trading_days(date(2026, 11, 23), date(2026, 11, 23)) == 0
    assert estimate_trading_days(date(2026, 11, 24), date(2026, 11, 23)) is None


class ShortWindow:
    """Like the real MarketCalendar: counts are known only for today-7..today+14."""

    def __init__(self, inner, today):
        self.inner, self.today = inner, today

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _covered(self, d):
        return self.today - timedelta(days=7) <= d <= self.today + timedelta(days=14)

    def trading_days_between(self, a, b):
        if not (self._covered(a) and self._covered(b)):
            return None
        return self.inner.trading_days_between(a, b)

    def next_session(self, d):
        return self.inner.next_session(d) if self._covered(d + timedelta(days=1)) else None


def _manager(world, tmp_path, direction=Direction.BUY):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", direction, 8)
    m = make_manager(world, tmp_path, gate=gate)
    m.calendar = ShortWindow(m.calendar, date(2026, 11, 23))
    return m


def test_scan_opens_31_dte_spread_with_short_calendar_window(world, tmp_path):
    m = _manager(world, tmp_path)
    out = run(m.scan())
    assert [bool(o.opened) for o in out] == [True], [o.reason for o in out]
    assert len(m.live()) == 1


def test_distant_structure_is_not_time_exited_by_unknown_count(world, tmp_path):
    m = _manager(world, tmp_path)
    run(m.scan())
    world.clock.advance(minutes=20)
    seed_chain(world)
    assert run(m.monitor()).exits == []


def test_near_unknown_count_still_exits(world, tmp_path):
    m = _manager(world, tmp_path)
    assert m._trading_days(date(2026, 11, 23), date(2026, 12, 24)) == 22
    # with no calendar at all a near date stays unknown, so the time exit fires
    m.calendar = ShortWindow(m.calendar.inner, date(2020, 1, 1))
    assert m._trading_days(date(2026, 11, 23), date(2026, 11, 30)) is None
