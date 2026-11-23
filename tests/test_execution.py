"""ExecutionEngine against the fake broker: mechanics first (DECISIONS A1, A3, A6, A8, B2, B3)."""
import asyncio
from datetime import timedelta

from stock_agent.ledger import IntentStatus
from tests.exec_helpers import enqueue, make_engine


def run(c):
    return asyncio.run(c)


def _stops(a, sym):
    return [o for o in a.open_orders(sym) if o.type == "stop" and o.side == "sell"]


def _no_shorts(a):
    return all(p.qty >= 0 for s, p in a.positions.items())


def test_buy_fills_then_one_gtc_stop_for_filled_qty(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    it = enqueue(eng, "AAA", "buy", ref=50)
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.FILLED, res
    pos = a.position("AAA")
    assert pos and pos.qty == res.filled_qty >= 1
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].time_in_force == "gtc"
    assert stops[0].qty == pos.qty
    assert 50 * 0.88 <= stops[0].stop_price <= 50 * 0.93 + 0.01
    assert eng.ledger.get_intent(it.intent_id).status == IntentStatus.FILLED


def test_buy_waits_for_open_plus_five(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 23, 9, 32)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    it = enqueue(eng, "AAA", "buy", ref=50)
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.QUEUED
    world.assert_no_orders()
    assert eng.ledger.get_intent(it.intent_id).next_attempt_at is not None


def test_buy_held_until_normalised(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.QUEUED
    world.assert_no_orders()


def test_buy_skipped_when_price_ran_away(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 55, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.SKIPPED
    world.assert_no_orders()


def test_lost_buy_response_does_not_double_buy(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    a.fail("/v2/orders", "lost_response", method="POST")
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.FILLED
    buys = [o for o in a.orders.values() if o.side == "buy"]
    assert len(buys) == 1


def test_exit_cancels_stop_then_sells_everything_no_short(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 45.0)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    assert len(_stops(a, "AAA")) == 1
    res = run(eng.execute_intent(enqueue(eng, "AAA", "sell", rule="model")))
    assert res.status == IntentStatus.FILLED and res.filled_qty == 10
    assert a.position("AAA") is None
    assert a.open_orders("AAA") == []
    assert _no_shorts(a)


def test_exit_after_stop_already_filled_sends_no_sell(world, tmp_path, monkeypatch):
    """The 1 May / 27 May double sale: stop fills, then the bot's own exit must not sell again."""
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 45.0)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    it = enqueue(eng, "AAA", "sell", rule="model")
    world.price("AAA", 40, spread=0.02)  # stop triggers and fills
    world.clock.advance(minutes=1)
    assert a.position("AAA") is None
    before = len(world.orders_sent())
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.SKIPPED
    assert len(world.orders_sent()) == before
    assert _no_shorts(a)


def test_trim_sells_part_and_restops_rest(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 45.0)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    res = run(eng.execute_intent(enqueue(eng, "AAA", "sell", rule="risk_limit", qty=4)))
    assert res.filled_qty == 4
    assert a.position("AAA").qty == 6
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].qty == 6


def test_protection_is_idempotent_and_one_stop_per_long(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    for s, px in (("AAA", 50), ("BBB", 20)):
        world.price(s, px, spread=0.02)
        a.seed_position(s, 10, px)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    r1 = run(eng.ensure_protection())
    assert sorted(r1.placed) == ["AAA", "BBB"]
    n = len(world.orders_sent())
    r2 = run(eng.ensure_protection())
    assert sorted(r2.kept) == ["AAA", "BBB"] and len(world.orders_sent()) == n
    assert all(len(_stops(a, s)) == 1 for s in ("AAA", "BBB"))


def test_stop_renewed_before_gtc_expiry(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    first = _stops(a, "AAA")[0]
    world.clock.advance(days=85)
    world.price("AAA", 50, spread=0.02)
    rep = run(eng.ensure_protection())
    assert rep.renewed == ["AAA"]
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].id != first.id and stops[0].stop_price == first.stop_price


def test_stop_replaced_after_broker_cancels_it(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    a.corporate_action_cancel("AAA")
    assert _stops(a, "AAA") == []
    run(eng.ensure_protection())
    assert len(_stops(a, "AAA")) == 1


def test_cover_short_buys_exact_qty(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("PPP", 100, spread=0.02)
    a.seed_position("PPP", -85, 100)
    eng = make_engine(world, tmp_path, monkeypatch)
    o = run(eng.cover_short("PPP"))
    assert o is not None and o.filled_qty == 85
    assert a.position("PPP") is None
    assert run(eng.cover_short("PPP")) is None  # idempotent


def test_model_exit_cap_holds_fifth_exit(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    syms = ["A1", "A2", "A3", "A4", "A5"]
    for s in syms:
        world.price(s, 10, spread=0.02)
        a.seed_position(s, 5, 10)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    res = [run(eng.execute_intent(enqueue(eng, s, "sell", rule="model"))) for s in syms]
    assert [r.status for r in res[:4]] == [IntentStatus.FILLED] * 4
    assert res[4].status == IntentStatus.QUEUED
    hard = run(eng.execute_intent(enqueue(eng, "A5", "sell", rule="hard_stop")))
    assert hard.status == IntentStatus.FILLED


def test_partial_fill_cancels_rest_and_stops_filled(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.script_partial_fills("AAA", [3])
    eng = make_engine(world, tmp_path, monkeypatch)
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    pos = a.position("AAA")
    assert res.status == IntentStatus.FILLED and pos.qty == res.filled_qty
    assert [o for o in a.open_orders("AAA") if o.side == "buy"] == []
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].qty == pos.qty


def test_resume_resolves_crash_mid_submit(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    it = enqueue(eng, "AAA", "buy", ref=50)
    eng.ledger.claim_intent(it.intent_id)  # crashed before any POST
    run(eng.resume())
    assert eng.ledger.get_intent(it.intent_id).status == IntentStatus.QUEUED
    world.assert_no_orders()


def test_normalise_plan_then_execute_next_session(world, tmp_path, monkeypatch):
    a = world.alpaca
    prices = {"JJJ": 300, "DDD": 100, "MMM": 500, "PPP": 100, "GGG": 300}
    for s, px in prices.items():
        world.price(s, px, spread=0.02)
    a.seed_position("JJJ", 130, 250)      # ~39% of equity: trim to 15%
    a.seed_position("DDD", 31, 100)
    a.seed_position("MMM", 0.5, 500)      # fractional
    a.seed_position("PPP", -85, 100)      # shorts
    a.seed_position("GGG", -10, 300)
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    st = run(eng.normalise_step())
    assert st.state == "planned"
    assert any("Account clean-up plan" == t for _, t, _ in eng.alerts)
    world.assert_no_orders()
    world.clock.set_et(2026, 11, 23, 9, 36)
    st = run(eng.normalise_step())
    assert st.state == "done", st
    assert eng.normalised
    assert a.configurations["no_shorting"] is True
    assert _no_shorts(a) and a.position("PPP") is None and a.position("GGG") is None
    assert a.position("MMM") is None
    acct = run(eng.broker.get_account())
    j = a.position("JJJ")
    assert j.qty * prices["JJJ"] <= 0.15 * float(acct.equity) + prices["JJJ"]
    for s in ("JJJ", "DDD"):
        stops = _stops(a, s)
        assert len(stops) == 1 and stops[0].qty == a.position(s).qty
    assert run(eng.normalise_step()).state == "done"


def test_monitor_tick_survives_broker_outage(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    eng = make_engine(world, tmp_path, monkeypatch)
    a.fail("/v2/positions", "5xx", times=None)
    rep = run(eng.monitor_tick())
    assert rep.errors
    assert any(sev == "critical" for sev, _, _ in eng.alerts)
    world.assert_no_orders()


def test_gate_closed_outside_session_for_exit(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 17, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    res = run(eng.execute_intent(enqueue(eng, "AAA", "sell", rule="model")))
    assert res.status == IntentStatus.QUEUED
    assert a.position("AAA").qty == 10
    _ = timedelta
