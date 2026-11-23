"""Reconcile through the REAL AlpacaClient against FakeAlpaca (HTTP fakes, no network)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from stock_agent.broker import AlpacaClient
from stock_agent.state_boot import open_ledger


def _kinds(snap):
    return {(d.kind.value, d.symbol) for d in snap.diffs}


def test_reconcile_with_real_client(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50.0)
    world.alpaca.seed_position("BBB", 3, 20.0, price=21.0)     # held before the bot ever ran
    world.alpaca.submit({"symbol": "AAA", "qty": "10", "side": "buy", "type": "market", "time_in_force": "day"})
    world.clock.advance(minutes=2)

    async def go():
        broker = AlpacaClient()
        try:
            led = open_ledger(tmp_path / "state", broker=broker, clock=world.clock.now).ledger
            first = await led.refresh("boot", rebuild=True)
            assert {h.symbol: h.qty for h in first.holdings} == {"AAA": Decimal("10"), "BBB": Decimal("3")}
            assert abs(first.equity - Decimal(str(world.alpaca.equity()))) < Decimal("0.01")
            assert led.last_reconcile()["ok"] and led.trading_block() is None
            assert led.open_position_meta("AAA") is not None and led.open_position_meta("BBB") is not None
            assert led.store.query("SELECT COUNT(*) FROM broker_activities WHERE activity_type='FILL'")[0][0] >= 1

            # a close made outside the bot is explained by its FILL and is not critical
            world.alpaca.submit({"symbol": "AAA", "qty": "10", "side": "sell", "type": "market",
                                 "time_in_force": "day"})
            world.clock.advance(minutes=2)
            second = await led.refresh("after sell")
            assert second.holding("AAA") is None and led.open_position_meta("AAA") is None
            assert all(d.severity.value != "critical" for d in second.diffs)
            assert led.trading_block() is None

            # a failed broker read raises and never looks like an empty book
            world.alpaca.fail("/v2/positions", "5xx", method="GET", times=None)
            with pytest.raises(Exception):
                await led.refresh("positions down")
            world.alpaca.clear_faults()
            assert led.open_position_meta("BBB") is not None
            third = await led.refresh("recovered")
            assert third.holding("BBB").qty == Decimal("3")
            assert not any(k == "mass_close" for k, _ in _kinds(third))
        finally:
            await broker.close()

    asyncio.run(go())
