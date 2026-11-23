from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from stock_agent.fundamentals_service import FundamentalsService
from stock_agent.health import ProviderHealthRegistry
from stock_agent.news_service import NewsService
from stock_agent.research_pipeline import ResearchPipeline, SourceUnavailable
from stock_agent.research_sources import ResearchSources
from stock_agent.state_store import StateStore

NOW = datetime(2026, 11, 23, 15, tzinfo=UTC)


def test_source_health_uses_explicit_symbol_coverage_and_provider_quote_time():
    class Prices:
        async def get_quotes(self, symbols, *, purpose, max_age_s):
            assert purpose == "order_sizing" and max_age_s == 90
            return SimpleNamespace(quotes={
                "AAA": SimpleNamespace(price=52, as_of=NOW - timedelta(seconds=30), prev_close=50),
                "BBB": SimpleNamespace(price=48, as_of=NOW - timedelta(minutes=4), prev_close=50),
            })

    class News:
        async def poll_news(self, symbols, *, scope, include_market, lookback_hours, mark_seen, focus, universe):
            assert include_market and lookback_hours == 24 and not mark_seen
            return SimpleNamespace(
                ok=True, degraded=True, as_of=NOW, covered_symbols={"AAA"}, items=[
                    SimpleNamespace(
                        symbols=("AAA",), materiality_score=7, story_id="abc", publisher="Issuer",
                        url="https://example.org/story", snippet="Reported results", headline="Results",
                        published_at=NOW - timedelta(minutes=10), retrieved_at=NOW,
                    ),
                    SimpleNamespace(
                        symbols=("AAA", "BBB"), materiality_score=2, story_id="ignore",
                        url="https://example.org/low", published_at=NOW, retrieved_at=NOW,
                    ),
                ],
            )

    async def universe():
        return ["AAA", "BBB"]

    async def fetch_company(symbol):
        return SimpleNamespace(
            canonical_sector="Technology", name=symbol, tradable=True,
            metrics={"pe_ttm": SimpleNamespace(value=20), "revenue_growth_yoy": 0.2,
                     "gross_margin_ttm": SimpleNamespace(value=0.3, as_of=NOW, source="Fixture", unit="ratio",
                                                         period="ttm", url="https://example.org/metric")},
        )

    async def run():
        sources = ResearchSources(Prices(), News(), list_universe=universe, fetch_company=fetch_company)
        candidates = await sources.universe(NOW)
        rows = await sources.securities(candidates, NOW)
        stories = await sources.news(candidates, NOW)
        assert [(item.story_id, item.symbol) for item in stories] == [("abc", "AAA")]
        assert rows["AAA"].price == Decimal("52")
        assert rows["AAA"].metrics["earnings_yield"] == 0.05
        assert rows["AAA"].metrics["gross_margin"] == 0.3
        assert rows["AAA"].metric_provenance["gross_margin_ttm"]["as_of"] == NOW.isoformat()
        assert rows["AAA"].metric_provenance["gross_margin_ttm"]["url"] == "https://example.org/metric"
        assert sources.quote_ok("AAA", NOW) and not sources.quote_ok("BBB", NOW)
        assert sources.news_ok("AAA", NOW) and not sources.news_ok("BBB", NOW)
        assert not sources.news_ok("AAA", NOW + timedelta(minutes=21))

    asyncio.run(run())


def test_global_feed_health_cannot_substitute_for_symbol_coverage():
    class News:
        fails = False

        async def poll_news(self, symbols, **_):
            return SimpleNamespace(ok=not self.fails, degraded=True, as_of=NOW, covered_symbols={"AAA"}, items=[])

    async def universe():
        return ["AAA", "BBB"]

    async def run():
        service = News()
        health = SimpleNamespace(research_entry_blockers=lambda *_args, **_kwargs: ())
        sources = ResearchSources(None, service, list_universe=universe, health=health)
        assert await sources.news(("AAA", "BBB"), NOW) == ()
        assert sources.news_ok("AAA", NOW)
        assert not sources.news_ok("BBB", NOW)
        assert not sources.news_ok("AAA", NOW - timedelta(seconds=1))
        service.fails = True
        with pytest.raises(SourceUnavailable):
            await sources.news(("AAA", "BBB"), NOW)
        assert not sources.news_ok("AAA", NOW)

    asyncio.run(run())


def test_fundamental_requests_are_capped_per_pass_and_cache_does_not_consume_allowance():
    class Prices:
        async def get_quotes(self, symbols, **_):
            return SimpleNamespace(quotes={})

    calls = []

    async def universe():
        return ["HELD", "FOCUS", "FIRST", "LATER"]

    async def company(symbol):
        calls.append(symbol)
        return SimpleNamespace(metrics={"pe_ttm": 20})

    async def run():
        sources = ResearchSources(Prices(), None, list_universe=universe, fetch_company=company, fundamental_limit=2)
        symbols = await sources.universe(NOW)
        first = await sources.securities(symbols, NOW)
        assert calls == ["HELD", "FOCUS"]
        assert tuple(first) == symbols
        assert "pe_ttm" in first["HELD"].metrics and first["FIRST"].metrics == {}
        second = await sources.securities(symbols, NOW + timedelta(minutes=1))
        assert calls == list(symbols)
        assert all(row.metrics["pe_ttm"] == 20 for row in second.values())
        await sources.securities(symbols, NOW + timedelta(hours=7))
        assert calls == [*symbols, "HELD", "FOCUS"]

    asyncio.run(run())


def test_shared_fundamentals_cache_does_not_consume_cold_allowance():
    class Prices:
        async def get_quotes(self, symbols, **_):
            return SimpleNamespace(quotes={})

    async def universe():
        return ["CACHED", "COLD"]

    async def company(symbol):
        assert symbol == "COLD"
        return SimpleNamespace(metrics={"pe_ttm": 20})

    async def run():
        cached = {"CACHED": SimpleNamespace(metrics={"pe_ttm": 10})}
        sources = ResearchSources(
            Prices(), None, list_universe=universe, fetch_company=company, cached_company=cached.get,
            fundamental_limit=1,
        )
        rows = await sources.securities(await sources.universe(NOW), NOW)
        assert rows["CACHED"].metrics["pe_ttm"] == 10
        assert rows["COLD"].metrics["pe_ttm"] == 20

    asyncio.run(run())


def test_only_confirmed_prior_earnings_release_materializes():
    class News:
        async def poll_news(self, symbols, **_):
            return SimpleNamespace(ok=True, degraded=False, as_of=NOW, items=[])

    async def universe():
        return ["AAA", "BBB"]

    async def releases(symbols, now):
        return [
            SimpleNamespace(symbol="AAA", is_published=False, published_at=None, eps_estimate=0.23),
            SimpleNamespace(symbol="AAA", is_published=True, published_at=NOW + timedelta(seconds=1)),
            SimpleNamespace(symbol="BBB", is_published=True, published_at=NOW - timedelta(minutes=1),
                            eps_actual=0.25, eps_estimate=0.23, url="https://example.org/results"),
        ]

    async def run():
        sources = ResearchSources(None, News(), list_universe=universe, fetch_releases=releases)
        items = await sources.releases(("AAA", "BBB"), NOW)
        assert len(items) == 1 and items[0].symbol == "BBB"
        assert items[0].evidence.published_at == NOW - timedelta(minutes=1)
        assert "Reported EPS: 0.25" in items[0].evidence.snippet

    asyncio.run(run())


def test_actual_fundamentals_service_maps_snapshot_and_release_offline():
    class News:
        async def poll_news(self, symbols, **_):
            return SimpleNamespace(ok=True, degraded=False, as_of=NOW, items=[])

    class Prices:
        async def get_quotes(self, symbols, **_):
            return SimpleNamespace(quotes={})

    def respond(request):
        if request.url.host == "market-data.example":
            assert request.url.path.endswith(("/most-actives", "/movers"))
            return httpx.Response(200, json={})
        assert request.url.host == "finnhub.io"
        payload = {
            "/api/v1/stock/metric": {"metric": {"peTTM": 20, "grossMarginTTM": 48, "marketCapitalization": 50}},
            "/api/v1/stock/profile2": {"name": "Fixture Industries", "finnhubIndustry": "Technology"},
            "/api/v1/stock/insider-transactions": {"data": []},
            "/api/v1/stock/recommendation": [],
            "/api/v1/calendar/earnings": {"earningsCalendar": [
                {"symbol": "AAA", "date": "2026-11-23", "hour": "bmo", "epsActual": 0.25, "epsEstimate": 0.23,
                 "year": 2026, "quarter": 4},
                {"symbol": "BBB", "date": "2026-11-23", "hour": "amc", "epsActual": 0.50},
            ]},
        }[request.url.path]
        return httpx.Response(200, json=payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            config = SimpleNamespace(FINNHUB_API_KEY="fixture", FMP_API_KEY="",
                                     ALPACA_DATA_URL="https://market-data.example")
            fundamentals = FundamentalsService(
                http, config=config, focus_symbols=("AAA", "BBB"), now=lambda: NOW, universe_size=0,
            )
            sources = ResearchSources(
                Prices(), News(), list_universe=fundamentals.list_universe,
                fetch_company=fundamentals.fetch_company, fetch_releases=fundamentals.fetch_releases,
                cached_company=fundamentals.cached_company,
            )
            symbols = await sources.universe(NOW)
            assert symbols == ("AAA", "BBB")
            row = (await sources.securities(("AAA",), NOW))["AAA"]
            assert row.name == "Fixture Industries"
            assert row.metrics["gross_margin_ttm"] == 0.48
            assert row.metrics["market_cap_usd"] == 50_000_000
            assert row.metric_provenance["gross_margin_ttm"] == {
                "as_of": NOW.isoformat(), "source": "finnhub", "period": "TTM", "unit": "ratio",
                "url": "https://finnhub.io/docs/api/company-basic-financials?symbol=AAA",
            }
            assert row.evidence[0].publisher == "Finnhub"
            releases = await sources.releases(symbols, NOW)
            assert [(release.symbol, release.story_id) for release in releases] == [("AAA", "earnings:AAA:2026-Q4")]
            assert "Reported EPS: 0.25" in releases[0].evidence.snippet

    asyncio.run(run())


def test_actual_news_service_delivers_material_evidence_and_explicit_empty_coverage(world, tmp_path):
    config = SimpleNamespace(
        ALPACA_API_KEY="fixture", ALPACA_SECRET_KEY="fixture", FINNHUB_API_KEY="",
        FMP_API_KEY="", OPENAI_API_KEY="", PERPLEXITY_API_KEY="", ANTHROPIC_API_KEY="",
    )
    store = StateStore(tmp_path / "adapter-news.db")
    health = ProviderHealthRegistry(config, store=store, defer_load=True, now=world.clock.now)
    news = NewsService(config=config, store=store, health=health, defer_load=True, now=world.clock.now)
    store.open()
    now = world.clock.now()
    headline = "NVIDIA raises Q4 revenue guidance on surging Blackwell GPU demand"
    world.news(["NVDA"], headline, summary="Issuer reported stronger data center demand.", at=now)
    uncitable = world.alpaca.add_news(
        ["NVDA"], "NVIDIA signs sovereign AI cluster contract win", summary="No original article URL.", created_at=now,
    )
    uncitable["url"] = ""

    async def universe():
        return ["NVDA", "EMPTY"]

    async def run():
        sources = ResearchSources(None, news, list_universe=universe, health=health)
        try:
            events = await sources.live_news(("NVDA",), (), ("EMPTY",), now)
            assert len(events) == 1 and events[0].symbol == "NVDA"
            assert events[0].evidence.title == headline
            assert events[0].evidence.published_at == now
            assert events[0].evidence.url.startswith("https://")
            assert sources.news_ok("NVDA", now) and sources.news_ok("EMPTY", now)
            assert not sources.news_ok("UNASKED", now)
            assert news.get_cursor("live") is None  # Only the research store durably tracks reaction work.
            assert await sources.live_news(("NVDA",), (), ("EMPTY",), now) == events
        finally:
            await news.aclose()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_scheduler_defaults_build_shared_research_sources_offline(world):
    from stock_agent.scheduler import StockAgentScheduler

    async def run():
        scheduler = StockAgentScheduler()
        try:
            assert isinstance(scheduler.news, NewsService)
            assert isinstance(scheduler.research, ResearchPipeline)
            assert scheduler.research.sources is scheduler.research_sources
            assert scheduler.research.gate is scheduler.research_gate
            assert scheduler.research_sources.news_service is scheduler.news
            assert scheduler.research_sources.health is scheduler.health
            assert scheduler.news.health is scheduler.health
            assert scheduler.research_sources.prices is scheduler.prices
        finally:
            await scheduler._cleanup()

    asyncio.run(run())
    world.assert_no_orders()
