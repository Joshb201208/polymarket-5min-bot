"""AlpacaClient against the fake broker: raises on failure, never double-submits."""
import asyncio

import pytest

from stock_agent.broker import AlpacaClient, BrokerError, OrderRequest


def run(coro):
    return asyncio.run(coro)


def _client():
    return AlpacaClient("https://paper-api.alpaca.markets", "k", "s")


def _sell(sym, qty, cid="sa-test-sell-1", **kw):
    return OrderRequest(symbol=sym, side="sell", qty=qty, type="market", time_in_force="day",
                        client_order_id=cid, position_intent="sell_to_close", **kw)


def _buy(sym, qty, cid="sa-test-buy-1", limit=None):
    return OrderRequest(symbol=sym, side="buy", qty=qty, type="limit" if limit else "market",
                        time_in_force="day", client_order_id=cid, position_intent="buy_to_open",
                        limit_price=limit)


def test_get_positions_raises_on_5xx_instead_of_returning_empty(world):
    world.alpaca.seed_position("AAA", 10, 20.0)
    world.alpaca.fail("/v2/positions", "5xx", times=None)
    with pytest.raises(BrokerError) as e:
        run(_client().get_positions())
    assert e.value.kind == "server"


def test_get_positions_raises_on_malformed_json(world):
    world.alpaca.fail("/v2/positions", "malformed_json", times=None)
    with pytest.raises(BrokerError) as e:
        run(_client().get_positions())
    assert e.value.kind == "bad_response"


def test_get_retries_transient_timeout(world):
    world.alpaca.seed_position("AAA", 10, 20.0)
    world.alpaca.fail("/v2/positions", "timeout", times=1)
    pos = run(_client().get_positions())
    assert [p.symbol for p in pos] == ["AAA"]


def test_get_position_none_only_on_404(world):
    assert run(_client().get_position("ZZZ")) is None


def test_lost_response_on_submit_is_found_by_client_id_not_resubmitted(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    world.alpaca.fail("/v2/orders", "lost_response", method="POST")
    order = run(_client().submit_order(_buy("AAA", 3, limit=51.0)))
    assert order.client_order_id == "sa-test-buy-1"
    posts = world.net.requests(service="alpaca", method="POST", path="/v2/orders")
    assert len(posts) == 1


def test_timeout_without_processing_resubmits_once_with_same_id(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    world.alpaca.fail("/v2/orders", "timeout", method="POST")
    order = run(_client().submit_order(_buy("AAA", 3, limit=51.0)))
    posts = world.net.requests(service="alpaca", method="POST", path="/v2/orders")
    assert len(posts) == 2
    assert order.client_order_id == "sa-test-buy-1"
    accepted = [r for r in posts if r.status == 200]
    assert len(accepted) == 1


def test_submit_uncertain_when_lookup_also_fails(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    world.alpaca.fail("/v2/orders", "timeout", method="POST")
    world.alpaca.fail("/v2/orders:by_client_order_id", "5xx", times=None)
    with pytest.raises(BrokerError) as e:
        run(_client().submit_order(_buy("AAA", 3, limit=51.0)))
    assert e.value.uncertain
    assert len(world.net.requests(service="alpaca", method="POST", path="/v2/orders")) == 1


def test_sell_guard_refuses_sell_with_no_position(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50)
    with pytest.raises(BrokerError) as e:
        run(_client().submit_order(_sell("AAA", 5)))
    assert e.value.kind == "guard"
    world.assert_no_orders()


def test_sell_guard_refuses_more_than_qty_available(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50)
    world.alpaca.seed_position("AAA", 10, 40.0)
    c = _client()
    stop = OrderRequest(symbol="AAA", side="sell", qty=10, type="stop", time_in_force="gtc",
                        client_order_id="sa-test-stop-1", position_intent="sell_to_close", stop_price=45.0)
    run(c.submit_order(stop))
    with pytest.raises(BrokerError) as e:
        run(c.submit_order(_sell("AAA", 10, cid="sa-test-sell-2")))
    assert e.value.kind == "guard"
    assert world.alpaca.position("AAA").qty == 10


def test_sell_guard_refuses_sell_to_open(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50)
    world.alpaca.seed_position("AAA", 10, 40.0)
    req = OrderRequest(symbol="AAA", side="sell", qty=1, type="market", time_in_force="day",
                       client_order_id="sa-x", position_intent="sell_to_open")
    with pytest.raises(BrokerError):
        run(_client().submit_order(req))
    world.assert_no_orders()


def test_free_shares_excludes_shares_held_by_stop(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50)
    world.alpaca.seed_position("AAA", 10, 40.0)
    c = _client()
    stop = OrderRequest(symbol="AAA", side="sell", qty=6, type="stop", time_in_force="gtc",
                        client_order_id="sa-test-stop-2", position_intent="sell_to_close", stop_price=45.0)
    run(c.submit_order(stop))
    assert run(c.free_shares("AAA")) == 4


def test_set_account_config_reads_back(world):
    c = _client()
    cfg = run(c.set_account_config(no_shorting=True))
    assert cfg.no_shorting is True
    assert run(c.get_account_config()).no_shorting is True


def test_clock_and_cancel(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    c = _client()
    assert run(c.get_clock()).is_open
    o = run(c.submit_order(_buy("AAA", 2, limit=40.0)))
    run(c.cancel_order(o.id))
    assert run(c.get_order(o.id)).is_terminal


def test_nan_position_qty_is_a_bad_response(world):
    import asyncio

    import pytest

    from stock_agent.broker import AlpacaClient, BrokerError

    world.alpaca.fail("/v2/positions", "status", method="GET", status=200,
                      body=[{"symbol": "AAA", "qty": "NaN", "qty_available": "NaN", "avg_entry_price": "50",
                             "side": "long", "asset_class": "us_equity"}])
    c = AlpacaClient("https://paper-api.alpaca.markets", "k", "s")
    with pytest.raises(BrokerError) as ei:
        asyncio.run(c.get_positions())
    assert ei.value.kind == "bad_response"


def test_resolve_base_url_prefers_settings_property():
    from types import SimpleNamespace

    from stock_agent.broker import resolve_base_url

    cfg = SimpleNamespace(settings=SimpleNamespace(trading_base_url="https://paper-api.alpaca.markets/"),
                          ALPACA_BASE_URL="https://other.example", MODE="PAPER")
    assert resolve_base_url(cfg) == "https://paper-api.alpaca.markets"


def test_client_uses_security_config_urls():
    from types import SimpleNamespace

    from stock_agent.broker import AlpacaClient

    cfg = SimpleNamespace(trading_base_url="https://paper-api.alpaca.markets",
                          market_data_url="https://data.alpaca.markets/", ALPACA_DATA_URL="https://x.example")
    c = AlpacaClient(key_id="k", secret="s", config=cfg)
    assert c.base_url == "https://paper-api.alpaca.markets" and c.data_url == "https://data.alpaca.markets"


def test_account_margin_fields_are_decimal_and_none_safe():
    from decimal import Decimal

    from stock_agent.broker import Account

    a = Account.model_validate({
        "equity": "100000", "cash": "-5000", "buying_power": "380000", "multiplier": "4",
        "regt_buying_power": "190000", "daytrading_buying_power": "380000",
        "non_marginable_buying_power": "0", "options_buying_power": "0", "initial_margin": "52000",
        "maintenance_margin": "31200", "last_maintenance_margin": "30000", "sma": "0"})
    assert a.regt_buying_power == Decimal("190000") and isinstance(a.maintenance_margin, Decimal)
    assert a.multiplier == Decimal("4") and a.margin_enabled_at_broker
    # A13b: overnight basis only, never the 4x intraday figure
    assert a.overnight_buying_power == Decimal("190000")
    assert a.margin_excess == Decimal("68800")
    assert a.margin_call is None
    bare = Account.model_validate({"equity": "1000", "cash": "1000", "multiplier": "2"})
    assert bare.regt_buying_power is None and bare.maintenance_margin is None and bare.sma is None
    assert bare.overnight_buying_power is None and bare.margin_excess is None  # missing never reads as 0
    cash = Account.model_validate({"equity": "1000", "cash": "800", "buying_power": "800", "multiplier": "1"})
    assert cash.overnight_buying_power == Decimal("800")
    with pytest.raises(ValueError):
        Account.model_validate({"equity": "1", "cash": "1", "regt_buying_power": "NaN"})


def test_insufficient_buying_power_error_is_recognised(world):
    world.price("AAA", 50, spread=0.02)
    world.alpaca.fail("/v2/orders", "status", method="POST", status=403,
                      body={"code": 40310000, "message": "insufficient buying power"})
    with pytest.raises(BrokerError) as e:
        run(_client().submit_order(_buy("AAA", 1, limit=51)))
    assert e.value.insufficient_buying_power and not e.value.retryable
