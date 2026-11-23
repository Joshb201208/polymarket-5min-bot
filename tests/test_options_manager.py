"""Options manager against FakeAlpaca through the real AlpacaClient."""

import asyncio
from datetime import date

import pytest

from stock_agent.options_book import OptionsStore, StructState
from stock_agent.options_manager import OptionCandidate
from stock_agent.options_settings import OptionsMode
from stock_agent.research_schemas import Direction
from stock_agent.risk_manager import TradeKind
from tests.fakes import occ_symbol
from tests.options_support import EXP, StubGate, StubRisk, make_manager, requote, seed_chain

C100 = occ_symbol("AAA", EXP, "call", 100)
C105 = occ_symbol("AAA", EXP, "call", 105)
P100 = occ_symbol("AAA", EXP, "put", 100)
P95 = occ_symbol("AAA", EXP, "put", 95)


def run(coro):
    return asyncio.run(coro)


def _open_spread(world, tmp_path, **kw):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = kw.pop("gate", None) or StubGate()
    if "AAA" not in gate.views:
        gate.set("AAA", Direction.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate, **kw)
    out = run(m.scan())
    return m, out


def set_value(world, value, short_mid=1.0, long_sym=C100, short_sym=C105):
    """Quote the spread so that long mid - short mid = value."""
    requote(world, short_sym, short_mid)
    requote(world, long_sym, short_mid + value)


def _mleg_posts(world):
    return [r for r in world.orders_sent() if r.json and r.json.get("order_class") == "mleg"]


# ------------------------------------------------------------------ opening
def test_bull_call_spread_opens_as_one_mleg_limit_order(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    assert out[0].opened, out
    posts = _mleg_posts(world)
    assert 1 <= len(posts) <= 3  # limit ladder from mid towards natural; earlier rungs cancelled
    prices = [float(p.json["limit_price"]) for p in posts]
    assert prices == sorted(prices) and len(set(p.json["client_order_id"] for p in posts)) == len(posts)
    body = posts[-1].json
    assert body["type"] == "limit" and float(body["limit_price"]) > 0  # positive = debit
    assert body["client_order_id"].startswith("sa-opt-open-")
    assert [(lg["symbol"], lg["side"], lg["position_intent"]) for lg in body["legs"]] == [
        (C100, "buy", "buy_to_open"), (C105, "sell", "sell_to_open")]
    st = m.get(out[0].opened)
    assert st.st is StructState.OPEN and st.strategy == "bull_call_spread"
    a = world.alpaca
    assert a.position(C100).qty == st.qty and a.position(C105).qty == -st.qty
    # entry debit from the leg fills (ask of long - bid of short)
    q100, q105 = a.quotes[C100], a.quotes[C105]
    assert st.entry_debit == pytest.approx(q100.ask - q105.bid)
    assert st.max_loss == pytest.approx(st.entry_debit * 100 * st.qty)
    assert st.max_loss <= 0.03 * a.equity() + 1e-6
    assert len({lg.expiry for lg in st.legs}) == 1
    kinds = m.journal.kinds()
    assert "options.open_submitted" in kinds and "options.opened" in kinds
    opened = dict(m.journal.events)["options.opened"]
    assert opened["thesis_id"] == "th-AAA" and opened["direction"] == "bullish"


def test_risk_intent_and_commitments(world, tmp_path):
    risk = StubRisk()
    m, out = _open_spread(world, tmp_path, risk=risk)
    it = risk.intents[0]
    assert it.kind is TradeKind.OPTION_OPEN and it.symbol == "AAA"
    assert it.option_max_loss > 0 and it.option_cash_debit == it.option_max_loss
    assert it.effective_conviction == 8 and it.minimum_conviction == 8 and it.structure_id
    assert risk.controls[0].option_commitments == ()
    c = m.commitments()
    assert len(c) == 1 and c[0].structure_id == out[0].opened


def test_risk_rejection_sends_nothing(world, tmp_path):
    m, out = _open_spread(world, tmp_path, risk=StubRisk(ok=False, reasons=("cash",)))
    assert out[0].opened is None and "risk" in out[0].reason
    assert world.orders_sent() == []


def test_bear_put_spread_for_bearish_thesis(world, tmp_path):
    gate = StubGate()
    gate.set("AAA", Direction.BEARISH, 8)
    m, out = _open_spread(world, tmp_path, gate=gate)
    assert out[0].opened
    legs = _mleg_posts(world)[0].json["legs"]
    assert [(lg["symbol"], lg["side"]) for lg in legs] == [(P100, "buy"), (P95, "sell")]
    assert world.alpaca.position("AAA") is None  # never short stock for a bearish view


def test_high_conviction_long_call_is_simple_limit(world, tmp_path):
    gate = StubGate()
    gate.set("AAA", Direction.BUY, 9)
    m, out = _open_spread(world, tmp_path, gate=gate)
    st = m.get(out[0].opened)
    assert st.strategy == "long_call"
    body = world.orders_sent()[-1].json
    assert body["symbol"] == C100 and body["type"] == "limit" and body["position_intent"] == "buy_to_open"


@pytest.mark.parametrize("view", [
    dict(direction=Direction.BUY, conviction=7),
    dict(direction=Direction.HOLD, conviction=9),
    dict(direction=Direction.BUY, conviction=9, ok=False, reasons=("stale_or_invalid_thesis",)),
])
def test_gate_blocks_entries(world, tmp_path, view):
    gate = StubGate()
    gate.set("AAA", **view)
    m, out = _open_spread(world, tmp_path, gate=gate)
    assert out[0].opened is None
    assert world.orders_sent() == []


@pytest.mark.parametrize("mode", [OptionsMode.OFF, OptionsMode.CLOSE_ONLY])
def test_modes_without_opens_send_nothing(world, tmp_path, mode):
    m, out = _open_spread(world, tmp_path, mode=mode)
    assert out[0].opened is None and mode.value in out[0].reason
    assert world.orders_sent() == []


def test_no_entry_in_opening_buffer_or_closed_market(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 9, 32)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA")
    m = make_manager(world, tmp_path, gate=gate)
    assert run(m.scan())[0].opened is None
    world.clock.set_et(2026, 11, 23, 17, 0)
    assert run(m.scan())[0].opened is None
    assert world.orders_sent() == []


def test_stale_underlying_quote_blocks(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    world.clock.set_et(2026, 11, 23, 10, 10)
    gate = StubGate()
    gate.set("AAA")
    m = make_manager(world, tmp_path, gate=gate)
    out = run(m.scan())
    assert out[0].opened is None and "stale" in out[0].reason


def test_unfilled_open_is_cancelled_and_voided(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA")
    m = make_manager(world, tmp_path, gate=gate)
    # the market moves away right after the quote is read: the limit no longer fills
    orig = m.broker.submit_raw_order

    async def submit(body):
        requote(world, C100, world.alpaca.quotes[C100].last + 1.0)
        return await orig(body)
    m.broker.submit_raw_order = submit
    out = run(m.scan())
    assert out[0].opened is None and "not filled" in out[0].reason
    assert world.cancels_sent()
    assert m.structures[0].st is StructState.VOID
    assert world.alpaca.position(C100) is None and world.alpaca.position(C105) is None
    assert m.open_blocker() is None


def test_one_structure_per_underlying_and_budget(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    assert out[0].opened
    world.clock.advance(minutes=1)
    seed_chain(world)
    again = run(m.scan())
    assert again[0].opened is None and "already holds" in again[0].reason
    assert len([p for p in _mleg_posts(world) if p.json["legs"][0]["position_intent"] == "buy_to_open"]) <= 3


def test_news_candidate_once_per_story_and_bounded(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 11, 0)
    gate = StubGate()
    for und in ("AAA", "BBB", "CCC"):
        seed_chain(world, und)
        gate.set(und, Direction.BEARISH, 8)
    m = make_manager(world, tmp_path, gate=gate, universe=())
    r1 = run(m.scan(candidates=[OptionCandidate("AAA", story_id="s1", direction_hint="bearish")]))
    assert r1[0].opened
    r_dup = run(m.scan(candidates=[OptionCandidate("BBB", story_id="s1", direction_hint="bearish")]))
    assert r_dup[0].opened is None and "story" in r_dup[0].reason
    r_wrong = run(m.scan(candidates=[OptionCandidate("BBB", story_id="s2", direction_hint="bullish")]))
    assert r_wrong[0].opened is None and "disagrees" in r_wrong[0].reason
    r2 = run(m.scan(candidates=[OptionCandidate("BBB", story_id="s3")]))
    assert r2[0].opened
    seed_chain(world, "CCC")
    r3 = run(m.scan(candidates=[OptionCandidate("CCC", story_id="s4")]))
    assert r3[0].opened is None and "used up" in r3[0].reason


def test_cooldown_blocks_reentry_after_loss(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    st = m.get(out[0].opened)
    world.clock.advance(minutes=30)
    world.price("AAA", 95, spread=0.02)
    set_value(world, st.entry_debit * 0.3, short_mid=0.5)
    res = run(m.monitor())
    assert res.exits and res.exits[0][1] == "loss_limit"
    assert st.st is StructState.CLOSED and st.realized_pnl < 0
    cd = m.cooldowns["AAA"]
    assert cd.consecutive_losses == 1 and cd.until > date(2026, 12, 1)
    seed_chain(world)
    again = run(m.scan())
    assert again[0].opened is None and "cool-down" in again[0].reason


# -------------------------------------------------------------------- exits
def test_profit_target_closes_with_credit_mleg(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    st = m.get(out[0].opened)
    world.clock.advance(minutes=30)
    world.price("AAA", 104, spread=0.02)
    set_value(world, st.entry_debit + 0.6 * (5 - st.entry_debit), short_mid=2.0)
    res = run(m.monitor())
    assert res.exits[0][1] == "profit_target", res
    close = _mleg_posts(world)[-1].json
    assert float(close["limit_price"]) < 0  # negative = credit
    assert [(lg["side"], lg["position_intent"]) for lg in close["legs"]] == [
        ("sell", "sell_to_close"), ("buy", "buy_to_close")]
    assert st.st is StructState.CLOSED and st.realized_pnl > 0
    assert world.alpaca.position(C100) is None and world.alpaca.position(C105) is None
    ev = dict(m.journal.events)["options.closed"]
    assert ev["reason"] == "profit_target" and ev["realized_pnl"] == st.realized_pnl
    assert "AAA" not in m.cooldowns or m.cooldowns["AAA"].until is None


def test_time_exit_at_t_minus_2(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    st = m.get(out[0].opened)
    world.clock.set_et(2026, 12, 18, 10, 0)  # sessions after: 21, 22, 23, 24 -> 4 left
    seed_chain(world)
    assert run(m.monitor()).exits == []
    world.clock.set_et(2026, 12, 22, 10, 0)  # 23, 24 -> 2 left
    seed_chain(world)
    res = run(m.monitor())
    assert res.exits and res.exits[0][1] == "time_exit"
    assert st.st is StructState.CLOSED


def test_started_inside_t2_closes_after_open_plus_5(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    st = m.get(out[0].opened)
    world.clock.set_et(2026, 12, 23, 8, 0)  # bot starts before the open, 1 session left
    seed_chain(world)
    res = run(m.monitor())
    assert res.skipped and st.st is StructState.OPEN
    world.clock.set_et(2026, 12, 23, 9, 32)
    seed_chain(world)
    assert run(m.monitor()).skipped
    world.clock.set_et(2026, 12, 23, 9, 36)
    seed_chain(world)
    res = run(m.monitor())
    assert res.exits[0][1] == "time_exit"
    assert st.st is StructState.CLOSED
    assert world.alpaca.position(C100) is None


def test_thesis_break_exit(world, tmp_path):
    gate = StubGate()
    gate.set("AAA", Direction.BUY, 8)
    m, out = _open_spread(world, tmp_path, gate=gate)
    world.clock.advance(minutes=20)
    seed_chain(world)
    gate.set("AAA", Direction.BEARISH, 8)
    res = run(m.monitor())
    assert res.exits[0][1] == "thesis_break"


def test_ex_dividend_closes_itm_short_call(world, tmp_path):
    m, out = _open_spread(world, tmp_path, ex_dividend=lambda und: (date(2026, 11, 24), 1.0))
    st = m.get(out[0].opened)
    world.clock.advance(minutes=20)
    world.price("AAA", 106, spread=0.02)
    set_value(world, st.entry_debit + 0.1, short_mid=3.0)  # short call extrinsic 2.0 > dividend
    assert run(m.monitor()).exits == []
    set_value(world, st.entry_debit + 0.1, short_mid=1.3)  # extrinsic 0.3 < dividend 1.0
    res = run(m.monitor())
    assert res.exits[0][1] == "ex_dividend"
    assert st.st is StructState.CLOSED


def test_unknown_ex_dividend_closes_only_itm_short_call(world, tmp_path):
    m, out = _open_spread(world, tmp_path)  # no ex-dividend source
    st = m.get(out[0].opened)
    world.clock.advance(minutes=20)
    seed_chain(world)
    assert run(m.monitor()).exits == []  # short call out of the money
    world.price("AAA", 106, spread=0.02)
    set_value(world, st.entry_debit + 0.1, short_mid=3.0)
    assert run(m.monitor()).exits[0][1] == "ex_dividend"


def test_unfilled_close_retries_more_aggressively(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    st = m.get(out[0].opened)
    world.clock.advance(minutes=30)
    world.price("AAA", 104, spread=0.02)
    target = st.entry_debit + 0.6 * (5 - st.entry_debit)
    set_value(world, target, short_mid=2.0)
    orig = m.broker.submit_raw_order
    calls = []
    moving = [True]

    async def submit(body):
        calls.append(body)
        if moving[0]:  # the long leg's bid drops further each time a close is sent
            requote(world, C100, 2.0 + target, half=0.5 + 0.5 * len(calls))
        return await orig(body)
    m.broker.submit_raw_order = submit
    run(m.monitor())
    assert st.st is StructState.OPEN and st.close_attempts == 1
    assert len(calls) == 3 and world.alpaca.position(C100) is not None
    moving[0] = False
    set_value(world, target, short_mid=2.0)
    run(m.monitor())
    assert st.st is StructState.CLOSED
    assert len({c["client_order_id"] for c in calls}) == len(calls)


# ------------------------------------------------------------- reconcile
def test_failed_reconcile_changes_nothing(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    before = (tmp_path / "options_book.json").read_text()
    world.alpaca.fail("/v2/positions", "5xx", times=None)
    res = run(m.monitor())
    assert "reconcile failed" in res.skipped
    assert (tmp_path / "options_book.json").read_text() == before
    assert m.get(out[0].opened).st is StructState.OPEN


def test_missing_legs_without_evidence_become_unknown_and_block(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    st = m.get(out[0].opened)
    a = world.alpaca
    del a.positions[C100]
    del a.positions[C105]
    rep = run(m.reconcile())
    assert st.st is StructState.UNKNOWN and st.realized_pnl is None
    assert st.id in rep.unknown
    assert m.open_blocker()
    seed_chain(world, "BBB")
    m.gate.set("BBB")
    m._universe = lambda: ["BBB"]
    assert run(m.scan())[0].opened is None


def test_expiry_evidence_closes_with_pnl(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    st = m.get(out[0].opened)
    world.price("AAA", 90, spread=0.02)
    a = world.alpaca
    a.settle_option_expiry(C100)
    a.settle_option_expiry(C105)
    rep = run(m.reconcile())
    assert st.id in rep.closed and st.st is StructState.CLOSED
    assert st.realized_pnl == pytest.approx(-st.entry_debit * 100 * st.qty)
    assert st.exit_reason == "expired"


def test_early_assignment_short_call_repairs(world, tmp_path):
    covers = []
    m, out = _open_spread(world, tmp_path, covers=covers)
    st = m.get(out[0].opened)
    world.clock.advance(minutes=20)
    world.price("AAA", 112, spread=0.02)
    requote(world, C100, 12.3)
    requote(world, C105, 7.3)
    world.alpaca.assign_early(C105)
    assert world.alpaca.position("AAA").qty < 0  # short stock appeared
    res = run(m.monitor())
    assert ("AAA", "assignment") in covers  # one owner for covers: the execution engine
    assert st.st is StructState.CLOSED  # orphan long leg sold under the repair rule
    assert world.alpaca.position(C100) is None
    assert any("repair" in k for k in m.journal.kinds())
    assert res.repairs


def test_adopts_existing_broker_spreads_and_lone_short_call(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    a = world.alpaca
    exp = date(2026, 11, 27)
    world.price("GGG", 930, spread=0.1)
    for k in (920, 940):
        a.add_option_contract("GGG", exp, "call", k)
    lng, sht = occ_symbol("GGG", exp, "call", 920), occ_symbol("GGG", exp, "call", 940)
    a.set_option_quote(lng, 14.0, 14.4)
    a.set_option_quote(sht, 5.0, 5.4)
    a.seed_position(lng, 1, 12.0)
    a.seed_position(sht, -1, 6.0)
    world.price("JJJ", 350, spread=0.05)
    a.seed_position("JJJ", 100, 300.0)
    a.add_option_contract("JJJ", date(2026, 12, 11), "call", 340)
    jc = occ_symbol("JJJ", date(2026, 12, 11), "call", 340)
    a.set_option_quote(jc, 12.0, 12.4)
    a.seed_position(jc, -1, 8.0)
    m = make_manager(world, tmp_path, universe=())
    rep = run(m.reconcile())
    assert len(rep.adopted) == 2
    spread = next(s for s in m.structures if s.underlying == "GGG")
    assert spread.strategy == "bull_call_spread" and spread.entry_debit == pytest.approx(6.0)
    assert spread.width == 20 and spread.source == "adopted"
    lone = next(s for s in m.structures if s.underlying == "JJJ")
    assert lone.st is StructState.REPAIR
    assert m.open_blocker()  # repair pending blocks new opens
    # T-2 for the 27 Nov spread: 24, 25, 27 -> 3 sessions left on Monday; 2 on Tuesday
    world.clock.set_et(2026, 11, 24, 9, 40)
    a.set_option_quote(lng, 14.0, 14.4)
    a.set_option_quote(sht, 5.0, 5.4)
    a.set_option_quote(jc, 12.0, 12.4)
    world.price("GGG", 930, spread=0.1)
    res = run(m.monitor())
    assert spread.st is StructState.CLOSED and spread.exit_reason == "adopted_near_expiry"
    assert a.position(lng) is None and a.position(sht) is None
    assert lone.st is StructState.CLOSED  # orphan short bought back
    assert a.position("JJJ").qty == 100
    assert res.repairs


def test_close_short_call_for_normalise(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    a = world.alpaca
    world.price("JJJ", 350, spread=0.05)
    a.seed_position("JJJ", 100, 300.0)
    a.add_option_contract("JJJ", date(2026, 12, 11), "call", 340)
    jc = occ_symbol("JJJ", date(2026, 12, 11), "call", 340)
    a.set_option_quote(jc, 12.0, 12.4)
    a.seed_position(jc, -1, 8.0)
    m = make_manager(world, tmp_path, universe=())
    r = run(m.close_short_call(jc))
    assert r.filled and r.filled_qty == 1 and r.avg_price == pytest.approx(12.4) and r.order_id
    body = world.orders_sent()[-1].json
    assert body["side"] == "buy" and body["position_intent"] == "buy_to_close" and body["type"] == "limit"
    assert a.position(jc) is None and a.position("JJJ").qty == 100
    r2 = run(m.close_short_call(jc))
    assert not r2.filled and r2.status == "no_position"


def test_off_mode_still_repairs_but_does_not_exit(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    st = m.get(out[0].opened)
    m.settings = type(m.settings)(**{**m.settings.__dict__, "mode": OptionsMode.OFF})
    world.clock.advance(minutes=30)
    requote(world, C100, 1.0)
    requote(world, C105, 0.6)
    res = run(m.monitor())
    assert res.exits == [] and st.st is StructState.OPEN


def test_book_survives_restart_and_pending_is_resolved(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    m2 = make_manager(world, tmp_path)
    assert [s.id for s in m2.live()] == [out[0].opened]
    # a crash right after submit leaves a pending record; restart resolves it from the broker
    st = m2.get(out[0].opened)
    st.state = StructState.PENDING.value
    m2._save()
    m3 = make_manager(world, tmp_path)
    run(m3.reconcile())
    assert m3.get(out[0].opened).st is StructState.OPEN


def test_corrupt_book_is_quarantined_and_rebuilt(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    (tmp_path / "options_book.json").write_text("{not json")
    m2 = make_manager(world, tmp_path)
    assert m2.load_error and m2.open_blocker()
    assert list(tmp_path.glob("options_book.corrupt-*"))
    run(m2.reconcile())
    assert len(m2.live()) == 1 and m2.live()[0].source == "adopted"
    assert m2.open_blocker() is None


def test_store_round_trip(tmp_path):
    s = OptionsStore(tmp_path / "b.json")
    assert s.load() == ([], {}, {})


def test_brief_lines(world, tmp_path):
    m, out = _open_spread(world, tmp_path)
    lines = m.brief_lines()
    assert lines[0].startswith("Options: mode defined_risk, 1 live")
    assert any("bull_call_spread" in ln for ln in lines)


def test_news_story_kept_on_transient_denial_and_acked_on_final(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 11, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", ok=False, reasons=("stale_quote",))
    m = make_manager(world, tmp_path, gate=gate, universe=())
    r = run(m.scan(candidates=[OptionCandidate("AAA", story_id="n1")]))
    assert r[0].final is False and r[0].story_handled is False and not m.is_story_handled("n1")
    gate.set("AAA", Direction.BUY, 6)  # thesis now healthy but conviction too low: a final answer
    r = run(m.scan(candidates=[OptionCandidate("AAA", story_id="n1")]))
    assert r[0].final and r[0].story_handled and m.handled_story_ids(["n1", "n2"]) == {"n1"}
    # durable across a restart
    m2 = make_manager(world, tmp_path, gate=gate, universe=())
    assert m2.is_story_handled("n1")


def test_invalid_risk_state_refuses_entries(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA")
    m = make_manager(world, tmp_path, gate=gate)

    def broken():
        raise ValueError("drawdown record invalid")
    m._risk_controls = broken
    out = run(m.scan())
    assert out[0].opened is None and "risk state" in out[0].reason
    assert not _mleg_posts(world)
