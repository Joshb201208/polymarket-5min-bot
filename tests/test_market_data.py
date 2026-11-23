import asyncio
from datetime import date, timedelta
from decimal import Decimal
from functools import wraps

import httpx

from stock_agent.asset_registry import AssetRegistry
from stock_agent.config import Config
from stock_agent.price_service import PriceService
from stock_agent.risk_manager import CanonicalSector, atr14
from stock_agent.state_store import StateStore


def run_async(test):
    @wraps(test)
    def wrapper(*args, **kwargs):
        asyncio.run(test(*args, **kwargs))
    return wrapper


@run_async
async def test_primary_quote_keeps_timestamp_and_fallback_rejects_bad_prices(world):
    world.clock.advance_to_open()
    world.price("ALFA", 101.25)
    world.price("BETA", 250, bid=251, ask=249)
    world.finnhub.on("GET", "/api/v1/quote", lambda request: {
        "c": 249.5 if request.url.params["symbol"] == "BETA" else 0,
        "pc": 240, "t": int(world.clock.now().timestamp()),
    })
    world.fmp.set_price("NONE", 10)  # Fake FMP's missing timestamp must not be invented.
    reports = []
    prices = PriceService(now=world.clock.now, report=lambda *row: reports.append(row))
    result = await prices.get_quotes(["ALFA", "BETA", "NONE"], purpose="order_sizing")
    alfa, beta = result.quotes["ALFA"], result.quotes["BETA"]
    assert alfa.price == Decimal("101.25") and alfa.source == "alpaca_iex"
    assert alfa.as_of == world.clock.now() and alfa.is_market_hours
    assert beta.price == Decimal("249.5") and beta.source == "finnhub_quote"
    assert beta.bid is None and beta.prev_close == Decimal(240)
    assert "fmp_quote_missing_timestamp" in result.missing["NONE"]
    assert reports[-1] == ("price_service", "live_quotes", False, "unresolved_quotes")
    assert prices.last_quote("alfa") == alfa

    world.clock.advance(seconds=91)
    stale = await prices.get_quotes(["ALFA"], purpose="order_sizing")
    assert not stale.quotes and "alpaca_iex_stale_or_future" in stale.missing["ALFA"]
    assert prices.last_quote("ALFA") is None
    world.assert_no_orders()


@run_async
async def test_fmp_is_last_and_uses_provider_time(world):
    world.fmp.on("GET", "/stable/quote", [{
        "symbol": "ALFA", "price": 51, "previousClose": 50, "timestamp": int(world.clock.now().timestamp()),
    }])
    batch = await PriceService(now=world.clock.now).get_quotes(["ALFA"], purpose="stop_check")
    assert batch.quotes["ALFA"].source == "fmp_quote"
    assert batch.quotes["ALFA"].change_pct == Decimal(2)
    finnhub = world.net.requests(service="finnhub", path="/api/v1/quote")
    assert len(finnhub) == 1


@run_async
async def test_completed_split_sip_sessions_cache_and_explicit_ranges(world, tmp_path):
    world.clock.advance_to_open()
    world.alpaca.seed_daily_bars("ALFA", list(range(80, 100)))
    world.price("ALFA", 100)
    store = StateStore(tmp_path / "cache.db", clock=world.clock.now)
    store.open()
    prices = PriceService(store=store, now=world.clock.now)
    bars = (await prices.get_daily_bars(["ALFA"], sessions=20))["ALFA"]
    assert len(bars) == 20
    assert bars[-1].session_date == date(2026, 11, 20)
    assert bars[-1].close == Decimal(99) and atr14(bars) is not None
    original = world.net.requests(service="alpaca", path="/v2/stocks/bars")
    assert len(original) == 1
    assert original[0].params["adjustment"] == "split" and original[0].params["feed"] == "sip"

    restored = PriceService(store=store, now=world.clock.now)
    subset = await restored.get_daily_bars(["ALFA"], start_date=date(2026, 11, 19),
                                           end_date=date(2026, 11, 20))
    assert subset["ALFA"] == bars[-2:]
    assert len(world.net.requests(service="alpaca", path="/v2/stocks/bars")) == 1

    world.clock.advance(hours=7)
    after_close = await prices.get_daily_bars(["ALFA"], sessions=20)
    assert after_close["ALFA"][-1].session_date == date(2026, 11, 23)
    request = world.net.requests(service="alpaca", path="/v2/stocks/bars")[-1]
    assert request.params["start"].startswith("2026-11-20")
    assert request.params["end"] == (world.clock.now() - timedelta(minutes=16)).isoformat()
    store.close()


@run_async
async def test_daily_history_never_bridges_gap_or_mixes_split_basis(world, tmp_path):
    world.alpaca.seed_daily_bars("ALFA", [100] * 20)
    store = StateStore(tmp_path / "bars.db")
    store.open()
    prices = PriceService(store=store, now=world.clock.now)
    initial = (await prices.get_daily_bars(["ALFA"], sessions=20))["ALFA"]
    assert len(initial) == 20 and initial[-1].close == 100

    history = world.alpaca.trades_history["ALFA"]
    world.alpaca.trades_history["ALFA"] = [(t, p / 2, v * 2) for t, p, v in history]
    world.clock.advance(days=1)
    world.alpaca.seed_daily_bars("ALFA", [50])
    adjusted = (await prices.get_daily_bars(["ALFA"], sessions=20))["ALFA"]
    assert len(adjusted) == 20 and {bar.close for bar in adjusted} == {Decimal(50)}
    assert len(world.net.requests(service="alpaca", path="/v2/stocks/bars")) == 3

    gap = adjusted[-3].session_date
    world.alpaca.trades_history["ALFA"] = [
        (t, p, v) for t, p, v in world.alpaca.trades_history["ALFA"] if t.date() != gap
    ]
    uncached = PriceService(now=world.clock.now)
    assert (await uncached.get_daily_bars(["ALFA"], sessions=20))["ALFA"] == []
    store.close()


@run_async
async def test_assets_deduplicate_verified_rename_and_stop_polling_dead_symbol(world, tmp_path, monkeypatch):
    world.alpaca.add_asset("OLD", status="inactive")
    world.alpaca.add_asset("NEW", status="active")
    world.price("NEW", 80)
    world.alpaca.add_asset("BRK.B")
    today = world.clock.now().date()
    events = []
    corporate_requests = []
    monkeypatch.setattr(Config, "SEC_USER_AGENT", "Test Data contact@example.test", raising=False)
    world.sec.on("GET", "/files/company_tickers_exchange.json", {
        "fields": ["cik", "name", "ticker", "exchange"], "data": [[42, "Berkshire", "BRK-B", "NYSE"]],
    })
    async with httpx.AsyncClient() as client:
        class Http:
            async def get(self, url, *, params, headers):
                if url.endswith("/v1/corporate-actions"):
                    corporate_requests.append(params)
                    return {"corporate_actions": {"name_changes": [{
                        "old_symbol": "OLD", "new_symbol": "NEW", "new_cusip": "TEST00001",
                        "process_date": today.isoformat(),
                    }]}}
                return await client.get(url, params=params, headers=headers)

        store = StateStore(tmp_path / "assets.db")
        store.open()
        registry = AssetRegistry(Http(), store=store, now=world.clock.now, on_rename=events.append)
        registry.set_sector("OLD", "Financial Services")
        assert len(await registry.refresh(["OLD", "DEAD"])) == 1
        assert registry.get_asset_meta("OLD").renamed_to == "NEW"
        assert not registry.get_asset_meta("OLD").tradable
        assert registry.get_asset_meta("NEW").canonical_sector is CanonicalSector.FINANCIALS
        assert registry.get_asset_meta("BRK-B").cik == "0000000042"
        assert registry.resolve_symbol("DEAD") is None
        assert AssetRegistry.provider_symbol("BRK.B", "fmp") == "BRK-B"
        assert corporate_requests[0]["types"] == "name_change"
        restored = AssetRegistry(Http(), store=store, now=world.clock.now, on_rename=events.append)
        assert not await restored.refresh(["OLD", "DEAD"])
        assert len(events) == len(corporate_requests) == 1
        result = await PriceService(Http(), assets=restored, now=world.clock.now).get_quotes(["OLD", "DEAD"])
        assert result.quotes["OLD"].symbol == "NEW"
        assert result.missing == {"DEAD": "inactive_or_unknown_symbol"}
        assert world.net.requests(service="alpaca", path="/v2/stocks/snapshots")[-1].params["symbols"] == "NEW"
        store.close()
