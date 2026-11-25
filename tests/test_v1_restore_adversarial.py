"""Adversarial breaking tests for the 5-part v1-intent restore (feat/v1-intent-restore).

Covers cross-component edge cases not duplicated by unit suites:
1. Handoff transient 1m retries + _analysis_lock contention must not inflate EOD entry funnel skips.
2. Handoff terminal expiry (including 15:50 window-close wait -> 16:00 expiry) counts once, never twice.
3. Weekly -> Monday prescan supersession replaces waiting handoff without phantom skips or #risk alerts.
4. Reconciled legacy v0 holding with missing conviction (opened_source='v0_verified') initializes once
   on first fresh BUY >= 8, refuses same-day loop, and blocks next-day conviction downgrade.
5. Rollback safety: SQLite DB + journal written by feat/v1-intent-restore opens and compiles cleanly
   under origin/master (b00dfcd).

All figures are synthetic round numbers only.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from stock_agent.execution import ExecSettings
from stock_agent.ledger import IntentStatus, NewIntent, ThesisRole
from stock_agent.migrate_v0 import migrate_v0
from stock_agent.reporting import ReportBuilder
from tests.exec_helpers import make_engine
from tests.fakes import ET
from tests.scenarios.v1_restore_support import Harness, Reply, monday_hold_catchup, tuesday_prescan_with_stale_quote

_SUNDAY = datetime(2026, 11, 15, 17, 0, tzinfo=ET)
_ROLLBACK_SHA = "b00dfcd602ca1eeb78482c853062142ce43f2f54"
_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.world_start(_SUNDAY)
def test_handoff_transient_retries_and_lock_contention_do_not_inflate_eod_funnel(world, monkeypatch):
    """5 transient 1m retries + lock contention before 09:41 promotion must leave EOD skips_by_reason empty."""
    h = Harness(world, monkeypatch).install(price=100.0)

    async def scenario():
        s = await h.start()
        try:
            await monday_hold_catchup(h)
            prescan_row = await tuesday_prescan_with_stale_quote(h)
            assert prescan_row.detail["entry_funnel"].get("handoff_staged") == 1
            assert prescan_row.detail["entry_funnel"]["rejected_by_reason"] == {}

            # Advance to Tue 09:35 with stale quotes so handoff retries transiently.
            h.quotes_go_stale()
            await h.advance(17, 9, 35)

            # 1) Lock contention: while _analysis_lock is held, run_entry_handoff must not spam empty journal rows.
            before_rows = list(s.ledger.journal.read(kinds={"research.entry_funnel"}))
            async with s.research._analysis_lock:
                locked_outcome = await s.research.run_entry_handoff()
            after_rows = list(s.ledger.journal.read(kinds={"research.entry_funnel"}))
            assert locked_outcome.detail["handoff_outcomes"] == {}
            assert len(after_rows) == len(before_rows)

            # 2) Run 4 transient retry windows (1m apart) while quote stays stale -> must hold, never skip.
            for minute in (35, 36, 37, 38):
                await h.advance(17, 9, minute)
                out = await s.research.run_entry_handoff()
                assert out.detail["handoff_outcomes"] == {"quote_stale_or_unavailable": 1}
                assert h.intents() == []

            # 3) Fresh quote arrives at 09:40 -> promotes and fills once; EOD funnel has 1 fill and 0 skips.
            await h.advance(17, 9, 40)
            h.fresh_quote(100.0)
            await h.execute(cycles=2)
            assert len(h.buys()) == 1

            rb = ReportBuilder(ledger=s.ledger, research_store=s.research_store)
            funnel = rb._compile_entry_funnel(date(2026, 11, 17))
            assert funnel["filled"] == 1
            assert funnel["skips_by_reason"] == {}
            assert funnel["research_skips_by_reason"] == {}
            assert funnel["execution_skips_by_reason"] == {}
        finally:
            await h.stop()

    asyncio.run(scenario())


@pytest.mark.world_start(_SUNDAY)
def test_handoff_terminal_expiry_counted_exactly_once_in_eod_funnel(world, monkeypatch):
    """A handoff waiting through 15:50 window-close and expiring at 16:00 counts once in EOD skips."""
    h = Harness(world, monkeypatch).install(price=100.0)

    async def scenario():
        s = await h.start()
        try:
            await monday_hold_catchup(h)
            await tuesday_prescan_with_stale_quote(h)
            h.quotes_go_stale()

            # Retry once at 09:35, hit 15:50 window-close wait, then expire at 16:00 close.
            await h.advance(17, 9, 35)
            r1 = await s.research.run_entry_handoff()
            assert r1.detail["handoff_outcomes"] == {"quote_stale_or_unavailable": 1}

            await h.advance(17, 15, 52)
            r2 = await s.research.run_entry_handoff()
            assert r2.detail["handoff_outcomes"] == {"handoff_execution_window_closed": 1}
            assert h.intents() == []

            await h.advance(17, 16, 1)
            r3 = await s.research.run_entry_handoff()
            assert r3.detail["handoff_outcomes"] == {"handoff_expired": 1}
            assert len(h.intents()) == 1
            assert h.intents()[0].status is IntentStatus.SKIPPED

            rb = ReportBuilder(ledger=s.ledger, research_store=s.research_store)
            funnel = rb._compile_entry_funnel(date(2026, 11, 17))
            assert funnel["filled"] == 0
            assert funnel["ordered"] == 0
            assert sum(funnel["skips_by_reason"].values()) == 1
            assert funnel["research_skips_by_reason"] == {}
        finally:
            await h.stop()

    asyncio.run(scenario())


@pytest.mark.world_start(_SUNDAY)
def test_weekly_to_monday_prescan_supersession_emits_no_phantom_skip_or_alert(world, monkeypatch):
    """Sunday weekly BUY superseded by Monday 08:00 prescan BUY must not emit a skipped intent or #risk alert."""
    h = Harness(world, monkeypatch).install(price=100.0)
    world.alpaca.seed_daily_bars("AAPL", [100.0, 100.0], end=date(2026, 11, 16))
    blocked_alerts: list[tuple] = []

    async def scenario():
        s = await h.start()
        s.alerts.alert_blocked_buy = lambda *a, **kw: blocked_alerts.append((a, kw))
        try:
            # Seed one weekend background story (>24h old by Mon 09:35 so live_news does not double-trade it).
            h.story(minutes_ago=16 * 60)
            # Sunday 18:00 weekly stages T1 for Monday 2026-11-16.
            await h.advance(15, 18, 0)
            w_row = await h.run_research("weekly")
            assert w_row.detail["entry_funnel"].get("handoff_staged") == 1

            # Monday 08:00 prescan writes fresher BUY T2 -> supersedes T1 cleanly.
            await h.advance(16, 8, 0)
            p_row = await h.run_research("prescan")
            assert p_row.detail["entry_funnel"].get("handoff_superseded") == 1
            assert p_row.detail["entry_funnel"].get("handoff_staged") == 1

            # Monday 09:35 promotes T2 once; catchup sees protected scheduled buy and does not overwrite it.
            await h.advance(16, 9, 35)
            h.fresh_quote(100.0)
            await h.execute(cycles=2)

            assert len(h.buys()) == 1, h.intent_states()
            assert h.intent_states() == [(IntentStatus.FILLED.value, "prescan", h.intents()[0].dedupe_key, None)], h.intent_states()
            assert blocked_alerts == []

            rb = ReportBuilder(ledger=s.ledger, research_store=s.research_store)
            funnel = rb._compile_entry_funnel(date(2026, 11, 16))
            assert funnel["filled"] == 1
            assert funnel["skips_by_reason"] == {}
        finally:
            await h.stop()

    asyncio.run(scenario())


@pytest.mark.world_start(datetime(2026, 10, 1, 10, 0, tzinfo=ET))
def test_reconciled_v0_verified_ungraded_holding_initializes_once_and_blocks_loop_or_downgrade(
    world, tmp_path, monkeypatch
):
    """Live startup path: migrate_v0 (ungraded holding) -> reconcile ('v0_verified') -> one top-up -> baseline pinned."""
    # Seed legacy portfolio.json with NO conviction / thesis for AAA (synthetic round figures).
    (tmp_path / "portfolio.json").write_text(json.dumps({
        "positions": [
            {
                "symbol": "AAA",
                "shares": 10,
                "entry_price": 50.0,
                "stop_loss": 45.0,
                "entry_date": "2026-10-01",
                "sector": "Industrials",
            }
        ]
    }))
    world.clock.set_et(2026, 10, 1, 10, 30)
    world.price("AAA", 50.0, spread=0.02)
    world.alpaca.submit({"symbol": "AAA", "side": "buy", "qty": 10, "type": "market", "time_in_force": "day"})
    world.clock.set_et(2026, 11, 23, 10, 0)
    world.price("AAA", 50.0, spread=0.02)

    engine = make_engine(world, tmp_path, monkeypatch, settings=ExecSettings(fill_wait_seconds=10))
    rep = migrate_v0(tmp_path, engine.ledger.store)
    assert rep.ran and rep.positions == 1

    asyncio.run(engine.ledger.refresh("startup", rebuild=True))
    asyncio.run(engine.ensure_protection())
    meta = engine.ledger.open_position_meta("AAA")
    assert meta is not None
    assert meta.opened_source == "v0_verified"
    assert engine.ledger.top_up_baseline(meta.position_id) == (None, True)

    def write_review(grade: int, *, direction: str = "BUY", change: str = "unchanged") -> str:
        return engine.ledger.record_thesis(
            "AAA",
            {
                "effective_conviction": grade,
                "raw_conviction": grade,
                "draft": {"thesis_change": change, "direction": direction},
            },
            ThesisRole.HELD_REVIEW,
            source="openai/gpt-5",
            activate=True,
            generated_at=engine.now(),
        )

    def queue_add(thesis_id: str, key: str, grade: int) -> object:
        [iid] = engine.ledger.enqueue_intents([NewIntent(
            "AAA", "buy", key, position_id=meta.position_id, thesis_ids=(thesis_id,),
            reference_price=Decimal("50"), session_date=engine.session_date(),
            meta={"raw_conviction": grade, "effective_conviction": grade,
                  "thesis_change": "unchanged", "sector": "Industrials"},
        )])
        return engine.ledger.get_intent(iid)

    # 1) HOLD 9 and BUY 7 cannot initialize or execute a top-up.
    r_hold = asyncio.run(engine.execute_intent(queue_add(write_review(9, direction="HOLD"), "hold-9", 9)))
    assert r_hold.status is IntentStatus.SKIPPED and r_hold.code == "top_up_thesis_direction"
    assert engine.ledger.top_up_baseline(meta.position_id) == (None, True)

    r_buy7 = asyncio.run(engine.execute_intent(queue_add(write_review(7, direction="BUY"), "buy-7", 7)))
    assert r_buy7.status is IntentStatus.SKIPPED and "top_up_conviction" in r_buy7.reason
    assert engine.ledger.top_up_baseline(meta.position_id) == (None, True)

    # 2) First fresh native BUY 9 ('unchanged') initializes baseline on fill (script partial 2 shares so <90% remains).
    world.alpaca.script_partial_fills("AAA", [2])
    first_intent = queue_add(write_review(9, direction="BUY", change="unchanged"), "buy-9-init", 9)
    r_fill = asyncio.run(engine.execute_intent(first_intent))
    assert r_fill.status is IntentStatus.FILLED and r_fill.filled_qty == 2
    stored = engine.ledger.get_intent(first_intent.intent_id)
    assert stored.meta.get("top_up_baseline_provenance") == "first_fresh_reaffirmation"
    assert stored.meta.get("top_up_baseline_grade") == 9
    assert engine.ledger.top_up_baseline(meta.position_id) == (9, False)
    stops = [o for o in world.alpaca.open_orders("AAA") if o.type == "stop"]
    assert len(stops) == 1 and stops[0].qty == 12

    # 3) Second top-up attempt same day is blocked (no same-day top-up loop).
    r_same_day = asyncio.run(engine.execute_intent(queue_add(write_review(9), "buy-9-loop", 9)))
    assert r_same_day.status is IntentStatus.SKIPPED and r_same_day.code == "top_up_already_today"

    # 4) Next trading day: BUY 8 ('strengthened') is refused because baseline 9 was established by the fill.
    world.clock.set_et(2026, 11, 24, 10, 0)
    world.price("AAA", 50.0, spread=0.02)
    r_downgrade = asyncio.run(
        engine.execute_intent(queue_add(write_review(8, change="strengthened"), "next-day-8", 8))
    )
    assert r_downgrade.status is IntentStatus.SKIPPED and "top_up_conviction" in r_downgrade.reason

    # 5) Next trading day: BUY 9 fills up to full target, after which another day's BUY 9 is refused as near_target.
    r_full = asyncio.run(engine.execute_intent(queue_add(write_review(9), "next-day-9-full", 9)))
    assert r_full.status is IntentStatus.FILLED and r_full.filled_qty > 0

    world.clock.set_et(2026, 11, 25, 10, 0)
    world.price("AAA", 50.0, spread=0.02)
    r_near = asyncio.run(engine.execute_intent(queue_add(write_review(9), "day3-near-target", 9)))
    assert r_near.status is IntentStatus.SKIPPED and "near_target" in r_near.reason


def test_sqlite_and_journal_downgrade_cleanly_to_b00dfcd(tmp_path):
    """State written by feat/v1-intent-restore must open and compile cleanly on origin/master (b00dfcd)."""
    check = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "cat-file", "-e", f"{_ROLLBACK_SHA}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if check.returncode != 0:
        pytest.skip(f"rollback commit {_ROLLBACK_SHA} not fetched in shallow clone")

    base_dir = tmp_path / "base_b00dfcd"
    base_dir.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "archive", "--format=tar", _ROLLBACK_SHA, "stock_agent"],
        capture_output=True,
        check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tf:
        tf.extractall(base_dir, filter="data")
    from stock_agent.config import Config
    from stock_agent.journal import Journal
    from stock_agent.ledger import Ledger
    from stock_agent.research_store import ResearchStore
    from stock_agent.state_store import StateStore

    now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    store = StateStore(tmp_path / "state.db", clock=lambda: now)
    ledger = Ledger(store, Journal(tmp_path / "journal.jsonl", clock=lambda: now), broker=None, clock=lambda: now)
    rstore = ResearchStore(store, Config())
    assert store.open().ok

    # Write every new v1-restore artifact shape into SQLite + journal:
    tid = ledger.record_thesis(
        "AAPL",
        {"effective_conviction": 9, "raw_conviction": 9, "draft": {"direction": "BUY", "thesis_change": "new"}},
        ThesisRole.ENTRY_CANDIDATE,
        source="openai/gpt-5",
        activate=True,
        generated_at=now,
    )
    pending_intent = NewIntent(
        symbol="AAPL", side="buy", dedupe_key="handoff:prescan:2026-11-24:AAPL",
        thesis_ids=(tid,), reference_price=Decimal("100"), origin="prescan",
        session_date=date(2026, 11, 24), expires_at=now + timedelta(hours=5),
        next_attempt_at=now + timedelta(minutes=5),
        meta={"scheduled_handoff": True, "reference_price_kind": "completed_split_adjusted_close"},
    )
    rstore.stage_pending_buy("prescan:2026-11-24:AAPL", tid, pending_intent, "completed_split_adjusted_close")

    [iid, _skipped_id] = ledger.enqueue_intents([
        NewIntent(
            symbol="MSFT", side="buy", dedupe_key="market_move:2026-11-24:1:MSFT",
            thesis_ids=(tid,), reference_price=Decimal("200"), origin="market_move",
            session_date=date(2026, 11, 24), expires_at=now + timedelta(hours=5),
            meta={"top_up_session": "2026-11-24", "top_up_baseline_provenance": "first_fresh_reaffirmation",
                  "top_up_baseline_grade": 9},
        ),
        NewIntent(
            symbol="NVDA", side="buy", dedupe_key="handoff:prescan:2026-11-24:NVDA",
            thesis_ids=(tid,), reference_price=Decimal("100"), origin="prescan",
            session_date=date(2026, 11, 24), expires_at=now,
            status=IntentStatus.SKIPPED, last_error="handoff_expired",
            meta={"scheduled_handoff": True, "skip_code": "handoff_expired", "handoff_reasons": ()},
        ),
    ])
    store.kv_set("scheduler.market_move", {
        "session": "2026-11-24", "slot": 1, "crossed": ["spy_down_close"],
        "claims": {"1": [{"signal": "spy_down_close", "text": "SPY -1.80% from the prior regular close"}]},
    })
    ledger.risk_state_set("options.rescan_requests", {"session": "2026-11-24", "keys": ["2026-11-24:1"]})
    ledger.journal.append(
        "research.entry_funnel", job="prescan", session_date="2026-11-24",
        screened=8, thesis_written=2, passed_conviction=2, buy_candidates=1,
        option_ideas=0, handoff_staged=1, handoff_superseded=1, rejected_by_reason={},
    )
    ledger.journal.append(
        "execution.buy_funnel", intent_id=iid, symbol="MSFT", origin="market_move",
        stage="filled", session_date="2026-11-24",
        top_up_baseline_provenance="first_fresh_reaffirmation", top_up_baseline_grade=9,
    )
    store.close()

    # Open the exact same state.db + journal.jsonl under b00dfcd code in a clean subprocess.
    probe = f"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from stock_agent.config import Config
from stock_agent.journal import Journal
from stock_agent.ledger import Ledger
from stock_agent.reporting import ReportBuilder
from stock_agent.research_store import ResearchStore
from stock_agent.state_store import StateStore

now = datetime(2026, 11, 24, 15, 5, tzinfo=timezone.utc)
root = Path({str(tmp_path)!r})
store = StateStore(root / "state.db", clock=lambda: now)
ledger = Ledger(store, Journal(root / "journal.jsonl", clock=lambda: now), broker=None, clock=lambda: now)
rstore = ResearchStore(store, Config())
health = store.open()
assert health.ok, health

due = ledger.due_intents(now)
assert len(due) == 1 and due[0].symbol == "MSFT", due
assert ledger.current_thesis("AAPL") is not None, "missing AAPL thesis"
rb = ReportBuilder(ledger=ledger, research_store=rstore)
funnel = rb._compile_entry_funnel(date(2026, 11, 24))
assert funnel["screened"] == 8, funnel
assert funnel["filled"] == 1, funnel
assert sum(funnel["skips_by_reason"].values()) == 1, funnel
store.close()
print("ROLLBACK_OK")
"""
    env = {**os.environ, "PYTHONPATH": str(base_dir)}
    proc = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "ROLLBACK_OK" in proc.stdout


@pytest.mark.world_start(_SUNDAY)
def test_entry_budget_ceiling_blocks_rescan_blowout_while_preserving_exit_reserve(world, monkeypatch):
    """Market-move rescans stop at the non-exit budget ceiling so the 20% exit reserve stays intact."""
    h = Harness(world, monkeypatch).install(price=100.0)
    async def scenario():
        s = await h.start()
        try:
            await monday_hold_catchup(h)
            rstore = s.research.store
            rstore.config.RESEARCH_WEEKLY_BUDGET_USD = 1.0
            rstore.config.RESEARCH_EXIT_RESERVE_PCT = 20  # 800_000 microusd entry cap, 200_000 exit reserve

            await h.advance(17, 10, 0)
            now = world.clock.now()
            used = rstore.available_budget(now)["entry_microusd"]
            top_up_spend = 799_999 - used
            assert top_up_spend > 0
            assert rstore.reserve("prior-entries", now, top_up_spend, exit_work=False) is True
            rstore.settle("prior-entries", top_up_spend)

            calls_before = len(h.calls)
            outcome = await s.research.run("market_move", work_scope="market_move:2026-11-17:1")
            assert len(h.calls) == calls_before
            assert outcome.detail["entry_funnel"]["rejected_by_reason"] != {} or outcome.detail["entry_funnel"]["screened"] >= 0

            # Non-exit reservations above 1 microusd are refused, while exit_work=True succeeds from the 200_000 reserve.
            assert rstore.reserve("blocked-rescan-entry", now, 10_000, exit_work=False) is False
            assert rstore.reserve("protected-exit-confirm", now, 150_000, exit_work=True) is True
            budget = rstore.available_budget(now)
            assert budget["entry_microusd"] == 799_999
            assert budget["billed_or_reserved_microusd"] == 949_999
        finally:
            await h.stop()

    asyncio.run(scenario())


def test_prebuy_refresh_rolling_minute_budget_caps_slow_provider_storm(world, tmp_path, monkeypatch):
    """4 stale-proof buys with 6s refreshes spend <=18s once, hold the 4th immediately, then fill next minute."""
    from stock_agent.research_gate import Eligibility
    from stock_agent.research_schemas import Direction

    world.clock.set_et(2026, 11, 30, 10, 0)
    for sym in ("AAA", "BBB", "CCC", "DDD"):
        world.price(sym, 50.0, spread=0.02)

    eng = make_engine(world, tmp_path, monkeypatch)
    fake_loop_time = [1000.0]

    class FakeLoop:
        def __init__(self, inner):
            self._inner = inner

        def time(self):
            return fake_loop_time[0]

        def __getattr__(self, name):
            return getattr(self._inner, name)

    fresh: set[str] = set()
    refresh_calls: list[str] = []
    theses: dict[str, str] = {}
    intents = []
    for sym in ("AAA", "BBB", "CCC", "DDD"):
        tid = eng.ledger.record_thesis(
            sym,
            {"conviction": 8, "draft": {"symbol": sym, "direction": "BUY"}},
            ThesisRole.ENTRY_CANDIDATE,
            source="test",
            activate=True,
        )
        theses[sym] = tid
        [iid] = eng.ledger.enqueue_intents([NewIntent(
            symbol=sym, side="buy", dedupe_key=f"market_move:2026-11-30:1:{sym}",
            reference_price=Decimal("50"), origin="market_move", thesis_ids=(tid,),
            expires_at=datetime(2026, 12, 1, 16, 0, tzinfo=ET),
            meta={"effective_conviction": 8, "raw_conviction": 8, "sector": "Technology"},
        )])
        intents.append(eng.ledger.get_intent(iid))

    class MultiGate:
        def is_entry_eligible(self, symbol, now):
            reasons = () if symbol in fresh else ("news_stale_or_unavailable",)
            return Eligibility(not reasons, reasons, theses[symbol], 8, 8, Direction.BUY, now)

    async def slow_refresh(symbol, now):
        refresh_calls.append(symbol)
        fake_loop_time[0] += 6.0  # 3 calls = 18s >= RESEARCH_REFRESH_BUDGET_S (15s)
        fresh.add(symbol)

    eng._research_gate = MultiGate()
    eng._research_refresh = slow_refresh

    async def scenario():
        real_get_loop = asyncio.get_running_loop
        loop = real_get_loop()
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop(loop))

        results = [await eng.execute_intent(it) for it in intents]
        assert [r.status for r in results] == [
            IntentStatus.FILLED,
            IntentStatus.FILLED,
            IntentStatus.FILLED,
            IntentStatus.QUEUED,
        ]
        assert results[3].code == "research_inputs_pending"
        # Crucially, DDD never even invoked slow_refresh once the 15s rolling-minute budget was exhausted.
        assert refresh_calls == ["AAA", "BBB", "CCC"]

        # A new NY session resets admissions as well as the 60s refresh window.
        fake_loop_time[0] += 24 * 3600
        world.clock.set_et(2026, 12, 1, 9, 36)
        world.price("DDD", 50.0, spread=0.02)
        ddd_retry = await eng.execute_intent(eng.ledger.get_intent(intents[3].intent_id))
        assert ddd_retry.status == IntentStatus.FILLED
        assert refresh_calls == ["AAA", "BBB", "CCC", "DDD"]

    asyncio.run(scenario())


def test_promoted_buy_superseded_by_newer_live_buy_fills_once_without_false_blocked_alert(world, tmp_path, monkeypatch):
    """Older QUEUED buy superseded by a newer live BUY fills once via the newer intent and never pages #risk."""
    from stock_agent.research_gate import Eligibility
    from stock_agent.research_schemas import Direction

    world.clock.set_et(2026, 11, 30, 9, 36)
    world.price("AAA", 50.0, spread=0.02)
    world.price("BBB", 50.0, spread=0.02)

    eng = make_engine(world, tmp_path, monkeypatch)
    blocked_calls: list[tuple[str, str, str, tuple]] = []

    class Notifier:
        def alert_blocked_buy(self, symbol, code, reason="", *, session_date=None, risk_reasons=()):
            blocked_calls.append((symbol, code, reason, tuple(risk_reasons)))

    eng._notifier = Notifier()

    # AAA: promoted prescan buy I_1, then newer live_news BUY T_2 queues I_2 before I_1 submits.
    t1 = eng.ledger.record_thesis(
        "AAA", {"conviction": 8, "draft": {"symbol": "AAA", "direction": "BUY"}},
        ThesisRole.ENTRY_CANDIDATE, source="test", activate=True,
    )
    [i1_id] = eng.ledger.enqueue_intents([NewIntent(
        symbol="AAA", side="buy", dedupe_key="handoff:prescan:2026-11-30:AAA",
        reference_price=Decimal("50"), origin="prescan", thesis_ids=(t1,),
        meta={"effective_conviction": 8, "raw_conviction": 8, "sector": "Technology", "scheduled_handoff": True},
    )])
    t2 = eng.ledger.record_thesis(
        "AAA", {"conviction": 9, "draft": {"symbol": "AAA", "direction": "BUY"}},
        ThesisRole.ENTRY_CANDIDATE, source="test", activate=True,
    )
    [i2_id] = eng.ledger.enqueue_intents([NewIntent(
        symbol="AAA", side="buy", dedupe_key="news:2026-11-30:AAA:beat",
        reference_price=Decimal("50"), origin="news", thesis_ids=(t2,),
        meta={"effective_conviction": 9, "raw_conviction": 9, "sector": "Technology"},
    )])

    # BBB: promoted prescan buy I_3, then adverse HOLD T_hold replaces thesis with no replacement buy.
    tb1 = eng.ledger.record_thesis(
        "BBB", {"conviction": 8, "draft": {"symbol": "BBB", "direction": "BUY"}},
        ThesisRole.ENTRY_CANDIDATE, source="test", activate=True,
    )
    [i3_id] = eng.ledger.enqueue_intents([NewIntent(
        symbol="BBB", side="buy", dedupe_key="handoff:prescan:2026-11-30:BBB",
        reference_price=Decimal("50"), origin="prescan", thesis_ids=(tb1,),
        meta={"effective_conviction": 8, "raw_conviction": 8, "sector": "Technology", "scheduled_handoff": True},
    )])
    tb2 = eng.ledger.record_thesis(
        "BBB", {"conviction": 4, "draft": {"symbol": "BBB", "direction": "HOLD"}},
        ThesisRole.ENTRY_CANDIDATE, source="test", activate=True,
    )

    class Gate:
        def is_entry_eligible(self, symbol, now):
            if symbol == "AAA":
                return Eligibility(True, (), t2, 9, 9, Direction.BUY, now)
            return Eligibility(False, ("non_entry_direction",), tb2, 4, 4, Direction.HOLD, now)

    eng._research_gate = Gate()

    async def scenario():
        r1 = await eng.execute_intent(eng.ledger.get_intent(i1_id))
        r2 = await eng.execute_intent(eng.ledger.get_intent(i2_id))
        r3 = await eng.execute_intent(eng.ledger.get_intent(i3_id))
        assert r1.status == IntentStatus.SKIPPED and r1.code == "research_stale"
        assert r2.status == IntentStatus.FILLED
        assert r3.status == IntentStatus.SKIPPED and r3.code == "research_stale"

    asyncio.run(scenario())

    # Exactly 1 buy order submitted for AAA (none for BBB), and #risk only notified for BBB, never AAA.
    aaa_buys = [req for req in world.orders_sent(include_failed=False)
                if req.method == "POST" and (req.json or {}).get("symbol") == "AAA"
                and (req.json or {}).get("side") == "buy"]
    assert len(aaa_buys) == 1
    assert [sym for sym, *_ in blocked_calls] == ["BBB"]
