import asyncio
import socket
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from stock_agent.broker import AlpacaClient
from stock_agent.market_calendar import ET, MarketCalendar, MarketSource, MarketStatus
from stock_agent.runtime_status import notify_systemd


class BrokerFixture:
    def __init__(self, now, days):
        self.now = now
        self.days = days
        self.clock_failed = False
        self.calendar_failed = False
        self.clock_skew = timedelta()

    async def get_calendar(self, start, end):
        if self.calendar_failed:
            raise TimeoutError
        return [
            SimpleNamespace(date=day, open="09:30", close=close)
            for day, close in sorted(self.days.items()) if start <= day <= end
        ]

    async def get_clock(self):
        if self.clock_failed:
            raise TimeoutError
        boundaries = [(datetime.combine(day, time(9, 30), ET),
                       datetime.combine(day, time.fromisoformat(close), ET))
                      for day, close in sorted(self.days.items())]
        return SimpleNamespace(
            timestamp=self.now + self.clock_skew,
            is_open=any(opening <= self.now < close for opening, close in boundaries),
            next_open=min(start for start, _ in boundaries if start > self.now),
            next_close=min(close for _, close in boundaries if close > self.now),
        )


def test_holiday_and_half_day_close_are_broker_calendar_days(tmp_path):
    broker = BrokerFixture(datetime(2026, 11, 26, 10, tzinfo=ET), {
        date(2026, 11, 25): "16:00", date(2026, 11, 27): "13:00", date(2026, 11, 30): "16:00",
    })
    market = MarketCalendar(broker, tmp_path / "calendar.json", now=lambda: broker.now)
    assert asyncio.run(market.refresh()).status is MarketStatus.CLOSED
    assert market.session_on(date(2026, 11, 26)) is None
    broker.now = datetime(2026, 11, 27, 12, 59, tzinfo=ET)
    assert not asyncio.run(market.market_gate("entry")).allowed
    assert asyncio.run(market.market_gate("exit")).allowed
    broker.now += timedelta(minutes=1)
    assert not asyncio.run(market.market_gate("exit")).allowed
    assert asyncio.run(market.market_gate("protection")).allowed


def test_good_friday_and_post_restart_cache_never_authorize(tmp_path):
    broker = BrokerFixture(datetime(2026, 4, 3, 9, 30, tzinfo=ET), {
        date(2026, 4, 2): "16:00", date(2026, 4, 6): "16:00", date(2026, 4, 7): "16:00",
    })
    cache = tmp_path / "calendar.json"
    market = MarketCalendar(broker, cache, now=lambda: broker.now)
    assert not asyncio.run(market.market_gate("entry")).allowed
    broker.now = datetime(2026, 4, 6, 9, 36, tzinfo=ET)
    # Persist a new valid snapshot, then simulate a same-day restart during a broker outage.
    assert asyncio.run(market.market_gate("entry")).allowed
    broker.clock_failed = broker.calendar_failed = True
    restored = MarketCalendar(broker, cache, now=lambda: broker.now)
    state = asyncio.run(restored.refresh())
    assert (state.status, state.source) == (MarketStatus.OPEN, MarketSource.CACHE)
    assert not asyncio.run(restored.market_gate("entry")).allowed
    assert restored.trading_days_between(date(2026, 4, 2), date(2026, 4, 6)) == 1


def test_scheduling_and_order_skip_opening_auction_and_invalid_clock(tmp_path):
    broker = BrokerFixture(datetime(2026, 11, 23, 9, 30, tzinfo=ET), {
        date(2026, 11, 23): "16:00", date(2026, 11, 24): "16:00",
    })
    market = MarketCalendar(broker, tmp_path / "calendar.json", now=lambda: broker.now)
    assert not asyncio.run(market.market_gate("entry")).allowed
    assert not asyncio.run(market.market_gate("exit")).allowed
    assert asyncio.run(market.market_gate("exit", rule="cover_short")).allowed
    broker.now += timedelta(minutes=5)
    assert asyncio.run(market.market_gate("entry")).allowed
    broker.clock_skew = timedelta(minutes=-3)
    assert not asyncio.run(market.market_gate("entry")).allowed


def test_zoneinfo_accounts_for_daylight_saving(tmp_path):
    broker = BrokerFixture(datetime(2026, 3, 6, 10, tzinfo=ET), {
        date(2026, 3, 6): "16:00", date(2026, 3, 9): "16:00",
    })
    market = MarketCalendar(broker, tmp_path / "calendar.json", now=lambda: broker.now)
    asyncio.run(market.refresh())
    assert market.session_on(date(2026, 3, 6)).opens_at.isoformat().endswith("-05:00")
    assert market.session_on(date(2026, 3, 9)).opens_at.isoformat().endswith("-04:00")


def test_systemd_readiness_protocol_datagram(tmp_path, monkeypatch):
    destination = str(tmp_path / "notify")
    monkeypatch.setenv("NOTIFY_SOCKET", destination)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.settimeout(1)
        listener.bind(destination)
        notify_systemd("READY=1\nWATCHDOG=1")
        assert listener.recv(1024) == b"READY=1\nWATCHDOG=1"


def test_calendar_contract_with_shared_broker_fake(world, tmp_path):
    world.clock.set_et(2026, 11, 26, 10, 0)

    async def check():
        broker = AlpacaClient()
        try:
            assert await broker.get_calendar(date(2026, 11, 18), date(2026, 12, 10))
            calendar = MarketCalendar(broker, tmp_path / "calendar.json", now=world.clock.now)
            state = await calendar.refresh()
            assert state.source is MarketSource.LIVE
            assert state.status is MarketStatus.CLOSED
            assert state.session is None
            assert (await calendar.market_gate("entry")).allowed is False
        finally:
            await broker.close()

    asyncio.run(check())
