"""Tests of the simulated Alpaca itself (no bot code involved)."""

from datetime import date, time, timedelta

import httpx
import pytest

from tests.fakes import AlpacaBehaviour, FakeAlpaca, FakeInternet, SimClock, occ_symbol, parse_occ

BASE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"


@pytest.fixture
def env():
    clock = SimClock.at_et(2026, 11, 23, 10, 0)  # Monday, mid-session
    alpaca = FakeAlpaca(clock)
    net = FakeInternet([alpaca])
    client = httpx.Client(transport=net.transport)
    alpaca.set_price("AAA", 100.0, spread=0.02)
    return clock, alpaca, net, client


def order(client, **kw):
    body = {"symbol": "AAA", "side": "buy", "type": "market", "time_in_force": "day", "qty": "10"}
    body.update(kw)
    return client.post(f"{BASE}/v2/orders", json=body)


# ------------------------------------------------------------------ account
def test_account_fields(env):
    _, alpaca, _, c = env
    a = c.get(f"{BASE}/v2/account").json()
    assert a["cash"] == "100000" and a["equity"] == "100000"
    assert a["multiplier"] == "4" and a["shorting_enabled"] is True
    assert float(a["buying_power"]) > 100000
    assert a["options_trading_level"] == 3


def test_cash_account_buying_power_is_cash():
    clock = SimClock.at_et(2026, 11, 23, 10, 0)
    alpaca = FakeAlpaca(clock, multiplier=1, cash=5000)
    c = httpx.Client(transport=FakeInternet([alpaca]).transport)
    assert c.get(f"{BASE}/v2/account").json()["buying_power"] == "5000"


def test_configurations_patch_disables_shorting(env):
    _, alpaca, _, c = env
    r = c.patch(f"{BASE}/v2/account/configurations", json={"no_shorting": True})
    assert r.json()["no_shorting"] is True
    assert c.get(f"{BASE}/v2/account").json()["shorting_enabled"] is False
    r = order(c, side="sell")
    assert r.status_code == 403 and r.json()["message"] == alpaca.behaviour.shorting_disabled_message


def test_clock_and_calendar(env):
    clock, _, _, c = env
    clock.set_et(2026, 11, 26, 10, 0)
    j = c.get(f"{BASE}/v2/clock").json()
    assert j["is_open"] is False and j["next_open"].startswith("2026-11-27T09:30")
    assert j["next_close"].startswith("2026-11-27T13:00")
    cal = c.get(f"{BASE}/v2/calendar", params={"start": "2026-11-25", "end": "2026-11-27"}).json()
    assert [d["date"] for d in cal] == ["2026-11-25", "2026-11-27"]
    assert cal[1]["close"] == "13:00"


# ------------------------------------------------------------------ orders
def test_market_buy_fills_at_ask_and_updates_book(env):
    _, alpaca, _, c = env
    o = order(c).json()
    assert o["status"] == "filled" and o["filled_avg_price"] == "100.01"
    pos = c.get(f"{BASE}/v2/positions/AAA").json()
    assert pos["qty"] == "10" and pos["side"] == "long"
    assert alpaca.cash == pytest.approx(100000 - 1000.1)


def test_opening_window_delays_fill(env):
    clock, alpaca, _, c = env
    clock.set_et(2026, 11, 24, 9, 30, 30)
    o = order(c).json()
    assert o["status"] == "new"
    clock.advance(minutes=4)
    assert c.get(f"{BASE}/v2/orders/{o['id']}").json()["status"] == "new"
    clock.advance(minutes=1)
    got = c.get(f"{BASE}/v2/orders/{o['id']}").json()
    assert got["status"] == "filled" and got["filled_at"].startswith("2026-11-24T14:35:30")


def test_order_before_open_is_accepted_then_fills_after_open(env):
    clock, _, _, c = env
    clock.set_et(2026, 11, 24, 8, 0)
    o = order(c, side="buy").json()
    assert o["status"] == "accepted"
    clock.set_et(2026, 11, 24, 9, 40)
    assert c.get(f"{BASE}/v2/orders/{o['id']}").json()["status"] == "filled"


def test_day_order_on_holiday_cancelled_at_close_by_default(env):
    clock, _, _, c = env
    clock.set_et(2026, 11, 26, 9, 30)
    o = order(c).json()
    assert o["status"] == "accepted"
    clock.set_et(2026, 11, 27, 10, 0)
    got = c.get(f"{BASE}/v2/orders/{o['id']}").json()
    assert got["status"] == "canceled" and got["filled_qty"] == "0"


def test_day_order_on_holiday_queued_when_switch_says_so(env):
    clock, alpaca, _, c = env
    alpaca.behaviour.holiday_day_order_policy = "queue_next_session"
    clock.set_et(2026, 11, 26, 9, 30)
    o = order(c).json()
    clock.set_et(2026, 11, 27, 10, 0)
    assert c.get(f"{BASE}/v2/orders/{o['id']}").json()["status"] == "filled"


def test_unfilled_day_limit_cancelled_at_early_close(env):
    clock, _, _, c = env
    clock.set_et(2026, 11, 27, 10, 0)
    o = order(c, type="limit", limit_price="90").json()
    clock.set_et(2026, 11, 27, 13, 0)
    got = c.get(f"{BASE}/v2/orders/{o['id']}").json()
    assert got["status"] == "canceled" and got["canceled_at"].startswith("2026-11-27T18:00")


def test_limit_fills_only_when_marketable(env):
    _, alpaca, _, c = env
    o = order(c, type="limit", limit_price="99").json()
    assert o["status"] == "new"
    alpaca.set_price("AAA", 98.5)
    got = c.get(f"{BASE}/v2/orders/{o['id']}").json()
    assert got["status"] == "filled" and float(got["filled_avg_price"]) <= 99


def test_stop_and_stop_limit(env):
    _, alpaca, _, c = env
    order(c, qty="20")
    s = order(c, side="sell", type="stop", stop_price="95", time_in_force="gtc", qty="10").json()
    sl = order(c, side="sell", type="stop_limit", stop_price="94", limit_price="93.5", time_in_force="gtc",
               qty="10").json()
    alpaca.set_price("AAA", 96)
    assert c.get(f"{BASE}/v2/orders/{s['id']}").json()["status"] == "new"
    alpaca.set_price("AAA", 93.9)
    assert c.get(f"{BASE}/v2/orders/{s['id']}").json()["status"] == "filled"
    assert c.get(f"{BASE}/v2/orders/{sl['id']}").json()["status"] == "filled"
    alpaca.set_price("AAA", 90)  # stop-limit that gapped through its limit would rest


def test_gtc_cancelled_after_90_days(env):
    clock, _, _, c = env
    order(c)
    s = order(c, side="sell", type="stop", stop_price="50", time_in_force="gtc").json()
    clock.advance(days=91)
    assert c.get(f"{BASE}/v2/orders/{s['id']}").json()["status"] == "canceled"


def test_partial_fills_are_scripted(env):
    clock, alpaca, _, c = env
    alpaca.script_partial_fills("AAA", [3, 3])
    o = order(c).json()
    assert o["status"] == "partially_filled" and o["filled_qty"] == "3"
    clock.advance(minutes=1)
    assert c.get(f"{BASE}/v2/orders/{o['id']}").json()["filled_qty"] == "6"
    clock.advance(minutes=1)
    got = c.get(f"{BASE}/v2/orders/{o['id']}").json()
    assert got["status"] == "filled" and got["filled_qty"] == "10"
    fills = c.get(f"{BASE}/v2/account/activities/FILL").json()
    assert [f["type"] for f in fills] == ["fill", "partial_fill", "partial_fill"]


def test_client_order_id_unique_and_lookup(env):
    _, _, _, c = env
    assert order(c, client_order_id="abc").status_code == 200
    r = order(c, client_order_id="abc")
    assert r.status_code == 422 and r.json()["message"] == "client_order_id must be unique"
    got = c.get(f"{BASE}/v2/orders:by_client_order_id", params={"client_order_id": "abc"}).json()
    assert got["client_order_id"] == "abc"
    assert order(c, client_order_id="x" * 129).status_code == 422


def test_insufficient_qty_when_shares_held_for_open_orders(env):
    _, _, _, c = env
    order(c)
    order(c, side="sell", type="limit", limit_price="120", time_in_force="gtc")
    r = order(c, side="sell", type="limit", limit_price="130")
    assert r.status_code == 403
    j = r.json()
    assert j["code"] == 40310000 and j["held_for_orders"] == "10" and j["available"] == "0"
    assert j["message"].startswith("insufficient qty available for order (requested: 10, available: 0)")
    assert c.get(f"{BASE}/v2/positions/AAA").json()["qty_available"] == "0"


def test_selling_more_than_held_rejected_by_default_and_flip_switch(env):
    _, alpaca, _, c = env
    order(c)
    assert order(c, side="sell", qty="15").status_code == 403
    alpaca.behaviour.allow_long_to_short_flip = True
    assert order(c, side="sell", qty="15").status_code == 200
    assert c.get(f"{BASE}/v2/positions/AAA").json()["qty"] == "-5"


def test_sell_without_position_opens_short_when_shorting_enabled(env):
    _, _, _, c = env
    o = order(c, side="sell").json()
    assert o["status"] == "filled" and o["position_intent"] == "sell_to_open_short"
    p = c.get(f"{BASE}/v2/positions/AAA").json()
    assert p["side"] == "short" and p["qty"] == "-10" and float(p["market_value"]) < 0


def test_not_shortable_asset(env):
    _, alpaca, _, c = env
    alpaca.add_asset("HTB", shortable=False)
    alpaca.set_price("HTB", 10)
    r = order(c, symbol="HTB", side="sell")
    assert r.status_code == 422 and "cannot be sold short" in r.json()["message"]


def test_insufficient_buying_power(env):
    _, _, _, c = env
    r = order(c, qty="100000")
    assert r.status_code == 403 and r.json()["message"] == "insufficient buying power"


def test_open_buy_orders_reduce_buying_power(env):
    _, _, _, c = env
    before = float(c.get(f"{BASE}/v2/account").json()["buying_power"])
    order(c, type="limit", limit_price="50", qty="100")
    after = float(c.get(f"{BASE}/v2/account").json()["buying_power"])
    assert before - after == pytest.approx(5000)


def test_wash_trade_rejection_for_stop_while_buy_pending(env):
    clock, _, _, c = env
    clock.set_et(2026, 11, 24, 9, 31)
    buy = order(c).json()
    r = order(c, side="sell", type="stop", stop_price="93", time_in_force="gtc")
    assert r.status_code == 403
    j = r.json()
    assert j["message"] == "potential wash trade detected. use complex orders"
    assert j["reject_reason"] == "opposite side market/stop order exists" and j["existing_order_id"] == buy["id"]


def test_wash_trade_switch_off(env):
    clock, alpaca, _, c = env
    alpaca.behaviour.wash_trade_check = False
    clock.set_et(2026, 11, 24, 9, 31)
    order(c)
    # no shares yet: with the check off the stop is accepted as a short stop
    assert order(c, side="sell", type="stop", stop_price="93", time_in_force="gtc").status_code == 200


def test_bracket_order_children_activate_on_fill_and_oco_cancels_sibling(env):
    clock, alpaca, _, c = env
    o = order(c, order_class="bracket", take_profit={"limit_price": "110"},
              stop_loss={"stop_price": "95"}, time_in_force="gtc").json()
    assert o["status"] == "filled" and len(o["legs"]) == 2
    legs = {leg["type"]: leg for leg in c.get(f"{BASE}/v2/orders/{o['id']}").json()["legs"]}
    assert legs["limit"]["status"] == "new" and legs["stop"]["status"] == "new"
    alpaca.set_price("AAA", 94)
    legs = {leg["type"]: leg for leg in c.get(f"{BASE}/v2/orders/{o['id']}").json()["legs"]}
    assert legs["stop"]["status"] == "filled" and legs["limit"]["status"] == "canceled"
    assert alpaca.position("AAA") is None


def test_bracket_legs_held_until_parent_fills(env):
    clock, _, _, c = env
    clock.set_et(2026, 11, 24, 9, 31)
    o = order(c, order_class="bracket", take_profit={"limit_price": "110"}, stop_loss={"stop_price": "95"}).json()
    assert {leg["status"] for leg in o["legs"]} == {"held"}
    open_orders = c.get(f"{BASE}/v2/orders", params={"status": "open", "nested": "true"}).json()
    assert len(open_orders) == 1 and len(open_orders[0]["legs"]) == 2


def test_cancel_replace_and_list(env):
    _, alpaca, _, c = env
    o = order(c, type="limit", limit_price="90").json()
    r = c.patch(f"{BASE}/v2/orders/{o['id']}", json={"limit_price": "91"})
    n = r.json()
    assert r.status_code == 200 and n["replaces"] == o["id"] and n["limit_price"] == "91"
    old = c.get(f"{BASE}/v2/orders/{o['id']}").json()
    assert old["status"] == "replaced" and old["replaced_by"] == n["id"]
    assert c.delete(f"{BASE}/v2/orders/{n['id']}").status_code == 204
    assert c.delete(f"{BASE}/v2/orders/{n['id']}").status_code == 422
    assert c.get(f"{BASE}/v2/orders/nope").status_code == 404
    closed = c.get(f"{BASE}/v2/orders", params={"status": "closed"}).json()
    assert {x["status"] for x in closed} == {"replaced", "canceled"}


def test_cancel_all_returns_207(env):
    _, _, _, c = env
    order(c, type="limit", limit_price="90")
    order(c, type="limit", limit_price="91")
    r = c.delete(f"{BASE}/v2/orders")
    assert r.status_code == 207 and [x["status"] for x in r.json()] == [200, 200]


def test_cancel_latency_switch(env):
    clock, alpaca, _, c = env
    alpaca.behaviour.cancel_latency_seconds = 2
    o = order(c, type="limit", limit_price="90").json()
    c.delete(f"{BASE}/v2/orders/{o['id']}")
    assert c.get(f"{BASE}/v2/orders/{o['id']}").json()["status"] == "pending_cancel"
    clock.advance(3)
    assert c.get(f"{BASE}/v2/orders/{o['id']}").json()["status"] == "canceled"


def test_fractional_rules(env):
    _, alpaca, _, c = env
    assert order(c, qty="1.5").json()["status"] == "filled"
    assert order(c, qty="1.5", time_in_force="gtc").status_code == 422
    alpaca.add_asset("NOFRAC", fractionable=False)
    alpaca.set_price("NOFRAC", 10)
    assert order(c, symbol="NOFRAC", qty="0.5").status_code == 422
    assert order(c, qty=None, notional="250").json()["status"] == "filled"


def test_close_position_and_404(env):
    _, alpaca, _, c = env
    order(c)
    r = c.delete(f"{BASE}/v2/positions/AAA")
    assert r.status_code == 200 and r.json()["side"] == "sell"
    assert alpaca.position("AAA") is None
    assert c.get(f"{BASE}/v2/positions/AAA").status_code == 404


def test_dividend_activity_and_pagination(env):
    _, alpaca, _, c = env
    for _ in range(3):
        order(c, qty="1")
    alpaca.pay_dividend("AAA", 0.5)
    assert alpaca.cash == pytest.approx(100000 - 3 * 100.01 + 1.5)
    acts = c.get(f"{BASE}/v2/account/activities", params={"page_size": "2"}).json()
    assert len(acts) == 2 and acts[0]["activity_type"] == "DIV"
    rest = c.get(f"{BASE}/v2/account/activities", params={"page_size": "2", "page_token": acts[-1]["id"]}).json()
    assert len(rest) == 2 and all(a["activity_type"] == "FILL" for a in rest)


def test_portfolio_history_records_closes(env):
    clock, _, _, c = env
    order(c)
    clock.set_et(2026, 11, 25, 17, 0)
    h = c.get(f"{BASE}/v2/account/portfolio/history").json()
    assert len(h["timestamp"]) == 4 and h["timeframe"] == "1D"


# ----------------------------------------------------------------- options
@pytest.fixture
def opt_env(env):
    clock, alpaca, net, c = env
    exp = date(2026, 12, 18)
    alpaca.add_option_chain("AAA", exp, [95, 100, 105, 110])
    for k, (b, a) in {95: (6.0, 6.2), 100: (3.0, 3.2), 105: (1.2, 1.4), 110: (0.4, 0.5)}.items():
        alpaca.set_option_quote(occ_symbol("AAA", exp, "call", k), b, a)
    return clock, alpaca, net, c, exp


def test_occ_symbols_round_trip():
    s = occ_symbol("AAA", date(2026, 12, 18), "call", 102.5)
    assert s == "AAA261218C00102500"
    assert parse_occ(s) == ("AAA", date(2026, 12, 18), "call", 102.5)


def test_option_contracts_endpoint(opt_env):
    _, _, _, c, exp = opt_env
    j = c.get(f"{BASE}/v2/options/contracts", params={"underlying_symbols": "AAA", "type": "call",
                                                      "strike_price_gte": "100"}).json()
    assert {x["strike_price"] for x in j["option_contracts"]} == {"100", "105", "110"}


def test_option_market_order_outside_hours_rejected(opt_env):
    clock, _, _, c, exp = opt_env
    clock.set_et(2026, 11, 23, 17, 0)
    r = order(c, symbol=occ_symbol("AAA", exp, "call", 100), qty="1")
    assert r.status_code == 422 and r.json()["message"] == "options market orders are only allowed during market hours"


def test_mleg_spread_fills_atomically(opt_env):
    _, alpaca, _, c, exp = opt_env
    long_c, short_c = occ_symbol("AAA", exp, "call", 100), occ_symbol("AAA", exp, "call", 105)
    r = c.post(f"{BASE}/v2/orders", json={
        "order_class": "mleg", "qty": "2", "type": "limit", "limit_price": "2.0", "time_in_force": "day",
        "legs": [{"symbol": long_c, "side": "buy", "ratio_qty": "1", "position_intent": "buy_to_open"},
                 {"symbol": short_c, "side": "sell", "ratio_qty": "1", "position_intent": "sell_to_open"}],
    })
    o = r.json()
    assert r.status_code == 200 and o["status"] == "filled" and o["filled_avg_price"] == "2"
    assert alpaca.position(long_c).qty == 2 and alpaca.position(short_c).qty == -2


def test_mleg_not_marketable_rests_and_both_legs_stay_open(opt_env):
    _, alpaca, _, c, exp = opt_env
    long_c, short_c = occ_symbol("AAA", exp, "call", 100), occ_symbol("AAA", exp, "call", 105)
    o = c.post(f"{BASE}/v2/orders", json={
        "order_class": "mleg", "qty": "1", "type": "limit", "limit_price": "1.0", "time_in_force": "day",
        "legs": [{"symbol": long_c, "side": "buy", "ratio_qty": "1"},
                 {"symbol": short_c, "side": "sell", "ratio_qty": "1"}],
    }).json()
    assert o["status"] == "new" and {leg["status"] for leg in o["legs"]} == {"new"}
    assert alpaca.position(long_c) is None and alpaca.position(short_c) is None


def test_option_expiry_exercise_assignment_and_expire(opt_env):
    clock, alpaca, _, c, exp = opt_env
    long_itm, short_itm, otm = (occ_symbol("AAA", exp, "call", k) for k in (95, 100, 110))
    alpaca.seed_position(long_itm, 1, 5.0)
    alpaca.seed_position(short_itm, -1, 3.0)
    alpaca.seed_position(otm, 1, 0.5)
    alpaca.set_price("AAA", 104)
    clock.set_et(2026, 12, 18, 16, 1)
    kinds = sorted(a["activity_type"] for a in c.get(f"{BASE}/v2/account/activities").json()
                   if a["activity_type"] != "FILL")
    assert kinds == ["OPASN", "OPEXC", "OPEXP", "OPTRD", "OPTRD"]
    assert alpaca.position("AAA") is None  # +100 from exercise, -100 from assignment
    assert not any(parse_occ(s) for s in alpaca.positions)


def test_expiry_day_liquidation_switch(opt_env):
    clock, alpaca, _, c, exp = opt_env
    alpaca.behaviour.expiry_day_liquidation_time = time(15, 30)
    sym = occ_symbol("AAA", exp, "call", 100)
    alpaca.seed_position(sym, 1, 3.0)
    clock.set_et(2026, 12, 18, 15, 31)
    assert alpaca.position(sym) is None


def test_exercise_endpoint(opt_env):
    _, alpaca, _, c, exp = opt_env
    sym = occ_symbol("AAA", exp, "call", 95)
    alpaca.seed_position(sym, 1, 5.0)
    assert c.post(f"{BASE}/v2/positions/{sym}/exercise").status_code == 200
    assert alpaca.position("AAA").qty == 100


def test_level_two_cannot_sell_uncovered(opt_env):
    _, alpaca, _, c, exp = opt_env
    alpaca.options_level = 2
    r = order(c, symbol=occ_symbol("AAA", exp, "call", 105), side="sell", qty="1")
    assert r.status_code == 403


# -------------------------------------------------------------- market data
def test_market_data_endpoints(opt_env):
    clock, alpaca, _, c, exp = opt_env
    clock.advance(minutes=1)
    alpaca.set_price("AAA", 101, spread=0.02)
    q = c.get(f"{DATA}/v2/stocks/AAA/quotes/latest").json()["quote"]
    assert q["bp"] == 100.99 and q["ap"] == 101.01
    assert c.get(f"{DATA}/v2/stocks/trades/latest", params={"symbols": "AAA"}).json()["trades"]["AAA"]["p"] == 101
    snap = c.get(f"{DATA}/v2/stocks/AAA/snapshot").json()
    assert snap["latestTrade"]["p"] == 101 and snap["dailyBar"]["h"] == 101
    bars = c.get(f"{DATA}/v2/stocks/AAA/bars", params={"timeframe": "1Min"}).json()["bars"]
    assert [b["c"] for b in bars] == [100, 101]
    opts = c.get(f"{DATA}/v1beta1/options/snapshots/AAA").json()["snapshots"]
    assert len(opts) == 4
    lq = c.get(f"{DATA}/v1beta1/options/quotes/latest", params={"symbols": occ_symbol("AAA", exp, "call", 100)})
    assert lq.json()["quotes"][occ_symbol("AAA", exp, "call", 100)]["ap"] == 3.2


def test_news_endpoint(env):
    _, alpaca, _, c = env
    alpaca.add_news(["AAA"], "AAA beats")
    alpaca.add_news(["BBB"], "BBB misses")
    j = c.get(f"{DATA}/v1beta1/news", params={"symbols": "AAA"}).json()
    assert [n["headline"] for n in j["news"]] == ["AAA beats"] and "next_page_token" in j


def test_require_auth_switch():
    clock = SimClock.at_et(2026, 11, 23, 10, 0)
    alpaca = FakeAlpaca(clock, behaviour=AlpacaBehaviour(require_auth=True))
    c = httpx.Client(transport=FakeInternet([alpaca]).transport)
    assert c.get(f"{BASE}/v2/account").status_code == 401
    assert c.get(f"{BASE}/v2/account", headers={"APCA-API-KEY-ID": "k"}).status_code == 200


# ------------------------------------------------- covered calls, assignment
def test_covered_call_locks_shares_and_blocks_stock_sale(opt_env):
    _, alpaca, _, c, exp = opt_env
    order(c, qty="150")
    call = occ_symbol("AAA", exp, "call", 105)
    r = order(c, symbol=call, side="sell", qty="1", type="limit", limit_price="1.2")
    assert r.status_code == 200 and r.json()["position_intent"] == "sell_to_open"
    assert c.get(f"{BASE}/v2/positions/AAA").json()["qty_available"] == "50"
    r = order(c, side="sell", qty="100")
    assert r.status_code == 403 and r.json()["held_for_options"] == "100" and r.json()["available"] == "50"
    r = order(c, symbol=call, side="sell", qty="1", type="limit", limit_price="1.2")
    assert r.status_code == 403
    assert r.json()["message"] == "insufficient underlying qty available for covered call (required: 100, available: 50)"


def test_early_assignment_of_short_call_can_leave_short_stock(opt_env):
    _, alpaca, _, c, exp = opt_env
    call = occ_symbol("AAA", exp, "call", 100)
    alpaca.seed_position(call, -1, 3.0)
    acts = alpaca.assign_early(call)
    assert [a["activity_type"] for a in acts] == ["OPASN", "OPTRD"]
    assert alpaca.position(call) is None and alpaca.position("AAA").qty == -100


def test_option_greeks_in_snapshots(opt_env):
    _, alpaca, _, c, exp = opt_env
    sym = occ_symbol("AAA", exp, "call", 100)
    alpaca.set_option_greeks(sym, delta=0.52, iv=0.31)
    snap = c.get(f"{DATA}/v1beta1/options/snapshots", params={"symbols": sym}).json()["snapshots"][sym]
    assert snap["greeks"]["delta"] == 0.52 and snap["impliedVolatility"] == 0.31


def test_corporate_action_cancel(env):
    _, alpaca, _, c = env
    order(c)
    s = order(c, side="sell", type="stop", stop_price="90", time_in_force="gtc").json()
    assert alpaca.corporate_action_cancel("AAA") == [s["id"]]
    assert c.get(f"{BASE}/v2/orders/{s['id']}").json()["status"] == "canceled"


def test_news_appears_when_clock_reaches_it(env):
    clock, alpaca, _, c = env
    alpaca.add_news(["AAA"], "later story", created_at=clock.now() + timedelta(minutes=10))
    assert c.get(f"{DATA}/v1beta1/news", params={"symbols": "AAA"}).json()["news"] == []
    clock.advance(minutes=10)
    assert len(c.get(f"{DATA}/v1beta1/news", params={"symbols": "AAA"}).json()["news"]) == 1


def test_seeded_daily_bars(env):
    _, alpaca, _, c = env
    alpaca.seed_daily_bars("BBB", [10, 11, 12, 13, 14])
    bars = c.get(f"{DATA}/v2/stocks/BBB/bars", params={"timeframe": "1Day"}).json()["bars"]
    assert [b["c"] for b in bars] == [10, 11, 12, 13, 14]
    assert bars[0]["h"] == pytest.approx(10.1) and bars[-1]["t"].startswith("2026-11-20")


# ------------------------------------------------------------------ screener
# Shapes and limits: https://docs.alpaca.markets/us/openapi/market-data-api.json
def test_most_actives_ranked_by_volume_or_trades(env):
    _, alpaca, _, c = env
    alpaca.set_most_actives([("AAA", 500, 90), ("BBB", 900, 10), ("CCC", 100, 50)])
    r = c.get(f"{DATA}/v1beta1/screener/stocks/most-actives", params={"top": 2}).json()
    assert [x["symbol"] for x in r["most_actives"]] == ["BBB", "AAA"] and r["last_updated"]
    assert set(r["most_actives"][0]) == {"symbol", "volume", "trade_count"}
    r = c.get(f"{DATA}/v1beta1/screener/stocks/most-actives", params={"by": "trades"}).json()
    assert [x["symbol"] for x in r["most_actives"]] == ["AAA", "CCC", "BBB"]


def test_most_actives_plain_symbols_keep_order_and_default_top_is_10(env):
    _, alpaca, _, c = env
    alpaca.set_most_actives([f"S{i}" for i in range(12)])
    r = c.get(f"{DATA}/v1beta1/screener/stocks/most-actives").json()
    assert [x["symbol"] for x in r["most_actives"]] == [f"S{i}" for i in range(10)]


def test_movers_shape_and_top(env):
    _, alpaca, _, c = env
    alpaca.set_movers(gainers=["AAA", ("PENNY", 0.05, 80.0)], losers=[("BBB", 9.0, -10.0)])
    r = c.get(f"{DATA}/v1beta1/screener/stocks/movers", params={"top": 1}).json()
    assert r["market_type"] == "stocks" and r["last_updated"]
    assert r["gainers"] == [{"symbol": "AAA", "price": 100.0, "change": 9.0909, "percent_change": 10.0}]
    assert r["losers"][0]["symbol"] == "BBB" and r["losers"][0]["change"] == -1.0
    assert set(r["gainers"][0]) == {"symbol", "price", "change", "percent_change"}


def test_screener_top_over_documented_max_is_a_switch(env):
    _, alpaca, _, c = env
    alpaca.set_most_actives(["AAA"])
    assert c.get(f"{DATA}/v1beta1/screener/stocks/most-actives", params={"top": 101}).status_code == 400
    assert c.get(f"{DATA}/v1beta1/screener/stocks/movers", params={"top": 51}).status_code == 400
    alpaca.behaviour.screener_top_over_max = "clamp"
    assert c.get(f"{DATA}/v1beta1/screener/stocks/movers", params={"top": 51}).status_code == 200
