"""FundamentalsService against the HTTP fakes (no network)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
import pytest

from stock_agent.config import Config
from stock_agent.fundamentals_service import (
    UNIVERSE_CATALOGUE, UNIVERSE_CATALOGUE_VERSION, FundamentalsService, release_time, sec_ttm,
)
from stock_agent.health import ProviderHealthRegistry
from stock_agent.http_safety import DataHttpClient
from stock_agent.research_pipeline import SourceUnavailable
from stock_agent.risk_manager import CanonicalSector
from tests.fakes.base import json_response

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)   # 10:00 ET Monday


@dataclass(frozen=True)
class Meta:
    symbol: str
    tradable: bool = True
    renamed_to: str | None = None
    company_name: str | None = None
    cik: str | None = None
    canonical_sector: CanonicalSector = CanonicalSector.UNKNOWN


class Assets:
    """Same interface as AssetRegistry for the calls the service makes."""

    def __init__(self, metas=(), aliases=None):
        self.metas = {m.symbol: m for m in metas}
        self.aliases = aliases or {}

    def active_symbols(self):
        return tuple(sorted(s for s, m in self.metas.items() if m.tradable))

    def resolve_symbol(self, symbol):
        symbol = self.aliases.get(symbol, symbol)
        return symbol if symbol in self.metas else None

    def get_asset_meta(self, symbol):
        return self.metas.get(symbol, Meta(symbol, tradable=False))


def screener(world, actives=(), gainers=(), losers=()):
    """FakeAlpaca has no screener routes yet; answer them here."""
    inner = world.alpaca.handle

    def handle(request: httpx.Request):
        if request.url.path == "/v1beta1/screener/stocks/most-actives":
            return json_response({"most_actives": [{"symbol": s, "volume": 1, "trade_count": 1} for s in actives],
                                  "last_updated": "2026-11-23T15:00:00Z"})
        if request.url.path == "/v1beta1/screener/stocks/movers":
            return json_response({"gainers": [{"symbol": s, "price": p, "change": 1, "percent_change": 5}
                                              for s, p in gainers],
                                  "losers": [{"symbol": s, "price": p, "change": -1, "percent_change": -5}
                                             for s, p in losers],
                                  "market_type": "stocks", "last_updated": "2026-11-23T15:00:00Z"})
        return inner(request)

    world.alpaca.handle = handle


def run_svc(fn, **kw):
    async def go():
        http = DataHttpClient(Config)
        svc = FundamentalsService(http, now=lambda: NOW, focus_symbols=kw.pop("focus", ("ZZZZ",)), **kw)
        try:
            return await fn(svc)
        finally:
            close = getattr(http, "aclose", None)
            if close:
                await close()
    return asyncio.run(go())


# ------------------------------------------------------------------ universe
def test_catalogue_is_versioned_and_covers_all_sectors():
    assert UNIVERSE_CATALOGUE_VERSION
    assert set(UNIVERSE_CATALOGUE) == set(CanonicalSector) - {CanonicalSector.UNKNOWN}
    names = [s for v in UNIVERSE_CATALOGUE.values() for s in v]
    assert len(names) == len(set(names)) >= 100


def test_universe_focus_first_catalogue_validated_and_screeners(world):
    screener(world, actives=("MEME", "NOTRADE", "WT.WS"), gainers=(("PENNY", 0.4), ("RUN", 30.0)))
    metas = [Meta("AAPL"), Meta("MSFT"), Meta("XOM", tradable=False), Meta("MEME"), Meta("RUN"), Meta("PENNY"),
             Meta("BNY")]
    assets = Assets(metas, aliases={"BK": "BNY"})
    uni = run_svc(lambda s: s.list_universe(), assets=assets, focus=("ZZZZ", "aapl"))
    assert uni[:2] == ("ZZZZ", "AAPL")          # owner focus always included, even if unknown
    assert "MSFT" in uni and "XOM" not in uni     # not tradable
    assert "MEME" in uni and "RUN" in uni and "PENNY" not in uni and "NOTRADE" not in uni
    assert len(uni) == len(set(uni))
    assert world.net.requests(service="alpaca", path="/v1beta1/screener/*")


def test_universe_without_registry_or_screener_is_catalogue_not_four_symbols(world):
    world.alpaca.fail("/v1beta1/screener/*", "5xx", times=None)
    health = ProviderHealthRegistry(Config, now=lambda: NOW)
    uni = run_svc(lambda s: s.list_universe(), assets=Assets(), health=health)
    assert len(uni) > 100 and "JPM" in uni and "NEE" in uni


def test_universe_size_caps_non_focus_only(world):
    screener(world)
    assets = Assets([Meta(s) for v in UNIVERSE_CATALOGUE.values() for s in v])
    uni = run_svc(lambda s: s.list_universe(), assets=assets, universe_size=5, focus=("F1", "F2"))
    assert uni[:2] == ("F1", "F2") and len(uni) == 7


# ------------------------------------------------------------------ SEC TTM
def _f(start, end, val, filed="2026-11-01"):
    return {"start": start, "end": end, "val": val, "filed": filed}


def test_sec_ttm_four_quarters():
    units = {"USD": [_f("2025-10-01", "2025-12-31", 10), _f("2026-01-01", "2026-03-31", 11),
                     _f("2026-04-01", "2026-06-30", 12), _f("2026-07-01", "2026-09-30", 13),
                     _f("2025-07-01", "2025-09-30", 99)]}
    assert sec_ttm(units, date(2026, 11, 23)) == (46, "TTM", date(2026, 9, 30))


def test_sec_ttm_ytd_method_for_cash_flow():
    units = {"USD": [_f("2025-01-01", "2025-12-31", 400), _f("2026-01-01", "2026-09-30", 330),
                     _f("2025-01-01", "2025-09-30", 300)]}
    assert sec_ttm(units, date(2026, 11, 23)) == (430, "TTM", date(2026, 9, 30))


def test_sec_ttm_latest_amendment_wins_and_stale_ignored():
    units = {"USD": [_f("2025-01-01", "2025-12-31", 400, "2026-02-01"),
                     _f("2025-01-01", "2025-12-31", 410, "2026-05-01")]}
    assert sec_ttm(units, date(2026, 11, 23)) == (410, "FY", date(2025, 12, 31))
    assert sec_ttm(units, date(2027, 6, 1)) is None
    assert sec_ttm({"USD-per-shares": [_f("2025-01-01", "2025-12-31", 5)]}, date(2026, 11, 23)) is None


# ------------------------------------------------------------------ company
def _seed_company(world, symbol="AAA", cik="0000000123"):
    world.finnhub.on("GET", "/api/v1/stock/metric", {"metric": {
        "peTTM": 20.0, "grossMarginTTM": 46.0, "revenueGrowthTTMYoy": 8.0, "marketCapitalization": 50_000,
        "beta": 1.1, "roeTTM": None}})
    world.finnhub.on("GET", "/api/v1/stock/profile2", {"name": "AAA Corp", "finnhubIndustry": "Banking"})
    world.finnhub.on("GET", "/api/v1/stock/insider-transactions", {"symbol": symbol, "data": [
        {"transactionCode": "P", "change": 100, "transactionPrice": 50.0, "transactionDate": "2026-11-01"},
        {"transactionCode": "S", "change": -10, "transactionPrice": 60.0, "transactionDate": "2026-10-01"},
        {"transactionCode": "M", "change": 5000, "transactionPrice": 1.0, "transactionDate": "2026-10-01"},
        {"transactionCode": "A", "change": 900, "transactionPrice": 0.0, "transactionDate": "2026-10-01"},
        {"transactionCode": "P", "change": 999, "transactionPrice": 50.0, "transactionDate": "2026-01-01"},
    ]})
    world.finnhub.on("GET", "/api/v1/stock/recommendation", [
        {"period": "2026-10-01", "strongBuy": 2, "buy": 4, "hold": 4, "sell": 0, "strongSell": 0},
        {"period": "2026-11-01", "strongBuy": 3, "buy": 5, "hold": 2, "sell": 0, "strongSell": 0},
    ])
    rev = {"units": {"USD": [_f("2025-10-01", "2025-12-31", 1e9), _f("2026-01-01", "2026-03-31", 1e9),
                             _f("2026-04-01", "2026-06-30", 1e9), _f("2026-07-01", "2026-09-30", 2e9)],
                     "USD-per-shares": [_f("2026-07-01", "2026-09-30", 1.5)]}}
    world.sec.on("GET", f"/api/xbrl/companyconcept/CIK{cik}/us-gaap/Revenues.json", rev)
    world.sec.on("GET", "/api/xbrl/companyconcept/*/NetIncomeLoss.json",
                 lambda r: json_response({"message": "not found"}, 404))
    return Assets([Meta(symbol, cik=cik, company_name="AAA Corporation")])


def test_company_snapshot_provenance_units_and_signals(world):
    assets = _seed_company(world)
    health = ProviderHealthRegistry(Config, now=lambda: NOW)
    snap = run_svc(lambda s: s.fetch_company("AAA"), assets=assets, health=health)
    m = snap.metrics
    assert snap.company_name == "AAA Corporation" and snap.canonical_sector == "Financials"
    assert snap.tradable is True
    assert m["pe_ttm"].value == 20.0 and m["pe_ttm"].source == "finnhub"
    assert m["gross_margin_ttm"].value == pytest.approx(0.46) and m["gross_margin_ttm"].unit == "ratio"
    assert m["market_cap_usd"].value == 50e9
    assert "roe_ttm" not in m                                  # null dropped
    assert m["revenue_ttm_usd"].value == 5e9 and m["revenue_ttm_usd"].source == "sec"   # dollars, not per share
    assert "net_income_ttm_usd" not in m
    assert m["insider_net_usd_90d"].value == 100 * 50 - 10 * 60  # P/S only, inside 90 days
    assert m["analyst_buy_ratio"].value == pytest.approx(0.8)
    assert m["analyst_trend_delta"].value == pytest.approx(0.2)
    assert {e.publisher for e in snap.evidence} == {"Finnhub", "SEC EDGAR"}
    assert all("token" not in e.url for e in snap.evidence)


def test_company_cached_within_ttl(world):
    assets = _seed_company(world)

    async def twice(s):
        await s.fetch_company("AAA")
        return await s.fetch_company("AAA")
    run_svc(twice, assets=assets)
    assert len(world.net.requests(service="finnhub", path="/api/v1/stock/metric")) == 1


def test_company_unavailable_when_no_core_source(world):
    world.finnhub.fail("/api/v1/stock/metric", "5xx", times=None)
    with pytest.raises(SourceUnavailable):
        run_svc(lambda s: s.fetch_company("AAA"), assets=Assets([Meta("AAA")]))


def test_fmp_profile_skipped_while_plan_blocked(world):
    assets = _seed_company(world)
    world.fmp.fail("/stable/profile", "status", status=402, body={"Error Message": "plan"}, times=None)
    health = ProviderHealthRegistry(Config, now=lambda: NOW)

    async def two(s):
        await s.fetch_company("AAA")
        s._companies.clear()
        return await s.fetch_company("AAA")
    run_svc(two, assets=assets, health=health)
    assert health.is_blocked("fmp", "fundamentals", now=NOW)
    assert len(world.net.requests(service="fmp", path="/stable/profile")) == 1


# ------------------------------------------------------------------ earnings
def test_release_times_are_eastern():
    assert release_time(date(2026, 11, 23), "bmo") == datetime(2026, 11, 23, 13, 0, tzinfo=UTC)
    assert release_time(date(2026, 7, 1), "amc") == datetime(2026, 7, 1, 20, 5, tzinfo=UTC)   # EDT
    assert release_time(date(2026, 11, 23), None) == release_time(date(2026, 11, 23), "amc")


def test_releases_published_only_with_actuals(world):
    world.finnhub.on("GET", "/api/v1/calendar/earnings", {"earningsCalendar": [
        {"symbol": "AAA", "date": "2026-11-23", "hour": "bmo", "epsActual": 1.2, "epsEstimate": 1.0,
         "quarter": 3, "year": 2026},
        {"symbol": "AAA", "date": "2026-11-23", "hour": "bmo", "epsActual": 1.2, "epsEstimate": 1.0,
         "quarter": 3, "year": 2026},                                   # duplicate row
        {"symbol": "BBB", "date": "2026-11-23", "hour": "amc", "epsActual": 0.5, "epsEstimate": 0.4,
         "quarter": 3, "year": 2026},                                   # later today: not yet
        {"symbol": "CCC", "date": "2026-11-20", "hour": "amc", "epsActual": None, "epsEstimate": 0.4,
         "quarter": 3, "year": 2026},                                   # date passed, no actual
        {"symbol": "DDD", "date": "2026-11-20", "hour": "amc", "epsActual": 0.03, "epsEstimate": 0.01,
         "quarter": 1, "year": 2027},                                   # tiny estimate
        {"symbol": "EEE", "date": "2026-11-20", "hour": "amc", "epsActual": 1, "epsEstimate": 1,
         "quarter": 1, "year": 2027},                                   # not requested
    ]})
    rows = run_svc(lambda s: s.fetch_releases(("AAA", "BBB", "CCC", "DDD"), NOW))
    by = {r.symbol: r for r in rows}
    assert set(by) == {"AAA", "DDD"}
    assert by["AAA"].story_id == "earnings:AAA:2026-Q3" and by["AAA"].is_published
    assert by["AAA"].published_at == datetime(2026, 11, 23, 13, 0, tzinfo=UTC)
    assert by["DDD"].eps_estimate is None and by["DDD"].eps_actual == 0.03
    req = world.net.requests(service="finnhub", path="/api/v1/calendar/earnings")[0]
    assert req.params["from"] == "2026-11-20" and req.params["to"] == "2026-11-23"


def test_releases_failure_is_not_empty(world):
    world.finnhub.fail("/api/v1/calendar/earnings", "429", times=None)
    with pytest.raises(SourceUnavailable):
        run_svc(lambda s: s.fetch_releases(("AAA",), NOW))


def test_cached_company_makes_no_requests(world):
    assets = _seed_company(world)

    async def go(s):
        assert s.cached_company("AAA") is None
        await s.fetch_company("AAA")
        before = len(world.net.recorded)
        snap = s.cached_company("aaa")
        assert snap is not None and len(world.net.recorded) == before
        return snap
    run_svc(go, assets=assets)
