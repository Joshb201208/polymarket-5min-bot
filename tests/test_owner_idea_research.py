import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_agent import research_focus
from stock_agent.analyst import OWNER_IDEA_PROMPT_VERSION
from stock_agent.ledger import IntentStatus, NewIntent, ThesisRole
from stock_agent.llm_client import ModelRoute
from stock_agent.market_calendar import Session
from stock_agent.research_focus import FocusItem
from stock_agent.research_pipeline import NewsEvent, OptionIdea
from stock_agent.research_schemas import Direction, Evidence
from stock_agent.scheduler import StockAgentScheduler
from tests.test_review_research import START
from tests.test_scheduled_best_ideas import FILING, openai_search_pass, original_input, response, rig


def _prescan(pipe, clock):
    opens = clock.t + timedelta(minutes=90)
    pipe.calendar.session_on = lambda day: Session(day, opens, opens + timedelta(hours=6))
    return opens


def _reply(body, *, direction, stop, cite="background", symbol="ABC"):
    raw = response(body, direction=direction, conviction=3, cite=cite, symbol=symbol).json()
    item = raw["output"][-1]["content"][0]
    value = json.loads(item["text"])
    value["stop_price"] = stop
    value["veto_claim_index"] = 0 if direction in {"AVOID", "BEARISH"} else None
    if value["veto_claim_index"] is not None:
        value["thesis_break"] = value["claims"][0]["text"]
    item["text"] = json.dumps(value)
    return httpx.Response(200, json=raw)


@pytest.mark.parametrize(("direction", "stop", "days_since_added", "expected"), [
    ("BUY", 17, 2, "proposed"),
    ("BUY", 21, 2, "unavailable"),
    ("INSUFFICIENT_EVIDENCE", None, 2, "unavailable"),
    ("AVOID", None, 0, "unavailable"),
    ("AVOID", None, 2, "vetoed"),
    ("BEARISH", None, 2, "vetoed"),
])
def test_owner_decision_is_priced_evidence_bound_and_never_silently_abstains(
    tmp_path, monkeypatch, direction, stop, days_since_added, expected,
):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Veto on an issuer-confirmed guidance cut.",
                          START.date() - timedelta(days=days_since_added), START.date() + timedelta(days=5),
                          stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    alerts = []
    calls = []

    def endpoint(req):
        body = json.loads(req.content)
        calls.append(body)
        if openai_search_pass(body):
            assert original_input(body)["owner_directive"]["note"] == directive.note
            return response(body)
        assert "Act as a risk reviewer" in body["instructions"]
        assert "web_evidence" in body["input"]
        assert original_input(body)["owner_directive"]["note"] == directive.note
        return _reply(body, direction=direction, stop=stop)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            pipe.alerts = SimpleNamespace(alert=lambda *args, **kwargs: alerts.append((args, kwargs)))
            primary, = pipe.analyst.llm._routes()
            monkeypatch.setattr(pipe.analyst.llm, "_routes", lambda *_: [primary, ModelRoute("openai", "gpt-6.2-sol")])
            _prescan(pipe, clock)
            first = await pipe.run("prescan")
            await pipe.run("prescan")
            return first

    outcome = asyncio.run(run())
    journal = list(ledger.journal.read({"research.owner_directive"}))
    assert [row["outcome"] for row in journal] == [expected]
    assert len(calls) == 2 and openai_search_pass(calls[0])
    assert ledger.owner_directive_vetoed("ABC", directive.added) is (expected == "vetoed")
    assert not ledger.owner_directive_used("ABC", directive.added)
    if expected == "proposed":
        (intent,) = outcome.intents
        (option,) = outcome.option_candidates
        record = ledger.current_thesis("ABC").payload
        assert record["prompt_version"] == OWNER_IDEA_PROMPT_VERSION
        assert record["model_effective_conviction"] == record["raw_conviction"] == 3
        assert record["effective_conviction"] == record["owner_conviction"] == 8
        assert record["adjustment_reason"] == intent.meta["adjustment_reason"] == "owner_directive"
        assert intent.origin == "prescan" and intent.meta["owner_review_stop"] == 17
        assert option.source == "owner" and option.direction == "BUY" and option.effective_conviction == 8
        assert option.owner_directive_added == directive.added.isoformat()
        assert option.story_id == f"owner:ABC:{directive.added}:{intent.session_date}"
        assert not alerts
    else:
        assert not outcome.intents and not outcome.option_candidates
        assert len(alerts) == 1
        if expected == "vetoed":
            assert FILING in alerts[0][0][2]


def test_daily_cadence_only_adds_forced_review_in_preopen_prescan(tmp_path):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    seen = []

    def endpoint(req):
        body = json.loads(req.content)
        data = original_input(body)
        if openai_search_pass(body):
            return response(body)
        seen.append(data["symbol"])
        return response(body, direction="AVOID", symbol=data["symbol"])

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.daily_focus = lambda **_: ("DAILY", "ABC")
            pipe.directives = lambda **_: {}
            _prescan(pipe, clock)
            await pipe.run("weekly")
            assert seen == ["ABC"]  # A daily name may still rank on its ordinary merits.
            await pipe.run("midweek")
            assert seen == ["ABC", "ABC"]
            await pipe.run("prescan")
            await pipe.run("prescan")
            assert seen == ["ABC", "ABC", "DAILY", "ABC"]

    asyncio.run(run())


def test_pre_directive_concern_can_be_noted_as_a_risk_in_one_priced_review(tmp_path, monkeypatch):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Veto on a guidance cut.", START.date(), START.date() + timedelta(days=5),
                          stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    calls = []

    def endpoint(req):
        body = json.loads(req.content)
        if openai_search_pass(body):
            return response(body)
        calls.append(body)
        raw = _reply(body, direction="BUY", stop=17).json()
        item = raw["output"][-1]["content"][0]
        value = json.loads(item["text"])
        value["risks"] = ["Earlier guidance cut remains a risk."]
        item["text"] = json.dumps(value)
        return httpx.Response(200, json=raw)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            _prescan(pipe, clock)
            return await pipe.run("prescan")

    assert asyncio.run(run()).intents
    assert not ledger.owner_directive_vetoed("ABC", directive.added)
    assert len(calls) == 1
    assert "Older or inconclusive concerns go in risks" in calls[0]["instructions"]
    assert ledger.current_thesis("ABC").payload["draft"]["risks"] == ["Earlier guidance cut remains a risk."]


@pytest.mark.parametrize(
    ("used_lane", "direction", "stock_expected", "option_expected"),
    [("stock", "BUY", False, True), ("option", "BUY", True, False), ("stock", "AVOID", False, False)],
)
def test_unused_unheld_lane_gets_a_fresh_review_even_if_the_other_lane_was_used(
    tmp_path, monkeypatch, used_lane, direction, stock_expected, option_expected,
):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Check for a guidance cut.", START.date() - timedelta(days=2),
                          START.date() + timedelta(days=5), stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    monkeypatch.setattr(ledger, "owner_directive_used", lambda symbol, added, *, lane="stock", **_: lane == used_lane)
    calls = []

    def endpoint(req):
        body = json.loads(req.content)
        if openai_search_pass(body):
            return response(body)
        calls.append(body)
        return _reply(body, direction=direction, stop=17 if direction == "BUY" else None)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            _prescan(pipe, clock)
            return await pipe.run("prescan")

    outcome = asyncio.run(run())
    assert len(calls) == 1
    assert bool(outcome.intents) is stock_expected
    assert bool(outcome.option_candidates) is option_expected
    assert ledger.owner_directive_vetoed("ABC", directive.added) is (direction == "AVOID")


@pytest.mark.parametrize("anchor_kind", ["completed_split_adjusted_close", "recent_research_quote"])
def test_analysis_anchors_option_only_research_while_underlying_quote_is_stale(tmp_path, monkeypatch, anchor_kind):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Check for a guidance cut.", START.date() - timedelta(days=2),
                          START.date() + timedelta(days=5), stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    monkeypatch.setattr(ledger, "owner_directive_used", lambda symbol, added, *, lane="stock", **_: lane == "stock")

    def endpoint(req):
        body = json.loads(req.content)
        data = original_input(body)
        assert data["reference_price"] == 20
        assert (data["quote_at"] is None) is (anchor_kind == "completed_split_adjusted_close")
        assert data["metrics"]["reference_price_kind"] == anchor_kind
        return _reply(body, direction="BUY", stop=17)

    async def get_quotes(symbols, *, purpose, **kwargs):
        return SimpleNamespace(quotes={
            sym: SimpleNamespace(price=20, as_of=clock.t - timedelta(minutes=5))
            for sym in symbols if anchor_kind == "recent_research_quote" and purpose == "brief"
        })

    async def prior_close(symbol, date):
        return 20 if anchor_kind == "completed_split_adjusted_close" else None

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            pipe.sources.directives_for = research_focus.owner_backed
            pipe.sources.prices.get_quotes = get_quotes
            pipe.sources.completed_close = prior_close
            _prescan(pipe, clock)
            pipe.calendar.previous_session = lambda day: SimpleNamespace(date=day - timedelta(days=3))
            outcome = await pipe.run("prescan")
            assert pipe.gate.is_entry_eligible("ABC", clock.t, allow_owner=True, owner_lane="option").owner_entry is None
            return outcome

    outcome = asyncio.run(run())
    assert not outcome.intents
    assert [idea.source for idea in outcome.option_candidates] == ["owner"]


@pytest.mark.parametrize("has_issuer_event", [False, True])
def test_staged_owner_handoff_survives_ordinary_review_or_defers_without_spending(
    tmp_path, monkeypatch, has_issuer_event,
):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Check for a guidance cut.", START.date() - timedelta(days=2),
                          START.date() + timedelta(days=5), stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    alerts, reviews, checks = [], [], []
    quoted = False

    def endpoint(req):
        body = json.loads(req.content)
        if openai_search_pass(body):
            return response(body)
        owner_review = "Act as a risk reviewer" in body["instructions"]
        reviews.append(owner_review)
        return _reply(body, direction="BUY", stop=17) if owner_review else response(body, direction="AVOID")

    async def get_quotes(symbols, **_):
        return SimpleNamespace(quotes={
            sym: SimpleNamespace(price=20, as_of=clock.t) for sym in symbols if quoted
        })

    async def prior_close(*_):
        return 20

    async def issuer_events(symbol, after, now, *, owner):
        assert symbol == "ABC" and owner and after == START and now > after
        checks.append(now)
        if not has_issuer_event:
            return ()
        return (Evidence(evidence_id="issuer-event", url=FILING, title="New issuer announcement", publisher="SEC",
                         published_at=after + timedelta(minutes=2), retrieved_at=now, snippet=""),)

    async def run():
        nonlocal quoted
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            pipe.alerts = SimpleNamespace(alert=lambda *args, **kwargs: alerts.append((args, kwargs)))
            pipe.sources.prices.get_quotes = get_quotes
            pipe.sources.completed_close = prior_close
            pipe.sources.handoff_material_news = issuer_events
            opens = _prescan(pipe, clock)
            pipe.calendar.previous_session = lambda day: SimpleNamespace(date=day - timedelta(days=1))
            initial = await pipe.run("prescan")
            assert not initial.intents and initial.option_candidates
            thesis_id = ledger.current_thesis("ABC").thesis_id
            quoted = True
            clock.t = START + timedelta(minutes=10)
            await ledger.refresh("fixture")
            await pipe.run("weekly")
            assert ledger.current_thesis("ABC").thesis_id == thesis_id
            clock.t = opens + timedelta(minutes=5)
            await ledger.refresh("fixture")
            result = await pipe.run_entry_handoff()
            expected = "handoff_owner_news_deferred" if has_issuer_event else "handoff_promoted"
            assert result.detail["handoff_outcomes"] == {expected: 1}
            assert not (await pipe.run_entry_handoff()).detail["handoff_outcomes"]
            assert not ledger.owner_directive_used("ABC", directive.added)
            assert not ledger.owner_directive_used("ABC", directive.added, lane="option")
            assert not ledger.owner_directive_vetoed("ABC", directive.added)
            assert ledger.owner_directive_held("ABC", START.date()) is has_issuer_event
            assert reviews == [True, False] and len(checks) == 1
            assert bool(alerts) == has_issuer_event and len(alerts) <= 1
            if has_issuer_event:
                clock.t = START + timedelta(days=1)
                _prescan(pipe, clock)
                await ledger.refresh("fixture")
                next_scan = await pipe.run("prescan")
                assert next_scan.intents and reviews == [True, False, True]

    asyncio.run(run())


@pytest.mark.parametrize("mixed_in_one_claim", [False, True])
def test_owner_veto_citations_bind_to_the_break_and_dont_fail_provider_health(tmp_path, monkeypatch, mixed_in_one_claim):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Check for a guidance cut.", START.date() - timedelta(days=1),
                          START.date() + timedelta(days=5), stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    old = Evidence(evidence_id="older", url="https://example.org/old", title="Cut",
                   published_at=START - timedelta(days=1), retrieved_at=START, publisher="Fixture", snippet="")
    health_failures = []

    def endpoint(req):
        body = json.loads(req.content)
        raw = _reply(body, direction="AVOID", stop=None).json()
        item = raw["output"][-1]["content"][0]
        value = json.loads(item["text"])
        value["thesis_break"] = "Guidance was cut before acceptance."
        value["claims"] = [
            {"text": value["thesis_break"], "evidence_ids": ["older", "background"] if mixed_in_one_claim else ["older"]},
            {"text": "A benign new source was published.", "evidence_ids": ["background"]},
        ]
        item["text"] = json.dumps(value)
        return httpx.Response(200, json=raw)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            pipe.analyst.llm.health = SimpleNamespace(record_llm_failure=lambda *a, **kw: health_failures.append(a))
            original_securities = pipe.sources.securities

            async def with_old(*args, **kwargs):
                rows = await original_securities(*args, **kwargs)
                return {sym: replace(row, evidence=(*row.evidence, old)) for sym, row in rows.items()}

            pipe.sources.securities = with_old
            _prescan(pipe, clock)
            outcome = await pipe.run("prescan")
            assert outcome.detail["unavailable"] == 1 and not outcome.intents and not outcome.option_candidates
            assert pipe.analyst.llm.is_healthy()

    asyncio.run(run())
    assert not health_failures
    assert not ledger.owner_directive_vetoed("ABC", directive.added)


@pytest.mark.parametrize("preopen", [True, False])
def test_metadata_only_filings_leave_ordinary_grounding_and_brief_stays_preopen(tmp_path, preopen):
    index_url = "https://www.sec.gov/Archives/edgar/data/1/fixture-index.htm"
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=index_url)
    extra = Evidence(evidence_id="metadata", url=index_url.replace("fixture", "another"), title="EDGAR",
                     published_at=START - timedelta(hours=2), retrieved_at=START, publisher="SEC", snippet="")
    calls = []

    def endpoint(req):
        body = json.loads(req.content)
        inputs = original_input(body)
        if not openai_search_pass(body):
            assert "web_evidence" in body["input"]
            return response(body, direction="AVOID")
        assert len(inputs["evidence"]) == 24
        assert any(row["evidence_id"] == "metadata" for row in inputs["evidence"])
        assert inputs["reference_price"] == (20 if preopen else None)
        assert inputs["metrics"].get("reference_price_kind") == ("recent_research_quote" if preopen else None)
        calls.append(inputs)
        return response(body, direction="AVOID")

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.daily_focus = lambda **_: ("ABC",)
            original_securities = pipe.sources.securities

            async def with_brief(*args, **kwargs):
                assert bool(kwargs.get("preopen")) is preopen
                assert not kwargs.get("prescan")
                rows = await original_securities(*args, **kwargs)
                filler = tuple(extra.model_copy(update={"evidence_id": f"routine-{i}", "published_at": None})
                               for i in range(24))
                return {
                    sym: replace(row, price=None, quote_at=None, evidence=(*row.evidence, *filler),
                                 priority_evidence=(extra,),
                                 analysis_price=20, analysis_quote_at=clock.t - timedelta(seconds=30))
                    for sym, row in rows.items()
                }

            pipe.sources.securities = with_brief
            if preopen:
                _prescan(pipe, clock)
            await pipe.run("weekly")

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize(("kind", "direction"), [
    ("news", "HOLD"), ("earnings", "INSUFFICIENT_EVIDENCE"),
    ("news", "AVOID"), ("earnings", "BEARISH"), ("news", "EXIT_PROPOSED"),
])
def test_live_review_preserves_owner_thesis_or_records_session_hold(tmp_path, monkeypatch, kind, direction):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Check for a guidance cut.", START.date() - timedelta(days=1),
                          START.date() + timedelta(days=5), stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    alerts = []
    story_count = 0

    def endpoint(req):
        body = json.loads(req.content)
        if "Act as a risk reviewer" in body["instructions"]:
            return _reply(body, direction="BUY", stop=17)
        ordinary = response(body, direction=direction, cite="live-event")
        if direction == "EXIT_PROPOSED":
            raw = ordinary.json()
            item = raw["output"][-1]["content"][0]
            value = json.loads(item["text"])
            item["text"] = json.dumps({**value, "thesis_change": "broken"})
            return httpx.Response(200, json=raw)
        return ordinary

    async def get_quotes(*args, **kwargs):
        return SimpleNamespace(quotes={})

    async def prior_close(*_):
        return 20

    async def live_news(*_):
        nonlocal story_count
        story_count += 1
        return (NewsEvent("ABC", f"live-event-{story_count}", Evidence(
            evidence_id="live-event", url="https://example.org/event", title="Issuer event",
            published_at=clock.t, retrieved_at=clock.t, publisher="Fixture", snippet="",
        ), 8),)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            pipe.alerts = SimpleNamespace(alert=lambda *args, **kwargs: alerts.append((args, kwargs)))
            pipe.sources.prices.get_quotes = get_quotes
            pipe.sources.completed_close = prior_close
            pipe.sources.live_news = live_news
            pipe.sources.releases = live_news
            opens = _prescan(pipe, clock)
            pipe.calendar.previous_session = lambda day: SimpleNamespace(date=day - timedelta(days=1))
            await pipe.run("prescan")
            old = ledger.current_thesis("ABC").thesis_id
            clock.t = opens + timedelta(minutes=1)
            await ledger.refresh("fixture")
            snapshot = ledger.snapshot()
            if direction == "EXIT_PROPOSED":
                # A position appearing after staging permits an ordinary exit
                # analysis; its lack of a position ID precludes a sell intent.
                snapshot = replace(snapshot, holdings=(SimpleNamespace(symbol="ABC", qty=1, meta=None),))
            await pipe.run(kind, snapshot=snapshot)
            await pipe.run(kind, snapshot=snapshot)
            if direction != "EXIT_PROPOSED":
                assert ledger.current_thesis("ABC").thesis_id == old

    asyncio.run(run())
    expected = direction in {"AVOID", "BEARISH", "EXIT_PROPOSED"}
    assert ledger.owner_directive_held("ABC", START.date()) is expected
    assert not ledger.owner_directive_vetoed("ABC", directive.added)
    assert not ledger.owner_directive_used("ABC", directive.added)
    assert not ledger.owner_directive_used("ABC", directive.added, lane="option")
    assert story_count == 2
    assert len(alerts) == int(expected)


def test_option_scheduler_waits_for_stock_proposal_queue_and_working_order(tmp_path):
    clock, ledger, _, make_pipe = rig(tmp_path)
    added = (clock.t.date() - timedelta(days=1)).isoformat()
    session_day = clock.t.date()
    pipe = make_pipe(None)
    scheduler = SimpleNamespace(ledger=ledger, research_store=pipe.store)
    idea = OptionIdea("ABC", Direction.BUY, "option", "thesis", 3, 8, clock.t, (), "owner", added)

    def ready(now=None):
        return StockAgentScheduler._owner_option_stock_ready(scheduler, idea, now or clock.t)

    proposal = NewIntent(
        "ABC", "buy", "stock-proposal", session_date=session_day, expires_at=clock.t + timedelta(hours=4),
        meta={"owner_directive_added": added},
    )
    assert ready()
    assert pipe.store.claim_work(proposal.dedupe_key, proposal.symbol, clock.t)
    pipe.store.record_proposal(proposal.dedupe_key, "thesis", proposal)
    assert not ready()
    (intent_id,) = ledger.enqueue_intents((proposal,))
    assert not ready()
    assert ready(clock.t + timedelta(days=1))
    ledger._transition(intent_id, IntentStatus.SUBMITTING)
    ledger._transition(intent_id, IntentStatus.SUBMITTED)
    assert not ready()
    ledger._transition(intent_id, IntentStatus.CANCELLED)
    assert ready()
    assert ledger.hold_owner_directive_for_session("ABC", session_day, "risk")
    assert not ready()


@pytest.mark.parametrize(("change", "option_expected", "later_change", "handled"), [
    ("unchanged", True, None, False),
    ("weakened", False, None, False),
    ("unchanged", True, "unchanged", False),
    ("unchanged", True, "strengthened", False),
    ("unchanged", True, "weakened", False),
    ("unchanged", True, "unchanged", True),
])
def test_later_held_review_reuses_one_model_call_and_never_overrides_stock_grade(
    tmp_path, monkeypatch, change, option_expected, later_change, handled,
):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Veto on a guidance cut.", START.date() - timedelta(days=2),
                          START.date() + timedelta(days=5), stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    monkeypatch.setattr(ledger, "owner_directive_used", lambda symbol, added, *, lane="stock", **_: lane == "stock")
    ledger.broker.pos("ABC", 5, 20)
    calls = []

    def endpoint(req):
        body = json.loads(req.content)
        if openai_search_pass(body):
            return response(body)
        calls.append(body)
        data = original_input(body)
        assert data["holding"]["held"] is True
        assert "owner_directive" not in data
        raw = response(body, direction="HOLD", conviction=6).json()
        item = raw["output"][-1]["content"][0]
        value = json.loads(item["text"])
        value["thesis_change"] = change if len(calls) == 1 else later_change
        item["text"] = json.dumps(value)
        return httpx.Response(200, json=raw)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            opens = _prescan(pipe, clock)
            prescan = await pipe.run("prescan")
            initial = ledger.current_thesis("ABC")
            if later_change:
                (idea,) = prescan.option_candidates
                if handled:
                    pipe.store.acknowledge_options((idea.story_id,))
                clock.t = opens + timedelta(minutes=5)
                await ledger.refresh("fixture")
                await pipe.run("weekly")
                current = ledger.current_thesis("ABC")
                kept = later_change in {"unchanged", "strengthened"} and not handled
                proof = pipe.gate.is_entry_eligible("ABC", clock.t, allow_owner=True, owner_lane="option")
                assert (current.thesis_id == initial.thesis_id) is kept
                assert (proof.ok and proof.owner_entry is not None) is kept
                if kept:
                    assert proof.owner_entry.thesis_id == idea.thesis_id == initial.thesis_id
                work = pipe.store.work_state(f"weekly:{clock.t.date()}:ABC")
                assert work["thesis_id"] != initial.thesis_id  # The newer HOLD is still recorded.
            return prescan, initial

    outcome, thesis = asyncio.run(run())
    assert not outcome.intents
    assert len(calls) == (2 if later_change else 1)
    assert thesis.role is ThesisRole.HELD_REVIEW
    assert thesis.payload["raw_conviction"] == thesis.payload["effective_conviction"] == 6
    assert thesis.payload["adjustment_reason"] == ""
    assert bool(outcome.option_candidates) is option_expected
    if option_expected:
        (idea,) = outcome.option_candidates
        assert (idea.symbol, idea.raw_conviction, idea.effective_conviction, idea.source) == ("ABC", 6, 8, "owner")
