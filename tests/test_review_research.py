"""Cross-family review findings for the research pipeline (RV-R-xx).

These tests exercise a single configured provider and run entirely offline.
HTTPX MockTransport stands in for OpenAI.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx

from stock_agent.analyst import Analyst
from stock_agent.journal import Journal
from stock_agent.ledger import Holding, Ledger, PositionMeta
from stock_agent.llm_client import LLMClient
from stock_agent.market_calendar import Session
from stock_agent.research_gate import ResearchGate
from stock_agent.research_pipeline import NewsEvent, ResearchPipeline, SecurityInputs
from stock_agent.research_schemas import Evidence
from stock_agent.research_store import ResearchStore
from stock_agent.state_store import StateStore

START = datetime(2026, 11, 23, 15, tzinfo=UTC)  # Monday 10:00 New York


def openai_only(**overrides):
    values = {
        "RESEARCH_PROVIDER_ORDER": ("perplexity", "openai", "anthropic"),
        "PERPLEXITY_API_KEY": "", "OPENAI_API_KEY": "fixture", "ANTHROPIC_API_KEY": "",
        "RESEARCH_OPENAI_MODEL": "gpt-6-luna", "RESEARCH_OPENAI_CONFIRM_MODEL": "gpt-5.6-luna",
        "RESEARCH_THESIS_MAX_AGE_H": 96, "RESEARCH_MAX_ADJUSTMENT": 1,
        "RESEARCH_WEEKLY_BUDGET_USD": 15, "RESEARCH_EXIT_RESERVE_PCT": 20,
        "RESEARCH_MAX_MODEL_EXITS_PER_SESSION": 4,
    }
    return SimpleNamespace(**(values | overrides))


def _ok(body, held=True):
    if body["model"] == "gpt-5.6-luna":
        value = {"symbol": "ABC", "verdict": "CONFIRM_BREAK", "conviction": 8,
                 "reason": "Independent", "evidence_ids": ["filing"]}
        tools = []
    else:
        value = {
            "symbol": "ABC", "direction": "EXIT_PROPOSED" if held else "BUY", "conviction": 9,
            "summary": "s", "bull_case": "b", "bear_case": "d",
            "claims": [{"text": "The issuer cut guidance.", "evidence_ids": ["filing"]}],
            "catalysts": [], "risks": [], "thesis_break": "Guidance cut", "target_price": None,
            "thesis_change": "broken" if held else "new", "horizon_days": 15,
        }
        tools = [{"type": "web_search_call", "status": "completed",
                  "action": {"sources": [{"url": "https://example.org/new", "title": "Issuer update"}]}}]
    return httpx.Response(200, json={"status": "completed", "model": body["model"], "output": [
        *tools, {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]},
    ]})


class Rig:
    """One held position with a bad-news story, OpenAI as the only provider."""

    def __init__(self, tmp_path, respond, config=None):
        self.now = START
        self.config = config or openai_only()
        clock = lambda: self.now  # noqa: E731
        self.journal = Journal(tmp_path / "journal", clock=clock)
        self.state = StateStore(tmp_path / "s.db", self.journal, clock=clock)
        self.spend = ResearchStore(self.state, self.config)
        self.state.open()
        self.ledger = Ledger(self.state, self.journal, clock=clock)
        gate = ResearchGate(self.ledger, SimpleNamespace(research_entry_blockers=lambda *_: ()), self.config)
        evidence = Evidence(
            evidence_id="filing", title="Issuer update", url="https://example.org/new", publisher="Issuer",
            published_at=START - timedelta(minutes=15), retrieved_at=START, snippet="Guidance cut",
        )
        event = NewsEvent("ABC", "abc-bad-1", evidence, 9)
        security = SecurityInputs("ABC", "Example", "Technology", Decimal("100"), START, True)
        session = Session(START.date(), START - timedelta(minutes=30), START + timedelta(hours=6))
        calendar = SimpleNamespace(session_on=lambda _: session, next_session=lambda _: None)
        holding = Holding("ABC", Decimal("10"), Decimal("10"), Decimal("100"), Decimal("1000"), True,
                          Decimal("100"), Decimal("1000"), meta=PositionMeta("p-1", "ABC", opened_at=START))
        self.snapshot = SimpleNamespace(holdings=(holding,))
        self.calls = []

        rig = self

        class Sources:
            async def universe(self, now):
                return ("ABC",)

            async def news(self, symbols, now):
                return (event,)

            async def securities(self, symbols, now):
                return {"ABC": security} if symbols else {}

        def endpoint(request):
            body = json.loads(request.content)
            rig.calls.append(body["model"])
            return respond(body, len(rig.calls))

        self.sources = Sources()
        self.gate = gate
        self.endpoint = endpoint

    def passes(self, count, every=timedelta(minutes=5), *, restart=False):
        """Run `count` news passes `every` apart; return all intents produced."""
        out = []

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.endpoint)) as http:
                def pipeline():
                    analyst = Analyst(LLMClient(self.config, client=http, spend=self.spend, now=lambda: self.now))
                    return ResearchPipeline(self.config, self.ledger, SimpleNamespace(
                        session_on=lambda _: Session(START.date(), START - timedelta(minutes=30),
                                                     START + timedelta(hours=6)),
                        next_session=lambda _: None), analyst, self.spend, self.sources, self.gate,
                        focus=lambda **_: ())

                pipe = pipeline()
                for index in range(count):
                    if index and restart:
                        self.state.close()
                        self.state.open()
                        pipe = pipeline()
                    outcome = await pipe.run("news", self.snapshot)
                    # Unqueued proposals replay each pass until enqueued; count each once.
                    out.extend(i for i in outcome.intents if i.dedupe_key not in {o.dedupe_key for o in out})
                    self.now += every

        asyncio.run(go())
        self.state.close()
        return out


def test_held_bad_news_is_retried_after_transient_provider_failure(tmp_path):
    def respond(body, n):
        return httpx.Response(503, json={}) if n == 1 else _ok(body)

    intents = Rig(tmp_path, respond).passes(4)
    assert [i.side for i in intents] == ["sell"], "bad news must be re-reviewed once the provider recovers"


def test_exit_confirmation_is_retried_after_transient_rate_limit(tmp_path):
    def respond(body, n):
        if body["model"] == "gpt-5.6-luna" and n == 2:
            return httpx.Response(429, json={"error": {"type": "rate_limit"}})
        return _ok(body)

    rig = Rig(tmp_path, respond)
    intents = rig.passes(4, restart=True)
    assert [i.side for i in intents] == ["sell"], "confirmation must be retried after the rate limit clears"
    assert rig.calls == ["gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-luna"], "do not redraw the primary result"


def test_exit_reserve_covers_primary_review_of_held_position_news(tmp_path):
    rig = Rig(tmp_path, lambda body, n: _ok(body))
    # Entry spend for the week sits exactly at the 80% entry cap (USD 12 of 15).
    assert rig.spend.reserve("entries", START, 12_000_000, exit_work=False)
    rig.spend.settle("entries", 12_000_000)
    intents = rig.passes(1)
    assert [i.side for i in intents] == ["sell"], "held-position bad news must be able to spend the exit reserve"


def test_control_held_bad_news_exits_when_provider_is_healthy(tmp_path):
    """Control for RV-R-01..03: the rig produces exactly one sell when nothing fails."""
    rig = Rig(tmp_path, lambda body, n: _ok(body))
    intents = rig.passes(3)
    assert [i.side for i in intents] == ["sell"]
    assert rig.calls == ["gpt-6-luna", "gpt-5.6-luna"]
