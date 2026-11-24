"""A14: real first-session research wiring against the simulated outside world."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

from stock_agent.config import Config
from stock_agent.ledger import IntentStatus, JobStatus
from stock_agent.research_pipeline import OptionIdea
from stock_agent.research_schemas import Direction
from tests.fakes import ET

_START = datetime(2026, 11, 23, 9, tzinfo=ET)
_SYMBOLS = ("AAPL", "MSFT", "JPM")


@pytest.mark.world_start(_START)
@pytest.mark.parametrize(("openai_rate_limited", "stale_preopen", "scheduled_submit"), [
    (False, None, False), (True, None, False), (False, "entry", False), (False, "hold", False),
    (True, None, True),
])
def test_day_one_cautious_research_produces_equity_and_bearish_options(
    world, monkeypatch, openai_rate_limited, stale_preopen, scheduled_submit,
):
    monkeypatch.setattr(Config, "PRIORITY_SYMBOLS", _SYMBOLS)
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "test-anthropic")
    for symbol in _SYMBOLS:
        world.price(symbol, 100.0, spread=0.02)
        world.news([symbol], f"{symbol} updates quarterly guidance", summary="The issuer publishes new guidance.",
                   at=world.clock.now() - timedelta(minutes=5))
    for benchmark in ("SPY", "QQQ"):
        world.alpaca.seed_daily_bars(benchmark, [100.0, 100.0], end=_START.date())
        world.price(benchmark, 95.0, spread=0.02)
    world.finnhub.on("GET", "/api/v1/stock/metric", {
        "metric": {"peTTM": 20, "grossMarginTTM": 50, "revenueGrowthTTMYoy": 10},
    })
    world.fmp.fail("/stable/profile", "status", status=402, times=None)
    world.alpaca.fail("/v1beta1/news", "status", status=403, times=None)
    world.perplexity.fail("/v1/agent", "status", status=401,
                          body={"error": {"code": "insufficient_credits"}}, times=None)

    def draft(body):
        text = body["messages"][0]["content"] if "messages" in body else body["input"]
        data = json.loads(text.split("INPUT_JSON:\n", 1)[1])
        symbol = data["symbol"]
        assert data["context"]["regime"] == "cautious"
        assert data["metrics"]["gross_margin_ttm"] == 0.5
        story = next(item for item in data["evidence"] if item["published_at"])
        assert not body.get("tools"), "date-stamped provider evidence should not require another search"
        if symbol == "AAPL" and world.clock.now().astimezone(ET).hour >= 10:
            world.clock.advance(seconds=100)  # Initial sizing quote expires during the model call.
        return {
            "symbol": symbol, "direction": (
                "HOLD" if stale_preopen == "hold" and data["reference_price"] is None
                else "BEARISH" if symbol == "MSFT" else "BUY"
            ),
            "conviction": 7 if symbol == "JPM" else 9,
            "summary": "The issuer updated its outlook.", "bull_case": "Improving demand.",
            "bear_case": "Demand may weaken.", "claims": [
                {"text": "The issuer issued updated guidance.", "evidence_ids": [story["evidence_id"]]},
            ],
            "catalysts": [], "risks": [], "thesis_break": "Next release contradicts the update.",
            "target_price": None, "thesis_change": "new", "horizon_days": 15,
        }

    world.openai.respond_with(draft)
    world.anthropic.respond_with(draft)
    if openai_rate_limited:
        world.openai.on("POST", "/v1/responses", lambda _: httpx.Response(429, json={"error": {}}))

    async def run():
        scheduler = world.scheduler()
        try:
            await scheduler.startup()
            await scheduler._refresh_assets()
            await scheduler._run_execution_cycle()
            research = scheduler.research
            if stale_preopen:
                world.clock.advance(minutes=21)
                for symbol in ("SPY", "QQQ"):
                    world.price(symbol, 95.0, spread=0.02)
                assert await scheduler._reconcile_scheduler("preopen fixture snapshot")
                await research.observe_regime()
                early = await research.run("weekly", work_scope="catchup-preopen")
                assert not early.intents and not early.option_candidates
                assert early.detail["entry_funnel"]["rejected_by_reason"] == (
                    {"non_entry_direction": 3} if stale_preopen == "hold"
                    else {"quote_stale_or_unavailable": 2, "conviction_below_floor": 1}
                )
                assert not research.has_fresh_thesis_set(world.clock.now())
            world.clock.advance(minutes=39 if stale_preopen else 60)
            for symbol in _SYMBOLS + ("SPY", "QQQ"):
                world.price(symbol, 100.0 if symbol in _SYMBOLS else 95.0, spread=0.02)
            await scheduler._run_execution_cycle()
            assert scheduler.engine.normalised and scheduler._reconciled
            assert not research.has_fresh_thesis_set(world.clock.now())
            if scheduled_submit:
                await scheduler.calendar.refresh()
                scheduler._schedule_research_catchup(world.clock.now_et())
                await scheduler._job_tasks["catchup"]
                await scheduler._collect_finished_jobs()
                job = scheduler.ledger.job_status("catchup", f"{_START.date()}:regular")
                assert job.status is JobStatus.DONE
                info = job.detail["entry_funnel"]
                intents = scheduler.ledger.due_intents(world.clock.now())
                ideas = [OptionIdea.from_payload(row) for row in
                         scheduler.research_store.pending_option_ideas(world.clock.now())]
            else:
                await research.observe_regime()
                outcome = await research.run("weekly", work_scope="catchup-regular")
                assert outcome.status is JobStatus.DONE
                intents, ideas = outcome.intents, outcome.option_candidates
                info = outcome.detail["entry_funnel"]
            assert [(intent.symbol, intent.side) for intent in intents] == [("AAPL", "buy")]
            assert intents[0].meta["minimum_conviction"] == 8
            assert intents[0].reference_price == 100
            assert scheduler.prices.last_quote("AAPL").as_of >= world.clock.now() - timedelta(seconds=90)
            assert [(idea.symbol, idea.direction, idea.source) for idea in ideas] == [
                ("MSFT", Direction.BEARISH, "research"),
            ]
            assert research.has_fresh_thesis_set(world.clock.now())
            assert not research.has_fresh_thesis_set(world.clock.now() + timedelta(hours=97))
            assert info == {"screened": 3, "thesis_written": 3, "passed_conviction": 2,
                            "buy_candidates": 1, "option_ideas": 1, "rejected_by_reason": {"conviction_below_floor": 1}}
            *_, event = scheduler.journal.read(kinds={"research.entry_funnel"})
            assert event["session_date"] == _START.date().isoformat()
            assert event["rejected_by_reason"] == {"conviction_below_floor": 1}
            expected_route = "anthropic/claude-sonnet-5-5" if openai_rate_limited else "openai/gpt-6-luna"
            assert scheduler.ledger.current_thesis("AAPL").source == expected_route
            spend = scheduler.research_store.available_budget(world.clock.now())
            assert 0 < spend["entry_microusd"] == spend["billed_or_reserved_microusd"] < 12_000_000
            assert scheduler.research_store.get_cooldown(research.analyst.llm._routes()[0]) > world.clock.now()
            if scheduled_submit:
                await scheduler._run_execution_cycle()
                filled = scheduler.ledger.get_intent(intents[0].intent_id)
                assert filled.status is IntentStatus.FILLED, filled.last_error
                assert world.alpaca.positions["AAPL"].qty > 0
                await scheduler._run_execution_cycle()
                orders = [row for row in world.orders_sent() if row.method == "POST"
                          and row.json.get("symbol") == "AAPL" and row.json.get("side") == "buy"]
                assert len(orders) == 1
        finally:
            await scheduler._cleanup()

    asyncio.run(run())
    perp_calls = world.net.requests(service="perplexity", path="/v1/agent")
    assert len(perp_calls) == 1 and perp_calls[0].status == 401
    assert world.net.requests(service="fmp", path="/stable/profile")
    assert world.net.requests(service="finnhub", path="/api/v1/stock/metric")
    assert world.net.requests(service="finnhub", path="/api/v1/company-news")
    if not scheduled_submit:
        world.assert_no_orders()
