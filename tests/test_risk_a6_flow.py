"""Run the A6 close-to-next-open contract with the real ledger and execution engine."""

import asyncio
from decimal import Decimal

from stock_agent.ledger import IntentStatus, JobStatus


def test_a6_two_closes_queue_one_trim_and_preserve_residual_protection(world):
    async def check():
        symbol = "ALFA"
        sched = world.scheduler()
        sched.store.open()
        ledger, eng = sched.ledger, sched.engine
        world.alpaca.cash = 80_000
        world.price(symbol, 100)
        world.alpaca.seed_position(symbol, 200, 100)

        async def close(day: int):
            world.clock.set_et(2026, 11, day, 16, 5)
            key = world.clock.now_et().date().isoformat()
            sched._schedule_job("drawdown_close", key, sched._close_drawdown, timeout=10)
            if task := sched._job_tasks.get("drawdown_close"):
                await task
                await sched._collect_finished_jobs()
            job = ledger.job_status("drawdown_close", key)
            assert job.status == JobStatus.DONE, job

        try:
            world.clock.set_et(2026, 11, 23, 10, 0)
            await sched.calendar.refresh()
            await ledger.refresh("seed long stock")
            await eng._protect_symbol(symbol)
            (old_stop,) = [o for o in world.alpaca.open_orders(symbol) if o.type == "stop"]
            assert old_stop.qty == 200

            await close(23)
            assert not ledger.list_intents([IntentStatus.QUEUED])
            assert ledger.risk_state_get("a6.close_drift")["counts"] == [["issuer_cap", symbol, 1]]
            await close(23)  # A second run on the same close must not confirm drift.
            assert ledger.risk_state_get("a6.close_drift")["counts"] == [["issuer_cap", symbol, 1]]
            await close(24)
            (trim,) = ledger.list_intents([IntentStatus.QUEUED])
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
            assert stop.id != old_stop.id
            assert ledger.store.kv_get(eng._model_exit_key()) is None
            assert not await eng.execute_due()  # Fill or restart cannot submit a second trim.
        finally:
            await sched._cleanup()

    asyncio.run(check())
