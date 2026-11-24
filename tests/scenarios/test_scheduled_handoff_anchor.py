"""An immutable analysed close or one fresh, durable re-review, never both."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

import pytest

from stock_agent.ledger import IntentStatus
from tests.fakes import ET
from tests.scenarios.v1_restore_support import Harness, monday_hold_catchup, tuesday_prescan_with_stale_quote

pytestmark = pytest.mark.world_start(datetime(2026, 11, 15, 17, tzinfo=ET))


def _run(world, monkeypatch, body, *, anchored=False):
    harness = Harness(world, monkeypatch).install()
    world.alpaca.behaviour.quotes_stay_fresh_in_session = False
    if anchored:
        world.alpaca.seed_daily_bars("AAPL", [100.0], end=datetime(2026, 11, 17, tzinfo=ET).date())

    async def no_completed_bar(*args):
        return None

    async def scenario():
        try:
            await harness.start()
            await monday_hold_catchup(harness)
            if not anchored:
                monkeypatch.setattr(harness.scheduler.research.sources, "completed_close", no_completed_bar)
            await tuesday_prescan_with_stale_quote(harness)
            assert harness.calls[-1][2] == (100 if anchored else None)
            await body(harness)
        finally:
            await harness.stop()

    asyncio.run(scenario())


async def _sweep(harness):
    return await harness.scheduler.research.run_entry_handoff()


def test_completed_close_is_used_at_submit_not_replaced_by_opening_quote(world, monkeypatch):
    async def body(h):
        before = len(h.calls)
        await h.advance(17, 9, 34)
        h.fresh_quote(104)
        assert not (await _sweep(h)).detail["handoff_outcomes"]
        assert not h.intents()
        await h.advance(17, 9, 35)
        h.fresh_quote(104)
        result = await _sweep(h)
        assert result.detail["handoff_outcomes"] == {"handoff_promoted": 1}
        (intent,) = h.intents()
        assert intent.reference_price == Decimal("100")
        assert intent.meta["reference_price_kind"] == "completed_split_adjusted_close"
        await h.scheduler._run_execution_cycle()
        (after,) = h.intents()
        assert after.status == IntentStatus.SKIPPED
        assert not h.buys() and len(h.calls) == before

    _run(world, monkeypatch, body, anchored=True)


@pytest.mark.parametrize("interrupted", [False, True])
def test_unanchored_buy_uses_at_most_one_targeted_review_after_restart(world, monkeypatch, interrupted):
    async def body(h):
        before = len(h.calls)
        await h.advance(17, 9, 35)
        h.fresh_quote(104)
        h.story()
        if interrupted:
            original = h.scheduler.research.analyst.evaluate

            async def interrupt_after_response(*args, **kwargs):
                await original(*args, **kwargs)
                raise asyncio.CancelledError

            monkeypatch.setattr(h.scheduler.research.analyst, "evaluate", interrupt_after_response)
            with pytest.raises(asyncio.CancelledError):
                await _sweep(h)
        else:
            result = await _sweep(h)
            assert result.detail["handoff_outcomes"] == {"handoff_reanchored": 1}
        assert len(h.calls) == before + 1 and h.calls[-1][2] == 104
        await h.restart()
        await h.advance(17, 9, 36)
        h.fresh_quote(104)
        result = await _sweep(h)
        expected = "handoff_reanchor_unavailable" if interrupted else "handoff_promoted"
        assert result.detail["handoff_outcomes"] == {expected: 1}
        await h.scheduler._run_execution_cycle()
        assert len(h.calls) == before + 1
        assert len(h.buys()) == (0 if interrupted else 1)
        (intent,) = h.intents()
        if not interrupted:
            assert intent.reference_price == Decimal("104")
            assert intent.meta["reference_price_kind"] == "quote"
        assert not (await _sweep(h)).detail["handoff_outcomes"]

    _run(world, monkeypatch, body)
