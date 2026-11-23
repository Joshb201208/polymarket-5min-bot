from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from stock_agent.research_sources import ResearchSources

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
        async def poll_news(self, symbols, *, consumer, include_market, lookback_hours):
            assert include_market and lookback_hours == 24
            return SimpleNamespace(
                status="degraded", as_of=NOW, covered_symbols={"AAA"}, items=[
                    SimpleNamespace(
                        symbols=("AAA",), materiality_score=.7, story_id="abc", publisher="Issuer",
                        url="https://example.org/story", snippet="Reported results", headline="Results",
                        published_at=NOW - timedelta(minutes=10), retrieved_at=NOW,
                    ),
                    SimpleNamespace(
                        symbols=("AAA", "BBB"), materiality_score=.2, story_id="ignore",
                        url="https://example.org/low", published_at=NOW, retrieved_at=NOW,
                    ),
                ],
            )

    async def universe():
        return ["AAA", "BBB"]

    async def fetch_company(symbol):
        return SimpleNamespace(
            canonical_sector="Technology", name=symbol, tradable=True,
            metrics={"pe_ttm": SimpleNamespace(value=20), "revenue_growth_yoy": 0.2},
        )

    async def run():
        sources = ResearchSources(Prices(), News(), list_universe=universe, fetch_company=fetch_company)
        candidates = await sources.universe(NOW)
        rows = await sources.securities(candidates, NOW)
        stories = await sources.news(candidates, NOW)
        assert [(item.story_id, item.symbol) for item in stories] == [("abc", "AAA")]
        assert rows["AAA"].price == Decimal("52")
        assert rows["AAA"].metrics["earnings_yield"] == 0.05
        assert sources.quote_ok("AAA", NOW) and not sources.quote_ok("BBB", NOW)
        assert sources.news_ok("AAA", NOW) and not sources.news_ok("BBB", NOW)
        assert not sources.news_ok("AAA", NOW + timedelta(minutes=21))

    asyncio.run(run())


def test_only_confirmed_prior_earnings_release_materializes():
    class News:
        async def poll_news(self, symbols, **_):
            return SimpleNamespace(status="ok", as_of=NOW, items=[])

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
