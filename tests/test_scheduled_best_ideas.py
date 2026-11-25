import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from stock_agent.analyst import BEST_IDEAS_PROMPT_VERSION, THESIS_PROMPT_VERSION, Analyst
from stock_agent.config import Config
from stock_agent.ledger import IntentStatus, NewIntent, NewStockEntryRefused
from stock_agent.llm_client import LLMClient
from stock_agent.market_calendar import Session
from stock_agent.research_gate import ResearchGate
from stock_agent.research_health import ResearchInputHealth
from stock_agent.research_pipeline import ResearchPipeline
from stock_agent.research_schemas import Evidence
from stock_agent.research_sources import ResearchSources
from stock_agent.research_store import ResearchStore
from stock_agent.screener import Candidate, Screener
from tests.exec_helpers import enqueue, make_engine
from tests.ledger_helpers import StubBroker, make_ledger
from tests.test_review_research import START, openai_only

FILING = "https://www.sec.gov/Archives/edgar/data/1/fixture.htm"
SECONDARY = "https://example.com/secondary"


def response(body, *, direction="BUY", cite=FILING, conviction=7, symbol="ABC"):
    thesis = {
        "symbol": symbol, "direction": direction, "conviction": conviction,
        "summary": "Falsifiable business-quality case versus the reference price",
        "bull_case": "Recurring gross profit grows faster than sales", "bear_case": "Margin risk",
        "claims": [{"text": "Reported margin expanded", "evidence_ids": [cite]}],
        "catalysts": [], "risks": ["Margin reversion"], "thesis_break": "Reported margins revert",
        "target_price": None, "thesis_change": "new", "horizon_days": 35,
    }
    return httpx.Response(200, json={"status": "completed", "model": body["model"], "output": [
        {"type": "web_search_call", "status": "completed", "action": {"sources": [
            {"url": FILING, "title": "10-Q", "date": "2026-11-20T15:00:00+00:00"},
        ]}},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(thesis)}]},
    ]})


def rig(tmp_path, *, supplied_url=SECONDARY, coverage=True, symbol="ABC"):
    clock = SimpleNamespace(t=START)
    now = lambda: clock.t  # noqa: E731
    config = openai_only()
    ledger = make_ledger(tmp_path, StubBroker(), clock=now)
    work = ResearchStore(ledger.store, config)
    ledger.store.apply_migrations()
    calendar = SimpleNamespace(
        session_on=lambda d: Session(d, clock.t - timedelta(minutes=30), clock.t + timedelta(hours=6)),
        next_session=lambda _: None,
    )
    background = Evidence(
        evidence_id="background", title="Background", url=supplied_url, publisher="Fixture",
        published_at=START - timedelta(hours=1), retrieved_at=START, snippet="Historical context",
    )

    class Prices:
        async def get_quotes(self, symbols, *, purpose, max_age_s):
            return SimpleNamespace(quotes={
                s: SimpleNamespace(price=Decimal(20), as_of=clock.t) for s in symbols
            })

    class News:
        items = ()

        async def poll_news(self, *args, **kwargs):
            return SimpleNamespace(ok=True, as_of=clock.t, covered_symbols={symbol} if coverage else set(),
                                   items=self.items if kwargs["include_market"] else ())

    async def universe():
        return [symbol]

    async def company(symbol):
        return SimpleNamespace(name="Example Devices", tradable=True, canonical_sector="Technology",
                               evidence=(background,), metrics={"revenue_growth_yoy": 0.11})

    news = News()
    sources = ResearchSources(Prices(), news, list_universe=universe, fetch_company=company,
                              asset_for=lambda _: SimpleNamespace(company_name="Example Devices Inc"))

    def pipeline(http):
        llm = LLMClient(config, client=http, spend=work, now=now)
        health = ResearchInputHealth(ledger, llm, news_ok=sources.news_ok, quote_ok=sources.quote_ok)
        return ResearchPipeline(config, ledger, calendar, Analyst(llm), work, sources,
                                ResearchGate(ledger, health, config), clock=now, focus=lambda **_: ())

    return clock, ledger, news, pipeline


@pytest.mark.parametrize(("url", "covered", "grounded", "proposed"), [
    (SECONDARY, True, True, True),
    (FILING, True, False, True),
    (SECONDARY, False, True, False),
])
def test_dated_secondary_does_not_disable_grounding_and_healthy_empty_news_can_buy(
    tmp_path, url, covered, grounded, proposed,
):
    _, ledger, _, pipeline = rig(tmp_path, supplied_url=url, coverage=covered)

    def endpoint(request):
        body = json.loads(request.content)
        assert bool(body.get("tools")) == grounded
        assert "No discrete catalyst is required" in body["instructions"]
        assert "latest valid" in body["instructions"]
        return response(body)

    async def go():
        await ledger.refresh("test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            return await pipeline(http).run("prescan")

    outcome = asyncio.run(go())
    record = ledger.current_thesis("ABC").payload
    assert record["prompt_version"] == BEST_IDEAS_PROMPT_VERSION
    assert record["draft"]["catalysts"] == [] and record["effective_conviction"] == 7
    assert bool(outcome.intents) == proposed, outcome
    if proposed:
        assert outcome.intents[0].side == "buy"
    else:
        assert outcome.detail["entry_funnel"]["rejected_by_reason"] == {"news_stale_or_unavailable": 1}


def test_best_ideas_rejects_hold_and_preserves_cautious_floor(tmp_path):
    clock, ledger, _, pipeline = rig(tmp_path)
    calls = []

    def endpoint(request):
        body = json.loads(request.content)
        calls.append(body)
        return response(body, direction="HOLD" if len(calls) == 1 else "BUY")

    async def go():
        await ledger.refresh("test")
        ledger.store.kv_set("research_market_regime", {"as_of": START.isoformat(), "regime": "cautious"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = pipeline(http)
            first = await pipe.run("prescan")
            assert not first.intents and ledger.current_thesis("ABC") is None
            clock.t += timedelta(minutes=1)
            await ledger.refresh("test")
            return await pipe.run("weekly")

    outcome = asyncio.run(go())
    assert not outcome.intents
    assert outcome.detail["entry_funnel"]["rejected_by_reason"]["conviction_below_floor"] == 1


@pytest.mark.parametrize(("score", "nominated"), [(7, False), (8, True)])
def test_scheduled_bearish_nomination_keeps_existing_options_floor_and_route(tmp_path, score, nominated):
    _, ledger, _, pipeline = rig(tmp_path, symbol="MSFT")

    async def go():
        await ledger.refresh("test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: response(json.loads(req.content), direction="BEARISH", conviction=score, symbol="MSFT"),
        )) as http:
            return await pipeline(http).run("prescan")

    outcome = asyncio.run(go())
    assert not outcome.intents
    assert bool(outcome.option_candidates) is nominated
    if nominated:
        idea, = outcome.option_candidates
        assert (idea.symbol, idea.direction, idea.source) == ("MSFT", "BEARISH", "research")
    else:
        assert outcome.detail["entry_funnel"]["rejected_by_reason"]["conviction_below_floor"] == 1


def test_indirect_symbol_cap_survives_restart_without_blocking_issuer_specific_news(tmp_path):
    clock, ledger, news, pipeline = rig(tmp_path)
    calls = []

    def story(key, title):
        return SimpleNamespace(symbols=("ABC",), materiality_score=9, story_id=key,
                               publisher="Fixture", url=f"https://example.com/{key}", snippet="Facts",
                               headline=title, published_at=clock.t, retrieved_at=clock.t)

    def endpoint(request):
        body = json.loads(request.content)
        assert "fresh material news story now" in body["instructions"]
        assert "No discrete catalyst is required" not in body["instructions"]
        calls.append(body)
        return response(body, direction="HOLD", cite="background")

    async def go():
        await ledger.refresh("test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            news.items = (story("market-1", "Other issuer leads the market"),)
            await pipeline(http).run("news")
            ledger.store.close()
            ledger.store.open()
            news.items = (story("market-2", "Unrelated sector news"), story("direct-1", "Example Devices lifts outlook"))
            second = await pipeline(http).run("news")
            assert second.detail["entry_funnel"]["rejected_by_reason"]["indirect_news_symbol_cap"] == 1
            again = await pipeline(http).run("news")
            assert "indirect_news_symbol_cap" not in again.detail["entry_funnel"]["rejected_by_reason"]
            clock.t += timedelta(days=1)
            await ledger.refresh("new NY day")
            news.items = (story("market-3", "Broader market rallies"),)
            await pipeline(http).run("news")

    asyncio.run(go())
    assert len(calls) == 3
    assert ledger.current_thesis("ABC").payload["prompt_version"] == THESIS_PROMPT_VERSION


def test_indirect_quota_is_per_symbol_and_same_story_may_retry(tmp_path):
    ledger = make_ledger(tmp_path)
    work = ResearchStore(ledger.store, openai_only())
    ledger.store.apply_migrations()
    day = START.date().isoformat()
    assert work.reserve_indirect_news(day, "ABC", "a")
    assert work.reserve_indirect_news(day, "ABC", "a")
    assert not work.reserve_indirect_news(day, "ABC", "b")
    assert work.reserve_indirect_news(day, "XYZ", "b")


def test_scheduled_screener_drops_relative_strength_weight_without_erasing_other_factors():
    names = [
        Candidate("AAA", "a", "Technology", True, {"revenue_growth_yoy": .20, "relative_strength": -.9}),
        Candidate("BBB", "b", "Technology", True, {"revenue_growth_yoy": .10, "relative_strength": .9}),
    ]
    row, _ = Screener.rank(names, include_relative_strength=False)
    assert row["symbol"] == "AAA" and "relative_strength" not in row["scores"]
    assert row["composite"] == pytest.approx((30 * 7.5 + 55 * 5) / 85)
    assert "relative_strength" in Screener.rank(names)[0]["scores"]


def test_session_reservations_survive_restart_and_release_on_zero_fill(tmp_path):
    ledger = make_ledger(tmp_path)
    day = START.date()
    reserve = {"new_stock_session": day, "new_stock_limit": 2}
    issuer_a, alias, issuer_b, third, fourth = ledger.enqueue_intents([
        NewIntent(s, "buy", s, origin=origin) for s, origin in
        [("GOOGL", "prescan"), ("GOOG", "news"), ("BBB", "weekly"), ("CCC", "market_move"), ("DDD", "earnings")]
    ])
    ledger.claim_intent(issuer_a, **reserve)
    ledger.ack_intent(issuer_a, IntentStatus.SUBMITTED)
    ledger.ack_intent(issuer_a, IntentStatus.PARTIALLY_FILLED, meta={"filled_qty": 1})
    ledger.ack_intent(issuer_a, IntentStatus.CANCELLED)
    ledger.claim_intent(issuer_b, **reserve)
    ledger.store.close()
    ledger = make_ledger(tmp_path)
    ledger.mark_submitting_ambiguous()
    with pytest.raises(NewStockEntryRefused) as same:
        ledger.claim_intent(alias, **reserve)
    assert same.value.code == "new_stock_issuer_already_admitted"
    with pytest.raises(NewStockEntryRefused) as full:
        ledger.claim_intent(third, **reserve)
    assert full.value.code == "new_stock_session_cap"
    ledger.ack_intent(issuer_b, IntentStatus.CANCELLED, meta={"filled_qty": 0})
    assert ledger.claim_intent(third, **reserve).status is IntentStatus.SUBMITTING
    assert ledger.claim_intent(
        fourth, new_stock_session=day + timedelta(days=1), new_stock_limit=2,
    ).status is IntentStatus.SUBMITTING


@pytest.mark.parametrize(("override", "expected"), [(None, 3), (2, 2)])
def test_common_execution_cap_journals_excess_without_order(world, tmp_path, monkeypatch, override, expected):
    if override is not None:
        monkeypatch.setattr(Config, "MAX_NEW_STOCK_POSITIONS_PER_SESSION", override)
    world.clock.set_et(2026, 11, 23, 10, 0)
    entries = [("AAA", "prescan"), ("BBB", "news"), ("CCC", "market_move"), ("DDD", "earnings")]
    for symbol, _ in entries:
        world.price(symbol, 50, spread=0.02)
    eng = make_engine(world, tmp_path, monkeypatch)

    async def go():
        results = []
        for symbol, origin in entries:
            it = enqueue(eng, symbol, "buy", ref=50)
            results.append(await eng.execute_intent(replace(it, origin=origin)))
        return results

    results = asyncio.run(go())
    assert all(r.status == IntentStatus.FILLED for r in results[:expected])
    assert all(r.status == IntentStatus.SKIPPED and r.code == "new_stock_session_cap" for r in results[expected:])
    assert {o.symbol for o in world.alpaca.orders.values() if o.side == "buy"} == {
        symbol for symbol, _ in entries[:expected]
    }
    rows = [r for r in eng.ledger.journal.read({"execution.buy_funnel"}) if r.get("code") == "new_stock_session_cap"]
    assert [(r["stage"], r["symbol"], r["origin"]) for r in rows] == [
        ("skipped", symbol, origin) for symbol, origin in entries[expected:]
    ]
