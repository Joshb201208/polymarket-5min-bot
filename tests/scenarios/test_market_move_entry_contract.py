"""Market-move entries still need submit proof and do not replace the scheduled set."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from tests.fakes import ET
from tests.scenarios.v1_restore_support import Harness, Reply


@pytest.mark.world_start(datetime(2026, 11, 15, 17, tzinfo=ET))
def test_market_move_buy_rechecks_gate_without_stamping_scheduled_coverage(world, monkeypatch):
    h = Harness(world, monkeypatch).install()

    async def scenario():
        try:
            await h.start()
            await h.advance(16, 10, 30)
            h.fresh_quote()
            await h.scheduler._run_execution_cycle()
            h.story()
            h.reply = Reply(conviction=7)
            outcome = await h.scheduler.research.run("market_move", work_scope="market_move:synthetic")
            (intent,) = outcome.intents
            assert intent.origin == "market_move"
            assert not h.scheduler.research.has_fresh_thesis_set(world.clock.now())
            h.scheduler.ledger.enqueue_intents((intent,))
            h.scheduler.store.kv_set("research_market_regime", {
                "regime": "cautious", "as_of": world.clock.now().isoformat(),
            })
            gate = h.scheduler.engine._research_gate.is_entry_eligible(h.symbol, world.clock.now())
            assert gate.direction == "BUY" and not gate.ok
            assert "conviction_below_floor_or_invalid" in gate.reasons
            await h.scheduler._run_execution_cycle()
            assert not h.buys(), h.intent_states()
            assert h.intents()[0].terminal
        finally:
            await h.stop()

    asyncio.run(scenario())
