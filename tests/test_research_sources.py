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


def test_owner_handoff_news_requires_fresh_issuer_catalysts_without_focus_boost():
    def item(key, headline, *, score=6, age=1, kind="news", url="https://example.org/news", symbols=("NVDA",)):
        return SimpleNamespace(
            story_id=key, headline=headline, snippet="", url=url, publisher="Fixture", symbols=symbols,
            kind=kind, materiality_score=score, published_at=NOW - timedelta(minutes=age), retrieved_at=NOW,
        )

    items = (
        # Exact F1(a) positive catalysts — each single real catalyst MUST block:
        item("cuts_guidance", "NVIDIA cuts Q4 revenue guidance"),
        item("delays_rubin", "NVIDIA delays Rubin shipments"),
        item("ceo_steps_down", "NVIDIA CEO Jensen Huang steps down"),
        item("earnings_single", "Nvidia reports earnings"),
        item("high", "Nvidia reports earnings and announces acquisition", score=10),
        item("filing", "Filed Form 8-K", score=3, kind="sec_filing"),
        item("regulator", "Agency announces binding restriction on Nvidia", score=3,
             url="https://www.bis.gov/notice"),
        # 24 Nov noise & negative controls — MUST NOT block owner handoff:
        item("peer_iren", "IREN announces merger and raises guidance after NVIDIA news"),
        item("peer_dell", "Dell reports Q3 earnings; AI server backlog rises", symbols=("DELL", "NVDA")),
        item("tariffs_wsj", "Trump Is Bringing Tariffs Back, and China Will Likely Get Hit Hardest: WSJ"),
        item("tariffs_broad", "US weighs broad tariff hike on semiconductor imports"),
        item("gov_macro", "Commerce Department outlines broad semiconductor tariff framework",
             score=3, url="https://www.commerce.gov/release"),
        item("wrap", "Stocks move lower as Nvidia euphoria wears off"),
        item("wrap_sp500", "S&P 500 rises as Nvidia leads chip stocks higher"),
        item("wrap_why", "Why Nvidia stock is moving today"),
        item("listicle", "3 AI stocks to buy before earnings"),
        item("price_fall", "Nvidia stock falls after epic rally on Jensen Huang's return as CEO"),
        item("shares_fall", "Nvidia Shares Fall 5% in Premarket Trading as Goldman Sachs Highlights Lingering Uncertainty"),
        item("gossip_cfo", "Nvidia's long-time CFO and now former interim CEO Colette Kress was left in the dark about Jensen Huang's return: Report"),
        item("scrutiny_board", "Nvidia board joins Jensen Huang under scrutiny after reinstating embattled CEO"),
        item("focus_non_catalyst", "Nvidia hosts developer meetup", score=8),
        item("multi_issuer", "Nvidia, AMD and Broadcom win hyperscaler AI chip deals"),
        item("foreign", "NVIDIA cuts Q4 revenue guidance", symbols=("IREN",)),
        item("past", "NVIDIA cuts Q4 revenue guidance", age=90),
        item("future", "NVIDIA cuts Q4 revenue guidance", age=-1),
    )

    async def poll(*args, **kwargs):
        assert kwargs["lookback_hours"] == 24 and not kwargs["include_market"]
        return SimpleNamespace(ok=True, as_of=NOW, covered_symbols={"NVDA"}, items=items)

    async def run():
        source = ResearchSources(None, SimpleNamespace(poll_news=poll), list_universe=None,
                                 asset_for=lambda _: SimpleNamespace(company_name="NVIDIA Corporation"))
        after = NOW - timedelta(minutes=90)
        ordinary = await source.handoff_material_news("NVDA", after, NOW)
        owner = await source.handoff_material_news("NVDA", after, NOW, owner=True)
        assert len(ordinary) == 6  # Capped at 6 newest stories with materiality_score >= 5
        # Verify with plenty of room (test exact blocking set without 6-item cap hiding any noise):
        assert {ev.evidence_id for ev in owner} <= {
            "cuts_guidance",
            "delays_rubin",
            "ceo_steps_down",
            "earnings_single",
            "high",
            "filing",
            "regulator",
        }

        # Check each item individually so the [:6] slice cannot mask a false positive or false negative.
        blocked_ids: set[str] = set()
        for single in items:
            async def poll_one(*_a, _single=single, **_kw):
                return SimpleNamespace(ok=True, as_of=NOW, covered_symbols={"NVDA"}, items=(_single,))

            s1 = ResearchSources(None, SimpleNamespace(poll_news=poll_one), list_universe=None,
                                 asset_for=lambda _: SimpleNamespace(company_name="NVIDIA Corporation Common Stock"))
            if await s1.handoff_material_news("NVDA", after, NOW, owner=True):
                blocked_ids.add(single.story_id)

        assert blocked_ids == {
            "cuts_guidance",
            "delays_rubin",
            "ceo_steps_down",
            "earnings_single",
            "high",
            "filing",
            "regulator",
        }

    asyncio.run(run())


def test_owner_handoff_strips_alpaca_share_classes_and_prefers_shorter_company_names():
    from stock_agent.research_sources import _issuer_headline

    # Direct _issuer_headline checks (no _FALLBACK_ISSUER_NAMES table involved) for Alpaca share classes:
    for alpaca_name, headline in (
        ("NVIDIA Corporation Common Stock", "NVIDIA cuts Q4 revenue guidance"),
        ("NVIDIA Corporation Common Stock", "NVIDIA delays Rubin shipments"),
        ("NVIDIA Corporation Common Stock", "NVIDIA CEO Jensen Huang steps down"),
        ("Apple Inc. Common Stock", "Apple cuts Q4 revenue guidance"),
        ("Alphabet Inc. Class A Common Stock", "Alphabet announces acquisition of cloud vendor"),
        ("Acme plc Ordinary Shares", "Acme cuts Q4 revenue guidance"),
        ("Acme Corp. Depositary Shares", "Acme CEO steps down"),
        ("Acme Co., Ltd. Sponsored ADR", "Acme delays server shipments"),
        (
            "Acme Group Limited American Depositary Shares (each representing four Ordinary Shares)",
            "Acme Group cuts Q4 revenue guidance",
        ),
    ):
        assert _issuer_headline("ZZZZ", alpaca_name, headline, leading=True), (alpaca_name, headline)

    after = NOW - timedelta(minutes=90)

    def make_story(sym, key, headline):
        return SimpleNamespace(
            story_id=key,
            headline=headline,
            snippet="",
            url="https://example.org/news",
            publisher="Fixture",
            symbols=(sym,),
            kind="news",
            materiality_score=6,
            published_at=NOW - timedelta(minutes=1),
            retrieved_at=NOW,
        )

    async def run():
        # 1. Unmapped symbol ("ACME") with Alpaca share-class suffix blocks without fallback map:
        acme_items = (make_story("ACME", "acme_cut", "Acme cuts Q4 revenue guidance"),)

        async def poll_acme(*_a, **_kw):
            return SimpleNamespace(ok=True, as_of=NOW, covered_symbols={"ACME"}, items=acme_items)

        s_acme = ResearchSources(
            None,
            SimpleNamespace(poll_news=poll_acme),
            list_universe=None,
            asset_for=lambda _: SimpleNamespace(company_name="Acme Corporation Common Stock"),
        )
        assert [ev.evidence_id for ev in await s_acme.handoff_material_news("ACME", after, NOW, owner=True)] == [
            "acme_cut"
        ]

        # 2. Shorter FMP cached company_name ("Acme") wins when Alpaca asset_for has a longer corporate title:
        s_fmp = ResearchSources(
            None,
            SimpleNamespace(poll_news=poll_acme),
            list_universe=None,
            asset_for=lambda _: SimpleNamespace(company_name="Acme Global Holdings Common Stock"),
            cached_company=lambda _: SimpleNamespace(company_name="Acme Inc."),
        )
        assert s_fmp._company_name("ACME") == "Acme Inc."
        assert [ev.evidence_id for ev in await s_fmp.handoff_material_news("ACME", after, NOW, owner=True)] == [
            "acme_cut"
        ]

        # 3. Shorter _FALLBACK_ISSUER_NAMES entry ("Micron" / "TSMC") works alongside full Alpaca ADR/Common Stock name:
        mu_items = (make_story("MU", "mu_cut", "Micron cuts Q4 revenue guidance"),)

        async def poll_mu(*_a, **_kw):
            return SimpleNamespace(ok=True, as_of=NOW, covered_symbols={"MU"}, items=mu_items)

        s_mu = ResearchSources(
            None,
            SimpleNamespace(poll_news=poll_mu),
            list_universe=None,
            asset_for=lambda _: SimpleNamespace(company_name="Micron Technology, Inc. Common Stock"),
        )
        assert s_mu._company_name("MU") == "Micron"
        assert [ev.evidence_id for ev in await s_mu.handoff_material_news("MU", after, NOW, owner=True)] == [
            "mu_cut"
        ]

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


def test_securities_cadence_order_and_preopen_only_analysis_quote():
    from decimal import Decimal
    from stock_agent.news_service import NewsItem
    from stock_agent.research_schemas import Evidence

    now = datetime(2026, 11, 25, 11, 31, tzinfo=UTC)
    brief_as_of = now - timedelta(minutes=8)
    quote_calls: list[tuple[tuple[str, ...], str, int]] = []

    class FakePrices:
        async def get_quotes(self, symbols, *, purpose, max_age_s):
            quote_calls.append((tuple(symbols), purpose, max_age_s))
            if purpose == "order_sizing":
                return SimpleNamespace(quotes={})
            return SimpleNamespace(
                quotes={
                    sym: SimpleNamespace(price=Decimal("142.50"), as_of=brief_as_of)
                    for sym in symbols
                }
            )

    filing_calls: list[tuple[str, ...]] = []

    class FakeNews:
        async def fetch_recent_filings(self, symbols, *, now=None):
            filing_calls.append(tuple(symbols))
            return {
                sym: (
                    NewsItem(
                        id=f"sec-{sym.lower()}-0001",
                        symbols=(sym,),
                        headline=f"{sym} filed SEC Form 8-K (Items 5.02)",
                        summary="SEC EDGAR Form 8-K filed on 2026-11-23 (Items 5.02).",
                        source="SEC EDGAR",
                        published_at=datetime(2026, 11, 23, 21, 10, tzinfo=UTC),
                        url="https://www.sec.gov/Archives/edgar/data/1045810/000104581026000090/0001045810-26-000090-index.htm",
                        retrieved_at=now,
                        provider="sec",
                        provider_id="0001045810-26-000090",
                        kind="sec_filing",
                        sec_form="8-K",
                        sec_items=("5.02",),
                        materiality=8,
                        materiality_reasons=("sec_8-k",),
                    ),
                )
                for sym in symbols
            }

    tr_ev = Evidence(
        evidence_id="tipranks:NVDA:2026-11-25",
        title="NVDA TipRanks screener snapshot (2026-11-25)",
        url="https://www.tipranks.com/stocks/nvda/forecast",
        publisher="TipRanks",
        published_at=datetime(2026, 11, 25, 0, 0, tzinfo=UTC),
        retrieved_at=now,
        snippet="TipRanks Smart Score 7/10; analyst consensus Strong Buy.",
    )
    snap_ev = Evidence(
        evidence_id="finnhub-metrics:NVDA:2026-11-25",
        title="NVDA key financial metrics",
        url="https://finnhub.io/docs/api/company-basic-financials?symbol=NVDA",
        publisher="Finnhub",
        published_at=now,
        retrieved_at=now,
        snippet="TTM metrics.",
    )

    class FakeTipRanks:
        def evidence_for(self, symbol, _now):
            return (tr_ev,) if symbol == "NVDA" else ()

    async def fake_company(symbol):
        return SimpleNamespace(
            company_name=f"{symbol} Corp",
            canonical_sector="Technology",
            tradable=True,
            metrics={},
            evidence=(snap_ev,) if symbol == "NVDA" else (),
        )

    async def list_uni():
        return ("ORCL", "AMD", "NVDA", "EEE")

    def fake_focus(*, now=None, cadence=None):
        if cadence == "live":
            return ("AMD",)
        if cadence == "daily":
            return ("ORCL",)
        return ("AMD", "ORCL")

    sources = ResearchSources(
        FakePrices(),
        FakeNews(),
        list_universe=list_uni,
        fetch_company=fake_company,
        tipranks=FakeTipRanks(),
        focus_for=fake_focus,
        directives_for=lambda *, now=None: {"NVDA": SimpleNamespace(symbol="NVDA")},
    )

    # 1. Pre-open prescan: daily ("ORCL") included, deterministic order owner -> live -> daily,
    #    filings on priority_evidence (baseline evidence unchanged), brief quote on analysis_price only.
    prescan_rows = asyncio.run(
        sources.securities(("ORCL", "AMD", "NVDA", "EEE"), now, prescan=True)
    )
    assert filing_calls == [("NVDA", "AMD", "ORCL")]
    assert quote_calls == [
        (("ORCL", "AMD", "NVDA", "EEE"), "order_sizing", 90),
        (("NVDA", "AMD", "ORCL"), "brief", 1200),
    ]
    assert prescan_rows["NVDA"].price is None
    assert prescan_rows["NVDA"].quote_at is None
    assert prescan_rows["NVDA"].analysis_price == Decimal("142.50")
    assert prescan_rows["NVDA"].analysis_quote_at == brief_as_of
    assert not sources.quote_ok("NVDA", now)
    assert [ev.publisher for ev in prescan_rows["NVDA"].evidence] == ["TipRanks", "Finnhub"]
    assert [ev.publisher for ev in prescan_rows["NVDA"].priority_evidence] == ["SEC EDGAR"]
    assert [ev.publisher for ev in prescan_rows["ORCL"].priority_evidence] == ["SEC EDGAR"]
    assert prescan_rows["EEE"].analysis_price is None

    # 2. Pre-open non-prescan (e.g., pre-open weekly): owner + live only; daily ("ORCL") excluded.
    filing_calls.clear()
    quote_calls.clear()
    preopen_weekly = asyncio.run(
        sources.securities(("ORCL", "AMD", "NVDA", "EEE"), now, preopen=True)
    )
    assert filing_calls == [("NVDA", "AMD")]
    assert quote_calls == [
        (("ORCL", "AMD", "NVDA", "EEE"), "order_sizing", 90),
        (("NVDA", "AMD"), "brief", 1200),
    ]
    assert preopen_weekly["ORCL"].priority_evidence == ()
    assert preopen_weekly["ORCL"].analysis_price is None

    # 3. Regular hours (preopen=False, prescan=False): NEVER fetches 20m brief quotes, daily excluded,
    #    price/quote_at/evidence identical to master.
    filing_calls.clear()
    quote_calls.clear()
    regular_rows = asyncio.run(sources.securities(("ORCL", "AMD", "NVDA", "EEE"), now))
    assert filing_calls == [("NVDA", "AMD")]
    assert quote_calls == [(("ORCL", "AMD", "NVDA", "EEE"), "order_sizing", 90)]
    assert regular_rows["NVDA"].price is None
    assert regular_rows["NVDA"].analysis_price is None
    assert regular_rows["NVDA"].analysis_quote_at is None
    assert regular_rows["ORCL"].priority_evidence == ()


def test_background_preserves_master_chronological_published_at_order():
    from stock_agent.news_service import NewsFeedBatch, NewsItem

    now = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    recent_items = [
        NewsItem(
            id=f"story-{i}",
            symbols=("AAA",),
            headline=f"Recent story #{i}",
            summary="Summary.",
            source="Yahoo",
            published_at=now - timedelta(minutes=i + 1),
            url=f"https://finance.yahoo.com/news/story-{i}",
            retrieved_at=now,
            provider="finnhub",
            materiality=2,
        )
        for i in range(6)
    ]
    older_filing = NewsItem(
        id="sec-aaa-8k",
        symbols=("AAA",),
        headline="AAA filed SEC Form 8-K (Items 5.02)",
        summary="SEC EDGAR Form 8-K filed on 2026-11-23 (Items 5.02).",
        source="SEC EDGAR",
        published_at=now - timedelta(hours=40),
        url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/0000000001-26-000001-index.htm",
        retrieved_at=now,
        provider="sec",
        kind="sec_filing",
        sec_form="8-K",
        sec_items=("5.02",),
        materiality=8,
    )

    class FakeNews:
        async def poll_news(self, symbols, **kwargs):
            all_items = (*recent_items, older_filing)
            return NewsFeedBatch(
                ok=True,
                degraded=False,
                as_of=now,
                items=all_items,
                new_items=all_items,
                material_new_items=(older_filing,),
                errors={},
                covered_symbols=tuple(symbols),
            )

    async def list_uni():
        return ("AAA",)

    sources = ResearchSources(None, FakeNews(), list_universe=list_uni)
    bg = asyncio.run(sources.background(("AAA",)))["AAA"]
    assert [ev.evidence_id for ev in bg] == [f"story-{i}" for i in range(6)]
