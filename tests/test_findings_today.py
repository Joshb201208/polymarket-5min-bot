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
from tests.fakes.bot import buy_signal, position


def finding(fid: str, why: str):
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{fid}: {why}")


def run(coro):
    return asyncio.run(coro)


def et(*args) -> datetime:
    return datetime(*args, tzinfo=ET)


def seed_held(world, sched, symbol="AAA", shares=10, entry=100.0, stop=93.0, *, with_stop=True):
    """A position that both the ledger and the broker hold, with a broker GTC stop."""
    world.price(symbol, entry)
    world.alpaca.seed_position(symbol, shares, entry)
    if with_stop:
        world.alpaca.submit({"symbol": symbol, "qty": str(shares), "side": "sell", "type": "stop",
                             "stop_price": str(stop), "time_in_force": "gtc"})
    sched.portfolio.add_position(position(symbol, shares, entry, stop=stop))


def test_F_03_01_stop_out_is_not_sold_twice(world, tmp_path, monkeypatch):
    """Fixed by the execution engine: the broker stop is the only stop; monitoring never sells it again."""
    from tests.exec_helpers import wired_scheduler

    world.clock.set_et(2026, 11, 23, 9, 50)
    sched, eng = wired_scheduler(world, tmp_path, monkeypatch)
    seed_held(world, sched)
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
    from tests.exec_helpers import wired_scheduler

    world.clock.set_et(2026, 11, 23, 9, 31)
    world.price("AAA", 50.0, spread=0.02)
    sched, eng = wired_scheduler(world, tmp_path, monkeypatch)
    run(sched._execute_signals([buy_signal("AAA")]))   # before open+5: queued, nothing sent
    world.assert_no_orders()
    world.clock.set_et(2026, 11, 23, 9, 40)
    world.price("AAA", 50.0, spread=0.02)
    run(sched._run_market_monitoring())
    world.clock.advance(minutes=15)
    held = world.alpaca.position("AAA")
    assert held is not None and held.qty > 0, "precondition: the buy filled"
    stops = [o for o in world.alpaca.open_orders("AAA") if o.side == "sell" and o.type in ("stop", "stop_limit")]
    assert sum(o.remaining for o in stops) == held.qty, "filled buy left without a broker stop"


@finding("F-03-04", "a failed positions request is read as 'no positions' and wipes the ledger")
def test_F_03_04_failed_positions_request_keeps_the_ledger(world):
    world.clock.set_et(2026, 11, 23, 10, 0)
    sched = world.scheduler()
    seed_held(world, sched, with_stop=False)
    world.alpaca.fail("/v2/positions", "5xx", method="GET")
    run(sched._sync_portfolio_from_alpaca())
    assert sched.portfolio.get_position("AAA") is not None, "ledger wiped by a failed broker read"


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
    from tests.exec_helpers import wired_scheduler

    (tmp_path / "portfolio.json").write_text("{not json")
    (tmp_path / "portfolio.json.bak").write_text("{also not json")
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 100.0)
    world.price("BBB", 40.0)
    world.alpaca.seed_position("AAA", 100, 90.0)
    sched, eng = wired_scheduler(world, tmp_path, monkeypatch)
    run(sched._execute_signals([buy_signal("BBB")]))
    snap = eng.ledger.last_snapshot()
    assert snap is not None and snap.holding("AAA") is not None, "sized a trade on a book without AAA"


def test_F_04_01_spread_not_recorded_when_a_leg_fails(world, monkeypatch):
    # Fixed: the scheduler no longer uses the legacy per-leg path; OptionsManager sends one
    # atomic mleg order (see tests/test_options_manager.py for leg-level coverage).
    from stock_agent.options_models import OptionSide, OptionsSignal, OptionsStrategy, OptionType

    world.clock.set_et(2026, 11, 23, 10, 0)
    exp = date(2026, 12, 18)
    world.price("AAA", 100.0)
    world.alpaca.add_option_chain("AAA", exp, [100, 110])
    long_c, short_c = occ_symbol("AAA", exp, "call", 100), occ_symbol("AAA", exp, "call", 110)
    world.alpaca.set_option_quote(long_c, 4.0, 4.2)
    world.alpaca.set_option_quote(short_c, 1.0, 1.1)
    sched = world.scheduler()
    signal = OptionsSignal(
        strategy=OptionsStrategy.BULL_CALL_SPREAD, underlying_symbol="AAA", underlying_price=100.0,
        contract_symbol=long_c, option_type=OptionType.CALL, side=OptionSide.BUY, target_strike=100,
        leg2_contract_symbol=short_c, leg2_option_type=OptionType.CALL, leg2_side=OptionSide.SELL,
        leg2_target_strike=110, qty=1, max_debit_per_contract=3.2,
    )

    async def one_signal(**kw):
        return [signal]

    monkeypatch.setattr(sched.options_engine, "scan_opportunities", one_signal)
    run(sched._run_options_scan())
    held = [s for s in (long_c, short_c) if world.alpaca.position(s)]
    assert len(held) != 1, "broker holds one leg of a spread"
    recorded = sched.options_portfolio.open_position_count > 0
    assert recorded == (len(held) == 2), "spread recorded as open without both legs at the broker"
