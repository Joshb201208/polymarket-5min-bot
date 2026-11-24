"""V1 restore part 3 (top-ups on re-affirmed holdings), end to end.

Tue 17 Nov: a prescan BUY at conviction 8 is only partly filled (20 shares), leaving the holding well below 90% of
its conviction target. Wed 18 Nov: the prescan re-affirms BUY 8 with thesis_change "unchanged". That must top up
toward target, which b00dfcd refuses (add_thesis_not_strengthened). "weakened" never tops up, and there is at most
one top-up per position per trading day. The stop is re-placed to cover the whole holding.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from tests.fakes import ET
from tests.scenarios.v1_restore_support import Harness, Reply, monday_hold_catchup

pytestmark = pytest.mark.world_start(datetime(2026, 11, 15, 17, tzinfo=ET))
FIRST_FILL = 20


def partial(n: int) -> list[float]:
    """Fill n shares, then nothing more for the rest of the day (zero-size chunks one minute apart)."""
    return [n] + [0] * 2000


def held_qty(h) -> float:
    p = h.world.alpaca.positions.get(h.symbol)
    return p.qty if p else 0.0


def stop_qty(h) -> float:
    return sum(o.qty for o in h.world.alpaca.open_orders(h.symbol) if o.type == "stop" and o.side == "sell")


async def prescan(h, day: int, reply: Reply):
    await h.advance(day, 8, 0)
    h.reply = reply
    h.fresh_quote()
    return await h.run_research("prescan")


def _run(world, monkeypatch, body):
    h = Harness(world, monkeypatch).install()

    async def go():
        try:
            await h.start()
            await monday_hold_catchup(h)
            await prescan(h, 17, Reply(conviction=8))
            h.world.alpaca.script_partial_fills(h.symbol, partial(FIRST_FILL))
            await h.advance(17, 9, 35)
            h.fresh_quote()
            await h.execute(6)
            await h.advance(17, 16, 5)
            await h.execute(2)
            await h.advance(18, 7, 0)
            await h.execute()
            assert held_qty(h) == FIRST_FILL, (held_qty(h), h.intent_states())
            await body(h)
        finally:
            await h.stop()

    asyncio.run(go())


def test_unchanged_buy_8_tops_up_to_target_and_restops_the_whole_holding(world, monkeypatch):
    async def body(h):
        row = await prescan(h, 18, Reply(conviction=8, thesis_change="unchanged"))
        assert "add_thesis_not_strengthened" not in repr(row.detail), row.detail  # the b00dfcd refusal
        await h.advance(18, 9, 35)
        h.fresh_quote()
        await h.execute(6)
        assert len(h.buys()) == 2, h.intent_states()
        assert held_qty(h) > FIRST_FILL
        assert stop_qty(h) == held_qty(h)  # one broker stop covering the topped-up holding

    _run(world, monkeypatch, body)


def test_weakened_buy_does_not_top_up(world, monkeypatch):
    async def body(h):
        await prescan(h, 18, Reply(conviction=8, thesis_change="weakened"))
        await h.advance(18, 9, 35)
        h.fresh_quote()
        await h.execute(6)
        assert len(h.buys()) == 1 and held_qty(h) == FIRST_FILL, h.intent_states()

    _run(world, monkeypatch, body)


def test_lower_conviction_than_last_fill_does_not_top_up(world, monkeypatch):
    async def body(h):
        await prescan(h, 18, Reply(conviction=7, thesis_change="unchanged"))
        await h.advance(18, 9, 35)
        h.fresh_quote()
        await h.execute(6)
        assert len(h.buys()) == 1 and held_qty(h) == FIRST_FILL, h.intent_states()

    _run(world, monkeypatch, body)


def test_at_most_one_top_up_per_position_per_day(world, monkeypatch):
    async def body(h):
        await prescan(h, 18, Reply(conviction=8, thesis_change="unchanged"))
        h.world.alpaca.script_partial_fills(h.symbol, partial(10))  # the top-up itself is only partly filled
        await h.advance(18, 9, 35)
        h.fresh_quote()
        await h.execute(6)
        assert len(h.buys()) == 2 and held_qty(h) == FIRST_FILL + 10, h.intent_states()
        # a second re-affirmation the same day (a strengthened news thesis) must not buy again today
        h.reply = Reply(conviction=9, thesis_change="strengthened")
        await h.advance(18, 11, 0)
        h.story(minutes_ago=2)
        h.fresh_quote()
        await h.execute(12)
        assert len(h.buys()) == 2, h.intent_states()

    _run(world, monkeypatch, body)
