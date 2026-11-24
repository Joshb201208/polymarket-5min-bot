"""An immutable analysed close or one fresh, durable re-review, never both."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from stock_agent.ledger import IntentStatus, NewIntent, ThesisRole
from tests.fakes import ET
from tests.scenarios.v1_restore_support import Harness, Reply, monday_hold_catchup, tuesday_prescan_with_stale_quote

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


@pytest.mark.parametrize("interrupted", [None, "before-stage", "after-stage"])
def test_unanchored_buy_uses_at_most_one_targeted_review_after_restart(world, monkeypatch, interrupted):
    async def body(h):
        before = len(h.calls)
        await h.advance(17, 9, 35)
        h.fresh_quote(104)
        h.story()
        if interrupted == "before-stage":
            original = h.scheduler.research.analyst.evaluate

            async def interrupt_after_response(*args, **kwargs):
                await original(*args, **kwargs)
                raise asyncio.CancelledError

            monkeypatch.setattr(h.scheduler.research.analyst, "evaluate", interrupt_after_response)
            with pytest.raises(asyncio.CancelledError):
                await _sweep(h)
        elif interrupted == "after-stage":
            original = h.scheduler.research._finish_handoff

            def interrupt_before_parent_flip(key, intent, code, **kwargs):
                if code == "handoff_reanchored":
                    raise asyncio.CancelledError
                return original(key, intent, code, **kwargs)

            monkeypatch.setattr(h.scheduler.research, "_finish_handoff", interrupt_before_parent_flip)
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
        expected = {"handoff_reanchor_unavailable": 1} if interrupted == "before-stage" else {"handoff_promoted": 1}
        if interrupted == "after-stage":
            expected["handoff_reanchored"] = 1
        assert result.detail["handoff_outcomes"] == expected
        await h.scheduler._run_execution_cycle()
        assert len(h.calls) == before + 1
        assert len(h.buys()) == (0 if interrupted == "before-stage" else 1)
        (intent,) = h.intents()
        if interrupted != "before-stage":
            assert intent.reference_price == Decimal("104")
            assert intent.meta["reference_price_kind"] == "quote"
        assert not (await _sweep(h)).detail["handoff_outcomes"]

    _run(world, monkeypatch, body)


def test_handoff_will_not_compete_with_a_buy_queued_during_rollback(world, monkeypatch):
    async def body(h):
        # b00dfcd does not see research_pending_buys and may queue an unrelated
        # conventional pre-market buy for the same symbol while rolled back.
        h.scheduler.ledger.enqueue_intents((NewIntent("AAPL", "buy", "weekly:rollback:synthetic"),))
        await h.restart()
        await h.advance(17, 9, 35)
        h.fresh_quote()
        result = await _sweep(h)
        assert result.detail["handoff_outcomes"] == {"handoff_buy_pending": 1}
        assert [i.dedupe_key for i in h.intents() if not i.terminal] == ["weekly:rollback:synthetic"]
        assert not (await _sweep(h)).detail["handoff_outcomes"]

    _run(world, monkeypatch, body, anchored=True)


@pytest.mark.parametrize("change", ["newer_hold", "pending_exit"])
def test_handoff_never_supersedes_a_current_hold_or_pending_exit(world, monkeypatch, change):
    async def body(h):
        await h.advance(17, 9, 35)
        h.fresh_quote()
        if change == "newer_hold":
            h.scheduler.ledger.record_thesis(
                "AAPL", {"draft": {"direction": "HOLD", "symbol": "AAPL"}},
                ThesisRole.ENTRY_CANDIDATE, source="synthetic", generated_at=world.clock.now(),
                activate=True,
            )
            expected = "handoff_thesis_changed"
        else:
            h.scheduler.ledger.enqueue_intents((NewIntent("AAPL", "sell", "synthetic:new-exit"),))
            expected = "handoff_exit_pending"
        assert (await _sweep(h)).detail["handoff_outcomes"] == {expected: 1}
        (buy,) = [intent for intent in h.intents() if intent.side == "buy"]
        assert buy.status == IntentStatus.SKIPPED and buy.last_error == expected
        assert not h.buys()

    _run(world, monkeypatch, body, anchored=True)


@pytest.mark.parametrize(("headline", "new_direction"), [
    ("AAPL cuts revenue guidance after revised quarterly results", "HOLD"),
    ("AAPL cuts revenue guidance after revised quarterly results", "BUY"),
    ("Why AAPL stock moved today", None),
])
def test_handoff_rechecks_new_material_news_only(world, monkeypatch, headline, new_direction):
    async def body(h):
        before = len(h.calls)
        await h.advance(17, 9, 35)
        h.fresh_quote(104)
        world.news(["AAPL"], headline, at=world.clock.now() - timedelta(minutes=50))
        if new_direction is not None:
            h.reply = Reply(direction=new_direction)
        result = await _sweep(h)
        if new_direction is None:
            assert result.detail["handoff_outcomes"] == {"handoff_promoted": 1}
            assert len(h.calls) == before
        elif new_direction == "HOLD":
            assert result.detail["handoff_outcomes"] == {"handoff_reanchor_unavailable": 1}
            assert len(h.calls) == before + 1
            assert h.intents()[0].status == IntentStatus.SKIPPED
            return
        else:
            assert result.detail["handoff_outcomes"] == {"handoff_reanchored": 1}
            assert len(h.calls) == before + 1
            assert (await _sweep(h)).detail["handoff_outcomes"] == {"handoff_promoted": 1}
        assert h.intents()[0].reference_price == Decimal("100")
        await h.scheduler._run_execution_cycle()
        assert not h.buys()  # Existing gap rule still uses the original close.

    _run(world, monkeypatch, body, anchored=True)


@pytest.mark.parametrize("material", [False, True], ids=["missing-anchor", "material-news"])
@pytest.mark.parametrize("recovers", [False, True], ids=["expires", "recovers"])
def test_handoff_waits_for_review_route_before_spending_its_attempt(world, monkeypatch, material, recovers):
    async def body(h):
        before = len(h.calls)
        await h.advance(17, 9, 35)
        h.fresh_quote()
        headline = ("AAPL cuts revenue guidance after revised quarterly results" if material
                    else "Why AAPL stock moved today")
        world.news(["AAPL"], headline, at=world.clock.now() - timedelta(minutes=5))
        model = h.scheduler.research.gate.health.llm
        healthy = model.is_healthy
        monkeypatch.setattr(model, "is_healthy", lambda: False)
        (row,) = h.scheduler.research.store.pending_buys(world.clock.now())
        review_key = f"{row['work_key']}:reanchor"

        assert (await _sweep(h)).detail["handoff_outcomes"] == {"llm_unavailable": 1}
        assert h.scheduler.research.store.work_state(review_key) is None
        assert len(h.calls) == before and not h.intents() and not h.buys()
        await h.scheduler.alerts.flush()
        assert not [line for line in world.discord.texts() if "Buy blocked" in line]

        if recovers:
            monkeypatch.setattr(model, "is_healthy", healthy)
            await h.advance(17, 9, 36)
            h.fresh_quote()
            assert (await _sweep(h)).detail["handoff_outcomes"] == {"handoff_reanchored": 1}
            assert (await _sweep(h)).detail["handoff_outcomes"] == {"handoff_promoted": 1}
            await h.scheduler._run_execution_cycle()
            assert len(h.calls) == before + 1 and len(h.buys()) == 1
        else:
            await h.restart()
            await h.advance(17, 16)
            assert (await _sweep(h)).detail["handoff_outcomes"] == {"handoff_expired": 1}
            assert not (await _sweep(h)).detail["handoff_outcomes"]
            assert len(h.calls) == before and not h.buys()
            assert h.intents()[0].meta["handoff_reasons"] == ["llm_unavailable"]
            await h.scheduler.alerts.flush()
            (line,) = [line for line in world.discord.texts() if "Buy blocked" in line]
            assert "AAPL" in line and "session close" in line and "AI review" in line

    _run(world, monkeypatch, body, anchored=material)


def test_handoff_research_budget_refusal_is_terminal_without_a_paid_call(world, monkeypatch):
    async def body(h):
        before = len(h.calls)
        await h.advance(17, 9, 35)
        h.fresh_quote()
        world.news(["AAPL"], "AAPL cuts revenue guidance after revised quarterly results",
                   at=world.clock.now() - timedelta(minutes=5))
        monkeypatch.setattr(h.scheduler.research.store.config, "RESEARCH_WEEKLY_BUDGET_USD", 0.0)
        assert (await _sweep(h)).detail["handoff_outcomes"] == {"handoff_reanchor_unavailable": 1}
        assert not (await _sweep(h)).detail["handoff_outcomes"]
        (intent,) = h.intents()
        assert intent.status == IntentStatus.SKIPPED and intent.meta["handoff_reasons"] == ["budget"]
        assert len(h.calls) == before and not h.buys()
        await h.scheduler.alerts.flush()
        (line,) = [line for line in world.discord.texts() if "Buy blocked" in line]
        assert "weekly AI research budget was exhausted" in line

    _run(world, monkeypatch, body, anchored=True)
