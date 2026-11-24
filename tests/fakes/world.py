"""One object holding the simulated clock, broker and every provider.

    def test_something(world):          # fixture from tests/conftest.py
        world.price("AAA", 100)          # broker + FMP agree on the price
        sched = world.scheduler()        # the real StockAgentScheduler
        ...
        world.assert_no_orders()

Placeholder credentials are set on `Config` so the bot's "no token, skip"
branches do not hide the alert and data code paths. They are not secrets.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from tests.fakes.alpaca import FakeAlpaca
from tests.fakes.base import FakeInternet, RecordedRequest
from tests.fakes.clock import SimClock
from tests.fakes.providers import (
    FakeAnthropic, FakeDiscord, FakeFinnhub, FakeFMP, FakeOpenAI, FakePerplexity, FakeSEC, FakeTelegram,
    FakeTipRanks,
)

PLACEHOLDER_CREDENTIALS = {
    "ALPACA_API_KEY": "test-key-id",
    "ALPACA_SECRET_KEY": "test-secret",
    "PERPLEXITY_API_KEY": "test-pplx",
    "OPENAI_API_KEY": "test-openai",
    "FMP_API_KEY": "test-fmp",
    "FINNHUB_API_KEY": "test-finnhub",
    "TELEGRAM_BOT_TOKEN": "123:test",
    "TELEGRAM_CHAT_ID": "1",
    "DISCORD_BOT_TOKEN": "test-discord",
}


class FakeWorld:
    def __init__(self, start: datetime | None = None, **alpaca_kw):
        self.data_dir: Path | None = None
        self.clock = SimClock(start) if start else SimClock.at_et(2026, 11, 23, 9, 0)
        self.alpaca = FakeAlpaca(self.clock, **alpaca_kw)
        self.fmp = FakeFMP(self.clock, price_source=self.alpaca.price)
        self.finnhub = FakeFinnhub(self.clock)
        self.sec = FakeSEC(self.clock)
        self.tipranks = FakeTipRanks(self.clock)
        self.openai = FakeOpenAI(self.clock)
        self.perplexity = FakePerplexity(self.clock)
        self.anthropic = FakeAnthropic(self.clock)
        self.discord = FakeDiscord(self.clock)
        self.telegram = FakeTelegram(self.clock)
        self.net = FakeInternet([
            self.alpaca, self.fmp, self.finnhub, self.sec, self.tipranks, self.openai, self.perplexity,
            self.anthropic, self.discord, self.telegram,
        ])

    # wiring
    def install(self, monkeypatch, data_dir: Path | None = None, *, credentials: bool = True) -> "FakeWorld":
        from stock_agent.config import Config

        self.net.install(monkeypatch)
        self.clock.install(monkeypatch)
        if data_dir is not None:
            monkeypatch.setattr(Config, "DATA_DIR", Path(data_dir))
            self.data_dir = Path(data_dir)
        if credentials:
            for k, v in PLACEHOLDER_CREDENTIALS.items():
                monkeypatch.setattr(Config, k, v)
        return self

    def scheduler(self):
        from stock_agent.scheduler import StockAgentScheduler

        return StockAgentScheduler()

    # market helpers
    def price(self, symbol: str, last: float, **kw) -> None:
        """Set a price at the broker; FMP quotes follow it."""
        self.alpaca.set_price(symbol, last, **kw)
        if symbol not in self.fmp.profiles:
            self.fmp.set_profile(symbol)

    def news(self, symbols: list[str], headline: str, *, at: datetime | None = None, summary: str = "") -> None:
        """A story on both news feeds (Alpaca /v1beta1/news and Finnhub company-news),
        visible from `at` (default now) as the simulated clock moves."""
        at = at or self.clock.now()
        self.alpaca.add_news(symbols, headline, summary=summary, created_at=at)
        for s in symbols:
            self.finnhub.add_news(s, headline, at=at, summary=summary)

    # assertions
    def orders_sent(self, *, include_failed: bool = True) -> list[RecordedRequest]:
        """Every order submission/replace the bot attempted at the broker."""
        reqs = self.net.requests(service="alpaca", where=lambda r: (
            (r.method == "POST" and r.path == "/v2/orders")
            or (r.method == "PATCH" and r.path.startswith("/v2/orders/"))
            or (r.method == "DELETE" and r.path.startswith("/v2/positions"))
        ))
        if not include_failed:
            reqs = [r for r in reqs if r.status == 200]
        return reqs

    def cancels_sent(self) -> list[RecordedRequest]:
        return self.net.requests(service="alpaca", method="DELETE", path="/v2/orders*")

    def alerts_sent(self) -> list[RecordedRequest]:
        return self.net.requests(where=lambda r: r.service in ("discord", "telegram") and r.method == "POST")

    def alert_texts(self) -> list[str]:
        return self.discord.texts() + self.telegram.texts()

    def assert_no_orders(self) -> None:
        sent = self.orders_sent()
        assert not sent, f"expected no orders, the bot sent: {sent}"

    def assert_no_unexpected_hosts(self) -> None:
        self.net.assert_no_unexpected()
