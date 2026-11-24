"""Drawdown record ordering and replay detail for the trusted close (synthetic book from test_trusted_close_gap)."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from stock_agent.broker import BrokerError
from stock_agent.ledger import JobStatus
from stock_agent.reconcile import ReconcileError
from tests.test_trusted_close_gap import GS_CALL, JPM_CALL, _run, close_mark, drawdown_session, seed_23nov

DAY = date(2026, 11, 23)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.TimeoutError], ids=["raises", "times_out"])
def test_drawdown_record_is_written_before_the_closing_risk_check(world, monkeypatch, failure):
    seed_23nov(world)

    async def body(sched):
        calls = []

        async def broken(*args, **kwargs):
            calls.append(drawdown_session(sched))  # the record is already advanced when the check runs
            raise failure("synthetic closing risk check failure")

        monkeypatch.setattr(sched, "_evaluate_close_limits", broken)
        world.clock.set_et(2026, 11, 23, 16, 5)
        with pytest.raises(failure):
            await sched._close_drawdown()
        assert calls == ["2026-11-23"]
        assert close_mark(sched, DAY) is not None and drawdown_session(sched) == "2026-11-23"
        # the terminal notice treats an applied session as not missed
        assert sched._terminal_close_failure("2026-11-23", {"error": failure.__name__}) == {
            "error": failure.__name__}

    _run(world, body)


def test_replay_reports_the_persisted_conservative_detail(world):
    seed_23nov(world)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        first = await sched._close_drawdown()
        assert first.status is JobStatus.DONE and first.detail["conservative"] is True
        mark = close_mark(sched, DAY)
        assert mark.extra["conservative"] is True and {GS_CALL, JPM_CALL} <= set(mark.extra["unpriced_option_legs"])
        world.alpaca.cash += 5_000  # a refreshed value must not leak into the replayed detail
        world.clock.set_et(2026, 11, 23, 16, 20)
        again = await sched._close_drawdown()
        for key in ("broker_equity", "close_equity", "haircut", "unpriced_option_legs", "summary"):
            assert again.detail[key] == first.detail[key]
        assert close_mark(sched, DAY) == mark

    _run(world, body)


@pytest.mark.parametrize("raw", ["NaN", "garbage", "inf"])
def test_malformed_market_value_on_unpriced_long_stays_untrusted(world, monkeypatch, raw):
    seed_23nov(world)
    original = world.alpaca.position_json

    def position_json(p):
        out = original(p)
        if p.symbol == JPM_CALL:
            out["market_value"] = raw  # not absent: malformed, so it cannot count as "missing and consistent"
        return out

    monkeypatch.setattr(world.alpaca, "position_json", position_json)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        # The broker client already refuses a non-finite or unreadable value, so the close fails closed.
        with pytest.raises((BrokerError, ReconcileError)):
            await sched._close_drawdown()
        assert close_mark(sched, DAY) is None and drawdown_session(sched) is None

    _run(world, body)


def test_absent_market_value_on_unpriced_long_is_consistent(world, monkeypatch):
    seed_23nov(world)
    original = world.alpaca.position_json

    def position_json(p):
        out = original(p)
        if p.symbol == JPM_CALL:
            out.pop("market_value", None)
        return out

    monkeypatch.setattr(world.alpaca, "position_json", position_json)

    async def body(sched):
        world.clock.set_et(2026, 11, 23, 16, 5)
        snap = await sched.ledger.refresh("fixture")
        jpm = next(item for item in snap.legs if item.occ_symbol == JPM_CALL)
        assert not jpm.market_value_reported and not jpm.market_value_malformed
        # JPM_CALL pairs with JPM_SHORT, so a missing value on a paired leg is untrusted (section 7)
        outcome = await sched._close_drawdown()
        assert outcome.detail["reason"] == "close_option_mark_missing"

    _run(world, body)
