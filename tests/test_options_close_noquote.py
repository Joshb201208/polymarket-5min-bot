"""Closing options without usable quotes: never type=market, intrinsic-based limits,
back-off, a per-session cap, one alert per structure per session.

Live 25 Nov 2026: Alpaca rejected the inherited GS/JPM closes, sent at market, with
"order has been rejected due to no available quote for symbol. please reenter with a
limit order" on every 30 s monitor pass.
"""

import asyncio
from datetime import date

from stock_agent.broker import BrokerError
from stock_agent.options_book import StructState
from stock_agent.options_policy import ExitReason, fallback_close_limit
from tests.fakes import occ_symbol
from tests.options_support import EXP, make_manager, seed_chain

NOQ = "order has been rejected due to no available quote for symbol. please reenter with a limit order"
FRI = date(2026, 11, 27)  # half day, early close 13:00 ET


def run(coro):
    return asyncio.run(coro)


class Recorder:
    """Wraps submit_raw_order: records every body; rejects what Alpaca rejects without a quote."""

    def __init__(self, m, *, reject_zero=False, reject_all_limits=False):
        self.bodies: list[dict] = []
        self._real = m.broker.submit_raw_order
        self.reject_zero = reject_zero
        self.reject_all_limits = reject_all_limits
        m.broker.submit_raw_order = self

    async def __call__(self, body):
        self.bodies.append(dict(body))
        if body.get("type") == "market":
            raise BrokerError("rejected", NOQ, status=403)
        if self.reject_all_limits:
            raise BrokerError("rejected", NOQ, status=403)
        if self.reject_zero and body.get("limit_price") in ("0.00", "-0.00"):
            raise BrokerError("rejected", "invalid limit price", status=422)
        return await self._real(body)


def _drop_quotes(a, *syms):
    for s in syms:
        a.quotes.pop(s, None)


def _adopt_spread(world, tmp_path, *, spot, long_k, short_k, und="AAA", exp=EXP, **kw):
    a = world.alpaca
    world.price(und, spot, spread=0.02)
    for k in (long_k, short_k):
        a.add_option_contract(und, exp, "call", k, open_interest=100)
    lo, sh = occ_symbol(und, exp, "call", long_k), occ_symbol(und, exp, "call", short_k)
    a.seed_position(lo, 1, 6.0)
    a.seed_position(sh, -1, 0.5)
    m = make_manager(world, tmp_path, universe=(), fill_wait_seconds=10, **kw)
    run(m.reconcile())
    _drop_quotes(a, lo, sh)
    return m, lo, sh


# ---------------------------------------------------------------- policy


class _L:
    def __init__(self, side, type_, strike, ratio=1):
        self.side, self.type, self.strike, self.ratio = side, type_, strike, ratio

    @property
    def sign(self):
        return 1 if self.side == "buy" else -1


KW = dict(haircut=0.10, cushion_pct=0.02, cushion_min=0.10, deep_otm_pct=0.05)


def test_fallback_lone_itm_long_is_haircut_intrinsic_rounded_down():
    fb = fallback_close_limit([_L("buy", "call", 100)], 112.37, debit_vertical=False, **KW)
    assert fb.credit == 11.13  # floor(12.37 * 0.9 = 11.133)
    assert not fb.deep_otm
    fb = fallback_close_limit([_L("buy", "put", 100)], 140, debit_vertical=False, **KW)
    assert fb.credit == 0.01 and fb.deep_otm  # worthless long: never below 0.01, never pays


def test_fallback_lone_short_pays_intrinsic_plus_cushion():
    fb = fallback_close_limit([_L("sell", "call", 100)], 105, debit_vertical=False, **KW)
    assert fb.credit == -(5 + 2.10)  # intrinsic 5 + max(0.10, 2% of 105)
    fb = fallback_close_limit([_L("sell", "call", 100)], 3, debit_vertical=False, **KW)
    assert fb.credit == -0.10


def test_fallback_debit_spread_never_pays():
    legs = [_L("buy", "call", 920), _L("sell", "call", 940)]
    fb = fallback_close_limit(legs, 780, debit_vertical=True, **KW)
    assert fb.credit == 0.0 and fb.deep_otm
    fb = fallback_close_limit(legs, 950, debit_vertical=True, **KW)
    assert fb.credit == 18.0 and not fb.deep_otm  # 20 wide, 10% haircut


# ---------------------------------------------------------------- manager


def test_noquote_adopted_spread_sends_mleg_limit_from_intrinsic_never_market(world, tmp_path):
    world.clock.set_et(2026, 12, 22, 10, 0)  # 2 trading days to EXP: adopted expiry exit
    m, lo, sh = _adopt_spread(world, tmp_path, spot=112.0, long_k=100, short_k=105)
    rec = Recorder(m)
    world.price("AAA", 112.0, spread=0.02)
    run(m.monitor())
    assert rec.bodies, "an expiry-driven close must still be tried"
    b = rec.bodies[0]
    assert b["type"] == "limit" and b["order_class"] == "mleg"
    assert b["limit_price"] == "-4.50"  # credit: 5 intrinsic less 10%
    assert all(x.get("type") != "market" for x in rec.bodies)


def test_deep_otm_zero_rejected_then_one_cent_once_then_stop(world, tmp_path):
    world.clock.set_et(2026, 12, 23, 10, 0)
    m, lo, sh = _adopt_spread(world, tmp_path, spot=60.0, long_k=100, short_k=105)
    rec = Recorder(m, reject_zero=True)
    for _ in range(20):
        world.price("AAA", 60.0, spread=0.02)
        run(m.monitor())
        world.clock.advance(minutes=1)
    prices = [b["limit_price"] for b in rec.bodies]
    # 0.00 rejected -> the 0.01-credit form in the same try, then only that form
    assert prices[0] == "0.00" and set(prices[1:]) == {"-0.01"}, prices
    st = m.structures[0]
    assert st.close_zero_form in ("credit_0.01", "stopped")
    infos = [x for x in m.alerts.sent if "AAA" in x[1]]
    assert len(infos) == 1 and infos[0][0] == "info", m.alerts.sent


def test_zero_form_both_rejected_stops_for_the_session(world, tmp_path):
    world.clock.set_et(2026, 12, 23, 10, 0)
    m, lo, sh = _adopt_spread(world, tmp_path, spot=60.0, long_k=100, short_k=105)
    rec = Recorder(m, reject_all_limits=True)
    for _ in range(30):
        world.price("AAA", 60.0, spread=0.02)
        run(m.monitor())
        world.clock.advance(minutes=1)
    assert [b["limit_price"] for b in rec.bodies] == ["0.00", "-0.01"]
    assert m.structures[0].close_zero_form == "stopped"
    assert len(m.alerts.sent) == 1


def test_backoff_and_session_cap_then_reset_next_session(world, tmp_path):
    world.clock.set_et(2026, 12, 22, 9, 31)
    m, lo, sh = _adopt_spread(world, tmp_path, spot=112.0, long_k=100, short_k=105)
    rec = Recorder(m)  # accepted, never fills (no quotes at the fake), cancelled after the wait
    times = []
    for _ in range(120):  # two hours of 1-minute passes
        world.price("AAA", 112.0, spread=0.02)
        n = len(rec.bodies)
        run(m.monitor())
        if len(rec.bodies) > n:
            times.append(world.clock.now())
        world.clock.advance(minutes=1)
    assert len(times) == 6, times
    gaps = [round((b - a).total_seconds() / 60) for a, b in zip(times, times[1:])]
    assert gaps == [1, 2, 4, 8, 15], gaps
    assert len(m.alerts.sent) == 1 and m.alerts.sent[0][0] == "critical"
    world.clock.set_et(2026, 12, 23, 9, 45)
    world.price("AAA", 112.0, spread=0.02)
    n = len(rec.bodies)
    run(m.monitor())
    assert len(rec.bodies) == n + 1  # a new session gets new tries
    assert len(m.alerts.sent) == 2  # and at most one new alert


def test_one_alert_per_session_survives_restart(world, tmp_path):
    world.clock.set_et(2026, 12, 22, 10, 0)
    m, lo, sh = _adopt_spread(world, tmp_path, spot=112.0, long_k=100, short_k=105)
    Recorder(m)
    world.price("AAA", 112.0, spread=0.02)
    run(m.monitor())
    assert len(m.alerts.sent) == 1
    m2 = make_manager(world, tmp_path, universe=(), fill_wait_seconds=10)
    Recorder(m2)
    for _ in range(40):
        world.clock.advance(minutes=1)
        world.price("AAA", 112.0, spread=0.02)
        run(m2.reconcile())
        run(m2.monitor())
    assert m2.alerts.sent == [], m2.alerts.sent
    st = m2.structures[0]
    assert st.close_tries_session == 6


def test_urgent_with_usable_quotes_is_a_limit_at_natural(world, tmp_path):
    world.clock.set_et(2026, 12, 23, 10, 0)  # 1 trading day left: urgent
    a = seed_chain(world)
    lo, sh = occ_symbol("AAA", EXP, "call", 100), occ_symbol("AAA", EXP, "call", 105)
    a.seed_position(lo, 1, 3.0)
    a.seed_position(sh, -1, 2.0)
    m = make_manager(world, tmp_path, universe=(), fill_wait_seconds=10)
    run(m.reconcile())
    rec = Recorder(m)
    run(m.monitor())
    b = rec.bodies[0]
    assert b["type"] == "limit"
    assert b["limit_price"] == "-0.50"  # natural: 100C bid 2.95 - 105C ask 2.45
    assert m.structures[0].st is StructState.CLOSED


def test_no_spot_sends_nothing_and_alerts_once(world, tmp_path):
    world.clock.set_et(2026, 12, 22, 10, 0)
    m, lo, sh = _adopt_spread(world, tmp_path, spot=112.0, long_k=100, short_k=105)
    rec = Recorder(m)
    world.clock.advance(minutes=3)  # the underlying quote goes stale too
    for _ in range(30):
        world.clock.advance(minutes=1)
        run(m.monitor())
    assert rec.bodies == []
    assert len(m.alerts.sent) == 1 and m.alerts.sent[0][0] == "critical"
    assert m.structures[0].close_tries_session <= 6


def test_non_expiry_exit_without_quote_sends_nothing(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    m, lo, sh = _adopt_spread(world, tmp_path, spot=112.0, long_k=100, short_k=105)
    rec = Recorder(m)
    res = run(m._close(m.structures[0], ExitReason.MANUAL, world.clock.now()))
    assert res == "no usable quote" and rec.bodies == []


def test_close_short_call_without_ask_uses_limit_not_market(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    a = world.alpaca
    world.price("AAA", 104.0, spread=0.02)
    a.add_option_contract("AAA", EXP, "call", 100, open_interest=100)
    sh = occ_symbol("AAA", EXP, "call", 100)
    a.seed_position(sh, -1, 2.0)
    m = make_manager(world, tmp_path, universe=(), fill_wait_seconds=10)
    rec = Recorder(m)
    _drop_quotes(a, sh)
    run(m.close_short_call(sh))
    assert rec.bodies and rec.bodies[0]["type"] == "limit"
    assert rec.bodies[0]["limit_price"] == "6.08"  # 4 intrinsic + 2% of 104
    world.alpaca.quotes.pop("AAA", None)
    world.clock.advance(minutes=10)
    n = len(rec.bodies)
    r = run(m.close_short_call(sh))
    assert len(rec.bodies) == n and r.status == "refused"


# ---------------------------------------------------------------- live scenario


def test_friday_half_day_inherited_gs_jpm_bounded_and_silent(world, tmp_path):
    """The two live structures on Fri 27 Nov (half day): GS 920/940 call spread, spot ~780;
    lone long JPM 345 call, spot ~312. No or stale option quotes all day."""
    a = world.alpaca
    world.clock.set_et(2026, 11, 25, 15, 0)
    world.price("GS", 780.0, spread=0.05)
    world.price("JPM", 312.0, spread=0.02)
    gs_l, gs_s = occ_symbol("GS", FRI, "call", 920), occ_symbol("GS", FRI, "call", 940)
    jpm = occ_symbol("JPM", FRI, "call", 345)
    for und, k in (("GS", 920), ("GS", 940), ("JPM", 345)):
        a.add_option_contract(und, FRI, "call", k, open_interest=100)
    a.set_option_quote(gs_l, 0.01, 0.05)  # stale by Friday
    a.set_option_quote(gs_s, 0.01, 0.05)
    a.seed_position(gs_l, 1, 11.0)
    a.seed_position(gs_s, -1, 0.0)
    a.seed_position(jpm, 1, 4.96)
    a.behaviour.expiry_day_liquidation_time = None
    m = make_manager(world, tmp_path, universe=(), fill_wait_seconds=10)
    run(m.reconcile())
    assert len(m.live()) == 2
    _drop_quotes(a, jpm)  # JPM: no quote at all; GS: quotes from Wednesday
    rec = Recorder(m, reject_zero=True)
    world.clock.set_et(2026, 11, 27, 9, 30)
    for _ in range(4 * 60):  # 09:30 -> 13:30 ET, a pass a minute
        world.price("GS", 780.0, spread=0.05)
        world.price("JPM", 312.0, spread=0.02)
        run(m.reconcile())
        run(m.monitor())
        world.clock.advance(minutes=1)
    bodies = rec.bodies
    assert bodies, "the bounded tries are still made"
    assert all(b.get("type") == "limit" for b in bodies)  # never market
    for b in bodies:
        if b.get("order_class") == "mleg":
            assert float(b["limit_price"]) <= 0  # credit only: never pays a debit
            assert {lg["position_intent"] for lg in b["legs"]} == {"sell_to_close", "buy_to_close"}
        else:
            assert b["side"] == "sell" and b["position_intent"] == "sell_to_close"
            assert b["limit_price"] == "0.01"
    gs = [b for b in bodies if b.get("order_class") == "mleg"]
    jp = [b for b in bodies if b.get("symbol") == jpm]
    # six tries each; GS's first try is 0.00 (rejected) then the 0.01-credit form
    assert len(jp) == 6 and len(gs) == 7, (len(gs), len(jp))
    assert [b["limit_price"] for b in gs] == ["0.00"] + ["-0.01"] * 6
    assert len(m.alerts.sent) == 2, m.alerts.sent  # one note per structure
    assert all(sev == "info" for sev, _, _ in m.alerts.sent)
    n = len(bodies)
    for _ in range(30):  # after the close: silence
        run(m.monitor())
        world.clock.advance(minutes=1)
    assert len(rec.bodies) == n and len(m.alerts.sent) == 2
