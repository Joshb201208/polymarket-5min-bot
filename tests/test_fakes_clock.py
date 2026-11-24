"""Tests of the simulated clock and the NYSE calendar it uses."""

import asyncio
from datetime import date, datetime, timedelta

import pytest

from tests.fakes.clock import (
    DAY_AFTER_THANKSGIVING_2026, DST_END_2026, DST_START_2027, ET, GOOD_FRIDAY_2026, THANKSGIVING_2026,
    UTC, MarketCalendar, SimClock,
)

CAL = MarketCalendar()


def test_thanksgiving_2026_closed_and_next_day_closes_early():
    assert THANKSGIVING_2026 == date(2026, 11, 26)
    assert CAL.session(THANKSGIVING_2026) is None
    s = CAL.session(DAY_AFTER_THANKSGIVING_2026)
    assert s.early_close and s.close.hour == 13
    assert not CAL.is_open(datetime(2026, 11, 27, 13, 0, tzinfo=ET))
    assert CAL.is_open(datetime(2026, 11, 27, 12, 59, tzinfo=ET))


def test_good_friday_2026_is_a_holiday_on_a_weekday():
    assert GOOD_FRIDAY_2026.weekday() == 4
    assert not CAL.is_trading_day(GOOD_FRIDAY_2026)


def test_next_open_skips_weekend_and_holiday():
    wed_close = datetime(2026, 11, 25, 16, 0, tzinfo=ET)
    assert CAL.next_open(wed_close) == datetime(2026, 11, 27, 9, 30, tzinfo=ET)
    fri = datetime(2026, 11, 27, 14, 0, tzinfo=ET)
    assert CAL.next_open(fri) == datetime(2026, 11, 30, 9, 30, tzinfo=ET)


def test_open_is_inclusive_close_is_exclusive():
    assert CAL.is_open(datetime(2026, 11, 23, 9, 30, tzinfo=ET))
    assert not CAL.is_open(datetime(2026, 11, 23, 9, 29, 59, tzinfo=ET))
    assert not CAL.is_open(datetime(2026, 11, 23, 16, 0, tzinfo=ET))


def test_dst_end_2026_shifts_the_utc_open():
    # EDT (UTC-4) before 1 Nov 2026, EST (UTC-5) after
    before = CAL.session(date(2026, 10, 30)).open.astimezone(UTC)
    after = CAL.session(date(2026, 11, 2)).open.astimezone(UTC)
    assert (before.hour, after.hour) == (13, 14)
    assert DST_END_2026 == date(2026, 11, 1)
    assert CAL.session(date(2027, 3, 15)).open.astimezone(UTC).hour == 13
    assert DST_START_2027 == date(2027, 3, 14)


def test_clock_advances_across_dst_by_real_elapsed_time():
    c = SimClock.at_et(2026, 10, 31, 12, 0)
    c.advance(hours=24)
    assert c.now_et().hour == 11  # 24 real hours later is 11:00 EST


def test_clock_cannot_go_backwards():
    c = SimClock.at_et(2026, 11, 23, 10, 0)
    with pytest.raises(ValueError):
        c.set(c.now() - timedelta(seconds=1))


def test_listeners_see_every_move():
    c = SimClock.at_et(2026, 11, 23, 10, 0)
    seen = []
    c.on_advance(lambda old, new: seen.append((new - old).total_seconds()))
    c.advance(5)
    c.advance(minutes=1)
    assert seen == [5, 60]


def test_install_patches_scheduler_now_and_datetime_and_sleep(monkeypatch):
    import stock_agent.scheduler as sched

    c = SimClock.at_et(2026, 11, 26, 9, 31)
    c.install(monkeypatch)
    assert sched._now_et() == c.now_et()
    assert sched.datetime.now(ET) == c.now_et()
    assert sched.datetime.utcnow() == c.now().replace(tzinfo=None)
    assert isinstance(datetime.now(), sched.datetime)  # isinstance still works

    async def go():
        await asyncio.sleep(3)

    asyncio.run(go())
    assert c.now_et() == datetime(2026, 11, 26, 9, 31, 3, tzinfo=ET)
    assert c.sleeps == [3]

