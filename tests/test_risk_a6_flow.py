"""Run the A6 close-to-next-open contract with the real ledger and execution engine."""

import asyncio
from datetime import timedelta
from decimal import Decimal

from stock_agent.ledger import JobStatus, NewIntent
from tests.exec_helpers import make_engine


def test_a6_two_closes_queue_one_trim_and_preserve_residual_protection(world, tmp_path, monkeypatch):
    async def check():
        symbol = "ALFA"
        eng = make_engine(world, tmp_path, monkeypatch)
        ledger, risk = eng.ledger, eng.risk
        world.alpaca.cash = 80_000
        world.price(symbol, 100)
        world.alpaca.seed_position(symbol, 200, 100)

        # The scheduler owns this adapter: atomic job completion persists the intent.
        # Keep the test on the real broker, ledger, planner and execution interfaces.
        last_session, counters = None, {}

        async def close(day: int):
            nonlocal last_session, counters
            world.clock.set_et(2026, 11, day, 16, 5)
            await eng.calendar.refresh()
            session = world.clock.now_et().date()
            key = session.isoformat()
            if ledger.job_status("drawdown_close", key) is not None:
                return ()
            ledger.start_job("drawdown_close", key)
            snapshot = await ledger.refresh("A6 trusted close")
            assert snapshot.quality == "live"
            previous = eng.calendar.previous_session(session)
            breaches = risk.evaluate_portfolio(snapshot, include_pending=False)
            counters = risk.advance_drift_counters(breaches, counters if previous.date == last_session else {})
            last_session = session
            candidates = risk.plan_remediation(snapshot, consecutive_closes=counters)
            next_session = eng.calendar.next_session(session)
            intents = tuple(NewIntent(
                intent.symbol, "sell", f"a6:{key}:{intent.symbol}", kind="trim",
                position_id=snapshot.holding(intent.symbol).meta.position_id,
                origin="limit_drift", session_date=next_session.date,
                next_attempt_at=next_session.opens_at + timedelta(minutes=5),
                meta={"rule": "risk_limit", "qty": str(intent.qty), "breach": intent.reason},
            ) for intent in candidates)
            ledger.finish_job("drawdown_close", key, JobStatus.DONE, intents=intents)
            return intents

        try:
            assert not await close(23)
            assert counters == {("issuer_cap", symbol): 1}
            assert not await close(23)  # A second run on the same close must not confirm drift.
            assert counters == {("issuer_cap", symbol): 1}
            (trim,) = await close(24)
            assert trim.meta["qty"] == "70"  # From 20% to the 13% trim target.
            assert not await eng.execute_due()  # Not at the close.

            world.clock.set_et(2026, 11, 25, 9, 34)
            assert not await eng.execute_due()  # Not before open + 5.
            world.clock.advance(minutes=1)
            (result,) = await eng.execute_due()
            assert result.filled_qty == 70
            assert world.alpaca.position(symbol).qty == 130
            (stop,) = [o for o in world.alpaca.open_orders(symbol) if o.type == "stop"]
            assert Decimal(str(stop.qty)) == 130 and stop.time_in_force == "gtc"
            assert ledger.store.kv_get(eng._model_exit_key()) is None
            assert not await eng.execute_due()  # Fill or restart cannot submit a second trim.
        finally:
            await eng.broker.close()
            ledger.store.close()

    asyncio.run(check())
