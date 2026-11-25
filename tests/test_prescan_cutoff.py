import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from stock_agent import research_focus
from stock_agent.ledger import JobStatus, ThesisRole
from stock_agent.market_calendar import Session
from stock_agent.research_focus import FocusItem
from tests.test_owner_idea_research import _reply
from tests.test_scheduled_best_ideas import FILING, openai_search_pass, response, rig


@pytest.mark.parametrize("direction", ["BUY", "AVOID"])
@pytest.mark.parametrize("late", [True, False])
def test_prescan_owner_provider_return_past_cutoff_cannot_publish(tmp_path, monkeypatch, direction, late):
    clock, ledger, _, make_pipeline = rig(tmp_path, supplied_url=FILING)
    start = clock.t
    opens = start + timedelta(minutes=31)
    owner = FocusItem("ABC", None, "Fixture directive", start.date() - timedelta(days=10), start.date(),
                      stance="bullish", owner_conviction=8)
    monkeypatch.setattr(research_focus, "owner_backed", lambda **_: {"ABC": owner})

    def endpoint(req):
        body = json.loads(req.content)
        if openai_search_pass(body):
            return response(body)
        if late:
            clock.t = opens - timedelta(minutes=9)  # Wall clock crossed the open−10m fence.
        return _reply(body, direction=direction, stop=17 if direction == "BUY" else None)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipeline(http)
            pipe.calendar.session_on = lambda day: Session(day, opens, opens + timedelta(hours=6))
            pipe.directives = lambda **_: {"ABC": owner}
            pipe.daily_focus = lambda **_: ()
            outcome = await pipe.run("prescan")
            assert outcome.status is JobStatus.DONE and outcome.detail["unavailable"] == 0
            assert outcome.detail["deferred"] == int(late)
            assert ledger.owner_directive_vetoed("ABC", owner.added) is (not late and direction == "AVOID")
            if late:
                assert ledger.current_thesis("ABC") is None
                assert not ledger.store.query("SELECT 1 FROM theses WHERE symbol='ABC'")
                assert not pipe.store.pending_buys(clock.t)
                assert not pipe.store.pending_option_ideas(clock.t)
                assert not outcome.intents and not outcome.option_candidates
                row = pipe.store.work_state(f"prescan:{start.date()}:ABC")
                assert row["status"] == "retry" and json.loads(row["details_json"]) == {"reason": "prescan_time_budget"}
            else:
                assert ledger.store.query("SELECT 1 FROM theses WHERE symbol='ABC'")
                assert bool(outcome.intents) == (direction == "BUY")
                assert bool(outcome.option_candidates) == (direction == "BUY")

    asyncio.run(run())


def test_prescan_late_exit_confirmation_does_not_write_exit_or_erase_initial_thesis(tmp_path):
    clock, ledger, _, make_pipeline = rig(tmp_path, supplied_url=FILING)
    start = clock.t
    opens = start + timedelta(minutes=31)
    ledger.broker.pos("ABC", 5, 20)
    calls = []

    def endpoint(req):
        body = json.loads(req.content)
        name = body["text"]["format"]["name"]
        calls.append(name)
        if name == "ExitConfirmation":
            clock.t = opens - timedelta(minutes=9)
            value = {"symbol": "ABC", "verdict": "CONFIRM_BREAK", "conviction": 8,
                     "reason": "Fixture break", "evidence_ids": ["background"]}
            return httpx.Response(200, json={"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]},
            ]})
        wire = response(body, direction="EXIT_PROPOSED", cite="background").json()
        part = wire["output"][-1]["content"][0]
        part["text"] = json.dumps({**json.loads(part["text"]), "thesis_change": "broken"})
        return httpx.Response(200, json=wire)

    async def run():
        await ledger.refresh("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
            pipe = make_pipeline(http)
            pipe.calendar.session_on = lambda day: Session(day, opens, opens + timedelta(hours=6))
            pipe.directives = lambda **_: {}
            pipe.daily_focus = lambda **_: ()
            outcome = await pipe.run("prescan")
            assert calls == ["ThesisDraft", "ExitConfirmation"]
            assert outcome.status is JobStatus.DONE and not outcome.intents
            assert ledger.current_thesis("ABC").role is ThesisRole.HELD_REVIEW
            assert not ledger.store.query("SELECT 1 FROM theses WHERE role=?", (ThesisRole.EXIT_CONFIRMATION,))
            assert not ledger.store.query("SELECT 1 FROM research_exit_slots")
            (row,) = ledger.store.query("SELECT status FROM research_confirmations")
            assert row["status"] == "waiting"

    asyncio.run(run())
