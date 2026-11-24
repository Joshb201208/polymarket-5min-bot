"""V1 restore part 2 (pre-buy recheck must test gate.ok), end to end.

A healthy buy queued before the open must still be submitted at 09:35, so the fix must not stop every buy (for
example through an old news-proof TTL). A buy whose gate is no longer ok at submit time (here the conviction floor
rose to 8 under a cautious market and the thesis is a 7) must not be sent, even though the gate's direction is still
BUY. That is the second red repro: on b00dfcd the Eligibility object is always truthy, so it is sent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from tests.fakes import ET
from tests.scenarios.v1_restore_support import Harness, Reply, monday_hold_catchup

pytestmark = pytest.mark.world_start(datetime(2026, 11, 15, 17, tzinfo=ET))


def _run(world, monkeypatch, body, *, conviction: int = 9):
    h = Harness(world, monkeypatch).install()

    async def go():
        try:
            await h.start()
            await monday_hold_catchup(h)
            await h.advance(17, 8, 0)
            h.reply = Reply(conviction=conviction)
            h.fresh_quote()  # a fresh off-hours quote: queued today on b00dfcd as well
            row = await h.run_research("prescan")
            assert row.detail["entry_funnel"]["passed_conviction"] == 1
            assert [s for s in h.intent_states() if s[0] == "queued"], h.intent_states()
            await body(h)
        finally:
            await h.stop()

    asyncio.run(go())


def test_buy_queued_at_0800_is_submitted_at_0935(world, monkeypatch):
    async def body(h):
        await h.advance(17, 9, 35)  # 95 minutes later: any news proof from 08:00 is old by now
        h.fresh_quote()
        await h.execute(4)
        assert len(h.buys()) == 1, h.intent_states()

    _run(world, monkeypatch, body)


def test_buy_queued_at_0800_is_submitted_later_in_the_morning(world, monkeypatch):
    async def body(h):
        await h.advance(17, 10, 45)  # a late first lane pass (e.g. after a restart) is still a healthy buy
        h.fresh_quote()
        await h.execute(4)
        assert len(h.buys()) == 1, h.intent_states()

    _run(world, monkeypatch, body)


def test_gate_not_ok_with_direction_buy_is_refused_at_submit(world, monkeypatch):
    async def body(h):
        store = h.scheduler.store
        await h.advance(17, 9, 34)
        store.kv_set("research_market_regime", {"regime": "cautious", "as_of": h.world.clock.now().isoformat()})
        gate = h.scheduler.engine._research_gate.is_entry_eligible(h.symbol, h.world.clock.now())
        assert gate.direction is not None and gate.direction.value == "BUY" and not gate.ok, gate
        assert "conviction_below_floor_or_invalid" in gate.reasons
        await h.advance(17, 9, 35)
        h.fresh_quote()
        await h.execute(4)
        assert not h.buys(), h.intent_states()

    _run(world, monkeypatch, body, conviction=7)
