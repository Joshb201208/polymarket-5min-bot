import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from stock_agent.analyst import Analyst, ResearchInput
from stock_agent.journal import Journal
from stock_agent.ledger import Holding, JobStatus, Ledger, PositionMeta, ThesisRole
from stock_agent.llm_client import LLMClient, LLMRequest, ModelRoute, ProviderUnavailable
from stock_agent.market_calendar import Session
from stock_agent.research_gate import ResearchGate
from stock_agent.research_pipeline import NewsEvent, ResearchPipeline, SecurityInputs
from stock_agent.research_schemas import Direction, Evidence, ResearchTask, StrictModel, strict_json, transport_schema
from stock_agent.research_store import ResearchStore
from stock_agent.screener import Candidate, Screener
from stock_agent.state_store import StateStore

NOW = datetime(2026, 11, 23, 15, tzinfo=UTC)


class TinyDraft(StrictModel):
    direction: Direction
    conviction: int
    note: str | None


def settings(**overrides):
    values = {
        "RESEARCH_PROVIDER_ORDER": ("perplexity", "openai", "anthropic"),
        "PERPLEXITY_API_KEY": "fixture", "OPENAI_API_KEY": "fixture", "ANTHROPIC_API_KEY": "",
        "RESEARCH_PERPLEXITY_MODEL": "anthropic/claude-sonnet-5-5",
        "RESEARCH_OPENAI_MODEL": "gpt-6-luna", "RESEARCH_OPENAI_CONFIRM_MODEL": "gpt-5.6-luna",
        "RESEARCH_THESIS_MAX_AGE_H": 96, "RESEARCH_MAX_ADJUSTMENT": 1,
        "RESEARCH_WEEKLY_BUDGET_USD": 15, "RESEARCH_EXIT_RESERVE_PCT": 20,
        "RESEARCH_MAX_MODEL_EXITS_PER_SESSION": 4,
    }
    return SimpleNamespace(**(values | overrides))


def test_strict_model_and_portable_schema():
    good = '{"direction":"BEARISH","conviction":9,"note":null}'
    assert strict_json(TinyDraft, good).direction == Direction.BEARISH
    for text in [good.replace("9", '"9"'), good.replace("9", "NaN"),
                 good.replace('"note":null', '"note":null,"note":"duplicate"')]:
        with pytest.raises(ValueError):
            strict_json(TinyDraft, text)
    schema = transport_schema(TinyDraft)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["direction", "conviction", "note"]


def test_perplexity_quota_falls_back_and_same_provider_confirmation_is_distinct():
    calls = []
    incidents = []

    def endpoint(request):
        body = json.loads(request.content)
        calls.append((request.url.host, body))
        if request.url.host == "api.perplexity.ai":
            return httpx.Response(401, json={"error": {"type": "insufficient_quota"}})
        sources = [{"url": "https://example.org/filing", "title": "Filing"}]
        return httpx.Response(200, json={
            "model": body["model"], "status": "completed",
            "output": [
                {"type": "web_search_call", "status": "completed", "action": {"sources": sources}},
                {"type": "message", "content": [
                    {"type": "output_text", "text": '{"direction":"BUY","conviction":8,"note":null}'},
                ]},
            ],
        })

    async def alert(error):
        incidents.append(error.kind)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            routes = LLMClient(settings(), client=http, on_failure=alert, now=lambda: NOW)
            task = LLMRequest(ResearchTask.THESIS, TinyDraft, "v1", "Decide", {"symbol": "ABC"}, grounded=True)
            first = await routes.complete(task)
            assert first.value.conviction == 8 and first.route == ModelRoute("openai", "gpt-6-luna")
            assert first.evidence[0].url == "https://example.org/filing"
            followup = await routes.complete(LLMRequest(
                ResearchTask.CONFIRM_EXIT, TinyDraft, "v1", "Decide independently", {"symbol": "ABC"}, primary=first.route,
            ))
            assert followup.route.model == "gpt-5.6-luna" and followup.degraded_confirmation

    asyncio.run(run())
    assert [host for host, _ in calls] == ["api.perplexity.ai"] + ["api.openai.com"] * 3
    assert calls[1][1]["tools"][0]["type"] == "web_search"
    assert "text" not in calls[1][1]
    assert calls[2][1]["text"]["format"]["strict"] is True and "tools" not in calls[2][1]
    assert incidents == ["quota"]


def test_refusal_is_not_hold_or_no_signal():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "refusal", "refusal": "Cannot answer"},
            ]}],
        }))) as http:
            llm = LLMClient(settings(PERPLEXITY_API_KEY=""), client=http, now=lambda: NOW)
            with pytest.raises(ProviderUnavailable) as rejected:
                await llm.complete(LLMRequest(ResearchTask.THESIS, TinyDraft, "v1", "Decide", {}))
            assert rejected.value.attempts[0].kind == "invalid_output"

    asyncio.run(run())


def test_gate_bearish_options_and_stale_or_low_conviction_no_entry():
    payload = {
        "draft": {"symbol": "ABC", "direction": "BEARISH"},
        "generated_at": NOW, "inputs_as_of": NOW, "raw_conviction": 7, "effective_conviction": 8,
    }
    health = SimpleNamespace(research_entry_blockers=lambda *_: ())
    ledger = SimpleNamespace(current_thesis=lambda _: SimpleNamespace(thesis_id="thesis", payload=payload))
    gate = ResearchGate(ledger, health, settings())
    allowed = gate.is_entry_eligible("ABC", NOW)
    assert allowed.ok and allowed.direction == Direction.BEARISH and allowed.conviction == 8
    assert not gate.is_entry_eligible("ABC", NOW + timedelta(hours=97)).ok
    payload["raw_conviction"] = 6
    assert not gate.is_entry_eligible("ABC", NOW).ok


def test_live_thesis_with_one_primary_dated_story_can_reach_nine_and_buy():
    evidence = Evidence(
        evidence_id="story-1", title="Reported results", url="https://example.org/filing",
        publisher="Issuer", published_at=NOW - timedelta(hours=1), retrieved_at=NOW,
        snippet="The issuer reported a new material contract and raised guidance.",
    )
    data = ResearchInput("ABC", NOW, "Example", "Technology", 100.0, NOW, {}, (evidence,), ("story-1",))
    draft = {
        "symbol": "ABC", "direction": "BUY", "conviction": 9, "summary": "The contract changes the base case.",
        "bull_case": "New contracted demand.","bear_case": "Customer concentration.",
        "claims": [{"text": "The issuer raised guidance today.", "evidence_ids": ["story-1"]}],
        "catalysts": ["Execution of the order"], "risks": ["Customer concentration"],
        "thesis_break": "The issuer rescinds the contract", "thesis_change": "new",
        "target_price": None, "horizon_days": 30,
    }

    def endpoint(request):
        body = json.loads(request.content)
        assert "The issuer reported" in body["input"]
        return httpx.Response(200, json={
            "status": "completed", "model": body["model"],
            "output": [
                {"type": "web_search_call", "status": "completed", "action": {
                    "sources": [{"url": "https://example.org/filing", "title": "Filing"}],
                }},
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(draft)}]},
            ],
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            analyst = Analyst(LLMClient(settings(PERPLEXITY_API_KEY=""), client=http, now=lambda: NOW))
            decision = await analyst.evaluate(data, task=ResearchTask.NEWS)
            assert decision.record.raw_conviction == decision.record.effective_conviction == 9
            assert decision.record.draft.direction is Direction.BUY
            draft["conviction"] = 6
            decision = await analyst.evaluate(data, adjustment=1, adjustment_reason="test")
            assert decision.record.effective_conviction == 6
            draft["claims"][0]["evidence_ids"] = ["does-not-exist"]
            with pytest.raises(ProviderUnavailable):
                await analyst.evaluate(data, task=ResearchTask.NEWS)

    asyncio.run(run())


def test_deterministic_rank_has_stable_ties_and_focus_does_not_make_untradable_eligible():
    rows = [
        Candidate("AAA", "A", "Technology", True, {"revenue_growth_yoy": 0.10}),
        Candidate("CCC", "C", "Technology", True, {"revenue_growth_yoy": 0.50}),
        Candidate("FOCS", "Focus", "Unknown", True, focus=True),
        Candidate("BAD", "Bad", "Unknown", False, focus=True),
    ]
    ranked = Screener.rank(rows)
    assert ranked == Screener.rank(rows[::-1])
    assert [r["symbol"] for r in ranked] == ["FOCS", "CCC", "AAA"]


def test_research_budget_and_claims_survive_restart(tmp_path):
    path = tmp_path / "s.db"
    store = StateStore(path)
    research = ResearchStore(store, settings())
    store.open()
    assert research.reserve("a", NOW, 11_000_000, exit_work=False)
    assert not research.reserve("b", NOW, 1_500_000, exit_work=False)
    assert research.reserve("c", NOW, 3_000_000, exit_work=True)
    assert research.claim_work("story", "ABC", NOW)
    assert not research.claim_work("story", "ABC", NOW)
    assert research.claim_work("rejected", "ABC", NOW)
    research.finish_work("rejected", "retry", retry_at=NOW + timedelta(minutes=15))
    store.close()

    other = StateStore(path)
    research = ResearchStore(other, settings())
    other.open()
    assert not research.claim_work("story", "ABC", NOW)
    assert not research.claim_work("rejected", "ABC", NOW)
    assert research.claim_work("rejected", "ABC", NOW + timedelta(minutes=15))
    assert not research.reserve("d", NOW, 1_100_000, exit_work=True)
    research.settle("a", 2_000_000)
    assert research.available_budget(NOW)["billed_or_reserved_microusd"] == 5_000_000
    assert research.reserve("d", NOW, 1_100_000, exit_work=True)
    for i in range(4):
        assert research.reserve_exit("2026-11-23", str(i))
    assert not research.reserve_exit("2026-11-23", "4")
    other.close()


@pytest.mark.parametrize(("held", "closes_during_confirmation"), [
    (False, False), (True, False), pytest.param(True, True, id="confirmed_at_1603_runs_next_session_open_plus_five"),
])
def test_live_story_creates_replayable_intent_once_with_blind_exit(monkeypatch, tmp_path, held,
                                                               closes_during_confirmation):
    config = settings(PERPLEXITY_API_KEY="")
    journal = Journal(tmp_path / "journal", clock=lambda: NOW)
    state = StateStore(tmp_path / "s.db", journal, clock=lambda: NOW)
    spend = ResearchStore(state, config)
    state.open()
    ledger = Ledger(state, journal, clock=lambda: NOW)
    gate = ResearchGate(ledger, SimpleNamespace(research_entry_blockers=lambda *_: ()), config)
    evidence = Evidence(
        evidence_id="filing", title="Issuer update", url="https://example.org/new",
        publisher="Issuer", published_at=NOW - timedelta(minutes=15), retrieved_at=NOW, snippet="Material update",
    )
    security = SecurityInputs("ABC", "Example", "Technology", Decimal("100"), NOW, True)
    event = NewsEvent("ABC", "abc-material-1", evidence, 8)
    session = Session(NOW.date(), NOW - timedelta(minutes=30), NOW + timedelta(hours=6))
    tomorrow = timedelta(days=1)
    next_session = Session(session.date + tomorrow, session.opens_at + tomorrow, session.closes_at + tomorrow)
    calendar = SimpleNamespace(session_on=lambda _: session, next_session=lambda _: next_session)
    holding = Holding("ABC", Decimal("1"), Decimal("1"), Decimal("100"), Decimal("100"), True,
                      Decimal("100"), Decimal("100"), meta=PositionMeta("p-1", "ABC", opened_at=NOW))
    snapshot = SimpleNamespace(holdings=(holding,) if held else ())
    calls = []
    now = NOW

    class Sources:
        async def universe(self, now):
            return ("ABC",)

        async def news(self, symbols, now):
            assert symbols == ("ABC", "FOCS")
            return (event, event)

        async def securities(self, symbols, now):
            return {"ABC": security} if symbols else {}

    def endpoint(request):
        nonlocal now
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "gpt-5.6-luna":
            assert "first model reasoning" not in body["input"]
            if closes_during_confirmation:
                now = session.closes_at + timedelta(minutes=3)
            value = {"symbol": "ABC", "verdict": "CONFIRM_BREAK", "conviction": 8,
                     "reason": "Independent", "evidence_ids": ["filing"]}
            tools = []
        else:
            value = {
                "symbol": "ABC", "direction": "EXIT_PROPOSED" if held else "BUY", "conviction": 9,
                "summary": "first model reasoning", "bull_case": "Upside", "bear_case": "Downside",
                "claims": [{"text": "The issuer changed guidance.", "evidence_ids": ["filing"]}],
                "catalysts": [], "risks": [], "thesis_break": "Reported change", "target_price": None,
                "thesis_change": "broken" if held else "new", "horizon_days": 15,
            }
            tools = [{"type": "web_search_call", "status": "completed",
                      "action": {"sources": [{"url": "https://example.org/new", "title": "Issuer update"}]}}]
        return httpx.Response(200, json={
            "status": "completed", "model": body["model"], "output": [
                *tools, {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]},
            ],
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            analyst = Analyst(LLMClient(config, client=http, spend=spend, now=lambda: now))
            pipe = ResearchPipeline(config, ledger, calendar, analyst, spend, Sources(), gate,
                                    clock=lambda: now, focus=lambda **_: ("FOCS",))
            # Held symbols lead focus names, which lead the rest of the universe.
            if not held:
                async def for_unheld(symbols, now):
                    assert symbols == ("FOCS", "ABC")
                    return (event, event)

                monkeypatch.setattr(pipe.sources, "news", for_unheld)
            first = await pipe.run("news", snapshot)
            if closes_during_confirmation:
                assert first.status is JobStatus.DONE and len(first.intents) == 1
                intent = first.intents[0]
                ready_at = next_session.opens_at + timedelta(minutes=5)
                assert intent.side == "sell" and intent.position_id == "p-1"
                assert intent.session_date == next_session.date and intent.expires_at == next_session.closes_at
                assert intent.next_attempt_at == ready_at
                assert (await pipe.run("news", snapshot)).intents == (intent,)
                ledger.enqueue_intents(first.intents)
                assert not ledger.due_intents(now=ready_at - timedelta(seconds=1))
                assert [item.dedupe_key for item in ledger.due_intents(now=ready_at)] == [intent.dedupe_key]
                assert len(calls) == 2
                return
            assert first.status is JobStatus.DONE and len(first.intents) == 1
            intent = first.intents[0]
            assert intent.side == ("sell" if held else "buy")
            assert intent.position_id == ("p-1" if held else None)
            assert intent.expires_at == session.closes_at
            if not held:
                assert intent.meta["minimum_conviction"] == 8
                assert intent.meta["signal_at"] == NOW.isoformat()
            replayed = await pipe.run("news", snapshot)
            assert replayed.intents == first.intents
            assert replayed.option_candidates == first.option_candidates
            if not held:
                assert first.option_candidates[0].direction is Direction.BUY
                pipe.acknowledge_options((first.option_candidates[0].story_id,))
            ledger.enqueue_intents(replayed.intents)
            after_ack = await pipe.run("news", snapshot)
            assert not after_ack.intents and not after_ack.option_candidates
            assert len(calls) == (2 if held else 1)
            if held:
                confirmation = ledger.get_thesis(intent.thesis_ids[1])
                assert confirmation.role is ThesisRole.EXIT_CONFIRMATION
                assert confirmation.payload["degraded"] is True

    asyncio.run(run())
    state.close()


def test_regime_change_requires_both_fresh_prices_and_never_runs_a_thesis(tmp_path):
    config = settings()
    state = StateStore(tmp_path / "s.db", clock=lambda: NOW)
    store = ResearchStore(state, config)
    state.open()
    benchmark = {
        "SPY": SecurityInputs("SPY", "SPY", "Unknown", Decimal("100"), NOW, True, {"session_return": 0.01}),
        "QQQ": SecurityInputs("QQQ", "QQQ", "Unknown", Decimal("100"), NOW, True, {"session_return": 0.01}),
    }

    class Sources:
        async def market(self, now):
            return tuple(benchmark.values())

    pipeline = ResearchPipeline(config, None, None, None, store, Sources(), None, clock=lambda: NOW)

    async def run():
        assert await pipeline.observe_regime() is None  # First observation seeds baseline.
        benchmark["SPY"] = SecurityInputs("SPY", "SPY", "Unknown", Decimal("95"), NOW, True, {"session_return": -.05})
        benchmark["QQQ"] = SecurityInputs("QQQ", "QQQ", "Unknown", Decimal("96"), NOW - timedelta(hours=1),
                                         True, {"session_return": -.04})
        assert await pipeline.observe_regime() is None  # Stale second benchmark does not fire.
        benchmark["QQQ"] = SecurityInputs("QQQ", "QQQ", "Unknown", Decimal("96"), NOW, True, {"session_return": -.04})
        assert await pipeline.observe_regime() == ("normal", "cautious")
        assert await pipeline.observe_regime() is None
        assert state.query("SELECT * FROM research_work") == []

    asyncio.run(run())
    state.close()
