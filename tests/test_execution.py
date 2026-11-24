"""ExecutionEngine against the fake broker: mechanics first (DECISIONS A1, A3, A6, A8, B2, B3)."""
import asyncio
import uuid
from datetime import timedelta

from stock_agent.ledger import IntentStatus, NewIntent, ThesisRole
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
    thesis = eng.ledger.record_thesis("AAA", {"draft": {"direction": "BUY"}},
                                     ThesisRole.HELD_REVIEW, activate=True)
    [intent_id] = eng.ledger.enqueue_intents([NewIntent(
        "AAA", "buy", "test-top-up-stop", position_id=eng.ledger.open_position_meta("AAA").position_id,
        thesis_ids=(thesis,),
    )])
    res = run(eng.execute_intent(eng.ledger.get_intent(intent_id)))
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


def test_cover_goes_through_without_cash_under_margin(world, tmp_path, monkeypatch):
    """A13i: covering an accidental short always reduces risk and need not be paid from cash."""
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("PPP", 100, spread=0.02)
    world.price("LLL", 100, spread=0.02)
    a.seed_position("LLL", 400, 100)
    a.seed_position("PPP", -85, 100)
    a.cash = -1000.0  # debit balance: no cash to pay for the buy-back
    eng = make_engine(world, tmp_path, monkeypatch)

    class NoCash:  # a cash-only risk check would refuse; the cover must not consult it
        def evaluate_trade(self, *a, **k):
            from stock_agent.risk_manager import RiskVerdict
            return RiskVerdict(False, ("insufficient_cash",))

    eng.risk = NoCash()
    o = run(eng.cover_short("PPP"))
    assert o is not None and float(o.filled_qty) == 85
    assert a.position("PPP") is None and _no_shorts(a)


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


def test_stop_filling_during_cancel_skips_exit_without_using_a_model_slot(world, tmp_path, monkeypatch):
    """REQ-002: the resting stop fills while exit() is cancelling it."""
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    a.seed_position("AAA", 10, 45.0)
    eng = make_engine(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    orig = eng._cancel_confirm

    async def racing_cancel(order):
        if order.type == "stop":
            world.price("AAA", 40, spread=0.02)  # stop triggers and fills mid-cancel
            world.clock.advance(minutes=1)
        return await orig(order)

    monkeypatch.setattr(eng, "_cancel_confirm", racing_cancel)
    before = len(world.orders_sent(include_failed=False))
    res = run(eng.execute_intent(enqueue(eng, "AAA", "sell", rule="model")))
    assert a.position("AAA") is None
    assert res.status == IntentStatus.SKIPPED and "stop" in res.reason
    assert int(eng._kv_get(eng._model_exit_key(), 0) or 0) == 0
    assert [r for r in world.orders_sent(include_failed=False)[before:] if r.method == "POST"] == []
    assert _no_shorts(a)


class _CloseCalls:
    """Minimal options manager: buys the short call back at the fake broker."""

    def __init__(self, eng, log):
        self.eng, self.log = eng, log

    async def close_short_call(self, occ):
        from types import SimpleNamespace

        from stock_agent.broker import OrderRequest

        self.log.append(("close_call", occ))
        o = await self.eng.broker.submit_order(OrderRequest(
            symbol=occ, side="buy", qty=1, type="market", time_in_force="day",
            client_order_id=f"t-close-{uuid.uuid4().hex[:8]}", position_intent="buy_to_close"))
        o = await self.eng.broker.wait_terminal(o.id, timeout_s=5, poll_s=0.01)
        return SimpleNamespace(filled=o.status == "filled", reason=o.status)


def test_oversized_stock_locked_by_short_call_closes_call_first(world, tmp_path, monkeypatch):
    """REQ-010 / A6: normalise trim and exit() both close the short call before selling shares."""
    a = world.alpaca
    # plan posted before the open (default clock), executed after open+5
    from datetime import date

    from tests.fakes import occ_symbol

    a.add_option_contract("JJJ", date(2026, 12, 18), "call", 320)
    occ = occ_symbol("JJJ", date(2026, 12, 18), "call", 320)
    world.price("JJJ", 300, spread=0.02)
    world.price("DDD", 100, spread=0.02)
    world.price(occ, 2.0, spread=0.02)
    a.seed_position("JJJ", 130, 250)   # far above 15% of equity
    a.seed_position("DDD", 31, 100)
    a.seed_position(occ, -1, 2.0)      # locks 100 JJJ shares
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    log = []
    eng.options = _CloseCalls(eng, log)
    plan = run(eng.plan_normalise())
    assert plan.index("close the short call on JJJ") < plan.index("trim JJJ to 15% of equity")
    st = run(eng.normalise_step())
    assert st.state == "planned"
    world.clock.set_et(2026, 11, 23, 9, 36)
    st = run(eng.normalise_step())
    assert st.state == "done", st
    assert log == [("close_call", occ)] and a.position(occ) is None
    acct = run(eng.broker.get_account())
    assert a.position("JJJ").qty * 300 <= 0.15 * float(acct.equity) + 300
    assert _no_shorts(a)

    # exit(): a new short call locks the remaining shares; a full model exit closes it first.
    a.seed_position(occ, -1, 2.0)
    run(eng.ledger.refresh("t"))
    log.clear()
    res = run(eng.execute_intent(enqueue(eng, "JJJ", "sell", rule="model")))
    assert log == [("close_call", occ)]
    assert res.status == IntentStatus.FILLED and a.position("JJJ") is None and a.position(occ) is None
    assert _no_shorts(a)


def test_accepted_then_rejected_buy_alerts(world, tmp_path, monkeypatch):
    """REQ-009: HTTP 200 on submit, then the order ends 'rejected' with nothing filled."""
    from stock_agent.broker import BrokerOrder

    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    orig = eng.broker.wait_terminal

    async def rejecting(order_id, **kw):
        o = await eng.broker.get_order(order_id)
        if o.side == "buy" and o.type != "stop":
            await eng.broker.cancel_order(order_id)
            return BrokerOrder.model_validate({**o.model_dump(), "status": "rejected", "filled_qty": 0})
        return await orig(order_id, **kw)

    monkeypatch.setattr(eng.broker, "wait_terminal", rejecting)
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.REJECTED and res.filled_qty == 0
    assert any("rejected by the broker" in t for _, t, _ in eng.alerts)


def test_drain_waits_until_a_fresh_fill_has_its_stop(world, tmp_path, monkeypatch):
    """REQ-055: SIGTERM drain must not report idle between a buy fill and its stop."""
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    seen = {}
    orig = eng._protect_symbol

    async def slow_protect(sym, **kw):
        seen["in_flight_ids"] = set(eng._in_flight)
        seen["drained"] = await eng.drain(timeout_s=0)  # SIGTERM arrives right here
        return await orig(sym, **kw)

    monkeypatch.setattr(eng, "_protect_symbol", slow_protect)
    res = run(eng.execute_intent(enqueue(eng, "AAA", "buy", ref=50)))
    assert res.status == IntentStatus.FILLED
    assert seen["in_flight_ids"] == set()    # the order itself is done...
    assert seen["drained"] is False          # ...but the workflow is not, so drain waits
    assert len(_stops(a, "AAA")) == 1
    assert run(eng.drain(timeout_s=0)) is True


def test_normalise_plan_lists_short_buyback_before_no_shorting(world, tmp_path, monkeypatch):
    """REQ-010(a): the posted plan matches _run_normalise's B3 order."""
    a = world.alpaca
    world.price("PPP", 100, spread=0.02)
    a.seed_position("PPP", -5, 100)
    a.configurations["no_shorting"] = False
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    plan = run(eng.plan_normalise())
    buy_back = next(i for i, s in enumerate(plan) if s.startswith("buy back the short in PPP"))
    assert buy_back < plan.index("turn shorting off at the broker (no_shorting) and read it back")


def _funnel(eng):
    return [e for e in eng.ledger.journal.read({"execution.buy_funnel"})]


def test_insufficient_buying_power_is_a_clean_skip_without_retry(world, tmp_path, monkeypatch):
    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)
    a.fail("/v2/orders", "status", method="POST", status=403,
           body={"code": 40310000, "message": "insufficient buying power"})
    it = enqueue(eng, "AAA", "buy", ref=50)
    res = run(eng.execute_intent(it))
    assert res.status == IntentStatus.SKIPPED and res.code == "insufficient_buying_power"
    cur = eng.ledger.get_intent(it.intent_id)
    assert cur.meta["skip_code"] == "insufficient_buying_power" and cur.meta["passed_risk"] is True
    assert cur.next_attempt_at is None and cur.status == IntentStatus.REJECTED  # terminal: no retry loop
    posts = len([r for r in world.orders_sent() if r.method == "POST"])
    world.clock.advance(minutes=10)
    run(eng.execute_due())
    assert len([r for r in world.orders_sent() if r.method == "POST"]) == posts
    stages = [(e["stage"], e.get("code")) for e in _funnel(eng)]
    assert ("passed_risk", None) in stages and ("skipped", "insufficient_buying_power") in stages


def test_every_skipped_or_held_buy_records_a_funnel_code(world, tmp_path, monkeypatch):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    world.price("BBB", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch, normalised=False)
    held = enqueue(eng, "AAA", "buy", ref=50)
    r = run(eng.execute_intent(held))
    assert r.code == "cleanup_pending"
    assert eng.ledger.get_intent(held.intent_id).meta["skip_code"] == "cleanup_pending"
    eng.ledger.store.kv_set(eng.NORMALISE_KEY, {"state": "done"})
    ran = enqueue(eng, "BBB", "buy", ref=40)  # price ran away from the analysed 40
    r = run(eng.execute_intent(ran))
    assert r.status == IntentStatus.SKIPPED and r.code == "price_ran_away"
    assert eng.ledger.get_intent(ran.intent_id).meta["skip_code"] == "price_ran_away"
    ok = enqueue(eng, "AAA", "buy", ref=50, key="t:AAA:ok")
    r = run(eng.execute_intent(ok))
    assert r.status == IntentStatus.FILLED
    stages = [e["stage"] for e in _funnel(eng) if e["intent_id"] == ok.intent_id]
    assert stages == ["passed_risk", "ordered", "filled"]
    assert set(eng.SKIP_CODES) >= {e["code"] for e in _funnel(eng) if e.get("code")}


def test_research_gate_outage_degrades_to_ledger_thesis(world, tmp_path, monkeypatch):
    """A14a: a broken submit-time recheck must not block every buy."""
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)

    class Broken:
        def is_entry_eligible(self, *a, **k):
            raise RuntimeError("gate down")

    eng._research_gate = Broken()
    it = enqueue(eng, "AAA", "buy", ref=50)
    from types import SimpleNamespace

    from dataclasses import replace
    it = replace(it, origin="weekly", thesis_ids=("th-1",))
    monkeypatch.setattr(eng.ledger, "current_thesis", lambda s: SimpleNamespace(
        thesis_id="th-1", actionable=True, payload={"draft": {"direction": "BUY"}}))
    assert run(eng._research_recheck(it, None)) is None
    monkeypatch.setattr(eng.ledger, "current_thesis", lambda s: SimpleNamespace(thesis_id="th-2"))
    assert "newer thesis" in run(eng._research_recheck(it, None))


def test_margin_trim_sells_fresh_qty_never_call_backed_shares_and_restops(world, tmp_path, monkeypatch):
    """A13e: margin_buffer trims go through exit(): replanned, stop resized, A6-locked shares kept."""
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace

    from stock_agent.risk_manager import TradeKind
    from tests.fakes import occ_symbol

    a = world.alpaca
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("JJJ", 100, spread=0.02)
    a.add_option_contract("JJJ", date(2026, 12, 18), "call", 120)
    occ = occ_symbol("JJJ", date(2026, 12, 18), "call", 120)
    world.price(occ, 1.0, spread=0.02)
    a.seed_position("JJJ", 160, 90)
    a.seed_position(occ, -1, 1.0)  # locks 100 shares
    eng = make_engine(world, tmp_path, monkeypatch)
    from stock_agent.broker import OrderRequest
    ext = run(eng.broker.submit_order(OrderRequest(  # someone else's resting sell: never touched
        symbol="JJJ", side="sell", qty=10, type="limit", time_in_force="gtc", limit_price=200,
        client_order_id="manual-ext-1", position_intent="sell_to_close")))
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    seen = {}

    def plan_margin_trim(snap, *, controls=None, force=False):
        seen["force"] = force
        return (SimpleNamespace(kind=TradeKind.SELL, symbol="JJJ", qty=Decimal(80)),)

    monkeypatch.setattr(eng.risk, "plan_margin_trim", plan_margin_trim, raising=False)
    eng._margin_controls = lambda snap: eng._risk_controls()
    for _ in range(5):  # uncapped: the model-exit cap does not apply
        eng._count_model_exit()
    it = enqueue(eng, "JJJ", "sell", rule="margin_buffer", qty=120)
    from dataclasses import replace
    it = replace(it, kind="trim", origin="margin_buffer")
    res = run(eng.exit(it))
    assert seen["force"] is True
    assert res.status == IntentStatus.FILLED and res.filled_qty == 50  # min(80 fresh, 120 queued, 50 free)
    assert a.position("JJJ").qty == 110 and a.position(occ) is not None
    assert run(eng.broker.get_order(ext.id)).status in ("new", "accepted")
    stops = _stops(a, "JJJ")
    assert len(stops) <= 1 and all(o.qty <= 100 for o in stops)
    assert a.position("JJJ").qty > 0


def _margin_setup(world, tmp_path, monkeypatch, qty=10, plan_qty=3):
    from decimal import Decimal
    from types import SimpleNamespace

    from stock_agent.risk_manager import TradeKind

    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 100, spread=0.02)
    world.alpaca.seed_position("AAA", qty, 90)
    eng = make_engine(world, tmp_path, monkeypatch)
    monkeypatch.setattr(eng.risk, "plan_margin_trim", lambda snap, *, controls=None, force=False: (
        SimpleNamespace(kind=TradeKind.SELL, symbol="AAA", qty=Decimal(plan_qty)),), raising=False)
    return eng


def _margin_intent(eng, **meta):
    from dataclasses import replace

    it = enqueue(eng, "AAA", "sell", rule="margin_buffer", **meta)
    return replace(it, kind="trim", origin="margin_buffer")


def test_margin_trim_keeps_external_sell_and_protects_the_free_rest(world, tmp_path, monkeypatch):
    from stock_agent.broker import OrderRequest

    a = world.alpaca
    eng = _margin_setup(world, tmp_path, monkeypatch)
    ext = run(eng.broker.submit_order(OrderRequest(
        symbol="AAA", side="sell", qty=2, type="limit", time_in_force="gtc", limit_price=150,
        client_order_id="manual-ext-2", position_intent="sell_to_close")))
    run(eng.ledger.refresh("t"))
    run(eng.ensure_protection())
    assert [o.qty for o in _stops(a, "AAA")] == [8]  # protected beside the external order
    res = run(eng.exit(_margin_intent(eng)))
    assert res.status == IntentStatus.FILLED and res.filled_qty == 3
    assert a.position("AAA").qty == 7
    assert run(eng.broker.get_order(ext.id)).status in ("new", "accepted")
    assert [o.qty for o in _stops(a, "AAA")] == [5]  # 7 held - 2 reserved by the external sell


def test_stale_margin_trim_from_an_earlier_episode_is_skipped(world, tmp_path, monkeypatch):
    a = world.alpaca
    eng = _margin_setup(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    eng.ledger.risk_state_set(eng.MARGIN_EPISODE_KEY, {"opened_at": "2026-11-23T15:00:00+00:00"})
    res = run(eng.exit(_margin_intent(eng, margin_episode="2026-11-20T15:00:00+00:00")))
    assert res.status == IntentStatus.SKIPPED and "earlier margin episode" in res.reason
    assert a.position("AAA").qty == 10
    res = run(eng.exit(_margin_intent(eng, margin_episode="2026-11-23T15:00:00+00:00")))
    assert res.status == IntentStatus.FILLED and a.position("AAA").qty == 7


def test_margin_trim_skips_when_its_symbol_is_no_longer_first(world, tmp_path, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    from stock_agent.risk_manager import TradeKind

    a = world.alpaca
    eng = _margin_setup(world, tmp_path, monkeypatch)
    run(eng.ledger.refresh("t"))
    monkeypatch.setattr(eng.risk, "plan_margin_trim", lambda snap, *, controls=None, force=False: (
        SimpleNamespace(kind=TradeKind.SELL, symbol="ZZZ", qty=Decimal(5)),
        SimpleNamespace(kind=TradeKind.SELL, symbol="AAA", qty=Decimal(3))), raising=False)
    res = run(eng.exit(_margin_intent(eng)))
    assert res.status == IntentStatus.SKIPPED and "ZZZ first" in res.reason
    assert a.position("AAA").qty == 10
