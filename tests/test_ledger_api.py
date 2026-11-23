from datetime import date, timedelta

import pytest

from stock_agent.ledger import (
    IntentStatus,
    JobStatus,
    LedgerError,
    NewIntent,
    StaleBook,
    ThesisRole,
    client_order_id_for,
)
from tests.ledger_helpers import Clock, StubBroker, make_ledger, run


def test_record_thesis_and_current(tmp_path):
    led = make_ledger(tmp_path)
    t1 = led.record_thesis("AAA", {"conviction": 8, "direction": "BULLISH"}, ThesisRole.ENTRY_CANDIDATE,
                           source="openai", activate=True)
    assert led.current_thesis("AAA").thesis_id == t1
    t2 = led.record_thesis("AAA", {"conviction": 9}, ThesisRole.HELD_REVIEW)
    assert led.current_thesis("AAA").thesis_id == t1        # not activated
    led.set_current_thesis("AAA", t2)
    cur = led.current_thesis("AAA")
    assert cur.thesis_id == t2 and cur.payload["conviction"] == 9


def test_set_current_thesis_rejects_legacy_and_confirmation(tmp_path):
    led = make_ledger(tmp_path)
    leg = led.record_thesis("AAA", {"summary": "imported"}, ThesisRole.LEGACY_UNVERIFIED)
    conf = led.record_thesis("AAA", {"verdict": "sell"}, ThesisRole.EXIT_CONFIRMATION)
    with pytest.raises(LedgerError):
        led.set_current_thesis("AAA", leg)
    with pytest.raises(LedgerError):
        led.set_current_thesis("AAA", conf)
    with pytest.raises(LedgerError):
        led.record_thesis("AAA", {}, ThesisRole.EXIT_CONFIRMATION, activate=True)
    assert led.current_thesis("AAA") is None


def test_current_thesis_max_age(tmp_path):
    clock = Clock()
    led = make_ledger(tmp_path, clock=clock)
    led.record_thesis("AAA", {}, ThesisRole.ENTRY_CANDIDATE, generated_at=clock.t - timedelta(hours=100),
                      activate=True)
    assert led.current_thesis("AAA", max_age=timedelta(hours=96)) is None
    assert led.current_thesis("AAA") is not None


def _buy(sym="AAA", key=None, **kw):
    return NewIntent(symbol=sym, side="buy", dedupe_key=key or f"weekly:2026-W48:{sym}:buy", **kw)


def test_intent_survives_restart(tmp_path):
    led = make_ledger(tmp_path)
    [iid] = led.enqueue_intents([_buy()], job="weekly", window_key="2026-W48")
    led.store.close()
    led2 = make_ledger(tmp_path)
    it = led2.get_intent(iid)
    assert it.status == IntentStatus.QUEUED and it.client_order_id == client_order_id_for(iid)
    assert len(it.client_order_id) <= 40


def test_dedupe_key_unique(tmp_path):
    led = make_ledger(tmp_path)
    a = led.enqueue_intents([_buy()])
    b = led.enqueue_intents([_buy()])
    assert a == b and len(led.list_intents()) == 1


def test_skipped_intent_records_refusal(tmp_path):
    led = make_ledger(tmp_path)
    [iid] = led.enqueue_intents([_buy(status=IntentStatus.SKIPPED, last_error="sector cap")])
    it = led.get_intent(iid)
    assert it.status == IntentStatus.SKIPPED and it.last_error == "sector cap" and it.terminal


def test_claim_then_ack_flow_and_illegal_transition(tmp_path):
    led = make_ledger(tmp_path)
    [iid] = led.enqueue_intents([_buy()])
    assert led.claim_intent(iid).status == IntentStatus.SUBMITTING
    with pytest.raises(LedgerError):
        led.claim_intent(iid)
    led.ack_intent(iid, IntentStatus.SUBMITTED, broker_order_id="b1")
    it = led.ack_intent(iid, IntentStatus.FILLED)
    assert it.broker_order_id == "b1" and it.attempts == 1
    with pytest.raises(LedgerError):
        led.ack_intent(iid, IntentStatus.QUEUED)


def test_submitting_becomes_ambiguous_on_start(tmp_path):
    led = make_ledger(tmp_path)
    [iid] = led.enqueue_intents([_buy()])
    led.claim_intent(iid)
    led.store.close()
    led2 = make_ledger(tmp_path)
    [amb] = led2.mark_submitting_ambiguous()
    assert amb.intent_id == iid and amb.status == IntentStatus.AMBIGUOUS
    # broker said 404 for the client id -> safe to queue again with the SAME client id
    again = led2.ack_intent(iid, IntentStatus.QUEUED, error="not found at broker")
    assert again.client_order_id == client_order_id_for(iid)
    assert led2.claim_intent(iid).attempts == 2


def test_intent_expires(tmp_path):
    clock = Clock()
    led = make_ledger(tmp_path, clock=clock)
    [iid] = led.enqueue_intents([_buy(expires_at=clock.t + timedelta(hours=1))])
    clock.t += timedelta(hours=2)
    assert led.due_intents() == []
    assert led.get_intent(iid).status == IntentStatus.EXPIRED


def test_next_attempt_at_defers(tmp_path):
    clock = Clock()
    led = make_ledger(tmp_path, clock=clock)
    led.enqueue_intents([_buy(next_attempt_at=clock.t + timedelta(minutes=5))])
    assert led.due_intents() == []
    clock.t += timedelta(minutes=6)
    assert len(led.due_intents()) == 1


def test_superseded_thesis_blocks_claim(tmp_path):
    led = make_ledger(tmp_path)
    t1 = led.record_thesis("AAA", {}, ThesisRole.ENTRY_CANDIDATE, activate=True)
    [iid] = led.enqueue_intents([_buy(thesis_ids=(t1,))])
    led.record_thesis("AAA", {}, ThesisRole.HELD_REVIEW, activate=True)
    with pytest.raises(LedgerError):
        led.claim_intent(iid)
    assert led.get_intent(iid).status == IntentStatus.CANCELLED


def test_finish_job_and_enqueue_atomic(tmp_path):
    led = make_ledger(tmp_path)
    led.start_job("weekly", "2026-W48")
    bad = [_buy("AAA"), NewIntent(symbol="BBB", side="short", dedupe_key="x")]
    with pytest.raises(LedgerError):
        led.finish_job("weekly", "2026-W48", JobStatus.DONE, intents=bad)
    assert led.job_status("weekly", "2026-W48").status == JobStatus.STARTED
    assert led.list_intents() == []
    ids = led.finish_job("weekly", "2026-W48", JobStatus.DONE, intents=[_buy("AAA")])
    assert len(ids) == 1 and led.job_status("weekly", "2026-W48").status == JobStatus.DONE
    with pytest.raises(LedgerError):
        led.start_job("weekly", "2026-W48")


def test_failed_job_retry_window(tmp_path):
    clock = Clock()
    led = make_ledger(tmp_path, clock=clock)
    led.start_job("prescan", "2026-11-23")
    led.finish_job("prescan", "2026-11-23", JobStatus.FAILED, eligible_retry_at=clock.t + timedelta(minutes=10))
    with pytest.raises(LedgerError):
        led.start_job("prescan", "2026-11-23")
    clock.t += timedelta(minutes=11)
    assert led.start_job("prescan", "2026-11-23").attempts == 2


def test_snapshot_stale_raises(tmp_path):
    clock = Clock()
    b = StubBroker()
    led = make_ledger(tmp_path, broker=b, clock=clock)
    with pytest.raises(StaleBook):
        led.snapshot()
    run(led.refresh("test"))
    assert led.snapshot().equity == 100000
    clock.t += timedelta(seconds=500)
    with pytest.raises(StaleBook):
        led.snapshot()


def test_equity_marks_and_risk_state(tmp_path):
    b = StubBroker(cash=50000)
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("close"))
    led.record_equity_mark(date(2026, 11, 23), "close", snap)
    [m] = led.equity_marks()
    assert m.equity == 50000 and m.session_date == date(2026, 11, 23)
    led.risk_state_set("drawdown", {"hwm": "50000"})
    assert led.risk_state_get("drawdown") == {"hwm": "50000"}
