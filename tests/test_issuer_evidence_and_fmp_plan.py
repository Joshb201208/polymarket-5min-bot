"""Offline recorded-fixture tests for SEC EDGAR primary filing text & FMP plan awareness."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx

from stock_agent.config import Config
from stock_agent.fundamentals_service import FundamentalsService
from stock_agent.health import ProviderHealthRegistry
from stock_agent.http_safety import DataHttpClient
from stock_agent.news_service import NewsService
from stock_agent.price_service import PriceService
from stock_agent.research_pipeline import _edgar_filing, _edgar_index_only
from stock_agent.research_sources import ResearchSources

FIXTURES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "issuer_evidence_fmp.json").read_text(encoding="utf-8")
)


def _cfg(tmp_path: Path, **overrides):
    base = {
        "DATA_DIR": tmp_path,
        "ALPACA_API_KEY": "test-alpaca",
        "ALPACA_SECRET_KEY": "test-secret",
        "ALPACA_DATA_URL": "https://data.alpaca.markets",
        "FINNHUB_API_KEY": "test-finnhub",
        "FMP_API_KEY": "test-fmp-key",
        "FMP_QUOTES_ENABLED": True,
        "SEC_USER_AGENT": "StockAgentTest/2.0 (ops@example.test)",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_sec_8k_exhibit_99_1_and_10q_summary_extraction_disk_cache_and_dates(world, tmp_path):
    now = datetime(2026, 11, 24, 13, 30, tzinfo=UTC)
    sec_fx = FIXTURES["sec"]
    archive_hits: list[str] = []
    seen_headers: list[dict[str, str]] = []

    world.sec.on("GET", "/submissions/CIK0001045810.json", sec_fx["nvda_submissions"])

    def serve_index(req: httpx.Request) -> httpx.Response:
        archive_hits.append(req.url.path)
        seen_headers.append(dict(req.headers))
        return httpx.Response(200, text=sec_fx["nvda_8k_index_html"], headers={"content-type": "text/html"})

    def serve_ex99(req: httpx.Request) -> httpx.Response:
        archive_hits.append(req.url.path)
        seen_headers.append(dict(req.headers))
        return httpx.Response(200, text=sec_fx["nvda_ex99_1_html"], headers={"content-type": "text/html"})

    def serve_10q(req: httpx.Request) -> httpx.Response:
        archive_hits.append(req.url.path)
        seen_headers.append(dict(req.headers))
        return httpx.Response(200, text=sec_fx["nvda_10q_html"], headers={"content-type": "text/html"})

    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000099/0001045810-26-000099-index.htm",
        serve_index,
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000099/q3fy27pr.htm",
        serve_ex99,
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000100/nvda-20261025.htm",
        serve_10q,
    )

    cfg = _cfg(tmp_path)

    async def run_test():
        svc1 = NewsService(config=cfg, enable_sec_filings=True, now=lambda: now)
        try:
            batches1 = await svc1.fetch_recent_filings(["NVDA"], now=now)
        finally:
            await svc1.aclose()

        # Brand-new NewsService instance on same DATA_DIR must hit on-disk cache (zero new Archive GETs)
        hits_after_first = len(archive_hits)
        svc2 = NewsService(config=cfg, enable_sec_filings=True, now=lambda: now)
        try:
            batches2 = await svc2.fetch_recent_filings(["NVDA"], now=now)
        finally:
            await svc2.aclose()
        return batches1, batches2, hits_after_first, len(archive_hits)

    batches1, batches2, hits_first, hits_second = asyncio.run(run_test())
    assert hits_first >= 2
    assert hits_second == hits_first, "Expected disk cache to prevent re-downloading immutable SEC Archive docs"

    # SEC fair-access User-Agent must be sent on every sec.gov/Archives request
    for hdrs in seen_headers:
        ua = hdrs.get("user-agent") or hdrs.get("User-Agent") or ""
        assert "StockAgentTest/2.0" in ua

    items = batches1["NVDA"]
    items_8k = [item for item in items if item.sec_form == "8-K"]
    items_10q = [item for item in items if item.sec_form == "10-Q"]
    assert items_8k and len(items_10q) == 1

    item_8k = items_8k[0]
    assert item_8k.id == "sec-nvda-000104581026000099:1"
    assert item_8k.url == "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000099/q3fy27pr.htm"
    assert _edgar_filing(item_8k.url) and not _edgar_index_only(item_8k.url)
    joined_8k = " ".join(i.summary for i in items_8k)
    assert "$35.08 billion" in joined_8k
    assert "$0.81" in joined_8k
    assert "<table" not in joined_8k and "console.log" not in joined_8k
    assert all(80 <= len(i.summary) <= 700 for i in items_8k)
    assert all(i.published_at == datetime(2026, 11, 19, 21, 20, tzinfo=UTC) for i in items_8k)

    item_10q = items_10q[0]
    assert item_10q.id == "sec-nvda-000104581026000100"
    assert item_10q.url == "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000100/nvda-20261025.htm"
    assert _edgar_filing(item_10q.url) and not _edgar_index_only(item_10q.url)
    assert "XBRL_HIDDEN_NOISE_DO_NOT_EMIT" not in item_10q.summary
    assert "Data Center compute revenue grew 112%" in item_10q.summary
    assert "$21.87 billion" in item_10q.summary
    assert len(item_10q.summary) <= 700

    # Cached second service must return identical primary evidence
    assert [i.summary for i in batches2["NVDA"]] == [i.summary for i in items]

    # Supply through ResearchSources -> check dated primary Evidence & day-precision vs instant-precision
    async def check_sources():
        svc = NewsService(config=cfg, enable_sec_filings=True, now=lambda: now)
        try:
            sources = ResearchSources(
                SimpleNamespace(get_quotes=lambda *_a, **_k: asyncio.sleep(0, SimpleNamespace(quotes={}))),
                svc,
                list_universe=lambda: asyncio.sleep(0, ("NVDA",)),
                held_symbols=lambda: ("NVDA",),
            )
            sec_map = await sources.securities(("NVDA",), now)
            return sec_map["NVDA"]
        finally:
            await svc.aclose()

    nvda_sec = asyncio.run(check_sources())
    ev_by_id = {ev.evidence_id: ev for ev in (*nvda_sec.evidence, *nvda_sec.priority_evidence)}
    ev_8k = ev_by_id["sec-nvda-000104581026000099:1"]
    ev_10q = ev_by_id["sec-nvda-000104581026000100"]
    assert ev_8k.date_precision == "instant"
    assert ev_8k.published_at == datetime(2026, 11, 19, 21, 20, tzinfo=UTC)
    assert ev_10q.date_precision == "day"
    assert ev_10q.published_at == datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    # Primary real-text filings must also be present in security.evidence so ResearchPipeline sees recent_primary
    assert any(not _edgar_index_only(ev.url) and _edgar_filing(ev.url) for ev in nvda_sec.evidence)


def test_sec_archive_failure_fails_closed_to_index_stub_and_enforces_rate_limit(world, tmp_path):
    from stock_agent.sec_filings import SecRateLimiter

    # 1. Verify 10 req/s limiter spaces calls >= 0.10s apart
    fake_mono = [100.0]
    slept: list[float] = []

    async def fake_sleep(dt: float) -> None:
        slept.append(round(dt, 4))
        fake_mono[0] += dt

    limiter = SecRateLimiter(max_per_second=10.0, clock=lambda: fake_mono[0], sleep=fake_sleep)

    async def burst():
        for _ in range(4):
            await limiter.acquire()

    asyncio.run(burst())
    assert len(slept) == 3
    assert all(dt >= 0.1 for dt in slept)

    # 2. Verify broken/404 Archive doc fails closed to -index.htm metadata without inventing text
    now = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
    world.sec.on(
        "GET",
        "/submissions/CIK0001045810.json",
        {
            "cik": "1045810",
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "filingDate": ["2026-11-23"],
                    "accessionNumber": ["0001045810-26-000111"],
                    "items": [["2.02"]],
                    "acceptanceDateTime": ["2026-11-23T21:05:00Z"],
                    "primaryDocument": ["broken.htm"],
                }
            },
        },
    )
    world.sec.fail("/Archives/edgar/data/*", "status", status=404, times=None)

    cfg = _cfg(tmp_path)

    async def fetch_broken():
        svc = NewsService(config=cfg, enable_sec_filings=True, now=lambda: now)
        try:
            return await svc.fetch_recent_filings(["NVDA"], now=now)
        finally:
            await svc.aclose()

    batches = asyncio.run(fetch_broken())
    item = batches["NVDA"][0]
    assert _edgar_index_only(item.url)
    assert item.url.endswith("0001045810-26-000111-index.htm")


def test_scheduled_background_supplies_held_and_screened_primary_filings_first(world, tmp_path):
    now = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
    sec_fx = FIXTURES["sec"]
    world.sec.on("GET", "/submissions/CIK0001045810.json", sec_fx["nvda_submissions"])
    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000099/0001045810-26-000099-index.htm",
        lambda _r: httpx.Response(200, text=sec_fx["nvda_8k_index_html"], headers={"content-type": "text/html"}),
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000099/q3fy27pr.htm",
        lambda _r: httpx.Response(200, text=sec_fx["nvda_ex99_1_html"], headers={"content-type": "text/html"}),
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000100/nvda-20261025.htm",
        lambda _r: httpx.Response(200, text=sec_fx["nvda_10q_html"], headers={"content-type": "text/html"}),
    )
    world.news(["NVDA"], "Analyst commentary on AI GPUs", summary="Secondary commentary.", at=now - timedelta(hours=2))

    cfg = _cfg(tmp_path)

    async def run_bg():
        svc = NewsService(config=cfg, enable_sec_filings=True, now=lambda: now)
        try:
            sources = ResearchSources(None, svc, list_universe=lambda: asyncio.sleep(0, ("NVDA",)))
            return await sources.background(("NVDA",))
        finally:
            await svc.aclose()

    bg = asyncio.run(run_bg())["NVDA"]
    assert any(
        ev.evidence_id == "sec-nvda-000104581026000099:1" and "$35.08 billion" in ev.snippet
        for ev in bg
    )


def test_fmp_free_plan_detects_refusals_at_startup_and_stops_calling_refused_endpoints(world, tmp_path):
    from stock_agent.fmp_plan import FmpPlanDetector

    now = world.clock.now()
    ts = int(now.timestamp())
    free_fx = FIXTURES["fmp_free"]

    def handle_quote(req: httpx.Request) -> httpx.Response:
        sym = req.url.params.get("symbol", "")
        if sym == "AAPL":
            row = {**free_fx["aapl_quote"][0], "timestamp": ts}
            return httpx.Response(200, json=[row])
        return httpx.Response(402, json=free_fx["restricted_symbol_error"])

    world.fmp.on("GET", "/stable/quote", handle_quote)
    world.fmp.on("GET", "/stable/batch-quote", lambda _r: httpx.Response(402, json=free_fx["restricted_endpoint_error"]))
    world.fmp.on(
        "GET",
        "/stable/analyst-estimates",
        lambda _r: httpx.Response(402, json=free_fx["restricted_endpoint_error"]),
    )
    world.fmp.on(
        "GET",
        "/stable/news/press-releases",
        lambda _r: httpx.Response(402, json=free_fx["restricted_endpoint_error"]),
    )
    # Finnhub quote fails so PriceService reaches FMP fallback
    world.finnhub.fail("/api/v1/quote", "status", status=503, times=None)
    world.finnhub.on(
        "GET",
        "/api/v1/stock/metric",
        {"metric": {"peTTM": 28.0, "grossMarginTTM": 64.0, "marketCapitalization": 110_000}},
    )
    world.finnhub.on("GET", "/api/v1/stock/profile2", {"name": "Arista Networks", "finnhubIndustry": "Technology"})
    world.finnhub.on("GET", "/api/v1/stock/insider-transactions", {"data": []})
    world.finnhub.on("GET", "/api/v1/stock/recommendation", [])
    world.fmp.on("GET", "/stable/profile", lambda _r: httpx.Response(402, json=free_fx["restricted_symbol_error"]))

    cfg = _cfg(tmp_path)
    health = ProviderHealthRegistry(cfg, now=world.clock.now)

    async def run_free():
        http = DataHttpClient(cfg)
        try:
            detector = FmpPlanDetector(http, config=cfg, health=health, now=world.clock.now)
            caps = await detector.probe()
            assert not caps.quote_all_symbols_allowed
            assert caps.sample_quotes_allowed
            assert not caps.batch_quotes_allowed
            assert not caps.quarterly_estimates_allowed
            assert not caps.press_releases_allowed

            before_runtime = len(world.net.requests(service="fmp"))

            prices = PriceService(http, config=cfg, now=world.clock.now, fmp_plan=detector)
            q_batch = await prices.get_quotes(["ANET", "AAPL"], purpose="stop_check")
            assert "ANET" in q_batch.missing
            assert q_batch.quotes["AAPL"].price == Decimal("229.5")
            assert q_batch.quotes["AAPL"].source == "fmp_quote"

            fund = FundamentalsService(http, config=cfg, health=health, now=world.clock.now, fmp_plan=detector)
            snap = await fund.fetch_company("ANET")
            assert "eps_estimate_next_q" not in snap.metrics

            news = NewsService(http, config=cfg, health=health, now=world.clock.now, fmp_plan=detector)
            nbatch = await news.poll_news(["ANET"], include_market=False)
            assert nbatch.ok

            runtime_fmp = world.net.requests(service="fmp")[before_runtime:]
            # Free plan must make ZERO runtime calls to batch-quote, analyst-estimates, press-releases, or ANET quote!
            assert not [r for r in runtime_fmp if r.path == "/stable/batch-quote"]
            assert not [r for r in runtime_fmp if r.path == "/stable/analyst-estimates"]
            assert not [r for r in runtime_fmp if r.path == "/stable/news/press-releases"]
            assert not [
                r for r in runtime_fmp if r.path == "/stable/quote" and r.params.get("symbol") == "ANET"
            ]
        finally:
            await http.aclose()

    asyncio.run(run_free())


def test_fmp_plan_upgrade_while_running_switches_on_batch_quotes_estimates_and_press_releases(world, tmp_path):
    from stock_agent.fmp_plan import FmpPlanDetector

    free_fx = FIXTURES["fmp_free"]
    prem_fx = FIXTURES["fmp_premium"]
    upgraded = False

    def handle_quote(req: httpx.Request) -> httpx.Response:
        ts = int(world.clock.now().timestamp())
        sym = req.url.params.get("symbol", "")
        if not upgraded:
            if sym == "AAPL":
                return httpx.Response(200, json=[{**free_fx["aapl_quote"][0], "timestamp": ts}])
            return httpx.Response(402, json=free_fx["restricted_symbol_error"])
        return httpx.Response(200, json=[{**prem_fx["anet_quote"][0], "symbol": sym, "timestamp": ts}])

    def handle_batch_quote(req: httpx.Request) -> httpx.Response:
        if not upgraded:
            return httpx.Response(402, json=free_fx["restricted_endpoint_error"])
        ts = int(world.clock.now().timestamp())
        return httpx.Response(200, json=[{**row, "timestamp": ts} for row in prem_fx["batch_quotes"]])

    def handle_estimates(_req: httpx.Request) -> httpx.Response:
        if not upgraded:
            return httpx.Response(402, json=free_fx["restricted_endpoint_error"])
        return httpx.Response(200, json=prem_fx["anet_quarterly_estimates"])

    def handle_press(_req: httpx.Request) -> httpx.Response:
        if not upgraded:
            return httpx.Response(402, json=free_fx["restricted_endpoint_error"])
        return httpx.Response(200, json=prem_fx["press_releases"])

    world.fmp.on("GET", "/stable/quote", handle_quote)
    world.fmp.on("GET", "/stable/batch-quote", handle_batch_quote)
    world.fmp.on("GET", "/stable/analyst-estimates", handle_estimates)
    world.fmp.on("GET", "/stable/news/press-releases", handle_press)
    world.finnhub.on(
        "GET",
        "/api/v1/stock/metric",
        {"metric": {"peTTM": 32.0, "grossMarginTTM": 64.0, "marketCapitalization": 110_000}},
    )
    world.finnhub.on("GET", "/api/v1/stock/profile2", {"name": "Arista Networks", "finnhubIndustry": "Technology"})
    world.finnhub.on("GET", "/api/v1/stock/insider-transactions", {"data": []})
    world.finnhub.on("GET", "/api/v1/stock/recommendation", [])

    cfg = _cfg(tmp_path)
    health = ProviderHealthRegistry(cfg, now=world.clock.now)
    # Seed a prior Free-plan 402 block on FMP fundamentals to ensure upgrade clears it
    health.record_failure("fmp", "fundamentals", status_code=402, symbol="ANET", at=world.clock.now())
    assert health.is_blocked("fmp", "fundamentals", "ANET", now=world.clock.now())

    async def run_upgrade():
        nonlocal upgraded
        http = DataHttpClient(cfg)
        try:
            detector = FmpPlanDetector(http, config=cfg, health=health, now=world.clock.now)
            caps0 = await detector.probe()
            assert not caps0.batch_quotes_allowed

            prices = PriceService(http, config=cfg, now=world.clock.now, fmp_plan=detector)
            fund = FundamentalsService(http, config=cfg, health=health, now=world.clock.now, fmp_plan=detector)
            news = NewsService(http, config=cfg, health=health, now=world.clock.now, fmp_plan=detector)

            # Josh upgrades FMP key in-place to Premium; 24h daily re-probe fires automatically
            upgraded = True
            world.clock.advance(hours=24, minutes=5)
            await detector.maybe_reprobe(world.clock.now())

            caps1 = detector.capabilities
            assert caps1.quote_all_symbols_allowed
            assert caps1.batch_quotes_allowed
            assert caps1.quarterly_estimates_allowed
            assert caps1.press_releases_allowed
            assert not health.is_blocked("fmp", "fundamentals", "ANET", now=world.clock.now())

            # 1. Batch quotes resolve ANET, BK, GS in ONE /stable/batch-quote HTTP request
            before_batch = len(world.net.requests(service="fmp", path="/stable/batch-quote"))
            q_res = await prices.get_quotes(["ANET", "BK", "GS"], purpose="stop_check")
            after_batch = len(world.net.requests(service="fmp", path="/stable/batch-quote"))
            assert after_batch - before_batch == 1
            assert q_res.quotes["ANET"].price == Decimal("104.25")
            assert q_res.quotes["BK"].price == Decimal("81.5")
            assert q_res.quotes["GS"].price == Decimal("592.0")

            # 2. Quarterly analyst estimates populate metrics + undated Evidence (never future published_at)
            snap = await fund.fetch_company("ANET")
            assert snap.metrics["eps_estimate_next_q"].value == 0.65
            assert snap.metrics["revenue_estimate_next_q_usd"].value == 2_225_000_000.0
            assert snap.metrics["analyst_count_eps_next_q"].value == 23.0
            est_ev = next(e for e in snap.evidence if e.evidence_id.startswith("fmp:estimates:ANET:"))
            assert est_ev.published_at is None
            assert "0.65" in est_ev.snippet

            # 3. Press releases switch on with exact ET -> UTC conversion (08:30 EST -> 13:30 UTC)
            nbatch = await news.poll_news(["ANET"], include_market=False, lookback_hours=48)
            pr_item = next(i for i in nbatch.items if "800G AI Spine" in i.headline)
            assert pr_item.published_at == datetime(2026, 11, 23, 13, 30, tzinfo=UTC)
            assert pr_item.provider == "fmp"
            assert "$2.20 billion to $2.25 billion" in pr_item.summary

            for req in world.net.requests(service="fmp"):
                assert "apikey=" not in str(req.url).lower()
        finally:
            await http.aclose()

    asyncio.run(run_upgrade())


def test_held_symbols_prioritized_ahead_of_non_held_under_per_run_cap(world, tmp_path):
    now = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    sec_fx = FIXTURES["sec"]
    queried_ciks: list[str] = []

    def lookup_cik(sym: str) -> str | None:
        if sym == "HELD1":
            return "0009990001"
        if sym.startswith("UNI"):
            return f"00088800{int(sym[3:]):02d}"
        return None

    def handle_submissions(req: httpx.Request) -> httpx.Response:
        cik = req.url.path.split("CIK")[1].split(".")[0]
        queried_ciks.append(cik)
        if cik == "0009990001":
            payload = {
                "cik": "9990001",
                "filings": {
                    "recent": {
                        "form": ["8-K"],
                        "filingDate": ["2026-11-19"],
                        "accessionNumber": ["0009990001-26-000001"],
                        "items": [["2.02"]],
                        "acceptanceDateTime": ["2026-11-19T21:20:00Z"],
                        "primaryDocument": ["q3pr.htm"],
                    }
                },
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(
            200, json={"cik": cik, "filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": []}}}
        )

    world.sec.on("GET", "/submissions/*.json", handle_submissions)
    world.sec.on(
        "GET",
        "/Archives/edgar/data/9990001/000999000126000001/0009990001-26-000001-index.htm",
        lambda _r: httpx.Response(
            200,
            text='<table><tr><td>1</td><td>PR</td><td><a href="q3pr.htm">q3pr.htm</a></td><td>EX-99.1</td></tr></table>',
            headers={"content-type": "text/html"},
        ),
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/9990001/000999000126000001/q3pr.htm",
        lambda _r: httpx.Response(200, text=sec_fx["nvda_ex99_1_html"], headers={"content-type": "text/html"}),
    )

    cfg = _cfg(tmp_path)

    async def run_cap():
        svc = NewsService(config=cfg, cik_lookup=lookup_cik, enable_sec_filings=True, now=lambda: now)
        try:
            # Tighten per-run cap on fetch_recent_filings to 2 symbols; HELD1 is passed last in caller list
            orig_fetch = svc.fetch_recent_filings

            async def capped_fetch(syms, *, now=None):
                return await orig_fetch(syms, now=now, max_symbols=2)

            svc.fetch_recent_filings = capped_fetch  # type: ignore[method-assign]
            sources = ResearchSources(
                SimpleNamespace(get_quotes=lambda *_a, **_k: asyncio.sleep(0, SimpleNamespace(quotes={}))),
                svc,
                list_universe=lambda: asyncio.sleep(0, ("UNI01", "UNI02", "UNI03", "HELD1")),
                focus_for=lambda **_kw: ("UNI01", "UNI02", "UNI03"),
                directives_for=lambda **_kw: {},
                held_symbols=lambda: ("HELD1",),
            )
            sec_map = await sources.securities(("UNI01", "UNI02", "UNI03", "HELD1"), now)
            return sec_map
        finally:
            await svc.aclose()

    sec_map = asyncio.run(run_cap())
    assert queried_ciks[0] == "0009990001", "Held symbol must be queried first before focus/universe under cap"
    assert len(queried_ciks) == 2
    assert any("$35.08 billion" in (ev.snippet or "") for ev in sec_map["HELD1"].evidence)


def test_fmp_key_rotation_and_runtime_402_tripwire(world, tmp_path):
    from stock_agent.fmp_plan import FmpPlanDetector

    prem_fx = FIXTURES["fmp_premium"]
    free_fx = FIXTURES["fmp_free"]
    key_mode = "free"

    def by_key(_req: httpx.Request) -> httpx.Response:
        if key_mode == "free":
            return httpx.Response(402, json=free_fx["restricted_endpoint_error"])
        ts = int(world.clock.now().timestamp())
        return httpx.Response(200, json=[{**row, "timestamp": ts} for row in prem_fx["batch_quotes"]])

    world.fmp.on("GET", "/stable/quote", by_key)
    world.fmp.on("GET", "/stable/batch-quote", by_key)
    world.fmp.on("GET", "/stable/analyst-estimates", lambda _r: httpx.Response(
        200 if key_mode == "premium" else 402,
        json=prem_fx["anet_quarterly_estimates"] if key_mode == "premium" else free_fx["restricted_endpoint_error"],
    ))
    world.fmp.on("GET", "/stable/news/press-releases", lambda _r: httpx.Response(
        200 if key_mode == "premium" else 402,
        json=prem_fx["press_releases"] if key_mode == "premium" else free_fx["restricted_endpoint_error"],
    ))

    cfg = _cfg(tmp_path, FMP_API_KEY="free-key-v1")

    async def run_rotation():
        nonlocal key_mode
        http = DataHttpClient(cfg)
        try:
            det = FmpPlanDetector(http, config=cfg, now=world.clock.now)
            await det.probe()
            assert not det.batch_quotes_enabled()

            # Operator swaps FMP_API_KEY in-memory before 24h elapses -> maybe_reprobe detects fingerprint change immediately
            key_mode = "premium"
            cfg.FMP_API_KEY = "premium-key-v2"
            await det.maybe_reprobe(world.clock.now())
            assert det.batch_quotes_enabled()
            assert det.quarterly_estimates_enabled()
            assert det.press_releases_enabled()

            # Runtime 402 tripwire immediately disables the refused capability until next daily probe
            det.record_endpoint_refused("batch_quotes")
            assert not det.batch_quotes_enabled()
            assert det.quarterly_estimates_enabled()
        finally:
            await http.aclose()

    asyncio.run(run_rotation())


def test_real_sec_layouts_strip_sgml_and_contacts_and_emit_three_dated_earnings_excerpts(world, tmp_path):
    now = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)
    real_fx = FIXTURES["sec_real_trimmed"]

    world.sec.on(
        "GET",
        "/submissions/CIK0001045810.json",
        {
            "cik": "1045810",
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "filingDate": ["2026-11-17"],
                    "accessionNumber": ["0001045810-26-000088"],
                    "items": [["2.02", "9.01"]],
                    "acceptanceDateTime": ["2026-11-17T21:20:00Z"],
                    "primaryDocument": ["nvda-20261117.htm"],
                }
            },
        },
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000088/0001045810-26-000088-index.htm",
        lambda _r: httpx.Response(
            200,
            text='<table><tr><td>2</td><td>EX-99.1</td><td><a href="q3fy27pr.htm">q3fy27pr.htm</a></td><td>EX-99.1</td></tr></table>',
            headers={"content-type": "text/html"},
        ),
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000088/q3fy27pr.htm",
        lambda _r: httpx.Response(200, text=real_fx["nvda_ex99_1_html"], headers={"content-type": "text/html"}),
    )

    world.sec.on(
        "GET",
        "/submissions/CIK0000723125.json",
        {
            "cik": "723125",
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "filingDate": ["2026-09-30"],
                    "accessionNumber": ["0000723125-26-000018"],
                    "items": [["2.02", "9.01"]],
                    "acceptanceDateTime": [""],
                    "primaryDocument": ["d8k.htm"],
                }
            },
        },
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/723125/000072312526000018/0000723125-26-000018-index.htm",
        lambda _r: httpx.Response(
            200,
            text='<table><tr><td>2</td><td>EX-99.1 EARNINGS RELEASE</td><td><a href="a2026q4ex991-pressrelease.htm">a2026q4ex991-pressrelease.htm</a></td><td>EX-99.1</td></tr></table>',
            headers={"content-type": "text/html"},
        ),
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/723125/000072312526000018/a2026q4ex991-pressrelease.htm",
        lambda _r: httpx.Response(200, text=real_fx["mu_ex99_1_html"], headers={"content-type": "text/html"}),
    )

    cfg = _cfg(tmp_path)

    async def run_real_8k():
        svc = NewsService(config=cfg, enable_sec_filings=True, now=lambda: now)
        try:
            sources = ResearchSources(
                SimpleNamespace(get_quotes=lambda *_a, **_k: asyncio.sleep(0, SimpleNamespace(quotes={}))),
                svc,
                list_universe=lambda: asyncio.sleep(0, ("NVDA", "MU")),
                held_symbols=lambda: ("NVDA", "MU"),
            )
            sec_map = await sources.securities(("NVDA", "MU"), now)
            bg_map = await sources.background(("NVDA", "MU"))
            return sec_map, bg_map
        finally:
            await svc.aclose()

    sec_map, bg_map = asyncio.run(run_real_8k())

    nvda_ev = {e.evidence_id: e for e in sec_map["NVDA"].evidence}
    assert {
        "sec-nvda-000104581026000088:1",
        "sec-nvda-000104581026000088:2",
        "sec-nvda-000104581026000088:3",
    } <= set(nvda_ev)
    for suffix in (":1", ":2", ":3"):
        ev = nvda_ev[f"sec-nvda-000104581026000088{suffix}"]
        assert len(ev.snippet or "") <= 700
        assert "q3fy27pr.htm" not in (ev.snippet or "")
        assert not (ev.snippet or "").startswith("EX-99.1")
        assert ev.published_at == datetime(2026, 11, 17, 21, 20, tzinfo=UTC)
    assert "$115.8 billion" in (nvda_ev["sec-nvda-000104581026000088:1"].snippet or "")
    assert "$2.70 and $2.68" in (nvda_ev["sec-nvda-000104581026000088:2"].snippet or "")
    assert "Outlook" in (nvda_ev["sec-nvda-000104581026000088:3"].snippet or "")
    assert "$130.0 billion" in (nvda_ev["sec-nvda-000104581026000088:3"].snippet or "")

    # Scheduled background() also receives all 3 NVDA chunks including Outlook
    bg_ids = {e.evidence_id for e in bg_map["NVDA"]}
    assert "sec-nvda-000104581026000088:3" in bg_ids

    # MU: leading Contacts/emails/phones stripped; day-precision preserved; Business Outlook extracted
    mu_ev = {e.evidence_id: e for e in sec_map["MU"].evidence}
    assert {
        "sec-mu-000072312526000018:1",
        "sec-mu-000072312526000018:2",
        "sec-mu-000072312526000018:3",
    } <= set(mu_ev)
    for suffix in (":1", ":2", ":3"):
        ev = mu_ev[f"sec-mu-000072312526000018{suffix}"]
        text = ev.snippet or ""
        assert len(text) <= 700
        assert "Satya Kumar" not in text
        assert "satyakumar@micron.com" not in text
        assert "(408) 450-6199" not in text
        assert not text.startswith("EX-99.1")
        assert ev.date_precision == "day"
        assert ev.published_at == datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    assert "$54.23 billion" in (mu_ev["sec-mu-000072312526000018:1"].snippet or "")
    assert "Business Outlook" in (mu_ev["sec-mu-000072312526000018:3"].snippet or "")
    assert "$61.5 billion" in (mu_ev["sec-mu-000072312526000018:3"].snippet or "")
    assert "$37.84" in (mu_ev["sec-mu-000072312526000018:3"].snippet or "")


def test_real_10q_10k_toc_rejected_prose_extracted_and_toc_only_fails_closed(world, tmp_path):
    from stock_agent.sec_filings import extract_periodic_summary_snippet

    real_fx = FIXTURES["sec_real_trimmed"]

    # 1. Real NVDA 10-Q (multi-cell TOC with standalone page number 25 + Item 3/4/Part II) -> skips TOC, extracts MD&A prose
    nvda_10q = extract_periodic_summary_snippet(real_fx["nvda_10q_html"], form="10-Q")
    assert nvda_10q is not None and len(nvda_10q) <= 700
    assert "Quantitative and Qualitative Disclosures" not in nvda_10q
    assert "Unregistered Sales of Equity Securities" not in nvda_10q
    assert "Vera Rubin" in nvda_10q

    # 2. Real AAPL 10-K (multi-cell TOC with standalone page number 24 + Item 7A/8/9) -> skips TOC, extracts Segment Operating Performance
    aapl_10k = extract_periodic_summary_snippet(real_fx["aapl_10k_html"], form="10-K")
    assert aapl_10k is not None and len(aapl_10k) <= 700
    assert "Item 7A." not in aapl_10k and "Item 8." not in aapl_10k
    assert "478,834" in aapl_10k

    # 3. TOC-only 10-Q must NEVER ship TOC text; must return None and fail closed to -index.htm
    assert extract_periodic_summary_snippet(real_fx["nvda_10q_toc_only_html"], form="10-Q") is None

    now = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    world.sec.on(
        "GET",
        "/submissions/CIK0001045810.json",
        {
            "cik": "1045810",
            "filings": {
                "recent": {
                    "form": ["10-Q"],
                    "filingDate": ["2026-08-26"],
                    "accessionNumber": ["0001045810-26-000075"],
                    "items": [[]],
                    "acceptanceDateTime": ["2026-08-26T20:15:00Z"],
                    "primaryDocument": ["nvda-20260726.htm"],
                }
            },
        },
    )
    world.sec.on(
        "GET",
        "/Archives/edgar/data/1045810/000104581026000075/nvda-20260726.htm",
        lambda _r: httpx.Response(200, text=real_fx["nvda_10q_toc_only_html"], headers={"content-type": "text/html"}),
    )

    cfg = _cfg(tmp_path)

    async def run_toc_only():
        svc = NewsService(config=cfg, enable_sec_filings=True, now=lambda: now)
        try:
            return await svc.fetch_recent_filings(["NVDA"], now=now)
        finally:
            await svc.aclose()

    batches = asyncio.run(run_toc_only())
    item = batches["NVDA"][0]
    assert _edgar_index_only(item.url)
    assert "Quantitative and Qualitative" not in item.summary


def test_live_news_stamps_spy_and_qqq_when_include_market_true(world, tmp_path):
    now = world.clock.now()
    cfg = _cfg(tmp_path)
    health = ProviderHealthRegistry(cfg, now=world.clock.now)

    async def run_market_stamp():
        news = NewsService(config=cfg, health=health, now=world.clock.now)
        try:
            sources = ResearchSources(
                None,
                news,
                list_universe=lambda: asyncio.sleep(0, ("AAPL",)),
                health=health,
            )
            assert not sources.news_ok("SPY", now)
            assert not sources.news_ok("QQQ", now)

            await sources.live_news((), (), ("AAPL",), now)
            assert sources.news_ok("AAPL", now)
            assert sources.news_ok("SPY", now)
            assert sources.news_ok("QQQ", now)

            # Fail-closed check: if Alpaca fails and Finnhub fallback covers only AAPL, SPY/QQQ lose their stamp
            world.alpaca.fail("/v1beta1/news", "status", status=503, times=1)
            world.finnhub.on("GET", "/api/v1/company-news", [])
            world.clock.advance(minutes=1)
            t2 = world.clock.now()
            await sources.live_news(("AAPL",), (), (), t2)
            assert sources.news_ok("AAPL", t2)
            assert not sources.news_ok("SPY", t2)
            assert not sources.news_ok("QQQ", t2)
        finally:
            await news.aclose()

    asyncio.run(run_market_stamp())


def test_fmp_batch_quote_partial_or_stale_falls_back_to_single_quote_aaa_bbb(world, tmp_path):
    """Risk Review #1: partial/empty/stale HTTP 200 batch-quote must fall back to /stable/quote."""
    from stock_agent.fmp_plan import FmpPlanDetector

    now = world.clock.now()
    fresh_ts = int((now - timedelta(seconds=10)).timestamp())
    stale_ts = int((now - timedelta(hours=3)).timestamp())

    # Alpaca + Finnhub unavailable so FMP fallback is exercised deterministically.
    world.alpaca.fail("/v2/stocks/snapshots", "status", status=503)
    world.finnhub.fail("/api/v1/quote", "status", status=503)

    single_quote_symbols: list[str] = []

    def handle_probe_or_single(req: httpx.Request) -> httpx.Response:
        sym = req.url.params.get("symbol", "")
        single_quote_symbols.append(sym)
        if sym in {"ANET", "AAPL"}:
            return httpx.Response(
                200,
                json=[{"symbol": sym, "price": 150.0, "previousClose": 148.0, "timestamp": fresh_ts}],
            )
        if sym == "BBB":
            return httpx.Response(
                200,
                json=[{"symbol": "BBB", "price": 88.50, "previousClose": 87.00, "timestamp": fresh_ts}],
            )
        if sym == "DDD":
            return httpx.Response(
                200,
                json=[{"symbol": "DDD", "price": 42.25, "previousClose": 41.50, "timestamp": fresh_ts}],
            )
        if sym == "CCC":
            return httpx.Response(402, json={"Error Message": "Restricted symbol"})
        return httpx.Response(200, json=[])

    batch_mode = {"mode": "partial_missing"}

    def handle_batch_quote(req: httpx.Request) -> httpx.Response:
        syms = req.url.params.get("symbols", "")
        if syms == "AAPL,ANET":
            return httpx.Response(
                200,
                json=[
                    {"symbol": "AAPL", "price": 225.0, "previousClose": 224.0, "timestamp": fresh_ts},
                    {"symbol": "ANET", "price": 95.0, "previousClose": 94.0, "timestamp": fresh_ts},
                ],
            )
        if batch_mode["mode"] == "partial_missing":
            # Batch for AAA,BBB returns fresh AAA only; BBB is omitted from HTTP 200 payload.
            return httpx.Response(
                200,
                json=[{"symbol": "AAA", "price": 101.25, "previousClose": 100.0, "timestamp": fresh_ts}],
            )
        # Batch returns fresh AAA + stale BBB + missing DDD (+ plan-refused CCC).
        return httpx.Response(
            200,
            json=[
                {"symbol": "AAA", "price": 101.25, "previousClose": 100.0, "timestamp": fresh_ts},
                {"symbol": "BBB", "price": 77.00, "previousClose": 76.0, "timestamp": stale_ts},
            ],
        )

    world.fmp.on("GET", "/stable/quote", handle_probe_or_single)
    world.fmp.on("GET", "/stable/batch-quote", handle_batch_quote)
    world.fmp.on("GET", "/stable/analyst-estimates", [{"symbol": "ANET", "epsAvg": 0.65}])
    world.fmp.on("GET", "/stable/news/press-releases", [])

    cfg = _cfg(tmp_path)
    health = ProviderHealthRegistry(cfg, now=world.clock.now)

    async def run_case():
        http = DataHttpClient(cfg)
        try:
            plan = FmpPlanDetector(http, config=cfg, health=health, now=world.clock.now)
            caps = await plan.probe(now=now)
            assert caps.batch_quotes_allowed
            single_quote_symbols.clear()

            prices = PriceService(
                http, config=cfg, fmp_plan=plan, now=world.clock.now
            )
            # Case 1: exact AAA,BBB repro where batch returns fresh AAA only.
            res1 = await prices.get_quotes(["AAA", "BBB"], purpose="order_sizing", max_age_s=90)
            assert set(res1.quotes.keys()) == {"AAA", "BBB"}
            assert res1.quotes["AAA"].price == Decimal("101.25")
            assert res1.quotes["BBB"].price == Decimal("88.5")
            assert single_quote_symbols == ["BBB"]

            # Case 2: stale BBB in batch + plan-refused CCC -> falls back for BBB & DDD, skips AAA & refused CCC.
            plan.record_quote_symbol_refused("CCC")
            prices2 = PriceService(
                http, config=cfg, fmp_plan=plan, now=world.clock.now
            )
            single_quote_symbols.clear()
            batch_mode["mode"] = "stale_and_refused"
            res2 = await prices2.get_quotes(
                ["AAA", "BBB", "CCC", "DDD"], purpose="order_sizing", max_age_s=90
            )
            assert set(res2.quotes.keys()) == {"AAA", "BBB", "DDD"}
            assert res2.quotes["BBB"].price == Decimal("88.5")
            assert res2.quotes["DDD"].price == Decimal("42.25")
            assert "AAA" not in single_quote_symbols
            assert "CCC" not in single_quote_symbols
            assert set(single_quote_symbols) == {"BBB", "DDD"}
        finally:
            await http.aclose()

    asyncio.run(run_case())


def test_live_poll_news_never_waits_on_stalled_sec_archive_and_scheduled_respects_deadline(
    world, tmp_path
):
    """Risk Review #2: live poll_news publishes Alpaca + index-only SEC without awaiting archive."""
    import time as _time

    now = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    world.clock.set(now)
    sec_fx = FIXTURES["sec"]

    world.alpaca.add_news(
        ["NVDA"],
        "NVDA cuts Q4 revenue guidance below consensus on supply delay",
        summary="NVIDIA lowered fourth-quarter revenue guidance by 8%.",
        source="reuters",
        created_at=now - timedelta(minutes=2),
    )
    world.finnhub.on("GET", "/api/v1/company-news", [])
    world.sec.on("GET", "/submissions/CIK0001045810.json", sec_fx["nvda_submissions"])

    cfg = _cfg(tmp_path)

    async def run_case():
        svc = NewsService(config=cfg, enable_sec_filings=True, now=lambda: now)
        try:
            hook_calls = 0

            async def stalled_archive_hook(**_kwargs):
                nonlocal hook_calls
                hook_calls += 1
                await asyncio.sleep(60.0)

            svc._sec_archives.fetch_filing_document = stalled_archive_hook  # type: ignore[method-assign]

            # 1. Live poll_news must NEVER call or await archive enrichment.
            t0 = _time.monotonic()
            batch = await svc.poll_news(["NVDA"], lookback_hours=144)
            live_elapsed = _time.monotonic() - t0

            assert live_elapsed < 1.5
            assert hook_calls == 0
            assert batch.ok is True
            headlines = [it.headline for it in batch.material_new_items]
            assert any("cuts Q4 revenue guidance" in h for h in headlines)
            sec_live_items = [it for it in batch.items if it.provider == "sec"]
            assert sec_live_items
            assert all(_edgar_index_only(it.url) for it in sec_live_items)

            # 2. Scheduled packet with stalled archive hook returns index-only within deadline.
            t1 = _time.monotonic()
            recent = await svc.fetch_recent_filings(["NVDA"], now=now, deadline_s=0.5)
            sched_elapsed = _time.monotonic() - t1

            assert sched_elapsed < 1.5
            assert hook_calls >= 1
            assert "NVDA" in recent and len(recent["NVDA"]) >= 1
            assert all(_edgar_index_only(it.url) for it in recent["NVDA"])
        finally:
            await svc.aclose()

    asyncio.run(run_case())


def test_sec_security_streaming_byte_ceiling_lifetime_neg_cache_ix_url_and_identifier_guards(
    tmp_path,
):
    """Security Review #1-#3: 6 MB stream cap + lifetime neg cache, /ix?doc= decode, CIK/accession guards."""
    from stock_agent.sec_filings import SecArchiveFetcher, SecRateLimiter, _safe_archive_doc_url

    # (2) _safe_archive_doc_url rejects ? or # even after double URL-decoding on /ix?doc=
    cik = "1045810"
    acc_nd = "000104581026000099"
    assert (
        _safe_archive_doc_url(
            f"/ix?doc=/Archives/edgar/data/{cik}/{acc_nd}/q3fy27pr.htm",
            cik_int=cik,
            acc_nodash=acc_nd,
        )
        == f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/q3fy27pr.htm"
    )
    for bad_href in (
        f"/ix?doc=/Archives/edgar/data/{cik}/{acc_nd}/q3.htm%3Fadmin=1",
        f"/ix?doc=/Archives/edgar/data/{cik}/{acc_nd}/q3.htm%253Fadmin=1",
        f"/ix?doc=/Archives/edgar/data/{cik}/{acc_nd}/q3.htm%23frag",
        f"/ix?doc=/Archives/edgar/data/{cik}/{acc_nd}/q3.htm%2523frag",
        "q3.htm?raw=1",
        "q3.htm#section",
    ):
        assert _safe_archive_doc_url(bad_href, cik_int=cik, acc_nodash=acc_nd) is None

    chunks_emitted = 0
    total_http_requests = 0

    class OversizeBodyStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal chunks_emitted
            chunk = b"<html><body><p>" + (b"A" * (1024 * 1024))
            for _ in range(50):  # 50 MB total if unbounded
                chunks_emitted += 1
                yield chunk

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal total_http_requests
        total_http_requests += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            stream=OversizeBodyStream(),
        )

    cfg = _cfg(tmp_path)

    async def run_case():
        raw_client = httpx.AsyncClient()
        raw_client._transport = httpx.MockTransport(mock_handler)
        http = DataHttpClient(cfg, client=raw_client)
        try:
            fetcher = SecArchiveFetcher(
                http,
                config=cfg,
                rate_limiter=SecRateLimiter(max_per_second=100.0),
            )
            # (3) Malformed cik_int or accession must be rejected BEFORE any HTTP call.
            for bad_cik, bad_acc in (
                ("0001045810", "0001045810-26-000099"),
                ("1045810/../99", "0001045810-26-000099"),
                ("abc", "0001045810-26-000099"),
                ("1045810", "000104581026000099"),
                ("1045810", "../0001045810-26-000099"),
                ("1045810", "0001045810-26-99"),
            ):
                res = await fetcher.fetch_filing_document(
                    cik_int=bad_cik,
                    acc_nodash=bad_acc.replace("-", ""),
                    accession=bad_acc,
                    form="10-K",
                    primary_document="nvda-20260125.htm",
                )
                assert res is None
            assert total_http_requests == 0

            # (1) Oversize 50 MB 10-K stream aborts at 6 MB ceiling and writes lifetime neg-cache.
            res_over = await fetcher.fetch_filing_document(
                cik_int="1045810",
                acc_nodash="000104581026000100",
                accession="0001045810-26-000100",
                form="10-K",
                primary_document="bank-10k-50mb.htm",
            )
            assert res_over is None
            assert total_http_requests == 1
            assert 6 <= chunks_emitted <= 7  # Stopped right past 6 MB; never read 50 MB!
            assert fetcher._neg_cache["1045810_000104581026000100"] == float("inf")

            # A fresh SecArchiveFetcher instance (>30m / restart) reads persisted oversize marker
            # and never refetches the oversize filing.
            fetcher2 = SecArchiveFetcher(
                http,
                config=cfg,
                rate_limiter=SecRateLimiter(max_per_second=100.0),
            )
            res_cached = await fetcher2.fetch_filing_document(
                cik_int="1045810",
                acc_nodash="000104581026000100",
                accession="0001045810-26-000100",
                form="10-K",
                primary_document="bank-10k-50mb.htm",
            )
            assert res_cached is None
            assert total_http_requests == 1
            assert fetcher2._neg_cache["1045810_000104581026000100"] == float("inf")
        finally:
            await raw_client.aclose()

    asyncio.run(run_case())


def test_fmp_hanging_probe_across_three_scheduler_ticks_attempts_once_and_never_delays_heartbeat(
    tmp_path, monkeypatch
):
    """Runtime Review A: hanging FMP across 3 ticks probes at most once and never delays heartbeat."""
    import time as _time
    from stock_agent import scheduler as sched_mod
    from stock_agent.fmp_plan import FmpPlanDetector

    t_cursor = {"now": datetime(2026, 11, 24, 14, 30, tzinfo=UTC)}
    monkeypatch.setattr(sched_mod, "_now_utc", lambda: t_cursor["now"])

    fmp_http_hits: list[tuple[str, float]] = []
    hang_fmp = {"hang": True}

    async def fmp_transport_handler(request: httpx.Request) -> httpx.Response:
        fmp_http_hits.append((request.url.path, _time.monotonic()))
        if hang_fmp["hang"]:
            await asyncio.sleep(30.0)
        return httpx.Response(
            200,
            json=[{"symbol": "ANET", "price": 95.0, "previousClose": 94.0}],
        )

    heartbeat_times: list[float] = []
    flush_times: list[float] = []

    class DummyHeartbeat:
        async def tick(self, _alerts, *, note: str = "") -> None:
            heartbeat_times.append(_time.monotonic())

    class DummyAlerts:
        async def flush(self, *, max_budget_sec: float = 7.0) -> None:
            flush_times.append(_time.monotonic())

    cfg = _cfg(tmp_path)

    async def run_case():
        raw_client = httpx.AsyncClient()
        raw_client._transport = httpx.MockTransport(fmp_transport_handler)
        http = DataHttpClient(cfg, client=raw_client)
        try:
            detector = FmpPlanDetector(
                http,
                config=cfg,
                now=lambda: t_cursor["now"],
                probe_timeout_s=0.25,
            )
            sched = object.__new__(sched_mod.StockAgentScheduler)
            sched.runtime = None
            sched.alerts = DummyAlerts()
            sched.heartbeat = DummyHeartbeat()
            sched.fmp_plan = detector
            sched.config = cfg
            sched._reconciled = True
            sched.store = SimpleNamespace(kv_get=lambda _k: None, kv_set=lambda _k, _v: None)

            # Tick 1 (t = 0s): heartbeat & alert flush run BEFORE FMP probe; hanging probe times out.
            await sched._notification_tick()
            assert len(heartbeat_times) == 1
            assert len(flush_times) == 1
            assert len(fmp_http_hits) >= 1
            first_probe_mono = fmp_http_hits[0][1]
            assert heartbeat_times[0] <= first_probe_mono
            assert flush_times[0] <= first_probe_mono
            hits_after_tick1 = len(fmp_http_hits)

            # Tick 2 (+30s) and Tick 3 (+60s): zero new FMP probe requests and instant heartbeat!
            for step in (1, 2):
                t_cursor["now"] = t_cursor["now"] + timedelta(seconds=30)
                t_start = _time.monotonic()
                await sched._notification_tick()
                assert (_time.monotonic() - t_start) < 0.1
                assert len(fmp_http_hits) == hits_after_tick1
                assert len(heartbeat_times) == 1 + step

            # After 15-minute incomplete backoff expires and FMP recovers, probe succeeds & sets 24h retry.
            hang_fmp["hang"] = False
            t_cursor["now"] = t_cursor["now"] + timedelta(minutes=16)
            await sched._notification_tick()
            assert detector.capabilities.quote_all_symbols_allowed
            hits_after_recovery = len(fmp_http_hits)

            # +1 hour later (< 24h complete interval): no reprobe.
            t_cursor["now"] = t_cursor["now"] + timedelta(hours=1)
            await sched._notification_tick()
            assert len(fmp_http_hits) == hits_after_recovery
        finally:
            await raw_client.aclose()

    asyncio.run(run_case())


def test_scheduled_sec_archive_hang_for_20_names_trips_breaker_records_health_under_25s(tmp_path):
    """Runtime Review B: 20 names with hanging www.sec.gov trips 2-strike breaker in <25s total."""
    import time as _time
    from stock_agent.health import HealthState

    now = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
    symbols_20 = tuple(f"SYM{i:02d}" for i in range(1, 21))
    cik_map = {sym: str(1_100_000 + idx) for idx, sym in enumerate(symbols_20, start=1)}

    archive_attempts = 0
    submissions_attempts = 0
    hang_archive = {"hang": True}
    sec_fx = FIXTURES["sec"]

    async def sec_and_news_handler(request: httpx.Request) -> httpx.Response:
        nonlocal archive_attempts, submissions_attempts
        host = request.url.host
        path = request.url.path
        if host == "data.alpaca.markets" and path == "/v1beta1/news":
            return httpx.Response(200, json={"news": [], "next_page_token": None})
        if host == "finnhub.io":
            return httpx.Response(200, json=[])
        if host == "data.sec.gov" and path.startswith("/submissions/CIK"):
            submissions_attempts += 1
            cik_digits = str(int(path.removeprefix("/submissions/CIK").removesuffix(".json")))
            acc_num = f"{cik_digits.zfill(10)}-26-000001"
            return httpx.Response(
                200,
                json={
                    "cik": cik_digits,
                    "filings": {
                        "recent": {
                            "form": ["8-K", "10-Q"],
                            "filingDate": ["2026-11-20", "2026-11-01"],
                            "accessionNumber": [acc_num, f"{cik_digits.zfill(10)}-26-000002"],
                            "items": [["2.02", "9.01"], []],
                            "acceptanceDateTime": [
                                "2026-11-20T21:05:00Z",
                                "2026-11-01T20:10:00Z",
                            ],
                            "primaryDocument": ["q3release.htm", "form10q.htm"],
                        }
                    },
                },
            )
        if host == "www.sec.gov" and path.startswith("/Archives/"):
            archive_attempts += 1
            if hang_archive["hang"]:
                # Genuine hang exceeding the 6 s production per-request timeout.
                await asyncio.sleep(60.0)
            if path.endswith("-index.htm"):
                return httpx.Response(200, text="<html><body></body></html>")
            return httpx.Response(200, text=sec_fx["nvda_ex99_1_html"])
        return httpx.Response(404, json={})

    # Use REAL production defaults (6 s per-request timeout, 20 s scheduled deadline) — no overrides!
    cfg = _cfg(tmp_path)
    health = ProviderHealthRegistry(cfg, now=lambda: now)

    class StubPrices:
        async def get_quotes(self, symbols, **_kw):
            return SimpleNamespace(quotes={})

    async def run_case():
        raw_client = httpx.AsyncClient()
        raw_client._transport = httpx.MockTransport(sec_and_news_handler)
        http = DataHttpClient(cfg, client=raw_client)
        try:
            news = NewsService(
                http,
                config=cfg,
                health=health,
                cik_lookup=lambda s: cik_map.get(s),
                enable_sec_filings=True,
                now=lambda: now,
            )
            sources = ResearchSources(
                StubPrices(),
                news,
                list_universe=lambda: asyncio.sleep(0, symbols_20),
                health=health,
                focus_for=lambda **_kw: symbols_20,
                held_symbols=lambda: symbols_20[:4],
            )

            t0 = _time.monotonic()
            sec_inputs = await sources.securities(symbols_20, now, preopen=True, prescan=True)
            await sources.background(symbols_20)
            elapsed = _time.monotonic() - t0

            assert elapsed < 25.0, f"20-symbol hanging archive took {elapsed:.2f}s (expected <25s)"
            assert submissions_attempts >= 20
            assert archive_attempts == 2  # 2-strike breaker halted all remaining archive GETs!

            # Every one of the 20 symbols still received its dated index-only filing evidence.
            for sym in symbols_20:
                pev = sec_inputs[sym].priority_evidence
                assert len(pev) >= 1
                assert all(_edgar_index_only(ev.url) for ev in pev)

            # Provider-health degradation recorded without blocking submissions.
            arch_status = health.status("sec", "edgar_archive")
            edgar_status = health.status("sec", "edgar")
            assert arch_status.state == HealthState.DEGRADED
            assert edgar_status.state == HealthState.DEGRADED
            assert not arch_status.required_for_trading
            assert not health.is_blocked("sec", "edgar", now=now)

            # Later scheduled run after www.sec.gov recovers populates primary archive cache!
            hang_archive["hang"] = False
            later = now + timedelta(minutes=15)
            recovered = await news.fetch_recent_filings(["SYM01"], now=later)
            assert any(not _edgar_index_only(it.url) for it in recovered["SYM01"])
            assert health.status("sec", "edgar_archive").state == HealthState.HEALTHY
        finally:
            await raw_client.aclose()

    asyncio.run(run_case())


def test_fmp_single_quote_fallback_filters_plan_denied_before_consuming_30_cap(world, tmp_path):
    """Follow-up #1: 30 plan-denied names must not consume fallback slots ahead of AAPL (#31)."""
    from stock_agent.fmp_plan import FmpPlanDetector

    now = world.clock.now()
    fresh_ts = int((now - timedelta(seconds=10)).timestamp())

    world.alpaca.fail("/v2/stocks/snapshots", "status", status=503)
    world.finnhub.fail("/api/v1/quote", "status", status=503)

    quote_calls: list[str] = []

    def handle_quote(req: httpx.Request) -> httpx.Response:
        sym = req.url.params.get("symbol", "")
        quote_calls.append(sym)
        if sym == "AAPL":
            return httpx.Response(
                200,
                json=[{"symbol": "AAPL", "price": 225.50, "previousClose": 224.0, "timestamp": fresh_ts}],
            )
        return httpx.Response(402, json={"Error Message": "Restricted symbol on free plan"})

    world.fmp.on("GET", "/stable/quote", handle_quote)
    world.fmp.on("GET", "/stable/batch-quote", lambda _r: httpx.Response(402, json={"Error Message": "Paid only"}))
    world.fmp.on("GET", "/stable/analyst-estimates", lambda _r: httpx.Response(402, json={"Error Message": "Paid only"}))
    world.fmp.on("GET", "/stable/news/press-releases", lambda _r: httpx.Response(402, json={"Error Message": "Paid only"}))

    cfg = _cfg(tmp_path)
    health = ProviderHealthRegistry(cfg, now=world.clock.now)

    async def run_case():
        http = DataHttpClient(cfg)
        try:
            plan = FmpPlanDetector(http, config=cfg, health=health, now=world.clock.now)
            caps = await plan.probe(now=now)
            assert not caps.quote_all_symbols_allowed
            assert caps.sample_quotes_allowed
            quote_calls.clear()

            prices = PriceService(http, config=cfg, fmp_plan=plan, now=world.clock.now)
            denied_30 = [f"DEN{i:02d}" for i in range(1, 31)]
            requested = [*denied_30, "AAPL"]  # AAPL is position 31 after 30 plan-denied tickers.
            res = await prices.get_quotes(requested, purpose="order_sizing", max_age_s=90)

            assert "AAPL" in res.quotes
            assert res.quotes["AAPL"].price == Decimal("225.5")
            assert quote_calls == ["AAPL"]
        finally:
            await http.aclose()

    asyncio.run(run_case())


def test_fmp_incomplete_probe_exponential_backoff_key_reset_and_25_per_day_cap(world, tmp_path):
    """Follow-up #2: incomplete probes back off 15m -> 1h -> 4h -> 24h and cap at 25 GETs/day."""
    from stock_agent.fmp_plan import FmpPlanDetector

    now0 = world.clock.now()
    fresh_ts = int((now0 - timedelta(seconds=5)).timestamp())
    press_nonempty = {"enabled": False}
    probe_gets: list[tuple[datetime, str]] = []

    def handle_quote(req: httpx.Request) -> httpx.Response:
        probe_gets.append((world.clock.now(), req.url.path))
        sym = req.url.params.get("symbol", "ANET")
        return httpx.Response(
            200,
            json=[{"symbol": sym, "price": 95.0, "previousClose": 94.0, "timestamp": fresh_ts}],
        )

    def handle_batch(req: httpx.Request) -> httpx.Response:
        probe_gets.append((world.clock.now(), req.url.path))
        return httpx.Response(
            200,
            json=[
                {"symbol": "AAPL", "price": 225.0, "previousClose": 224.0, "timestamp": fresh_ts},
                {"symbol": "ANET", "price": 95.0, "previousClose": 94.0, "timestamp": fresh_ts},
            ],
        )

    def handle_est(req: httpx.Request) -> httpx.Response:
        probe_gets.append((world.clock.now(), req.url.path))
        return httpx.Response(200, json=[{"symbol": "ANET", "epsAvg": 0.65}])

    def handle_press(req: httpx.Request) -> httpx.Response:
        probe_gets.append((world.clock.now(), req.url.path))
        if press_nonempty["enabled"]:
            return httpx.Response(
                200,
                json=[{"symbol": "ANET", "title": "Launch", "publishedDate": "2026-11-24 08:30:00"}],
            )
        # Empty 200 [] leaves press_releases == "unknown" (incomplete probe).
        return httpx.Response(200, json=[])

    world.fmp.on("GET", "/stable/quote", handle_quote)
    world.fmp.on("GET", "/stable/batch-quote", handle_batch)
    world.fmp.on("GET", "/stable/analyst-estimates", handle_est)
    world.fmp.on("GET", "/stable/news/press-releases", handle_press)

    cfg = _cfg(tmp_path)

    async def run_case():
        http = DataHttpClient(cfg)
        try:
            detector = FmpPlanDetector(http, config=cfg, now=world.clock.now)

            # Probe 1 (t=0): incomplete -> 15 min backoff (4 GETs).
            c1 = await detector.maybe_reprobe(world.clock.now())
            assert c1.press_releases == "unknown"
            assert len(probe_gets) == 4

            # +14m: still backed off; +15m: Probe 2 fires -> 1h backoff (8 GETs).
            world.clock.advance(minutes=14)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 4
            world.clock.advance(minutes=1)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 8

            # +59m: still backed off; +60m: Probe 3 fires -> 4h backoff (12 GETs).
            world.clock.advance(minutes=59)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 8
            world.clock.advance(minutes=1)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 12

            # +3h59m: still backed off; +4h: Probe 4 fires -> 24h backoff (16 GETs).
            world.clock.advance(hours=3, minutes=59)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 12
            world.clock.advance(minutes=1)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 16

            # +18h (< 24h): no reprobe! Over a full day of 200 [] press releases, <= 16 GETs!
            world.clock.advance(hours=18)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 16

            # Key rotation resets incomplete backoff streak to 15m and immediately probes:
            # First advance past the first 24h window so all 25 slots are fresh for cap test.
            world.clock.advance(hours=7)
            probe_gets.clear()

            cfg.FMP_API_KEY = "test-fmp-key-2"
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 4
            # Next incomplete backoff after key change is back to 15m (not 24h).
            world.clock.advance(minutes=14)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 4
            world.clock.advance(minutes=1)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 8

            # Direct probe() calls within the same 24h window strictly cap at 25 GETs/day!
            for _ in range(10):
                await detector.probe(now=world.clock.now())
            assert len(probe_gets) == 25
            # Preserved already-discovered capabilities on same key when cap was reached.
            assert detector.capabilities.batch_quotes_allowed

            # After 24h window rolls over and press-releases returns data, complete probe resets to 24h.
            world.clock.advance(hours=24, minutes=1)
            probe_gets.clear()
            press_nonempty["enabled"] = True
            c_done = await detector.maybe_reprobe(world.clock.now())
            assert c_done.press_releases_allowed
            assert len(probe_gets) == 4
            world.clock.advance(hours=12)
            await detector.maybe_reprobe(world.clock.now())
            assert len(probe_gets) == 4
        finally:
            await http.aclose()

    asyncio.run(run_case())



