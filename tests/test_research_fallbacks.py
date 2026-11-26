import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_agent.analyst import Analyst, ResearchInput
from stock_agent.config import Settings
from stock_agent.ledger import ThesisRole
from stock_agent.llm_client import LLMClient, ModelRoute, ProviderUnavailable
from stock_agent.market_calendar import Session
from stock_agent.research_focus import FocusItem
from stock_agent.research_schemas import Direction, Evidence
from stock_agent.research_store import ResearchStore
from stock_agent.scheduler import StockAgentScheduler
from tests.test_grounded_transport import completed, request
from tests.test_research_contracts import NOW, settings
from tests.test_scheduled_best_ideas import FILING, response, rig


@pytest.mark.parametrize("other_source", [False, True])
def test_explicit_blank_page_cannot_be_grounding_or_revived_by_annotation(other_source, caplog):
    url = "https://example.org/empty"
    wire = completed(sources=[
        {"url": url, "content": " \n", "snippet": "Preview only", "published_at": NOW.isoformat()},
        *([{"url": FILING, "content": "Reported audited results", "published_at": NOW.isoformat()}]
          if other_source else []),
    ])
    wire["output"][1]["content"][0]["annotations"] = [{"type": "url_citation", "url": url + "/#result"}]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=wire))) as http:
            llm = LLMClient(settings(OPENAI_API_KEY=""), client=http, now=lambda: NOW)
            if other_source:
                result = await llm.complete(request())
                [(actual, snippet)] = [(item.url, item.snippet) for item in result.evidence]
                assert actual == FILING and snippet == "Reported audited results"
            else:
                with pytest.raises(ProviderUnavailable) as exc:
                    await llm.complete(request())
                assert exc.value.failure_codes == {"failure_codes": ["perplexity:blank_page_content"]}

    asyncio.run(run())
    assert "code=blank_page_content, count=1" in caplog.text


@pytest.mark.parametrize("bad_claim", [False, True])
def test_owner_fallback_reuses_returned_and_local_ids_and_repairs_only_claude_summary(bad_claim):
    local = Evidence(evidence_id="sec-abc-001", url=FILING, title="10-Q", publisher="SEC EDGAR",
                     published_at=NOW - timedelta(days=1), retrieved_at=NOW, snippet="Operating income rose.")
    data = ResearchInput("ABC", NOW, "Example", "Technology", 20, None, {}, (local,))
    owner = FocusItem("ABC", None, "Review the risks.", NOW.date() - timedelta(days=2), NOW.date(),
                      stance="bullish", owner_conviction=8)
    providers = []
    metered = []
    web_id = "returned-10k"

    def endpoint(req):
        body = json.loads(req.content)
        providers.append(req.url.host)
        usage = {"input_tokens": 10, "output_tokens": 10}
        if len(providers) == 1:
            assert body.get("tools")
            return httpx.Response(200, json=completed(text="not JSON", sources=[{
                "url": "https://example.org/results", "id": web_id, "title": "Earnings",
                "content": "Earnings rose 10 percent.", "published_at": (NOW - timedelta(days=1)).isoformat(),
            }]) | {"usage": usage})
        assert "tools" not in body
        if len(providers) == 2:
            packet = json.loads(body["input"].split("INPUT_JSON:\n")[1])
            assert packet["web_evidence"][0]["evidence_id"] == web_id
            assert packet["original_input"]["evidence"][0]["evidence_id"] == local.evidence_id
            return httpx.Response(500, json={"error": {"code": "server_error"}})
        packet = json.loads(body["messages"][0]["content"].split("INPUT_JSON:\n")[1])
        if len(providers) == 3:
            assert packet["web_evidence"][0]["evidence_id"] == web_id
            assert packet["original_input"]["evidence"][0]["evidence_id"] == local.evidence_id
            draft = json.loads(response(body, cite=web_id).json()["output"][1]["content"][0]["text"])
            draft.update(stop_price=17, veto_claim_index=None, summary="x" * 1001)
            draft["claims"][0]["evidence_ids"].append("made-up" if bad_claim else local.evidence_id)
        else:
            assert packet == {"summary": "x" * 1001}
            draft = {"summary": "A short summary."}
        return httpx.Response(200, json={"stop_reason": "end_turn", "usage": usage,
                                        "content": [{"type": "text", "text": json.dumps(draft)}]})

    async def run():
        cfg = settings(ANTHROPIC_API_KEY="fixture", RESEARCH_ANTHROPIC_MODEL="claude-sonnet-5-5")
        spend = SimpleNamespace(reserve=lambda *_, **kw: metered.append(kw) or True, settle=lambda *_: None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            analyst = Analyst(LLMClient(cfg, client=http, spend=spend, now=lambda: NOW))
            if bad_claim:
                with pytest.raises(ProviderUnavailable) as exc:
                    await analyst.evaluate(data, owner=owner)
                assert exc.value.attempts[-1].safe_code == "thesis_unknown_evidence"
            else:
                evaluation = await analyst.evaluate(data, owner=owner)
                assert evaluation.record.draft.direction is Direction.BUY
                assert evaluation.record.draft.summary == "A short summary."
                assert evaluation.record.draft.claims[0].evidence_ids == [web_id, local.evidence_id]
                assert evaluation.owner_stop_price == 17
                assert evaluation.record.normalization_notes == {"owner_summary_repaired": 1}

    asyncio.run(run())
    assert providers == ["api.perplexity.ai", "api.openai.com", "api.anthropic.com", "api.anthropic.com"]
    assert metered == [{"exit_work": False}] * 4


@pytest.mark.parametrize("document", [False, True])
def test_owner_fallback_after_primary_outage_uses_real_dated_filing_not_index(document):
    url = FILING if document else FILING.replace(".htm", "-index.htm")
    local = Evidence(evidence_id="sec-abc-001", url=url, title="10-Q", publisher="SEC EDGAR",
                     published_at=NOW - timedelta(days=1), retrieved_at=NOW, snippet="Issuer text.")
    data = ResearchInput("ABC", NOW, "Example", "Technology", 20, None, {}, (local,))
    owner = FocusItem("ABC", None, "Review the risks.", NOW.date() - timedelta(days=2), NOW.date(),
                      stance="bullish", owner_conviction=8)
    calls = []

    def endpoint(req):
        body = json.loads(req.content)
        calls.append(body)
        if req.url.host == "api.perplexity.ai":
            return httpx.Response(503)
        if body.get("tools"):
            assert not document
            return httpx.Response(503)
        packet = json.loads(body["input"].split("INPUT_JSON:\n")[1])
        assert packet["web_evidence"] == [] and packet["original_input"]["evidence"][0]["evidence_id"] == local.evidence_id
        wire = response(body, cite=local.evidence_id).json()
        item = wire["output"][1]["content"][0]
        draft = json.loads(item["text"])
        item["text"] = json.dumps(draft | {"stop_price": 17, "veto_claim_index": None})
        return httpx.Response(200, json=wire)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            analyst = Analyst(LLMClient(settings(), client=http, now=lambda: NOW))
            if document:
                result = await analyst.evaluate(data, owner=owner)
                assert result.record.draft.claims[0].evidence_ids == [local.evidence_id]
            else:
                with pytest.raises(ProviderUnavailable):
                    await analyst.evaluate(data, owner=owner)

    asyncio.run(run())
    assert len(calls) == 2


def test_shadow_batch_is_off_by_default_ranked_unique_and_idempotent(tmp_path):
    from stock_agent.research_shadow import run_shadow_research

    clock, ledger, _, _ = rig(tmp_path)
    store = ResearchStore(ledger.store, settings())
    assert Settings().RESEARCH_SHADOW_WEEKLY_DEEP is False
    assert Settings().RESEARCH_SHADOW_BUY_SECOND_OPINION is False
    candidates = [
        ("ABC", 10, Direction.BUY), ("ABC", 9, Direction.BUY), ("DEF", 8, Direction.BEARISH),
        ("GHI", 7, Direction.BUY), ("JKL", 7, Direction.BUY), ("MNO", 9, Direction.HOLD),
    ]
    for index, (symbol, conviction, direction) in enumerate(candidates):
        when = clock.t - timedelta(minutes=index)
        inputs = ResearchInput(symbol, when, symbol, "Tech", 20, None, {}, (),
                               entry_thesis={"prior": True}, current_thesis={"prior": True}).payload()
        ledger.record_thesis(symbol, {"draft": {"direction": direction}, "provider": "openai",
                                      "model": "gpt-6-luna", "inputs": inputs, "evidence": [],
                                      "effective_conviction": conviction},
                             ThesisRole.ENTRY_CANDIDATE, generated_at=when)
    seen = []

    async def review(data, primary, *, deep):
        seen.append((data.symbol, deep))
        assert primary == ModelRoute("openai", "gpt-6-luna")
        record = SimpleNamespace(draft=SimpleNamespace(direction=Direction.BUY, claims=[]), raw_conviction=9,
                                 provider="anthropic", model="claude-haiku-5-5")
        return SimpleNamespace(record=record, result=SimpleNamespace(metered_microusd=50))

    async def run():
        analyst = SimpleNamespace(shadow_review=review)
        await run_shadow_research(analyst, store, clock.t, buys=False, weekly=False)
        assert not seen
        assert await run_shadow_research(analyst, store, clock.t, buys=True, weekly=True) == {
            "reviewed": 7, "unavailable": 0,
        }
        assert await run_shadow_research(analyst, store, clock.t, buys=True, weekly=True) == {
            "reviewed": 0, "unavailable": 0,
        }

    asyncio.run(run())
    assert seen == [("ABC", True), ("DEF", True), ("GHI", True), ("ABC", False), ("ABC", False),
                    ("GHI", False), ("JKL", False)]
    assert ledger.store.query("SELECT count(*) FROM theses")[0][0] == len(candidates)
    assert all(ledger.current_thesis(symbol) is None for symbol, _, _ in candidates)
    assert not store.unqueued_proposals()


@pytest.mark.parametrize("budget", [False, True])
def test_shadow_second_opinion_blind_independent_and_cannot_spend_exit_reserve(budget):
    seen = []
    local = Evidence(evidence_id="issuer", url=FILING, title="10-Q", publisher="SEC EDGAR",
                     published_at=NOW - timedelta(days=1), retrieved_at=NOW, snippet="Operating income rose.")
    data = ResearchInput("ABC", NOW, "Example", "Technology", 20, None, {}, (local,), holding={},
                         entry_thesis={"prior": True}, current_thesis={"prior": True})
    reservations = []

    def endpoint(req):
        body = json.loads(req.content)
        seen.append(body)
        packet = json.loads(body["input"].split("INPUT_JSON:\n")[1])
        assert packet["entry_thesis"] is None and packet["current_thesis"] is None
        assert packet["evidence"][0]["evidence_id"] == "issuer"
        assert body["model"] == "gpt-5.6-luna" and not body.get("tools")
        return response(body, cite="issuer")

    async def run():
        spend = SimpleNamespace(reserve=lambda *_, **kw: reservations.append(kw) or budget,
                                settle=lambda *_: None)
        cfg = settings(PERPLEXITY_API_KEY="")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            analyst = Analyst(LLMClient(cfg, client=http, spend=spend, now=lambda: NOW))
            primary = ModelRoute("openai", "gpt-6-luna")
            if budget:
                result = await analyst.shadow_review(data, primary, deep=False)
                assert result.result.route.underlying != primary.underlying
                assert result.result.evidence == ()  # Ignore sources not in the blind review packet.
            else:
                with pytest.raises(ProviderUnavailable) as exc:
                    await analyst.shadow_review(data, primary, deep=False)
                assert exc.value.attempts[-1].kind == "budget"

    asyncio.run(run())
    assert reservations == [{"exit_work": False}] and len(seen) == int(budget)


def test_shadow_weekly_window_uses_last_covered_session_even_with_holiday():
    now = NOW + timedelta(days=2)
    session = Session(now.date(), now - timedelta(hours=6), now - timedelta(minutes=16))
    following = Session(now.date() + timedelta(days=5), now + timedelta(days=5), now + timedelta(days=5, hours=6))
    scheduler = object.__new__(StockAgentScheduler)
    scheduler.research = object()
    scheduler.config = Settings(RESEARCH_SHADOW_WEEKLY_DEEP=True)
    scheduler.calendar = SimpleNamespace(session_on=lambda _: session, next_session=lambda _: following)
    assert not scheduler._should_run_research_shadow(now - timedelta(minutes=1))
    assert scheduler._should_run_research_shadow(now)
    scheduler.calendar.next_session = lambda _: None
    assert not scheduler._should_run_research_shadow(now)
    scheduler.config = Settings(RESEARCH_SHADOW_BUY_SECOND_OPINION=True)
    assert scheduler._should_run_research_shadow(now)


def test_deep_shadow_uses_metered_openai_search_without_trade_lane_health_side_effects():
    calls = []
    cooldowns = []
    metered = []

    def endpoint(req):
        body = json.loads(req.content)
        calls.append(body)
        if body.get("tools"):
            return response(body)
        assert "retrospective shadow review" in body["instructions"]
        return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})

    async def run():
        cfg = settings()
        spend = SimpleNamespace(reserve=lambda *_, **kw: metered.append(kw) or True,
                                settle=lambda *_: None, set_cooldown=lambda *a, **kw: cooldowns.append(a))
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            llm = LLMClient(cfg, client=http, spend=spend, now=lambda: NOW)
            data = ResearchInput("ABC", NOW, "Example", "Technology", 20, None, {}, ())
            with pytest.raises(ProviderUnavailable) as exc:
                await Analyst(llm).shadow_review(data, ModelRoute("openai", "gpt-6-luna"), deep=True)
            assert exc.value.attempts[-1].kind == "rate_limit"
            assert llm.is_healthy()

    asyncio.run(run())
    assert [body["model"] for body in calls] == ["gpt-6.2-sol"] * 2
    assert all(body["reasoning"] == {"effort": "medium"} for body in calls)
    assert metered == [{"exit_work": False}] * 2 and not cooldowns
