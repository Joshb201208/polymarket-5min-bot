"""DrawdownState v3 persistence: migration, round trips, idempotent replays, fail-closed parsing."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_agent.config import Config
from stock_agent.risk_manager import DrawdownState, update_drawdown

REPO = Path(__file__).resolve().parents[1]


def cfg(**overrides):
    base = {name: getattr(Config, name) for name in (
        "DRAWDOWN_ALERT_PCT", "DRAWDOWN_THROTTLE_PCT", "DRAWDOWN_PAUSE_PCT", "DRAWDOWN_RECOVERY_SESSIONS",
        "DRAWDOWN_ROLLING_SESSIONS", "DRAWDOWN_PAUSE_MAX_SESSIONS")}
    base.update(overrides)
    return SimpleNamespace(**base)


CFG = cfg()


def sessions(start: str, n: int) -> list[str]:
    out, day = [], date.fromisoformat(start)
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def persisted(state: DrawdownState, config=CFG) -> DrawdownState:
    """Exactly what the ledger does: json.dumps(default=str) then json.loads."""
    return DrawdownState.from_record(json.loads(json.dumps(state.to_record(), default=str)), config=config)


def run(closes, *, reload_each=False, config=CFG, flows=None, start="2026-01-05"):
    flows = flows or {}
    state, prev, out = DrawdownState(), None, []
    for day, equity in zip(sessions(start, len(closes)), closes):
        state = update_drawdown(state, day, D(str(equity)), previous_session=prev,
                                net_external_flow=D(str(flows.get(day, 0))), config=config)
        if reload_each:
            state = persisted(state, config)
        prev = day
        out.append(state)
    return out


# ---------------------------------------------------------------- the real production path

def test_empty_record_first_close_and_second_close_are_v3():
    empty = DrawdownState.from_record(None, config=CFG)
    assert empty == DrawdownState()
    first = update_drawdown(empty, "2026-11-24", D("122966.00"), previous_session="2026-11-23", config=CFG)
    record = first.to_record()
    assert record["version"] == 3
    assert record["rolling_closes"] == [["2026-11-24", "122966.00"]]
    assert record["rolling_drawdown"] == "0" and record["drawdown"] == "0"
    assert record["pause_start_session"] is None and record["pause_sessions_governed"] == 0
    assert record["last_equity"] == "122966.00" and record["last_session"] == "2026-11-24"
    assert persisted(first) == first
    second = update_drawdown(persisted(first), "2026-11-25", D("120000"), previous_session="2026-11-24", config=CFG)
    rec2 = second.to_record()
    assert [label for label, _ in rec2["rolling_closes"]] == ["2026-11-24", "2026-11-25"]
    assert persisted(second) == second
    # Empty state round-trips as an explicit empty v3 record.
    assert DrawdownState().to_record()["version"] == 3
    assert persisted(DrawdownState()) == DrawdownState()


def test_round_trip_through_real_ledger_store(tmp_path):
    from tests.ledger_helpers import make_ledger
    from tests.fakes.world import FakeWorld
    from stock_agent.risk_manager import DRAWDOWN_STATE_KEY

    ledger = make_ledger(tmp_path / "ledger", FakeWorld())
    state = run([100, 79, 79])[-1]
    ledger.risk_state_set(DRAWDOWN_STATE_KEY, state.to_record())
    assert DrawdownState.from_record(ledger.risk_state_get(DRAWDOWN_STATE_KEY), config=CFG) == state


# ---------------------------------------------------------------- migration from v1/v2

def legacy(version=2, **kw):
    record = {"version": version, "high_water": "100000", "drawdown": "0.1", "paused": False,
              "recovery_closes": 0, "last_session": "2026-11-20"}
    if version == 2:
        record["last_equity"] = "90500"
    record.update(kw)
    return record


def test_v1_migration_seeds_synthetic_peak_and_keeps_decisions():
    state = DrawdownState.from_record(legacy(1), config=CFG)
    assert state.rolling_closes == ((None, D("100000")), ("2026-11-20", D("90000.0")))
    assert state.decision_drawdown == state.drawdown == D("0.1")
    assert state.rolling_high_water == D("100000")
    assert state.last_equity is None and not state.paused and state.pause_start_session is None
    assert state.should_alert(CFG) and not state.is_throttled(CFG)
    # v1 used raw equity as the index: the next close chains from 90,000.
    nxt = update_drawdown(state, "2026-11-23", D("81000"), previous_session="2026-11-20", config=CFG)
    assert nxt.rolling_drawdown == D("0.19") and not nxt.paused
    assert persisted(nxt) == nxt and persisted(state) == state


def test_v2_unpaused_migration_chains_from_last_raw_equity():
    state = DrawdownState.from_record(legacy(2), config=CFG)
    assert state.last_equity == D("90500") and state.rolling_drawdown == D("0.1")
    nxt = update_drawdown(state, "2026-11-23", D("90500") * D("0.9"), previous_session="2026-11-20", config=CFG)
    assert nxt.rolling_drawdown == D("0.19")
    assert nxt.rolling_closes[0] == (None, D("100000"))


def test_v2_paused_migration_starts_a_full_capped_episode():
    state = DrawdownState.from_record(legacy(2, drawdown="0.25", paused=True, recovery_closes=2), config=CFG)
    assert state.paused and state.pause_start_session == "2026-11-20" and state.pause_sessions_governed == 0
    assert state.recovery_closes == 2  # the shipped early-release streak is preserved
    assert state.pause_sessions_remaining(CFG) == CFG.DRAWDOWN_PAUSE_MAX_SESSIONS
    assert persisted(state) == state
    # Stays under water: no instant release, no trap: clears at the 21st governed close.
    days = sessions("2026-11-23", 21)
    prev = "2026-11-20"
    for i, day in enumerate(days, 1):
        state = persisted(update_drawdown(state, day, D("90500") * D("0.75") / D("0.9"),
                                          previous_session=prev, config=CFG))
        prev = day
        assert state.paused == (i < 21), i
    assert state.pause_sessions_governed == 21 and state.rolling_drawdown > D("0.2")


def test_legacy_record_with_no_close_migrates_to_empty_state():
    record = {"version": 1, "high_water": "0", "drawdown": "0", "paused": False,
              "recovery_closes": 0, "last_session": None}
    assert DrawdownState.from_record(record, config=CFG) == DrawdownState()


@pytest.mark.parametrize("bad", [
    legacy(2, drawdown="0.2"),                    # unpaused at the old latch is still corrupt in v1/v2
    legacy(2, recovery_closes=1),
    legacy(2, last_equity="-1"),
    legacy(1, last_session="2026-13-01"),
    legacy(2, paused="yes"),
])
def test_legacy_validation_is_unchanged(bad):
    with pytest.raises(ValueError):
        DrawdownState.from_record(bad, config=CFG)


def test_directly_constructed_legacy_state_serializes_as_its_migration():
    built = DrawdownState(D("100000"), D("0.1"), True, 2, "2026-11-23", D("90000"))
    assert persisted(built) == DrawdownState.from_record(legacy(2, paused=True, recovery_closes=2,
                                                                last_session="2026-11-23",
                                                                last_equity="90000"), config=CFG)


# ---------------------------------------------------------------- restart / replay

TRAP = [100, 79] + [79] * 40
EARLY_THEN_DROP = [100, 79, 90, 90, 90, 90, 90, 78, 78, 78] + [78] * 20 + [60] * 5
RECOVER = [100, 85, 79, 80, 82, 81, 79.5, 84] + [100 + i for i in range(30)]


@pytest.mark.parametrize("closes", [TRAP, EARLY_THEN_DROP, RECOVER], ids=["trap", "early_then_drop", "recover"])
def test_restart_every_close_matches_uninterrupted_run(closes):
    assert run(closes, reload_each=True) == run(closes)


def test_trap_releases_at_21st_governed_close_and_round_trips():
    states = run(TRAP, reload_each=True)
    latch = states[1]
    assert latch.paused and latch.pause_sessions_governed == 0 and latch.pause_start_session == sessions("2026-01-05", 2)[1]
    assert all(s.paused for s in states[1:22]) and not states[22].paused
    expired = states[-1]
    assert expired.rolling_drawdown > D("0.2") and not expired.paused and expired.is_throttled(CFG)
    assert expired.pause_sessions_governed == CFG.DRAWDOWN_PAUSE_MAX_SESSIONS
    assert persisted(expired) == expired


def test_mid_cooldown_unpaused_above_pause_line_loads():
    states = run(EARLY_THEN_DROP, reload_each=True)
    mid = [s for s in states if not s.paused and s.decision_drawdown > D("0.2")
           and s.pause_sessions_governed < CFG.DRAWDOWN_PAUSE_MAX_SESSIONS]
    assert mid, "scenario must reach early release followed by a re-drop inside the cooldown"
    for state in mid:
        assert persisted(state) == state


def test_replayed_session_is_identity_before_and_after_reload():
    states = run(TRAP[:10], reload_each=True)
    state, day = states[-1], states[-1].last_session
    assert update_drawdown(state, day, D(1), config=CFG) == state
    assert update_drawdown(persisted(state), day, D(1), config=CFG) == state
    older = states[-3].last_session
    assert update_drawdown(persisted(state), older, D(1), config=CFG) == state


def test_anchor_may_precede_short_rolling_window():
    short = cfg(DRAWDOWN_ROLLING_SESSIONS=5)
    states = run(TRAP, reload_each=True, config=short)
    final = states[-1]
    assert final.pause_start_session < final.rolling_closes[0][0]
    assert persisted(final, short) == final
    # A window longer than the configured length is not silently truncated.
    with pytest.raises(ValueError):
        DrawdownState.from_record(run(TRAP)[-1].to_record(), config=short)


def test_window_slides_out_the_synthetic_peak():
    state = DrawdownState.from_record(legacy(2, drawdown="0", last_equity="100000"), config=CFG)
    prev = "2026-11-20"
    for day in sessions("2026-11-23", CFG.DRAWDOWN_ROLLING_SESSIONS):
        state = persisted(update_drawdown(state, day, D(100000), previous_session=prev, config=CFG))
        prev = day
    assert len(state.rolling_closes) == CFG.DRAWDOWN_ROLLING_SESSIONS
    assert all(label is not None for label, _ in state.rolling_closes)


# ---------------------------------------------------------------- flows

def test_deposit_and_withdrawal_during_episode_survive_restarts():
    closes = [100, 79, 79, 129, 129, 99, 99] + [99] * 20
    days = sessions("2026-01-05", len(closes))
    flows = {days[3]: 50, days[5]: -30}
    a, b = run(closes, flows=flows), run(closes, flows=flows, reload_each=True)
    assert a == b
    assert all(abs(s.rolling_drawdown - D("0.21")) < D("1e-20") for s in b[1:])
    assert b[1].paused and b[21].paused and not b[22].paused


# ---------------------------------------------------------------- fail closed

def good():
    return run(EARLY_THEN_DROP[:4])[-1].to_record()


def mutate(**kw):
    record = good()
    record.update(kw)
    return record


def drop(key):
    record = good()
    record.pop(key)
    return record


@pytest.mark.parametrize("record", [
    "not a mapping",
    mutate(version=4), mutate(version="3"), mutate(version=True),
    drop("rolling_closes"), mutate(extra=1),
    mutate(paused="false"), mutate(pause_sessions_governed=-1), mutate(pause_sessions_governed=True),
    mutate(recovery_closes=5),
    mutate(rolling_closes=[]), mutate(rolling_closes="[]"),
    mutate(rolling_closes=[["2026-01-07", "100"], ["2026-01-06", "79"]]),
    mutate(rolling_closes=[["2026-01-05", "100"], [None, "79"]]),
    mutate(rolling_closes=[["2026-01-05", "100"], ["2026-01-06", "-79"]]),
    mutate(rolling_drawdown="0.05"), mutate(rolling_drawdown=None), mutate(rolling_drawdown="NaN"),
    mutate(drawdown="0.5"), mutate(high_water="10"),
    mutate(last_session="2026-01-09"), mutate(last_session="08/01/2026"),
    mutate(pause_start_session=None),
    mutate(pause_start_session="2026-02-01"),
    mutate(pause_start_session="2026-01-08"),          # governed must be 0 on the latch close itself
    mutate(pause_sessions_governed=9),                # more governed closes than closes since the anchor
    mutate(last_equity="0"),
    {**DrawdownState().to_record(), "paused": True},
    {**DrawdownState().to_record(), "rolling_closes": [["2026-01-05", "100"]]},
], ids=lambda r: str(r)[:60])
def test_corrupt_v3_records_fail_closed(record):
    with pytest.raises(ValueError):
        DrawdownState.from_record(record, config=CFG)


def test_unanchored_close_above_pause_line_is_corrupt():
    record = run([100, 85])[-1].to_record()
    record["rolling_closes"][-1][1] = "79"
    record["rolling_drawdown"], record["drawdown"] = "0.21", "0.21"
    with pytest.raises(ValueError, match="anchor"):
        DrawdownState.from_record(record, config=CFG)


def test_long_decimal_chain_never_trips_tolerance():
    closes = [D(100) * (D(1) + D((i * 7919) % 41 - 20) / D(997)) for i in range(400)]
    flows = {day: (D(i % 5) - 2) * D("0.37") for i, day in enumerate(sessions("2026-01-05", 400)) if i % 9 == 4}
    assert run(closes, reload_each=True, flows=flows) == run(closes, flows=flows)


# ---------------------------------------------------------------- rollback to 4fed519

def _old_risk_manager():
    source = subprocess.run(["git", "show", "4fed519:stock_agent/risk_manager.py"], cwd=REPO,
                            capture_output=True, text=True)
    if source.returncode:
        pytest.skip("4fed519 not available in this checkout")
    spec = importlib.util.spec_from_loader("risk_manager_4fed519", loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    exec(compile(source.stdout, "risk_manager_4fed519.py", "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("closes", [TRAP[:5], TRAP, EARLY_THEN_DROP, RECOVER, [100, 95]],
                         ids=["paused", "expired", "cooldown", "recovered", "plain"])
def test_old_code_rejects_v3_and_accepts_the_conservative_projection(closes):
    old = _old_risk_manager()
    state = run(closes)[-1]
    with pytest.raises(ValueError):
        old.DrawdownState.from_record(state.to_record(), config=Config)
    projected = old.DrawdownState.from_record(state.to_legacy_record(CFG), config=Config)
    assert projected.high_water == state.high_water and projected.drawdown == state.drawdown
    assert projected.last_session == state.last_session and projected.last_equity == state.last_equity
    assert projected.paused == (state.paused or state.drawdown >= D(str(CFG.DRAWDOWN_PAUSE_PCT)))
    assert projected.recovery_closes == 0
    # Never looser than v3 on the rollback day.
    assert projected.paused >= state.paused
    assert projected.is_throttled(Config) >= state.is_throttled(CFG)
    assert projected.should_alert(Config) >= state.should_alert(CFG)


def test_projection_of_empty_state_is_the_old_empty_record():
    old = _old_risk_manager()
    assert old.DrawdownState.from_record(DrawdownState().to_legacy_record(CFG), config=Config) == old.DrawdownState()
