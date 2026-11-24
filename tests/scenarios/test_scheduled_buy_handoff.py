"""Offline reproduction of stale off-hours quotes dropping scheduled BUYs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from stock_agent.config import Config
from stock_agent.ledger import JobStatus
from stock_agent.research_pipeline import _FRESH_SET
from tests.fakes import ET

_SUNDAY = datetime(2026, 11, 15, 17, tzinfo=ET)


@pytest.mark.world_start(_SUNDAY)
@pytest.mark.parametrize(("kind", "decision_day", "hour", "target_day", "fresh_offhours_quote"), [
    ("prescan", 17, 8, 17, False),
    ("prescan", 17, 8, 17, True),
    ("midweek", 18, 18, 19, False),
    ("weekly", 15, 18, 16, False),
])
def test_scheduled_buy_can_reach_open_with_fresh_quote(
    world, monkeypatch, kind, decision_day, hour, target_day, fresh_offhours_quote,
):
    monkeypatch.setattr(Config, "PRIORITY_SYMBOLS", ("AAPL",))
    monkeypatch.setattr(Config, "UNIVERSE_SIZE", 1)
    world.finnhub.fail("/api/v1/quote", "status", status=503, times=None)
    world.fmp.fail("/stable/quote", "status", status=503, times=None)
    world.price("AAPL", 100, spread=0.02)
    replies = []

    def draft(body):
        text = body.get("input") or body["messages"][0]["content"]
        data = json.loads(text.split("INPUT_JSON:\n", 1)[1])
        # Catch-up can stamp a healthy all-HOLD set; it never supplies the BUY.
        regular = world.clock.now_et().hour == 9
        direction = "HOLD" if regular else "BUY"
        replies.append((regular, direction, data["reference_price"]))
        evidence = next(item for item in data["evidence"] if item["published_at"])
        return {
            "symbol": "AAPL", "direction": direction, "conviction": 9,
            "summary": "The issuer updated its outlook.", "bull_case": "Improving demand.",
            "bear_case": "Demand may weaken.", "claims": [
                {"text": "The issuer issued updated guidance.", "evidence_ids": [evidence["evidence_id"]]},
            ],
            "catalysts": [], "risks": [], "thesis_break": "Next release contradicts the update.",
            "target_price": None, "thesis_change": "new", "horizon_days": 15,
        }

    world.openai.respond_with(draft)
    world.perplexity.fail("/v1/agent", "status", status=401, times=None)
    world.alpaca.seed_daily_bars("AAPL", [100.0, 100.0], end=datetime(2026, 11, 16, tzinfo=ET).date())

    async def run():
        scheduler = world.scheduler()
        try:
            await scheduler.startup()
            await scheduler._refresh_assets()
            await scheduler._run_execution_cycle()

            async def advance(day, hh, mm=0):
                world.clock.set_et(2026, 11, day, hh, mm)
                await scheduler.calendar.refresh()
                assert await scheduler._reconcile_scheduler("offline schedule audit")

            async def drain(name):
                if name in scheduler._job_tasks:
                    await scheduler._job_tasks[name]
                    await scheduler._collect_finished_jobs()

            def story():
                world.news(["AAPL"], f"Apple updates outlook {world.clock.now_et().isoformat()}",
                           summary="The issuer publishes new guidance.",
                           at=world.clock.now() - timedelta(minutes=5))

            if kind != "weekly":
                await advance(16, 9, 35)
                world.price("AAPL", 100, spread=0.02)
                await scheduler._run_execution_cycle()
                assert scheduler.engine.normalised
                story()
                scheduler._schedule_research_catchup(world.clock.now_et())
                await drain("catchup")
                assert replies[-1] == (True, "HOLD", 100)
                assert scheduler.research.has_fresh_thesis_set(world.clock.now())

            await advance(decision_day, hour)
            story()
            if fresh_offhours_quote:
                world.price("AAPL", 100, spread=0.02)
            elif kind != "weekly":
                # Seed each target's exact prior regular close, not a current
                # extended-hours print.
                world.alpaca.seed_daily_bars("AAPL", [100.0], end=datetime(2026, 11, target_day, tzinfo=ET).date())
            assert not scheduler._should_run_daily_prescan(
                world.clock.now_et().replace(hour=7, minute=30),
            )
            if kind == "prescan":
                assert scheduler._should_run_daily_prescan(world.clock.now_et())
                key, callback = world.clock.now_et().date().isoformat(), scheduler._run_daily_prescan
            else:
                assert getattr(scheduler, f"_should_run_{kind}")(world.clock.now_et())
                key = scheduler._research_week_key(world.clock.now_et(), kind)
                callback = getattr(scheduler, f"_run_{kind}_analysis")
            scheduler._schedule_job(kind, key, callback, timeout=120)
            await drain(kind)
            job = scheduler.ledger.job_status(kind, key)
            assert job.status is JobStatus.DONE, job.detail
            assert replies[-1] == (False, "BUY", 100)
            assert job.detail["entry_funnel"]["passed_conviction"] == 1
            world.assert_no_orders()

            # A healthy pre-open BUY must survive fallback even if an unrelated
            # symbol never established a healthy scheduled set.
            if fresh_offhours_quote:
                scheduler.store.kv_set(_FRESH_SET, None)
            await advance(target_day, 9, 35)
            world.price("AAPL", 100, spread=0.02)
            if not scheduler.engine.normalised:
                await scheduler._run_execution_cycle()
            scheduler._schedule_entry_handoff(world.clock.now_et())
            await drain("entry_handoff")
            scheduler._schedule_research_catchup(world.clock.now_et())
            await drain("catchup")
            catch = scheduler.ledger.job_status("catchup", f"2026-11-{target_day}:regular")
            if kind == "weekly" or fresh_offhours_quote:
                assert catch.status is JobStatus.DONE
                assert catch.detail["reviewed"] == 0  # Do not replace the scheduled thesis.
                assert replies[-1] == (False, "BUY", 100)
            else:
                assert scheduler.research.has_fresh_thesis_set(world.clock.now())
                assert catch is None
            await scheduler._run_execution_cycle()
            buys = [row for row in world.orders_sent() if row.method == "POST" and row.json.get("side") == "buy"]
            state = [(intent.status.value, intent.last_error) for intent in scheduler.ledger.list_intents(symbol="AAPL")]
            assert len(buys) == 1, (kind, job.detail["entry_funnel"], state)
            (intent,) = scheduler.ledger.list_intents(symbol="AAPL")
            prefix = kind if fresh_offhours_quote else f"handoff:{kind}"
            assert intent.origin == kind and intent.dedupe_key.startswith(f"{prefix}:")
        finally:
            await scheduler._cleanup()

    asyncio.run(run())
