"""Live-shape replay of the 25 Nov pre-open research outage, through the real scheduler and pipeline.

On 25 Nov every grounded research route failed before the 08:00 pre-open scan could write a thesis:

* OpenAI Responses answered HTTP 400 ``invalid_type`` on ``tools[0].return_token_budget`` sent as a
  number (the web_search tool takes no numeric budget;
  https://platform.openai.com/docs/api-reference/responses/create).
* The Perplexity Agent API counts search-step and answer generation against one ``max_output_tokens``
  bound, so a grounded call capped near 1,800 tokens ended ``status: incomplete`` with
  ``incomplete_details.reason: max_output_tokens`` (https://docs.perplexity.ai/api-reference/agent-post).
  Its search results carry a day-only ``date`` (``YYYY-MM-DD``) and no timestamp.
* Claude's web search with ``max_uses: 1`` returned one usable ``web_search_tool_result`` plus two
  ``web_search_tool_result_error`` blocks with ``error_code: max_uses_exceeded``, and the evidence pass
  stopped with ``stop_reason: max_tokens``
  (https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool).

The provider fakes below answer in exactly those shapes. The scenarios assert that the pre-open scan now
writes theses and that the owner NVDA review reaches a decision, for the production provider order and
for each provider on its own. Only synthetic round numbers and example.com URLs are used.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from stock_agent.config import Config
from tests.fakes.base import json_response, request_json
from tests.fakes.providers import LLMReply
from tests.scenarios.owner_support import ADDED, OwnerHarness, et

START = et(2026, 11, 25, 7, 0)
SOURCE_DAY = "2026-11-24"   # Perplexity search results: a publication day only
PPLX_SEARCH_TOKENS = 1_900  # search-step generation; with the 300-token answer, above 1,800 and below 4,096
WEB = [{"url": "https://example.com/fixture/issuer-update", "title": "Synthetic issuer update", "date": SOURCE_DAY}]


def _numeric_budget(body: dict) -> bool:
    return any(t.get("type") == "web_search" and isinstance(t.get("return_token_budget"), int | float)
               for t in body.get("tools") or [])


class LiveShapeProviders:
    """Install live-shaped handlers on the three research fakes, all driven by the harness's scripted drafts."""

    def __init__(self, h: OwnerHarness):
        self.h = h
        self.calls: dict[str, list[str]] = {"openai": [], "perplexity": [], "anthropic": []}
        w = h.world
        w.perplexity.clear_faults()  # the base harness answers Perplexity 401 to force OpenAI; this replay uses all three
        openai_fake, pplx_fake, claude_fake = w.openai, w.perplexity, w.anthropic
        openai_default, pplx_default = openai_fake._responses, pplx_fake._agent

        def openai(request):
            body = request_json(request) or {}
            if _numeric_budget(body):
                self.calls["openai"].append("400 invalid_type")
                return json_response({"error": {"type": "invalid_request_error", "code": "invalid_type",
                                                "param": "tools[0].return_token_budget",
                                                "message": "Invalid type for 'tools[0].return_token_budget'."}}, 400)
            self.calls["openai"].append("answered")
            return openai_default(request)

        def perplexity(request):
            body = request_json(request) or {}
            reply = self._draft(body)
            if body.get("tools"):
                reply = LLMReply(text=reply.text, sources=WEB, output_tokens=300)
                total = reply.output_tokens + PPLX_SEARCH_TOKENS
                cap = body.get("max_output_tokens")
                if isinstance(cap, int) and total > cap:
                    self.calls["perplexity"].append("incomplete max_output_tokens")
                    pplx_fake.prompts.append(body)
                    return {"id": "resp_fake_cap", "object": "response", "status": "incomplete",
                            "model": body.get("model"), "incomplete_details": {"reason": "max_output_tokens"},
                            "output": [{"type": "search_results", "queries": ["q"], "results": [
                                {"id": 1, "title": WEB[0]["title"], "url": WEB[0]["url"], "date": SOURCE_DAY,
                                 "snippet": "", "source": "web"}]}],
                            "usage": {"input_tokens": 1000, "output_tokens": cap, "total_tokens": 1000 + cap,
                                      "cost": {"currency": "USD", "total_cost": 0.001}}}
            self.calls["perplexity"].append("answered")
            pplx_fake.replies.insert(0, reply)
            return pplx_default(request)

        def claude(request):
            body = request_json(request) or {}
            claude_fake.prompts.append(body)
            if body.get("tools"):  # evidence pass: one good result, then the search-limit errors, then the cap
                self.calls["anthropic"].append("evidence max_tokens")
                result = [{"type": "web_search_result", "url": WEB[0]["url"], "title": WEB[0]["title"],
                           "page_age": "November 24, 2026", "encrypted_content": "fixture"}]
                limit = {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}
                content = [
                    {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
                    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": result},
                    {"type": "server_tool_use", "id": "srvtoolu_2", "name": "web_search", "input": {"query": "q2"}},
                    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_2", "content": limit},
                    {"type": "server_tool_use", "id": "srvtoolu_3", "name": "web_search", "input": {"query": "q3"}},
                    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_3", "content": limit},
                    {"type": "text", "text": "The issuer published an update on"},
                ]
                return {"id": "msg_fixture_ev", "type": "message", "role": "assistant", "model": body.get("model"),
                        "content": content, "stop_reason": "max_tokens",
                        "usage": {"input_tokens": 1000, "output_tokens": 1800,
                                  "server_tool_use": {"web_search_requests": 1}}}
            self.calls["anthropic"].append("answered")
            return {"id": "msg_fixture", "type": "message", "role": "assistant", "model": body.get("model"),
                    "content": [{"type": "text", "text": self._draft(body).text}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1000, "output_tokens": 300}}

        openai_fake.on("POST", "/v1/responses", openai)
        pplx_fake.on("POST", "/v1/agent", perplexity)
        claude_fake.on("POST", "/v1/messages", claude)

    def _draft(self, body: dict) -> LLMReply:
        """The harness's scripted draft for any provider's request shape (Claude's second pass wraps the input)."""
        text = body.get("input") or body["messages"][0]["content"]
        head, blob = text.split("INPUT_JSON:\n", 1)
        data = json.loads(blob)
        data = data.get("original_input", data)
        system = body.get("instructions") or body.get("system") or ""
        shim = {"input": head + "INPUT_JSON:\n" + json.dumps(data), "instructions": system}
        draft = self.h._owner_draft(shim)
        return LLMReply(text=json.dumps(draft))


def drive(h, scenario):
    async def wrapper():
        try:
            await scenario()
        finally:
            await h.stop()

    asyncio.run(wrapper())


@pytest.mark.parametrize("order", [
    ("perplexity", "openai", "anthropic"),  # production order
    ("openai",), ("perplexity",), ("anthropic",),
], ids=["production-order", "openai-only", "perplexity-only", "anthropic-only"])
@pytest.mark.world_start(START)
def test_preopen_scan_writes_theses_and_owner_review_decides_on_live_provider_shapes(
        world, monkeypatch, tmp_path, order):
    h = OwnerHarness(world, monkeypatch, others=("AAPL",), owner="BUY",
                     ordinary=lambda body, data: "BUY").install(tmp_path=tmp_path)
    monkeypatch.setattr(Config, "UNIVERSE_SIZE", 2)
    monkeypatch.setattr(Config, "RESEARCH_PROVIDER_ORDER", order)
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "test-anthropic")
    live = LiveShapeProviders(h)

    async def scenario():
        await h.start()
        h.headline("NVIDIA presents at an industry conference", minutes_ago=30, symbols=["NVDA", "AAPL"])
        await h.advance(25, 7, 50)
        h.fresh_quote()
        await h.advance(25, 8, 0)
        row = await h.run_job("prescan")
        funnel = row.detail["entry_funnel"]
        assert row.status.value == "done", (row.status, row.detail, live.calls)
        assert funnel["thesis_written"] == 2, (row.detail, live.calls)
        assert funnel.get("handoff_staged") == 2, (row.detail, live.calls)
        assert [p[2] for p in h.owner_prompts()] == ["BUY"], ("one owner review, and it decided", h.prompts)
        decided = [(e["symbol"], e["outcome"], e.get("staged")) for e in h.owner_events()]
        assert decided == [(h.symbol, "proposed", True)], decided
        ledger = h.scheduler.ledger
        assert not ledger.owner_directive_vetoed(h.symbol, ADDED)
        assert h.scheduler.research.analyst.llm.is_healthy(), live.calls
        first = order[0]
        assert live.calls[first] and all(c in ("answered", "evidence max_tokens") for c in live.calls[first]), live.calls
        assert not any(c.startswith(("400", "incomplete")) for calls in live.calls.values() for c in calls), live.calls
        if first == "anthropic":
            assert "evidence max_tokens" in live.calls["anthropic"], live.calls
        if first == "openai":
            assert all("return_token_budget" not in t for b in h.world.openai.prompts for t in b.get("tools") or [])

    drive(h, scenario)
