import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_agent.llm_client import LLMClient, LLMRequest, ModelRoute, ProviderUnavailable
from stock_agent.research_gate import ResearchGate
from stock_agent.research_schemas import Direction, ResearchTask, StrictModel, strict_json, transport_schema

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
        "RESEARCH_THESIS_MAX_AGE_H": 96,
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
    assert [host for host, _ in calls] == ["api.perplexity.ai", "api.openai.com", "api.openai.com"]
    assert calls[1][1]["tools"][0]["type"] == "web_search"
    assert calls[1][1]["text"]["format"]["strict"] is True
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
