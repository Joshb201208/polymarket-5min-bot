"""Tests for http_safety, ProviderHealthRegistry (A12), materiality, and NewsService (A5)."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_agent.health import HealthState, ProviderHealthRegistry
from stock_agent.http_safety import DataHttpClient, RedactingLogFilter, scrub_secrets
from stock_agent.materiality import MATERIALITY_THRESHOLD, score_materiality
from stock_agent.news_service import NewsService
from stock_agent.research_gate import ResearchGate
from stock_agent.state_store import StateStore


def run(coro):
    return asyncio.run(coro)


def make_cfg(**overrides):
    defaults = {
        "ALPACA_API_KEY": "synthetic-alpaca-key",
        "ALPACA_SECRET_KEY": "synthetic-alpaca-secret",
        "FINNHUB_API_KEY": "synthetic-finnhub-key",
        "FMP_API_KEY": "synthetic-fmp-key",
        "OPENAI_API_KEY": "synthetic-openai-key",
        "PERPLEXITY_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "SEC_USER_AGENT": "TestAgent/1.0 (test@example.invalid)",
        "RESEARCH_THESIS_MAX_AGE_H": 96,
    }
    return SimpleNamespace(**(defaults | overrides))


def ready_registry(world, **cfg_overrides):
    cfg = make_cfg(**cfg_overrides)
    health = ProviderHealthRegistry(cfg, now=world.clock.now)
    now = world.clock.now()
    health.record_broker_ok()
    health.record_llm_ok("openai")
    health.record_quote("AAA", now, source="alpaca")
    health.record_news_poll(["AAA"], now, source="alpaca")
    return health


# --------------------------------------------------------- HTTP safety & redaction


def test_redaction_covers_query_params_headers_and_bot_authorization():
    assert "synthetic-discord-secret" not in scrub_secrets(
        "Authorization: Bot synthetic-discord-secret"
    )
    assert "synthetic-bearer-secret" not in scrub_secrets(
        "Authorization: Bearer synthetic-bearer-secret"
    )
    assert "synthetic-alpaca-id" not in scrub_secrets(
        "APCA-API-KEY-ID: synthetic-alpaca-id"
    )
    assert "synthetic-finnhub" not in scrub_secrets(
        "GET https://finnhub.io/api/v1/quote?symbol=NVDA&token=synthetic-finnhub"
    )
    assert "synthetic-fmp" not in scrub_secrets(
        "GET https://financialmodelingprep.com/stable/quote?symbol=NVDA&apikey=synthetic-fmp"
    )


def test_redaction_covers_exception_tracebacks_and_url_args():
    try:
        raise RuntimeError("https://fixture.invalid/?apikey=synthetic-traceback-secret")
    except RuntimeError:
        record = logging.LogRecord(
            "fixture", logging.ERROR, __file__, 0, "failure %s",
            (httpx.URL("https://finnhub.io/api/v1/quote?token=synthetic-url-arg"),),
            sys.exc_info(),
        )
    assert RedactingLogFilter().filter(record)
    formatted = logging.Formatter().format(record)
    assert "synthetic-traceback-secret" not in formatted
    assert "synthetic-url-arg" not in formatted


def test_data_http_client_binds_headers_only_to_verified_https_hosts():
    cfg = make_cfg()
    http = DataHttpClient(cfg)
    assert "APCA-API-KEY-ID" in http.default_headers_for_url(
        "https://data.alpaca.markets/v1beta1/news"
    )
    assert http.default_headers_for_url(
        "https://evil.example.invalid/redirect?target=data.alpaca.markets"
    ) == {}
    assert http.default_headers_for_url(
        "http://data.alpaca.markets/v1beta1/news"
    ) == {}
    assert http.default_headers_for_url(
        "https://notalpaca.markets/v1beta1/news"
    ) == {}


# --------------------------------------------------------- ProviderHealthRegistry (A12)


def test_unprobed_broker_blocks_entries_until_positive_probe(world):
    cfg = make_cfg()
    health = ProviderHealthRegistry(cfg, now=world.clock.now)
    now = world.clock.now()
    health.record_llm_ok("openai")
    health.record_quote("AAA", now, source="alpaca")
    health.record_news_poll(["AAA"], now, source="alpaca")
    assert "broker_unhealthy" in health.research_entry_blockers("AAA")
    health.record_broker_ok()
    assert health.research_entry_blockers("AAA") == ()


def test_no_entry_after_first_required_news_poll_failure(world):
    health = ready_registry(world)
    assert health.research_entry_blockers("AAA") == ()
    health.record_news_poll(["AAA"], world.clock.now(), source="none", ok=False)
    assert "news_missing_or_stale" in health.research_entry_blockers("AAA")


def test_unrelated_news_poll_does_not_prove_symbol_is_fresh(world):
    health = ready_registry(world)
    health.record_quote("BBB", world.clock.now(), source="alpaca")
    health.record_news_poll(["AAA"], world.clock.now(), source="alpaca", include_market=True)
    assert "news_missing_or_stale" in health.research_entry_blockers("BBB")


def test_transient_failure_of_only_llm_route_blocks_entries(world):
    health = ready_registry(world)
    health.record_llm_failure("openai", status_code=503)
    assert "llm_unhealthy" in health.research_entry_blockers("AAA")


def test_symbol_plan_block_does_not_disable_healthy_unrelated_symbols(world):
    health = ready_registry(world)
    health.record_failure("finnhub", "company_news", status_code=403, symbol="AAA")
    assert health.is_blocked("finnhub", "company_news", "AAA")
    assert not health.is_blocked("finnhub", "company_news", "BBB")


def test_endpoint_plan_block_persists_across_restart_and_expires_after_ttl(world, tmp_path):
    store = StateStore(tmp_path / "state.db")
    cfg = make_cfg()
    # Construct BEFORE store.open() to verify deferred/lazy hydration safety
    health = ProviderHealthRegistry(cfg, store=store, defer_load=True, now=world.clock.now)
    store.open()
    assert health.load_state()
    health.record_failure("fmp", "screener", status_code=402)
    assert health.is_blocked("fmp", "screener")
    store.close()

    store2 = StateStore(tmp_path / "state.db")
    restarted = ProviderHealthRegistry(cfg, store=store2, now=world.clock.now)
    store2.open()
    assert restarted.is_blocked("fmp", "screener")

    world.clock.advance(days=1, seconds=1)
    assert not restarted.is_blocked("fmp", "screener")
    store2.close()


def test_llm_plan_block_expires_before_next_probe(world):
    health = ready_registry(world)
    health.record_llm_failure("openai", status_code=403)
    assert health.is_blocked("llm_openai", "route")
    assert "llm_unhealthy" in health.research_entry_blockers("AAA")
    world.clock.advance(days=1, seconds=1)
    health.record_broker_ok()
    health.record_quote("AAA", world.clock.now(), source="alpaca")
    health.record_news_poll(["AAA"], world.clock.now(), source="alpaca")
    assert not health.is_blocked("llm_openai", "route")
    assert "llm_unhealthy" not in health.research_entry_blockers("AAA")


def test_healthy_alpaca_finnhub_openai_survive_optional_failures_and_gate_passes(world):
    health = ready_registry(world)
    health.record_failure("fmp", "fundamentals", status_code=402)
    health.record_failure("tipranks", "screener", status_code=401)
    health.record_failure("sec", "edgar", status_code=503)
    health.record_failure("alpaca", "bars", status_code=503)
    assert health.research_entry_blockers("AAA") == ()

    now = world.clock.now()
    payload = {
        "draft": {"symbol": "AAA", "direction": "BUY"},
        "generated_at": now,
        "inputs_as_of": now,
        "raw_conviction": 8,
        "effective_conviction": 8,
    }
    ledger = SimpleNamespace(
        current_thesis=lambda _: SimpleNamespace(thesis_id="th-1", payload=payload)
    )
    gate = ResearchGate(ledger, health, make_cfg())
    verdict = gate.is_entry_eligible("AAA", now)
    assert verdict.ok


# --------------------------------------------------------- Materiality & NewsService (A5)


def test_materiality_distinguishes_nvda_ai_and_earnings_from_recap_noise():
    ai = score_materiality(
        "NVIDIA announces next-gen Rubin GPU supply agreement with hyperscaler datacenter capex boost",
        symbols=("NVDA",),
        focus_set={"NVDA"},
    )
    assert ai.is_material
    assert ai.score >= 7

    earnings = score_materiality(
        "Arista Networks beats estimates and raises guidance for FY26 revenue",
        symbols=("ANET",),
    )
    assert earnings.is_material
    assert earnings.score >= MATERIALITY_THRESHOLD

    noise = score_materiality(
        "Why NVDA Stock Moved Today: Top 5 Stocks To Watch Today",
        symbols=("NVDA",),
    )
    assert not noise.is_material
    assert noise.score < MATERIALITY_THRESHOLD


def test_news_service_timed_arrival_cross_provider_dedupe_and_cursor_persistence(world, tmp_path):
    cfg = make_cfg()
    store = StateStore(tmp_path / "news.db")
    # Construct before store.open() to prove constructor never touches unopened SQLite
    health = ProviderHealthRegistry(cfg, store=store, defer_load=True, now=world.clock.now)
    svc = NewsService(
        config=cfg,
        store=store,
        defer_load=True,
        health=health,
        enable_sec_filings=True,
        now=world.clock.now,
    )
    store.open()
    health.record_broker_ok()
    health.record_llm_ok("openai")
    health.record_quote("NVDA", world.clock.now(), source="alpaca")

    t0 = world.clock.now()
    headline = "NVIDIA raises Q4 revenue guidance on surging Blackwell GPU demand"
    # world.news() publishes the same story on both Alpaca and Finnhub; future story scheduled +10m
    world.news(["NVDA"], headline, summary="Strong data center demand.", at=t0)
    world.news(
        ["NVDA"],
        "NVIDIA signs sovereign AI cluster contract win",
        summary="Multi-year deal.",
        at=t0 + timedelta(minutes=10),
    )
    # SEC 8-K Item 2.02 filing
    world.sec.on(
        "GET",
        "/submissions/CIK0001045810.json",
        {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "filingDate": [t0.strftime("%Y-%m-%d")],
                    "accessionNumber": ["0001045810-26-000099"],
                    "items": [["2.02"]],
                    "acceptanceDateTime": [t0.isoformat()],
                }
            }
        },
    )

    batch1 = run(svc.poll_news(["NVDA"], focus=["NVDA"], include_market=True))
    assert batch1.ok and not batch1.degraded
    # Future story at t0+10m is invisible at t0; duplicate Alpaca+Finnhub story collapsed to 1; +1 SEC 8-K
    assert len(batch1.items) == 2
    assert len(batch1.new_items) == 2
    assert len(batch1.material_new_items) == 2
    assert health.research_entry_blockers("NVDA") == ()
    for req in world.net.requests():
        assert "apikey=" not in str(req.url).lower()
        assert "token=" not in str(req.url).lower()

    # Mark evaluated so A5 never sends the same story twice even if re-polled
    svc.mark_evaluated(i.id for i in batch1.material_new_items)
    store.close()

    # Restart process on same DB, advance clock +11m so future story becomes visible
    world.clock.advance(minutes=11)
    store2 = StateStore(tmp_path / "news.db")
    store2.open()
    svc2 = NewsService(config=cfg, store=store2, now=world.clock.now)
    batch2 = run(svc2.poll_news(["NVDA"], focus=["NVDA"], include_market=True))
    assert batch2.ok
    # Only the newly arrived t0+10m story is new & material!
    assert [i.headline for i in batch2.new_items] == [
        "NVIDIA signs sovereign AI cluster contract win"
    ]
    assert len(batch2.material_new_items) == 1
    store2.close()


def test_failed_news_providers_never_masquerade_as_empty_news(world):
    cfg = make_cfg()
    health = ready_registry(world)
    svc = NewsService(config=cfg, health=health, now=world.clock.now)

    world.alpaca.fail("/v1beta1/news", "status", status=503, times=5)
    world.finnhub.fail("/api/v1/company-news", "status", status=503, times=5)

    batch = run(svc.poll_news(["AAA"], include_market=False))
    assert batch.ok is False
    assert batch.errors
    assert health.status("news", "poll").state is HealthState.UNHEALTHY
    assert "news_missing_or_stale" in health.research_entry_blockers("AAA")


@pytest.mark.parametrize("primary", ["missing_secret", "plan_blocked", "malformed_response"])
def test_finnhub_fallback_does_not_certify_the_unpolled_universe(world, primary):
    cfg = make_cfg(**({"ALPACA_SECRET_KEY": ""} if primary == "missing_secret" else {}))
    health = ProviderHealthRegistry(cfg, now=world.clock.now)
    health.record_news_poll(["CCC"], world.clock.now(), source="alpaca")  # Prior proof must be cleared.
    if primary == "plan_blocked":
        health.record_failure("alpaca", "news", status_code=403)
    if primary == "malformed_response":
        world.alpaca.fail("/v1beta1/news", "status", status=200, body={"unexpected": []}, times=None)
    svc = NewsService(config=cfg, health=health, now=world.clock.now)

    async def poll():
        try:
            return await svc.poll_news(["AAA"], universe=["CCC"], include_market=True)
        finally:
            await svc.aclose()

    batch = run(poll())
    assert batch.ok and batch.degraded and "alpaca" in batch.errors
    assert batch.covered_symbols == ("AAA",)
    assert "news_missing_or_stale" not in health.research_entry_blockers("AAA")
    assert "news_missing_or_stale" in health.research_entry_blockers("CCC")
    assert "news_missing_or_stale" in health.research_entry_blockers("SPY")
    requests = world.net.requests(service="alpaca", path="/v1beta1/news")
    assert bool(requests) == (primary == "malformed_response")


def test_partial_finnhub_fallback_preserves_only_valid_symbol_coverage(world):
    cfg = make_cfg(ALPACA_API_KEY="")
    health = ProviderHealthRegistry(cfg, now=world.clock.now)
    health.record_news_poll(["AAA", "BBB", "CCC", "OTHER"], world.clock.now(), source="alpaca")

    def company_news(request):
        symbol = request.url.params["symbol"]
        if symbol == "BBB":
            return httpx.Response(403, json={"error": "symbol not covered on this plan"})
        if symbol == "CCC":
            return httpx.Response(200, content=b"invalid JSON")
        return []

    world.finnhub.on("GET", "/api/v1/company-news", company_news)
    svc = NewsService(config=cfg, health=health, now=world.clock.now)

    async def poll():
        try:
            return await svc.poll_news(["AAA", "BBB", "CCC", "DDD"], include_market=False)
        finally:
            await svc.aclose()

    batch = run(poll())
    assert batch.ok and batch.degraded and "finnhub" in batch.errors
    assert batch.covered_symbols == ("AAA", "DDD")
    for symbol in ("BBB", "CCC"):
        assert "news_missing_or_stale" in health.research_entry_blockers(symbol)
    for symbol in ("AAA", "DDD", "OTHER"):
        assert "news_missing_or_stale" not in health.research_entry_blockers(symbol)
