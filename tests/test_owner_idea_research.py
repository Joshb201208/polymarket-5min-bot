import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_agent import research_focus
from stock_agent.analyst import OWNER_IDEA_PROMPT_VERSION
from stock_agent.ledger import ThesisRole
from stock_agent.llm_client import ModelRoute
from stock_agent.market_calendar import Session
from stock_agent.research_focus import FocusItem
from tests.test_review_research import START
from tests.test_scheduled_best_ideas import FILING, response, rig


def _prescan(pipe, clock):
    opens = clock.t + timedelta(minutes=90)
    pipe.calendar.session_on = lambda day: Session(day, opens, opens + timedelta(hours=6))
    return opens


def _reply(body, *, direction, stop, cite="background", symbol="ABC"):
    raw = response(body, direction=direction, conviction=3, cite=cite, symbol=symbol).json()
    item = raw["output"][-1]["content"][0]
    value = json.loads(item["text"])
    value["stop_price"] = stop
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
        assert "Act as a risk reviewer" in body["instructions"]
        assert json.loads(body["input"].split("INPUT_JSON:\n")[1])["owner_directive"]["note"] == directive.note
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
    assert len(calls) == 1
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
        data = json.loads(body["input"].split("INPUT_JSON:\n")[1])
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


def test_completed_close_anchors_option_only_research_while_underlying_quote_is_stale(tmp_path, monkeypatch):
    clock, ledger, _, make_pipe = rig(tmp_path, supplied_url=FILING)
    directive = FocusItem("ABC", None, "Check for a guidance cut.", START.date() - timedelta(days=2),
                          START.date() + timedelta(days=5), stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": directive})
    monkeypatch.setattr(ledger, "owner_directive_used", lambda symbol, added, *, lane="stock", **_: lane == "stock")

    def endpoint(req):
        body = json.loads(req.content)
        data = json.loads(body["input"].split("INPUT_JSON:\n")[1])
        assert data["reference_price"] == 20 and data["quote_at"] is None
        assert data["metrics"]["reference_price_kind"] == "completed_split_adjusted_close"
        return _reply(body, direction="BUY", stop=17)

    async def get_quotes(*args, **kwargs):
        return SimpleNamespace(quotes={})

    async def prior_close(symbol, date):
        return 20

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
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


@pytest.mark.parametrize(("change", "option_expected"), [("unchanged", True), ("weakened", False)])
def test_later_held_review_reuses_one_model_call_and_never_overrides_stock_grade(
    tmp_path, monkeypatch, change, option_expected,
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
        calls.append(body)
        data = json.loads(body["input"].split("INPUT_JSON:\n")[1])
        assert data["holding"]["held"] is True
        assert "owner_directive" not in data
        raw = response(body, direction="HOLD", conviction=6).json()
        item = raw["output"][-1]["content"][0]
        value = json.loads(item["text"])
        value["thesis_change"] = change
        item["text"] = json.dumps(value)
        return httpx.Response(200, json=raw)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipe(http)
            pipe.directives = research_focus.owner_backed
            _prescan(pipe, clock)
            return await pipe.run("prescan")

    outcome = asyncio.run(run())
    assert not outcome.intents
    assert len(calls) == 1
    thesis = ledger.current_thesis("ABC")
    assert thesis.role is ThesisRole.HELD_REVIEW
    assert thesis.payload["raw_conviction"] == thesis.payload["effective_conviction"] == 6
    assert thesis.payload["adjustment_reason"] == ""
    assert bool(outcome.option_candidates) is option_expected
    if option_expected:
        (idea,) = outcome.option_candidates
        assert (idea.symbol, idea.raw_conviction, idea.effective_conviction, idea.source) == ("ABC", 6, 8, "owner")
