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


def test_top_up_releases_stop_then_reprotects_whole_position(world, tmp_path, monkeypatch):
    a = world.alpaca
    a.behaviour.wash_trade_check = True
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())

    class AddThree:  # sizing for adds is risk_limits' rule; here only the mechanics matter
        def propose_equity_qty(self, *a, **k):
            return 3

        def evaluate_trade(self, *a, **k):
            from stock_agent.risk_manager import RiskVerdict
            return RiskVerdict(True)

    eng.risk = AddThree()
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.FILLED, res
    pos = a.position("AAA")
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].qty == pos.qty > 10


def test_failed_exit_puts_the_stop_back(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    a.fail("/v2/orders", "status", method="POST", status=403, times=1)
    run(eng.execute_intent(enqueue(eng, "AAA", "sell", rule="model")))
    assert a.position("AAA").qty == 10
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].qty == 10


def test_resume_counts_market_fallback_fills_and_does_not_resell(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    it = enqueue(eng, "AAA", "sell", rule="risk_limit", qty=4)
    claimed = eng.ledger.claim_intent(it.intent_id, check_thesis=False)
    # crash after the fallback market order went out
    a.submit({"symbol": "AAA", "qty": "4", "side": "sell", "type": "market", "time_in_force": "day",
              "client_order_id": claimed.client_order_id + "-m"})
    world.clock.advance(minutes=1)
    assert a.position("AAA").qty == 6
    run(eng.resume())
    got = eng.ledger.get_intent(it.intent_id)
    assert got.status == IntentStatus.FILLED
    run(eng.execute_due())
    assert a.position("AAA").qty == 6
    assert len(_stops(a, "AAA")) == 1


def test_cover_refused_when_it_would_need_borrowing(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("PPP", 100, spread=0.02)
    a.seed_position("PPP", -85, 100)
    eng = make_engine(world, tmp_path, monkeypatch)

    class NoCash:
        def evaluate_trade(self, *a, **k):
            from stock_agent.risk_manager import RiskVerdict
            return RiskVerdict(False, ("insufficient_cash",))

    eng.risk = NoCash()
    try:
        run(eng.cover_short("PPP"))
    except Exception:
        pass
    assert a.position("PPP").qty == -85
    assert any(sev == "critical" for sev, _, _ in eng.alerts)


def test_clean_account_unlocks_entries_without_waiting(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    a.configurations["no_shorting"] = True
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    st = run(eng.normalise_step())
    assert st.state == "done", st
    assert len(_stops(a, "AAA")) == 1


def test_normalise_rerun_after_cancelled_fraction_sale_finishes_the_job(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 9, 36)
    world.price("MMM", 500, spread=0.02)
    a.seed_position("MMM", 3.5, 500)
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    eng._kv_set(eng.NORMALISE_KEY, {"state": "planned", "planned_at": "2026-11-20T12:00:00+00:00"})
    a.fail("/v2/orders", "status", method="POST", status=403, times=1)
    st = run(eng.normalise_step())
    assert st.state == "failed"
    assert len(_stops(a, "MMM")) == 1  # the whole shares stay protected
    st = run(eng.normalise_step())
    assert st.state == "done", st
    assert a.position("MMM").qty == 3
    assert len(_stops(a, "MMM")) == 1


# ---------------------------------------------------------------- lead review (a)-(f) and analyst repros
def test_buy_to_cover_works_with_no_shorting_already_true(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("PPP", 100, spread=0.02)
    a.configurations["no_shorting"] = True
    a.shorting_enabled = False
    a.seed_position("PPP", -20, 100)
    eng = make_engine(world, tmp_path, monkeypatch)
    o = run(eng.cover_short("PPP", reason="assignment"))
    assert o is not None and o.filled_qty == 20 and a.position("PPP") is None


def test_normalise_covers_first_and_continues_when_no_shorting_cannot_be_set(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.price("PPP", 100, spread=0.02)
    world.price("DDD", 100, spread=0.02)
    a.seed_position("PPP", -10, 100)
    a.seed_position("DDD", 10, 100)
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    assert run(eng.normalise_step()).state == "planned"
    world.clock.set_et(2026, 11, 23, 9, 36)
    a.fail("/v2/account/configurations", "status", method="PATCH", status=403, times=None,
           body={"code": 40310000, "message": "forbidden"})
    st = run(eng.normalise_step())
    assert st.state == "done", st
    assert a.position("PPP") is None and _no_shorts(a)
    assert any(t == "Could not turn shorting off at the broker" for _, t, _ in eng.alerts)
    assert len(_stops(a, "DDD")) == 1


def test_first_monitor_tick_before_normalise_covers_nothing(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 9, 31)
    world.price("PPP", 100, spread=0.02)
    a.seed_position("PPP", -10, 100)
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    rep = run(eng.monitor_tick())
    assert rep.normalise.state == "planned" and rep.covered == []
    assert [o for o in a.orders.values() if o.side == "buy"] == []


def test_early_assignment_short_repaired_under_no_shorting(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("CCC", 50, spread=0.02)
    a.configurations["no_shorting"] = True
    a.shorting_enabled = False
    occ = "CCC261218C00045000"
    a.seed_position(occ, -1, 2.0)
    eng = make_engine(world, tmp_path, monkeypatch)
    a.assign_early(occ)
    assert a.position("CCC").qty == -100
    rep = run(eng.monitor_tick())
    assert rep.covered == ["CCC"] and a.position("CCC") is None


def test_rejected_buy_and_stop_raise_alerts(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    a.fail("/v2/orders", "status", method="POST", status=403, body={"code": 40310000, "message": "no"})
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.REJECTED
    assert any("buy failed" in t for _, t, _ in eng.alerts)
    a.seed_position("BBB", 10, 50)
    world.price("BBB", 50, spread=0.02)
    a.fail("/v2/orders", "status", method="POST", status=403, body={"code": 40310000, "message": "no"})
    run(eng.ensure_protection())
    assert any(s == "critical" and "stop not in place" in t for s, t, _ in eng.alerts)


def test_execute_sell_on_wired_scheduler_does_not_touch_tipranks(world, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.exec_helpers import wired_scheduler

    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    sched, eng = wired_scheduler(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))

    def boom(*_a, **_k):
        raise AssertionError("tipranks lookup must not run on the sell path")

    sched._tipranks_lookup = boom
    run(sched._execute_sell(SimpleNamespace(symbol="AAA"), "model exit"))
    assert a.position("AAA") is None and _no_shorts(a)


def test_crash_after_buy_filled_resume_places_stop(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    it = enqueue(eng, "AAA", "buy", ref=50)
    c = eng.ledger.claim_intent(it.intent_id)
    a.submit({"symbol": "AAA", "side": "buy", "qty": "5", "type": "market", "time_in_force": "day",
              "client_order_id": c.client_order_id})
    world.clock.advance(seconds=5)  # filled at the broker; the process died before recording it
    assert a.position("AAA").qty == 5 and _stops(a, "AAA") == []
    run(eng.resume())
    assert eng.ledger.get_intent(it.intent_id).status == IntentStatus.FILLED
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].qty == 5


def test_confirmed_partial_is_protected_when_cancel_refused(world, tmp_path, monkeypatch):
    """Transient cancel refusal: retried at once, so the 3 filled are stopped in the same call."""
    from stock_agent.execution import ExecSettings

    a = world.alpaca
    assert a.behaviour.wash_trade_check  # the broker's default wash protection stays on
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.script_partial_fills("AAA", [3])
    eng = make_engine(world, tmp_path, monkeypatch, settings=ExecSettings(fill_wait_seconds=10))
    a.fail("/v2/orders/*", "status", method="DELETE", status=403, body={"code": 40310000, "message": "no"})
    it = enqueue(eng, "AAA", "buy", ref=50)
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.FILLED and res.filled_qty == 3
    assert eng.ledger.get_intent(it.intent_id).status == IntentStatus.FILLED
    assert [o for o in a.open_orders("AAA") if o.side == "buy"] == []
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].qty == a.position("AAA").qty == 3


def test_partial_with_persistent_cancel_refusal_is_loud_then_recovers(world, tmp_path, monkeypatch):
    """Unavoidable gap: while the broker refuses both the cancel and (wash trade) the stop,
    the filled shares cannot be protected. The bot alerts critical and retries each tick."""
    from stock_agent.execution import ExecSettings

    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.script_partial_fills("AAA", [3])
    eng = make_engine(world, tmp_path, monkeypatch, settings=ExecSettings(fill_wait_seconds=10))
    f = a.fail("/v2/orders/*", "status", method="DELETE", status=403, times=None,
               body={"code": 40310000, "message": "no"})
    it = enqueue(eng, "AAA", "buy", ref=50)
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.SUBMITTED
    assert any(s == "critical" and "unprotected" in t for s, t, _ in eng.alerts)
    run(eng.monitor_tick())
    assert eng.ledger.get_intent(it.intent_id).status == IntentStatus.SUBMITTED  # still refusing
    a.faults.remove(f)
    run(eng.monitor_tick())
    assert eng.ledger.get_intent(it.intent_id).status == IntentStatus.FILLED
    assert [o for o in a.open_orders("AAA") if o.side == "buy"] == []
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].qty == a.position("AAA").qty

def test_split_rescales_recorded_stop_instead_of_flattening(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.FILLED
    old_stop = _stops(a, "AAA")[0].stop_price
    q0 = a.position("AAA").qty
    a.corporate_action_cancel("AAA")
    p = a.positions["AAA"]
    p.qty, p.avg_entry_price = q0 * 2, p.avg_entry_price / 2  # 2:1 split
    world.price("AAA", 25, spread=0.02)
    run(eng.monitor_tick())
    assert a.position("AAA").qty == q0 * 2
    assert not any(o.side == "sell" and o.type != "stop" for o in a.orders.values())
    stops = _stops(a, "AAA")
    assert len(stops) == 1 and stops[0].qty == q0 * 2
    assert abs(stops[0].stop_price - old_stop / 2) <= 0.02


def test_entry_gate_rechecked_right_before_submit(world, tmp_path, monkeypatch):
    from types import SimpleNamespace

    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    calls = []

    async def gate(purpose, rule=None):
        calls.append(purpose)
        ok = len(calls) == 1
        return SimpleNamespace(allowed=ok, reason="" if ok else "entry cutoff", next_open=None)

    eng._market_gate = gate
    it = enqueue(eng, "AAA", "buy", ref=50)
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.QUEUED and len(calls) == 2
    world.assert_no_orders()
    assert eng.ledger.get_intent(it.intent_id).status == IntentStatus.QUEUED


def test_capital_lock_serialises_opening_buys_but_not_protection(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)

    async def go():
        lock = asyncio.Lock()
        eng._capital_lock = lock
        await lock.acquire()  # the options engine is authorising
        t = asyncio.ensure_future(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not t.done()
        await eng.ensure_protection()  # protection never waits for the lock
        lock.release()
        res = await t
        assert not lock.locked()
        return res

    res = run(go())
    assert res.status == IntentStatus.FILLED and len(_stops(world.alpaca, "AAA")) == 1


def test_research_gate_rechecked_at_submission(world, tmp_path, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    from stock_agent.ledger import NewIntent

    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)

    class Gate:
        def is_entry_eligible(self, symbol, now):
            return SimpleNamespace(direction="BUY", thesis_id="th-newer")

    eng._research_gate = Gate()
    ids = eng.ledger.enqueue_intents([NewIntent(
        symbol="AAA", side="buy", dedupe_key="news:AAA", reference_price=Decimal("50"), origin="news",
        thesis_ids=("th-old",), meta={"effective_conviction": 8, "raw_conviction": 8, "sector": "Technology"})])
    res = run(eng.execute_intent(eng.ledger.get_intent(ids[0])))
    assert res.status == IntentStatus.SKIPPED and "thesis" in res.reason
    world.assert_no_orders()


def test_buy_skipped_when_quote_reports_a_renamed_symbol(world, tmp_path, monkeypatch):
    from types import SimpleNamespace

    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    orig = eng.prices.get_quotes

    async def renamed(symbols, **kw):
        res = await orig(symbols, **kw)
        q = res.quotes["AAA"]
        res.quotes["AAA"] = SimpleNamespace(symbol="AAAB", price=q.price, bid=q.bid, ask=q.ask, as_of=q.as_of)
        return res

    eng.prices.get_quotes = renamed
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.SKIPPED and "AAAB" in res.reason
    world.assert_no_orders()


def _trim(eng, sym, origin, **meta):
    from stock_agent.ledger import NewIntent

    ids = eng.ledger.enqueue_intents([NewIntent(
        symbol=sym, side="sell", kind="trim", dedupe_key=f"{origin}:{sym}", origin=origin,
        meta={"rule": "risk_limit" if origin == "limit_drift" else "assignment", **meta})])
    return eng.ledger.get_intent(ids[0])


def test_queued_risk_trim_is_replanned_and_capped_to_fresh_quantity(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("DDD", 100, spread=0.02)
    a.seed_position("DDD", 400, 100)
    eng = make_engine(world, tmp_path, monkeypatch)
    snap = run(eng.ledger.refresh("t"))
    counters = {("issuer_cap", "DDD"): 3}
    fresh = eng.risk.plan_remediation(snap, consecutive_closes=counters)[0]
    assert 0 < fresh.qty < 400
    it = _trim(eng, "DDD", "limit_drift", qty=400, consecutive_closes=[["issuer_cap", "DDD", 3]])
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.FILLED and res.filled_qty == float(fresh.qty)
    left = a.position("DDD").qty
    assert left == 400 - float(fresh.qty)
    assert len(_stops(a, "DDD")) == 1 and _stops(a, "DDD")[0].qty == left


def test_queued_risk_trim_skipped_when_no_longer_needed(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("DDD", 100, spread=0.02)
    a.seed_position("DDD", 50, 100)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    it = _trim(eng, "DDD", "limit_drift", qty=20, consecutive_closes=[["issuer_cap", "DDD", 3]])
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.SKIPPED and "no longer needed" in res.reason
    assert not [o for o in a.orders.values() if o.side == "sell" and o.type != "stop"]


def test_one_crashing_intent_does_not_stop_the_others(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    for s in ("AAA", "BBB"):
        world.price(s, 50, spread=0.02)
        a.seed_position(s, 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    enqueue(eng, "AAA", "sell", rule="hard_stop")
    enqueue(eng, "BBB", "sell", rule="hard_stop")
    orig = eng.exit

    async def flaky(intent):
        if intent.symbol == "AAA":
            raise RuntimeError("bug")
        return await orig(intent)

    eng.exit = flaky
    out = run(eng.execute_due())
    assert a.position("BBB") is None and [r.symbol for r in out] == ["BBB"]
    assert any("crashed" in t for _, t, _ in eng.alerts)
    assert len(_stops(a, "AAA")) == 1


def test_no_shorting_retried_after_failed_cleanup(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    a.configurations["no_shorting"] = False
    a.shorting_enabled = True
    eng = make_engine(world, tmp_path, monkeypatch)
    f = a.fail("/v2/account/configurations", "status", method="PATCH", status=500, times=None)
    run(eng.monitor_tick())
    assert a.configurations["no_shorting"] is False
    a.faults.remove(f)
    world.clock.advance(minutes=30)
    run(eng.monitor_tick())
    assert a.configurations["no_shorting"] is False  # hourly back-off
    world.clock.advance(minutes=31)
    run(eng.monitor_tick())
    assert a.configurations["no_shorting"] is True


def test_normalise_plan_and_result_go_to_the_notifier(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.price("PPP", 100, spread=0.02)
    a.seed_position("PPP", -10, 100)
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    sent = []

    class Notifier:
        def send_normalise_plan(self, steps):
            sent.append(("plan", list(steps)))
            return True

        async def send_normalise_result(self, results, *, all_clean=True):
            sent.append(("result", all_clean))
            return True

    eng._notifier = Notifier()
    assert run(eng.normalise_step()).state == "planned"
    assert sent[0][0] == "plan" and any("PPP" in s for s in sent[0][1])
    assert not any(t == "Account clean-up plan" for _, t, _ in eng.alerts)
    world.clock.set_et(2026, 11, 23, 9, 36)
    assert run(eng.normalise_step()).state == "done"
    assert sent[-1] == ("result", True)


def _research_exit(eng, sym, thesis_ids, position_id=None):
    from stock_agent.ledger import NewIntent

    ids = eng.ledger.enqueue_intents([NewIntent(
        symbol=sym, side="sell", dedupe_key=f"rx:{sym}:{thesis_ids[0]}", origin="news",
        position_id=position_id, thesis_ids=tuple(thesis_ids), meta={"rule": "model"})])
    return eng.ledger.get_intent(ids[0])


def test_research_exit_skipped_when_a_newer_thesis_exists(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    old = eng.ledger.record_thesis("AAA", {"view": "sell"}, "held_review", activate=True)
    conf = eng.ledger.record_thesis("AAA", {"view": "confirm"}, "exit_confirmation")
    it = _research_exit(eng, "AAA", [old, conf])
    eng.ledger.record_thesis("AAA", {"view": "hold"}, "held_review", activate=True)  # newer research
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.SKIPPED and "newer thesis" in res.reason
    assert a.position("AAA").qty == 10
    assert not [o for o in a.orders.values() if o.side == "sell" and o.type != "stop"]


def test_research_exit_runs_when_its_thesis_is_current(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 50)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    th = eng.ledger.record_thesis("AAA", {"view": "sell"}, "held_review", activate=True)
    pid = eng.ledger.open_position_meta("AAA").position_id
    res = run(eng.execute_intent(_research_exit(eng, "AAA", [th], position_id=pid)))
    assert res.status == IntentStatus.FILLED and a.position("AAA") is None


def test_resume_binds_every_ambiguous_order_before_resolving(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    eng = make_engine(world, tmp_path, monkeypatch)
    its = []
    for s in ("AAA", "BBB"):
        world.price(s, 50, spread=0.02)
        it = enqueue(eng, s, "buy", ref=50)
        c = eng.ledger.claim_intent(it.intent_id)
        a.submit({"symbol": s, "side": "buy", "qty": "5", "type": "market", "time_in_force": "day",
                  "client_order_id": c.client_order_id})
        its.append(it)
    world.clock.advance(seconds=5)
    eng.ledger.mark_submitting_ambiguous()
    seen = []
    orig = eng.ledger.refresh

    async def spy(*args, **kw):
        seen.append([eng.ledger.get_intent(i.intent_id).broker_order_id for i in its])
        return await orig(*args, **kw)

    eng.ledger.refresh = spy
    run(eng.resume())
    assert seen and all(all(ids) for ids in seen)  # every fill was bound before the first reconcile
    for it in its:
        assert eng.ledger.get_intent(it.intent_id).status == IntentStatus.FILLED
        assert len(_stops(a, it.symbol)) == 1
