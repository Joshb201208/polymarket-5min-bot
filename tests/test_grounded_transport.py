import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from stock_agent.analyst import Analyst, ResearchInput, _index_evidence
from stock_agent.ledger import JobStatus
from stock_agent.llm_client import LLMClient, LLMRequest, ProviderError, ProviderUnavailable
from stock_agent.market_calendar import Session
from stock_agent.research_focus import FocusItem
from stock_agent.research_pipeline import prescan_runtime_seconds
from stock_agent.research_schemas import Evidence, ResearchTask
from tests.fakes.base import FakeInternet
from tests.fakes.providers import FakeOpenAI, FakePerplexity, LLMReply
from tests.test_research_contracts import NOW, TinyDraft, settings
from tests.test_scheduled_best_ideas import response, rig


ANSWER = '{"direction":"BUY","conviction":8,"note":null}'
SOURCES = [{"url": "https://example.org/results", "published_at": "2026-11-23T13:00:00Z"}]


def request(*, grounded=True, validate=None):
    return LLMRequest(ResearchTask.THESIS, TinyDraft, "test", "Test", {}, grounded=grounded,
                      validate=validate, single_review=True, local_decision_validation=True)


def completed(text=ANSWER, sources=SOURCES):
    return {"status": "completed", "output": [
        {"type": "search_results", "results": sources},
        {"type": "message", "content": [{"type": "output_text", "text": text}]},
    ]}


def test_openai_search_payload_and_reservation_match_contract():
    fake = FakeOpenAI()
    fake.reply(LLMReply(text="Search results", sources=SOURCES), ANSWER)
    reservations = []
    ledger = SimpleNamespace(reserve=lambda *args, **kwargs: reservations.append(args[2]) or True,
                             settle=lambda *args: None)
    internet = FakeInternet([fake])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(internet.handle)) as http:
            old = await http.post("https://api.openai.com/v1/responses",
                                  json={"tools": [{"type": "web_search", "return_token_budget": 2500}]})
            assert old.status_code == 400 and old.json()["error"]["param"] == "tools[0].return_token_budget"
            llm = LLMClient(settings(PERPLEXITY_API_KEY=""), client=http, spend=ledger, now=lambda: NOW)
            result = await llm.complete(request())
            assert result.route.provider == "openai"
            assert fake.prompts[0]["tools"] == [{"type": "web_search"}]
            assert reservations[0] > 90_000  # Includes the documented search-window allowance.
            assert 0 < reservations[1] < 10_000  # Only structure, no additional search fee.

    asyncio.run(run())


@pytest.mark.parametrize("use_supplied_id", [True, False])
def test_openai_structures_against_returned_source_ids_and_ignores_second_pass_sources(use_supplied_id):
    real_url = "https://example.org/returned-annotation"
    fake_url = "https://example.org/made-up-by-structured-pass"
    bodies = []
    def endpoint(req):
        body = json.loads(req.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json={"status": "completed", "output": [
                {"type": "web_search_call", "status": "completed", "action": {"sources": [
                    *({"url": f"https://example.org/other/{index}"} for index in range(20)),
                    {"url": real_url, "published_at": (NOW - timedelta(days=1)).isoformat()},
                ]}},
                {"type": "message", "content": [{"type": "output_text", "text": "Search notes", "annotations": [
                    {"type": "url_citation", "url": real_url, "title": "Returned primary evidence"},
                ]}]},
            ]})
        assert "tools" not in body and body["text"]["format"]["strict"]
        web = json.loads(body["input"].split("INPUT_JSON:\n")[1])["web_evidence"]
        assert len(web) == 8 and web[0]["url"] == real_url and web[0]["title"] == "Returned primary evidence"
        wire = response(body, cite=web[0]["evidence_id"] if use_supplied_id else fake_url).json()
        wire["output"][0]["action"]["sources"] = [{"url": fake_url, "published_at": NOW.isoformat()}]
        return httpx.Response(200, json=wire)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            analyst = Analyst(LLMClient(settings(PERPLEXITY_API_KEY=""), client=http, now=lambda: NOW))
            data = ResearchInput("ABC", NOW, "ABC", "t", 20, None, {}, ())
            if use_supplied_id:
                result = await analyst.evaluate(data, best_ideas=True)
                assert result.record.draft.claims[0].evidence_ids == [result.result.evidence[0].evidence_id]
                assert not any(item.url == fake_url for item in result.result.evidence)
            else:
                with pytest.raises(ProviderUnavailable) as exc:
                    await analyst.evaluate(data, best_ideas=True)
                assert exc.value.failure_codes == {"failure_codes": ["openai:thesis_unknown_evidence"]}

    asyncio.run(run())
    assert len(bodies) == 2


def test_scheduled_search_has_longer_timeouts_only_on_search_post(monkeypatch):
    posted_timeouts, outer = [], []
    actual_timeout = asyncio.timeout

    def track_timeout(seconds):
        outer.append(seconds)
        return actual_timeout(seconds)

    def endpoint(req):
        posted_timeouts.append(req.extensions["timeout"]["read"])
        return httpx.Response(200, json=completed())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            llm = LLMClient(settings(PERPLEXITY_API_KEY=""), client=http, now=lambda: NOW)
            await llm.complete(replace(request(), long_search=True))
            await llm.complete(request())
            await llm.complete(replace(request(grounded=False), long_search=True))

    monkeypatch.setattr(asyncio, "timeout", track_timeout)
    asyncio.run(run())
    assert posted_timeouts == [75, 25, 25, 25, 25]
    assert outer == [90, 30, 30, 30, 30]


def test_prescan_prioritizes_directive_held_daily_and_defers_uncharged_tail(tmp_path):
    clock, ledger, _, make_pipeline = rig(tmp_path)
    ledger.broker.pos("HELD", 5, 20)
    reviews = []

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("LLM must not be called"))) as http:
            pipe = make_pipeline(http)
            start = clock.t
            opens = start + timedelta(minutes=90)
            pipe.calendar.session_on = lambda day: Session(day, opens, opens + timedelta(hours=6))
            pipe.directives = lambda **_: {"OWNER": FocusItem(
                "OWNER", None, "Fixture", start.date(), start.date(), stance="bullish", owner_conviction=8,
            )}
            pipe.daily_focus = lambda **_: ("DAILY",)
            pipe.focus = lambda **_: ("FOCUS",)

            async def review(kind, key, item, holding, *_, **kwargs):
                reviews.append((item.symbol, holding is not None, kwargs["owner"] is not None))
                pipe.store.finish_work(key, "no_action")
                if item.symbol == "DAILY":
                    clock.t = start + timedelta(minutes=30)

            pipe._review = review
            outcome = await pipe.run("prescan")
            assert outcome.status is JobStatus.DONE and outcome.detail["reviewed"] == 3
            assert outcome.detail["deferred"] == 2 and outcome.detail["unavailable"] == 0
            assert outcome.detail["entry_funnel"]["rejected_by_reason"]["prescan_time_budget"] == 2
            assert pipe.store.work_state(f"prescan:{start.date()}:ABC") is None
            assert pipe.store.work_state(f"prescan:{start.date()}:FOCUS") is None
            assert pipe.store.available_budget(clock.t)["billed_or_reserved_microusd"] == 0
            assert prescan_runtime_seconds(start, opens) == 2100
            assert prescan_runtime_seconds(opens - timedelta(minutes=31), opens) == 1260
            assert prescan_runtime_seconds(opens - timedelta(minutes=9), opens) == 0

    asyncio.run(run())
    assert reviews == [("OWNER", False, True), ("HELD", True, False), ("DAILY", False, False)]


def test_perplexity_fake_counts_search_and_answer_against_one_cap():
    fake = FakePerplexity()
    fake.reply(*(LLMReply(text=ANSWER, output_tokens=500, search_output_tokens=1500) for _ in range(2)))
    with httpx.Client(transport=FakeInternet([fake]).transport) as http:
        low, high = (http.post("https://api.perplexity.ai/v1/agent", json={"max_output_tokens": limit}).json()
                     for limit in (1800, 4096))
    assert low["status"] == "incomplete" and low["incomplete_details"] == {"reason": "max_output_tokens"}
    assert low["usage"]["output_tokens"] == 1800
    assert high["status"] == "completed" and high["usage"]["output_tokens"] == 2000


def test_perplexity_raises_only_grounded_cap_and_reserves_it():
    fake = FakePerplexity()
    fake.reply(LLMReply(text=ANSWER, output_tokens=700, search_output_tokens=1400, sources=SOURCES), ANSWER)
    reserved = []
    spend = SimpleNamespace(reserve=lambda *args, **kwargs: reserved.append(args[2]) or True, settle=lambda *args: None)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(FakeInternet([fake]).handle)) as http:
            llm = LLMClient(settings(OPENAI_API_KEY=""), client=http, spend=spend, now=lambda: NOW)
            await llm.complete(request())
            await llm.complete(request(grounded=False))

    asyncio.run(run())
    grounded, plain = fake.prompts
    assert (grounded["max_output_tokens"], plain["max_output_tokens"]) == (4096, 1800)
    expected = ((len(json.dumps(grounded, ensure_ascii=False).encode()) + 2000 + 2 * 2500) * 3 * 2
                + 4096 * 10 + 2 * 2500 + 1)
    assert reserved[0] == expected


@pytest.mark.parametrize(("first", "kind", "code", "can_fallback"), [
    ((200, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}),
     "incomplete_output", "max_output_tokens", True),
    ((400, {"error": {"type": "invalid_request_error", "code": "invalid_type", "message": "never record this"}}),
     "request_rejected", "invalid_type", True),
    ((200, completed('{"direction"', [])), "invalid_output", "answer_json", True),
    ((200, completed(ANSWER, [])), "no_grounding", None, False),
    ((200, completed()), "decision_rejected", "decision_validation", False),
])
def test_owner_retries_transport_but_not_a_parsed_decision(first, kind, code, can_fallback):
    calls = []
    health = []

    def endpoint(req):
        calls.append(req.url.host)
        status, body = first if len(calls) == 1 else (200, completed())
        return httpx.Response(status, json=body)

    def validate(*_):
        if kind == "decision_rejected":
            raise ValueError("not a valid owner decision")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            llm = LLMClient(settings(), client=http, now=lambda: NOW, health=SimpleNamespace(
                record_llm_failure=lambda *args, **kwargs: health.append(kwargs["summary"]),
            ))
            if can_fallback:
                result = await llm.complete(request(validate=validate))
                assert result.route.provider == "openai"
                attempts = result.attempts
            else:
                with pytest.raises(ProviderUnavailable) as exc:
                    await llm.complete(request(validate=validate))
                attempts = exc.value.attempts
            assert [(error.kind, error.code) for error in attempts] == [(kind, code)]
            tags = [f"perplexity:{code}"] if code else []
            if code == "answer_json":
                tags.append("perplexity:$:json_invalid")
            assert ProviderUnavailable(attempts).failure_codes == ({"failure_codes": tags} if tags else {})

    asyncio.run(run())
    assert calls == ["api.perplexity.ai"] + (["api.openai.com"] * 2 if can_fallback else [])
    if kind in {"decision_rejected", "no_grounding"}:
        assert not health


def test_only_allowlisted_error_codes_are_kept():
    assert ProviderError("openai", "", "invalid_output", code="key-looking-text").safe_code is None

    async def run():
        def endpoint(_):
            return httpx.Response(400, json={"error": {
                "code": "thesis_unknown_evidence", "message": "never record this",
            }})

        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            llm = LLMClient(settings(OPENAI_API_KEY=""), client=http, now=lambda: NOW)
            with pytest.raises(ProviderUnavailable) as err:
                await llm.complete(request())
            assert err.value.failure_codes == {}
            assert err.value.attempts[0].code is None
            assert "never record" not in json.dumps(str(err.value))

    asyncio.run(run())


@pytest.mark.parametrize(("failure", "code"), [
    ("request_rejected", "invalid_type"),
    ("invalid_output", "answer_json"),
    ("invalid_output", "schema_invalid"),
])
def test_research_work_adds_safe_codes_without_replacing_failure_kinds(tmp_path, failure, code, caplog):
    clock, ledger, _, make_pipeline = rig(tmp_path)

    def endpoint(_):
        if code == "schema_invalid":
            return httpx.Response(200, json=completed(json.dumps({
                "symbol": "ABC", "direction": "BUY", "conviction": 8, "summary": "hidden" * 200,
                "bull_case": "bull", "bear_case": "bear", "claims": [{"text": "claim", "evidence_ids": []}],
                "catalysts": [], "risks": [], "thesis_break": None, "thesis_change": "unchanged",
                "target_price": None, "horizon_days": 2, "hidden_key": "never record",
            })))
        if failure == "invalid_output":
            return httpx.Response(200, json=completed("never record this hidden malformed answer"))
        return httpx.Response(400, json={"error": {
            "code": "invalid_type", "param": "hidden", "message": "never record this"
        }})

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipeline(http)
            await pipe.run("weekly")
            work = pipe.store.work_state(f"weekly:{clock.t.date()}:ABC")
            details = json.loads(work["details_json"])
            assert details["failures"] == [f"openai:{failure}"]
            issues = {
                "answer_json": ["$:json_invalid"],
                "schema_invalid": ["summary:string_too_long", "claims[].evidence_ids:too_short", "$:extra_forbidden"],
            }.get(code, [])
            assert details["failure_codes"] == [f"openai:{tag}" for tag in [code, *issues]]
            assert "never record" not in work["details_json"] and "hidden" not in work["details_json"]

    asyncio.run(run())
    assert "never record" not in caplog.text and "hidden" not in caplog.text


@pytest.mark.parametrize(("answer", "code", "issue"), [
    ("", "answer_empty", None),
    ("x" * 22_001, "answer_too_large", None),
    ('{"direction":"BUY","direction":"HOLD","conviction":8,"note":null}', "answer_json", "$:json_invalid"),
    ('{"direction":"BUY","conviction":NaN,"note":null}', "answer_json", "$:json_invalid"),
    ('{"direction":"hidden","conviction":8,"note":null}', "schema_direction", "direction:enum"),
    ('{"direction":"BUY","conviction":8}', "schema_missing_field", "$:missing"),
    ('{"direction":"BUY","conviction":8,"note":null,"hidden":true}', "schema_extra_field", "$:extra_forbidden"),
    ('{"direction":"BUY","conviction":"hidden","note":null}', "schema_invalid", "conviction:int_type"),
])
def test_ungrounded_invalid_answer_codes_are_safe(answer, code, issue, caplog):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=completed(answer, [])),
        )) as http:
            llm = LLMClient(settings(OPENAI_API_KEY=""), client=http, now=lambda: NOW)
            with pytest.raises(ProviderUnavailable) as exc:
                await llm.complete(request(grounded=False))
            assert exc.value.attempts[0].kind == "invalid_output"
            assert exc.value.attempts[0].safe_schema_issues == ((issue,) if issue else ())
            assert exc.value.failure_codes == {"failure_codes": [f"perplexity:{tag}" for tag in (code, issue) if tag]}

    asyncio.run(run())
    assert f"code={code}" in caplog.text and "hidden" not in caplog.text


@pytest.mark.parametrize(("direction", "cite", "dated", "thesis_break", "code"), [
    ("HOLD", "source", True, "Break", "thesis_direction"),
    ("BUY", "bogus", True, "Break", "thesis_unknown_evidence"),
    ("BUY", "source", False, "Break", "thesis_missing_dated_evidence"),
    ("BUY", "source", True, None, "thesis_missing_invalidation"),
])
def test_best_ideas_codes_distinguish_parsed_thesis_validation(direction, cite, dated, thesis_break, code):
    def endpoint(req):
        wire = response(json.loads(req.content), direction=direction, cite=cite).json()
        draft = json.loads(wire["output"][-1]["content"][0]["text"])
        draft["thesis_break"] = thesis_break
        source = {"url": "https://example.org/results", "id": "source"}
        if dated:
            source["published_at"] = (NOW - timedelta(days=1)).isoformat()
        return httpx.Response(200, json=completed(json.dumps(draft), [source]))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            analyst = Analyst(LLMClient(settings(OPENAI_API_KEY=""), client=http, now=lambda: NOW))
            data = ResearchInput("ABC", NOW, "ABC", "t", 20, None, {}, ())
            with pytest.raises(ProviderUnavailable) as exc:
                await analyst.evaluate(data, best_ideas=True)
            assert exc.value.attempts[0].kind == "invalid_output"
            assert exc.value.failure_codes == {"failure_codes": [f"perplexity:{code}"]}

    asyncio.run(run())


@pytest.mark.parametrize(("good", "error_codes", "success"), [
    (True, ["max_uses_exceeded", "max_uses_exceeded"], True),
    (False, ["max_uses_exceeded"], False),
    (True, ["max_uses_exceeded", "unavailable"], False),
])
def test_anthropic_evidence_pass_uses_real_results_after_exhausting_search_limit(good, error_codes, success):
    calls = []
    ok = {"type": "web_search_tool_result", "content": [
        {"type": "web_search_result", "url": "https://example.org/claude", "page_age": "November 23, 2026"},
    ]}
    errors = [{"type": "web_search_tool_result", "content": {
        "type": "web_search_tool_result_error", "error_code": code,
    }} for code in error_codes]

    def endpoint(req):
        body = json.loads(req.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200, json={"stop_reason": "max_tokens", "content": [*([ok] if good else []), *errors]})
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": ANSWER}]})

    async def run():
        config = settings(RESEARCH_PROVIDER_ORDER=("anthropic",), ANTHROPIC_API_KEY="fixture",
                          RESEARCH_ANTHROPIC_MODEL="claude-sonnet-5-5")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            llm = LLMClient(config, client=http, now=lambda: NOW)
            if success:
                result = await llm.complete(request())
                assert len(result.evidence) == 1
                assert result.evidence[0].published_at is None  # page_age is not publication.
            else:
                with pytest.raises(ProviderUnavailable) as exc:
                    await llm.complete(request())
                assert exc.value.attempts[0].kind == "search_failed"
                assert exc.value.failure_codes == {"failure_codes": [f"anthropic:{error_codes[-1]}"]}

    asyncio.run(run())
    assert "exactly one focused web search query" in calls[0]["system"]
    assert calls[0]["tools"][0]["max_uses"] == 1
    assert len(calls) == (2 if success else 1)
    if success:
        assert "tools" not in calls[1] and "output_config" in calls[1]


def test_perplexity_calendar_day_precision_is_bounded_and_only_for_search_results():
    now = datetime(2026, 11, 25, 9, tzinfo=UTC)
    rows = [
        {"date": "2026-11-25"},  # today: earliest is yesterday at 10:00 UTC
        {"date": "2026-11-26"},  # earliest is one hour after as-of
        {"date": "2026-02-31"},
        {"date": "2026-11-24T13:00:00Z"},
        {"last_updated": "2026-11-25"},
        {"date": "2026-07-28"},  # date appears <120 days old; earliest instant is >120 days
    ]
    for i, row in enumerate(rows):
        row.update(url=f"https://example.org/{i}", id=str(i))
    llm = LLMClient(settings(), now=lambda: now)
    envelope = completed(sources=rows)
    envelope["output"][-1]["content"][0]["annotations"] = [
        {"type": "url_citation", "url": "https://example.org/annotation", "date": "2026-11-25"},
    ]
    today, tomorrow, invalid, not_exact, modification, old, annotation = llm._evidence("perplexity", envelope)
    assert today.date_precision == "day"
    assert today.published_at == datetime(2026, 11, 24, 10, tzinfo=UTC)
    assert all(item.published_at is None for item in (tomorrow, invalid, not_exact, modification, annotation))
    assert Evidence.model_validate_json(today.model_dump_json()).date_precision == "day"
    assert "date_precision" not in modification.model_dump()
    assert Evidence.model_validate(modification.model_dump()).date_precision == "instant"
    for provider in ("openai", "anthropic"):
        assert llm._evidence(provider, envelope if provider == "openai" else {
            "content": [{"type": "text", "citations": [rows[0]]}],
        })[0].published_at is None
    known = _index_evidence(ResearchInput("ABC", now, "ABC", "t", 20, None, {}, ()), (today, old))
    assert today.evidence_id in known and old.evidence_id not in known


@pytest.mark.parametrize(("direction", "added_days", "day_source", "expected"), [
    ("BUY", 0, True, True),
    ("AVOID", 0, True, False),
    ("AVOID", 1, True, False),
    ("AVOID", 2, True, True),
    ("AVOID", 1, False, True),
])
def test_owner_buy_and_veto_apply_publication_precision(direction, added_days, day_source, expected):
    now = datetime(2026, 11, 25, 15, tzinfo=UTC)
    owner = FocusItem("ABC", None, "Veto on a cut.", now.date() - timedelta(days=added_days),
                      now.date() + timedelta(days=7), stance="bullish", owner_conviction=8)

    def endpoint(req):
        body = json.loads(req.content)
        wire = response(body, direction=direction, cite="calendar").json()
        item = wire["output"][-1]["content"][0]
        draft = json.loads(item["text"])
        draft.update(stop_price=17 if direction == "BUY" else None, veto_claim_index=None if direction == "BUY" else 0)
        if direction == "AVOID":
            draft["thesis_break"] = draft["claims"][0]["text"]
        stamp = {"date": "2026-11-25"} if day_source else {"published_at": "2026-11-25T12:00:00Z"}
        return httpx.Response(200, json=completed(json.dumps(draft), [{
            "url": "https://example.org/calendar", "id": "calendar", **stamp,
        }]))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            analyst = Analyst(LLMClient(settings(OPENAI_API_KEY=""), client=http, now=lambda: now))
            data = ResearchInput("ABC", now, "ABC", "t", 20, None, {}, ())
            if expected:
                result = await analyst.evaluate(data, owner=owner)
                assert result.record.draft.direction == direction
            else:
                with pytest.raises(ProviderUnavailable) as exc:
                    await analyst.evaluate(data, owner=owner)
                assert [a.kind for a in exc.value.attempts] == ["decision_rejected"]
                assert exc.value.failure_codes == {"failure_codes": ["perplexity:owner_veto_date"]}

    asyncio.run(run())
