"""V1 restore part 1 (out-of-hours buy handoff), end to end. These complement the red repro in
test_scheduled_buy_handoff.py: restart, duplicate prevention, expiry, same-day retry and the drift anchor."""

from __future__ import annotations

import asyncio

import pytest

from tests.scenarios.v1_restore_support import (
    Harness,
    monday_hold_catchup,
    tuesday_prescan_with_stale_quote,
)
from tests.fakes import ET
from datetime import datetime

_SUNDAY = datetime(2026, 11, 15, 17, tzinfo=ET)
pytestmark = pytest.mark.world_start(_SUNDAY)


def _run(world, monkeypatch, body):
    h = Harness(world, monkeypatch).install()

    async def go():
        try:
            await h.start()
            await monday_hold_catchup(h)
            await tuesday_prescan_with_stale_quote(h)
            await body(h)
        finally:
            await h.stop()

    asyncio.run(go())


def test_handoff_survives_a_restart_before_the_open_and_buys_once(world, monkeypatch):
    async def body(h):
        await h.restart()  # state lives in the DB, not in memory
        await h.advance(17, 9, 35)
        h.fresh_quote()
        await h.execute(3)
        await h.catchup()
        await h.execute(2)
        assert len(h.buys()) == 1, h.intent_states()
        await h.restart()  # a restart after the buy must not buy again
        await h.advance(17, 10, 5)
        h.fresh_quote()
        await h.execute(3)
        assert len(h.buys()) == 1, h.intent_states()

    _run(world, monkeypatch, body)


def test_handoff_restart_mid_target_session_buys_once(world, monkeypatch):
    async def body(h):
        await h.advance(17, 9, 35)
        h.fresh_quote()
        await h.execute()
        await h.restart()  # crash between promotion and submission
        await h.advance(17, 9, 36)
        h.fresh_quote()
        await h.execute(3)
        assert len(h.buys()) == 1, h.intent_states()

    _run(world, monkeypatch, body)


def test_handoff_retries_a_transient_quote_gap_within_the_target_day(world, monkeypatch):
    async def body(h):
        await h.advance(17, 9, 35)  # no fresh quote yet: the handoff must wait, not die
        await h.execute(2)
        assert not h.buys()
        await h.advance(17, 10, 30)
        h.fresh_quote()
        await h.execute(3)
        assert len(h.buys()) == 1, h.intent_states()

    _run(world, monkeypatch, body)


def test_handoff_expires_at_the_target_close_and_never_buys_later(world, monkeypatch):
    async def body(h):
        for hh, mm in ((9, 35), (12, 0), (15, 49)):
            await h.advance(17, hh, mm)  # never a fresh quote on the target day
            await h.execute()
        await h.advance(17, 16, 5)
        await h.execute()
        await h.advance(18, 9, 35)
        h.fresh_quote()
        await h.execute(3)
        assert not h.buys(), h.intent_states()
        assert not [x for x in h.intent_states() if x[0] in {"queued", "submitted"}], h.intent_states()

    _run(world, monkeypatch, body)


def test_handoff_does_not_buy_through_a_gap_above_the_drift_limit_without_re_review(world, monkeypatch):
    """Analysed around 100; the target open is 104 (more than 3% above). Either no buy, or a bounded targeted
    re-review on fresh inputs happened on the target day first. The drift anchor is never the opening ask."""
    async def body(h):
        before = len(h.calls)
        await h.advance(17, 9, 35)
        h.fresh_quote(104.0)
        await h.execute(3)
        if h.buys():
            rereview = [c for c in h.calls[before:] if c[0].date().day == 17 and c[0].hour >= 9]
            assert len(rereview) == 1 and rereview[0][2] is not None and abs(rereview[0][2] - 104) < 1
        assert len(h.calls) - before <= 1  # bounded: at most one targeted re-review

    _run(world, monkeypatch, body)
