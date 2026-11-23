"""Characterisation tests: today's code against the fake world.

Each test states the CORRECT behaviour and is marked
`xfail(strict=True, raises=AssertionError)` while today's code gets it
wrong. When a builder fixes the finding, the test starts passing, strict
xfail turns that into a failure, and the builder removes the marker so the
test becomes a plain regression test. `raises=AssertionError` means that a
crash (for example a renamed method) is reported as an error, not hidden
as an expected failure.

Findings: docs/revamp/audit/findings/*.md.
"""

import asyncio
from datetime import date, datetime

import pytest

from tests.fakes import ET, occ_symbol


def finding(fid: str, why: str):
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{fid}: {why}")


def run(coro):
    return asyncio.run(coro)


def et(*args) -> datetime:
    return datetime(*args, tzinfo=ET)


def seed_held(world, symbol="AAA", shares=10, entry=100.0, stop=93.0, *, with_stop=True):
    """Seed a broker position for the real ledger reconcile to adopt."""
    world.price(symbol, entry)
    world.alpaca.seed_position(symbol, shares, entry)
    if with_stop:
        world.alpaca.submit({"symbol": symbol, "qty": str(shares), "side": "sell", "type": "stop",
                             "stop_price": str(stop), "time_in_force": "gtc"})


def test_F_03_01_stop_out_is_not_sold_twice(world, tmp_path, monkeypatch):
    """Fixed by the execution engine: the broker stop is the only stop; monitoring never sells it again."""
    from tests.exec_helpers import wired_scheduler

    world.clock.set_et(2026, 11, 23, 9, 50)
    sched, eng = wired_scheduler(world, tmp_path, monkeypatch)
    seed_held(world)
    run(eng.ledger.refresh("t"))
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 92.0)  # broker stop at 93 fills
    assert world.alpaca.position("AAA") is None
    sent = len(world.orders_sent())
    run(sched._run_market_monitoring())
    world.clock.advance(minutes=1)
    assert world.alpaca.position("AAA") is None, "the bot sold shares it no longer held (short opened)"
    assert len(world.orders_sent()) == sent, "monitoring sent an order after the broker stop filled"
    fills = [o for o in world.alpaca.orders.values() if o.symbol == "AAA" and o.status == "filled"]
    assert [o.type for o in fills] == ["stop"]


def test_F_03_05_every_filled_buy_gets_a_broker_stop(world, tmp_path, monkeypatch):
    """Fixed: the stop is placed only after the fill is confirmed, for the filled quantity."""
    from tests.exec_helpers import enqueue, wired_scheduler

    world.clock.set_et(2026, 11, 23, 9, 31)
    world.price("AAA", 50.0, spread=0.02)
    sched, eng = wired_scheduler(world, tmp_path, monkeypatch)
    enqueue(eng, "AAA", "buy", ref=50)
    run(sched._run_market_monitoring())  # before open+5: queued, nothing sent
    world.assert_no_orders()
    world.clock.set_et(2026, 11, 23, 9, 40)
    world.price("AAA", 50.0, spread=0.02)
    run(sched._run_market_monitoring())
    world.clock.advance(minutes=15)
    held = world.alpaca.position("AAA")
    assert held is not None and held.qty > 0, "precondition: the buy filled"
    stops = [o for o in world.alpaca.open_orders("AAA") if o.side == "sell" and o.type in ("stop", "stop_limit")]
    assert sum(o.remaining for o in stops) == held.qty, "filled buy left without a broker stop"


# F-03-04 fixed by state_ledger: a failed positions read leaves the ledger untouched.
def test_F_03_04_failed_positions_request_keeps_the_ledger(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    sched = world.scheduler()
    seed_held(world, with_stop=False)
    sched.store.open()

    async def scenario():
        try:
            assert await sched._reconcile_scheduler("seed the book")
            before = sched.ledger.last_snapshot()
            assert before is not None and before.holding("AAA") is not None
            world.alpaca.fail("/v2/positions", "5xx", method="GET", times=None)
            assert not await sched._reconcile_scheduler("failed broker read")
            assert sched.ledger.last_snapshot() == before, "ledger wiped by a failed broker read"
        finally:
            await sched._cleanup()

    run(scenario())


def test_F_01_03_holiday_order_not_booked_as_filled(world):
    from stock_agent.ledger import IntentStatus, NewIntent

    world.clock.set_et(2026, 11, 26, 9, 35)  # Thanksgiving
    world.price("AAA", 50.0)
    sched = world.scheduler()
    now = world.clock.now_et()
    sched.store.open()
    intent_id = sched.ledger.enqueue_intents([NewIntent(
        "AAA", "buy", "thanksgiving-fixture", session_date=now.date(),
        expires_at=now.replace(hour=16, minute=0),
    )])[0]

    async def scenario():
        sched._normalised = sched._reconciled = True
        await sched._tick(now)
        assert sched.calendar.state.source == "live"
        assert not (await sched.market_gate("entry")).allowed
        world.assert_no_orders()
        world.clock.set_et(2026, 11, 27, 10, 0)
        sched.ledger.expire_intents(world.clock.now_et())
        assert sched.ledger.get_intent(intent_id).status is IntentStatus.EXPIRED
        assert world.alpaca.position("AAA") is None
        await sched._cleanup()

    run(scenario())


def test_F_01_03_market_closed_on_thanksgiving(world):
    world.clock.set_et(2026, 11, 26, 10, 0)
    sched = world.scheduler()
    state = run(sched.calendar.refresh())
    assert state.source == "live"
    assert sched._is_market_open(world.clock.now_et()) is False


def test_F_07_01_unreadable_state_is_not_an_empty_book(world, tmp_path, monkeypatch):
    """Fixed for orders: the engine sizes from a fresh broker reconcile, not portfolio.json."""
    from tests.exec_helpers import enqueue, wired_scheduler

    (tmp_path / "portfolio.json").write_text("{not json")
    (tmp_path / "portfolio.json.bak").write_text("{also not json")
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 100.0)
    world.price("BBB", 40.0)
    world.alpaca.seed_position("AAA", 100, 90.0)
    sched, eng = wired_scheduler(world, tmp_path, monkeypatch)
    enqueue(eng, "BBB", "buy", ref=40)
    run(sched._run_market_monitoring())
    snap = eng.ledger.last_snapshot()
    assert snap is not None and snap.holding("AAA") is not None, "sized a trade on a book without AAA"


def test_F_04_01_spread_not_recorded_when_a_leg_fails(world, tmp_path):
    # Fixed: the scheduler's options scan goes through OptionsManager, which sends a spread
    # as one atomic mleg order and records it only from the broker's fills.
    from stock_agent.options_manager import OptionsManager
    from tests.options_support import EXP, StubGate, make_manager, seed_chain

    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    long_c, short_c = occ_symbol("AAA", EXP, "call", 100), occ_symbol("AAA", EXP, "call", 105)
    gate = StubGate()
    gate.set("AAA")
    sched = world.scheduler()
    sched.options = make_manager(world, tmp_path, gate=gate)
    assert isinstance(sched.options, OptionsManager)
    world.alpaca.fail("/v2/orders", "status", method="POST", status=422, times=None,
                      body={"code": 42210000, "message": "leg rejected"})
    run(sched._run_options_scan())
    sent = [r.json for r in world.orders_sent()]
    assert sent and all(b.get("order_class") == "mleg" for b in sent), "spread legs sent separately"
    held = [s for s in (long_c, short_c) if world.alpaca.position(s)]
    assert len(held) != 1, "broker holds one leg of a spread"
    recorded = bool(sched.options.live())
    assert recorded == (len(held) == 2), "spread recorded as open without both legs at the broker"
