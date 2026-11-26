"""Release 14: unknown open interest fails closed for openings only, and every
options.scan_skip journal event carries a funnel stage code."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from stock_agent.broker import BrokerError
from stock_agent.options_manager import SCAN_STAGES, _gate_stage, _owner_stage
from stock_agent.options_policy import (
    ContractQuote,
    Direction,
    Proposal,
    Rejection,
    build_proposal,
    occ_symbol,
    quote_problem,
)
from stock_agent.options_settings import OptionsMode, OptionsSettings
from stock_agent.research_schemas import Direction as RDir
from tests.options_support import Gate, StubGate, StubRisk, make_manager, seed_chain

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 23)
EXP = date(2026, 12, 24)
S = OptionsSettings()


def td(a, b):
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def chain(oi, spot=100.0, strikes=(90, 95, 100, 105, 110, 115)):
    out = []
    for k in strikes:
        mid = max(spot - k, 0) + 3.0 * max(0.2, 1 - abs(spot - k) / 25)
        out.append(ContractQuote(occ_symbol("AAA", EXP, "call", k), "AAA", EXP, "call", k,
                                 round(mid - 0.05, 2), round(mid + 0.05, 2), NOW, oi))
    return out


def propose(oi):
    return build_proposal(underlying="AAA", direction=Direction.BULLISH, conviction=8, spot=100.0,
                          chain=chain(oi), today=TODAY, now=NOW, s=S, trading_days=td)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ open interest
def test_unknown_open_interest_blocks_an_open():
    r = propose(None)
    assert isinstance(r, Rejection) and "open interest unknown" in r.reason
    # both shapes refuse, so the reason names each one
    assert "spread:" in r.reason and "single:" in r.reason


@pytest.mark.parametrize("oi,ok", [(99, False), (100, True), (1000, True)])
def test_open_interest_floor(oi, ok):
    assert S.min_open_interest == 100
    r = propose(oi)
    assert isinstance(r, Proposal) is ok, r
    if not ok:
        assert f"open interest {oi}" in r.reason


def test_quote_problem_tolerates_unknown_oi_outside_openings():
    q = chain(None)[2]
    assert quote_problem(q, NOW, S) is None  # close / exit / mark callers: default unchanged
    assert quote_problem(q, NOW, S, need_bid=False) is None
    assert quote_problem(q, NOW, S, need_oi=True) == "open interest unknown"


def _null_oi(m):
    """The contracts listing returns JSON null open_interest (e.g. a newly listed expiry)."""
    orig = m.broker.get_option_contracts

    async def contracts(*a, **k):
        out = await orig(*a, **k)
        for c in out:
            c["open_interest"] = None
        return out
    m.broker.get_option_contracts = contracts


def test_manager_open_refused_with_visible_reason_when_listing_has_no_oi(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", RDir.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate)
    _null_oi(m)
    out = run(m.scan())
    assert out[0].opened is None and "open interest unknown" in out[0].reason
    assert out[0].stage == "structure"
    skip = [p for k, p in m.journal.events if k == "options.scan_skip"]
    assert skip and "open interest unknown" in skip[0]["reason"] and skip[0]["stage"] == "structure"
    assert not [r for r in world.orders_sent() if r.json and r.json.get("order_class") == "mleg"]


def test_unknown_oi_does_not_affect_monitor_or_close(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", RDir.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate)
    st = m.get(run(m.scan())[0].opened)
    # from here on the listing carries no open interest; exits and closes never need it
    _null_oi(m)
    world.clock.advance(minutes=30)
    world.price("AAA", 104, spread=0.02)
    c100, c105 = (occ_symbol("AAA", EXP, "call", k) for k in (100, 105))
    world.alpaca.set_option_quote(c105, 1.95, 2.05)
    v = 2.0 + st.entry_debit + 0.6 * (5 - st.entry_debit)
    world.alpaca.set_option_quote(c100, round(v - 0.05, 2), round(v + 0.05, 2))
    res = run(m.monitor())
    assert res.exits and res.exits[0][1] == "profit_target", res
    assert st.st.value == "closed"


# ------------------------------------------------------------ stage codes
def test_stage_helpers():
    assert _gate_stage(("news_stale_or_unavailable",)) == "news"
    assert _gate_stage(("news_stale_or_unavailable", "missing_thesis")) == "news"
    assert _gate_stage(("quote_stale_or_unavailable",)) == "underlying_quote"
    assert _gate_stage(("missing_thesis",)) == "research"
    assert _gate_stage("research gate unavailable: boom") == "research"
    assert _owner_stage("research gate: news_stale_or_unavailable") == "news"
    assert _owner_stage("owner directive: no proof from the research gate") == "owner"
    assert SCAN_STAGES[0] == "gate" and SCAN_STAGES[-1] == "broker" and len(set(SCAN_STAGES)) == 14


def _setup(world, tmp_path, kw):
    world.clock.set_et(2026, 11, 23, *kw.pop("at", (10, 0)))
    if kw.pop("chain", True):
        seed_chain(world, oi=kw.pop("oi", 1000))
    else:
        world.price("AAA", 100.0, spread=0.02)
    gate = StubGate()
    gate.set("AAA", kw.pop("direction", RDir.BUY), kw.pop("conviction", 8), ok=kw.pop("ok", True),
             reasons=kw.pop("reasons", ()))
    level = kw.pop("level", None)
    if level is not None:
        world.alpaca.options_level = level
    return make_manager(world, tmp_path, gate=gate, **kw)


def _broker_down(m):
    async def boom(*a, **k):
        raise BrokerError("server", "fake outage")
    m.broker.get_account = boom


def _order_rejected(m):
    async def no(body):
        raise BrokerError("rejected", "fake refusal", status=422)
    m.broker.submit_raw_order = no


def _no_spot(m):
    async def none(sym, now):
        return None, "quote unavailable"
    m._spot_now = none


CASES = [
    ("underlying_quote", {}, _no_spot, "AAA"),
    ("news", dict(ok=False, reasons=("news_stale_or_unavailable",)), None, "AAA"),
    ("research", dict(ok=False, reasons=("missing_thesis",)), None, "AAA"),
    ("research", dict(direction=RDir.HOLD), None, "AAA"),
    ("research", dict(conviction=5), None, "AAA"),
    ("permission", dict(level=1), None, "AAA"),
    ("chain", dict(chain=False), None, "AAA"),
    ("structure", dict(oi=10), None, "AAA"),
    ("sizing", dict(max_loss_per_structure_pct=0.00001), None, "AAA"),
    ("risk", dict(risk=StubRisk(ok=False, reasons=("fake cap",))), None, "AAA"),
    ("order", {}, _order_rejected, "AAA"),
    ("broker", {}, _broker_down, "AAA"),
]


@pytest.mark.parametrize("stage,kw,patch,sym", CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_each_skip_path_carries_its_stage(world, tmp_path, stage, kw, patch, sym):
    m = _setup(world, tmp_path, dict(kw))
    if patch:
        patch(m)
    out = run(m.scan())
    assert out and out[0].opened is None and out[0].stage == stage, out
    skips = [p for k, p in m.journal.events if k == "options.scan_skip"]
    assert skips, m.journal.kinds()
    for p in skips:
        assert set(p) >= {"symbol", "reason", "stage"} and p["stage"] in SCAN_STAGES
    assert skips[0]["stage"] == stage and skips[0]["symbol"] == sym and skips[0]["reason"] == out[0].reason


@pytest.mark.parametrize("kw", [dict(mode=OptionsMode.CLOSE_ONLY), dict(at=(9, 0))])
def test_global_blockers_carry_gate_stage(world, tmp_path, kw):
    m = _setup(world, tmp_path, dict(kw))
    out = run(m.scan())
    assert out and all(o.stage == "gate" and o.opened is None for o in out), out
    skips = [p for k, p in m.journal.events if k == "options.scan_skip"]
    assert [(p["symbol"], p["stage"]) for p in skips] == [("*", "gate")] and skips[0]["reason"] == out[0].reason


def test_capacity_stage_on_a_second_scan(world, tmp_path):
    m = _setup(world, tmp_path, {})
    assert run(m.scan())[0].opened
    again = run(m.scan())
    assert again[0].opened is None and again[0].stage == "capacity"
    assert [p["stage"] for k, p in m.journal.events if k == "options.scan_skip"] == ["capacity"]


def test_opened_outcome_has_no_skip_event(world, tmp_path):
    m = _setup(world, tmp_path, {})
    out = run(m.scan())
    assert out[0].opened and out[0].stage == ""
    assert "options.scan_skip" not in m.journal.kinds() and "options.opened" in m.journal.kinds()


# ------------------------------------------------------------ expiry preference (A, B)
from stock_agent.options_policy import candidate_expiries, is_monthly_expiry  # noqa: E402

DEC18, DEC24, DEC31, JAN08, JAN15 = (date(2026, 12, 18), date(2026, 12, 24), date(2026, 12, 31),
                                     date(2027, 1, 8), date(2027, 1, 15))


def _wd(a, b):
    return td(a, b)


def test_monthly_detection_including_holiday_thursday():
    assert is_monthly_expiry(DEC18) and is_monthly_expiry(JAN15)
    assert not is_monthly_expiry(DEC24) and not is_monthly_expiry(DEC31)
    # 19 Jun 2026 (third Friday) is Juneteenth: the Thursday is the monthly when Friday isn't listed
    assert is_monthly_expiry(date(2026, 6, 18), [date(2026, 6, 18)])
    assert not is_monthly_expiry(date(2026, 6, 18), [date(2026, 6, 18), date(2026, 6, 19)])


def test_candidate_order_monthlies_first_then_nearest_target():
    assert S.max_dte == 60 and S.min_dte == 21 and S.target_dte == 35
    got = candidate_expiries([DEC24, DEC31, DEC18, JAN08, JAN15], TODAY, S, _wd, limit=5)
    # monthlies by |dte-35|: 18 Dec (25 -> 10), 15 Jan (53 -> 18); then weeklies: 31 Dec (3), 24 Dec (4), 8 Jan (11)
    assert got == [DEC18, JAN15, DEC31, DEC24, JAN08]
    assert candidate_expiries([DEC24, DEC31, DEC18, JAN08, JAN15], TODAY, S, _wd) == [DEC18, JAN15, DEC31]
    # the window still binds: 61+ days or under 21 days never appear
    assert candidate_expiries([date(2026, 12, 11), date(2027, 1, 29)], TODAY, S, _wd) == []


def _opt_snapshot_calls(m):
    calls = []
    orig = m.broker.get_option_snapshots

    async def wrapped(symbols, *a, **k):
        calls.append(list(symbols))
        return await orig(symbols, *a, **k)
    m.broker.get_option_snapshots = wrapped
    return calls


def _mgr(world, tmp_path, chains, **kw):
    world.clock.set_et(2026, 11, 23, 10, 0)
    for exp, oi in chains:
        seed_chain(world, exp=exp, oi=oi)
    gate = StubGate()
    gate.set("AAA", RDir.BUY, 8)
    return make_manager(world, tmp_path, gate=gate, **kw)


def test_monthly_preferred_over_nearer_weekly(world, tmp_path):
    m = _mgr(world, tmp_path, [(DEC31, 1000), (DEC18, 1000)])  # 31 Dec is nearest 35 DTE
    calls = _opt_snapshot_calls(m)
    out = run(m.scan())
    st = m.get(out[0].opened)
    assert st and str(st.expiry) == DEC18.isoformat(), out
    assert not any(DEC31.strftime("%y%m%d") in sym for c in calls for sym in c)  # never priced


def test_fallback_to_next_expiry_when_first_fails_open_interest(world, tmp_path):
    m = _mgr(world, tmp_path, [(DEC18, 10), (DEC31, 1000)])
    out = run(m.scan())
    st = m.get(out[0].opened)
    assert st and str(st.expiry) == DEC31.isoformat(), out


def test_all_fail_reason_lists_attempts_and_caps_at_three(world, tmp_path):
    m = _mgr(world, tmp_path, [(DEC18, 10), (JAN15, ""), (DEC31, 10), (DEC24, 10)])
    calls = _opt_snapshot_calls(m)
    out = run(m.scan())
    o = out[0]
    assert o.opened is None and o.stage == "structure"
    assert o.reason.startswith("2026-12-18: ") and "; 2027-01-15: " in o.reason and "; 2026-12-31: " in o.reason
    assert "open interest 10" in o.reason and "open interest unknown" in o.reason
    assert "2026-12-24" not in o.reason  # 4th candidate never tried
    assert len(calls) == 3
    assert not any(DEC24.strftime("%y%m%d") in sym for c in calls for sym in c)


def test_first_success_adds_no_snapshot_calls(world, tmp_path):
    m = _mgr(world, tmp_path, [(DEC18, 1000), (JAN15, 1000), (DEC31, 1000)])
    calls = _opt_snapshot_calls(m)
    assert run(m.scan())[0].opened
    priced = {sym[3:9] for c in calls for sym in c}  # yymmdd of every contract priced
    assert priced == {DEC18.strftime("%y%m%d")}, priced


# ------------------------------------------------------------ gate events on state change (C)
def test_gate_event_only_on_change(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 9, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", RDir.BUY, 8)
    m = make_manager(world, tmp_path, gate=gate)

    def gate_events():
        return [p for k, p in m.journal.events if k == "options.scan_skip" and p["symbol"] == "*"]
    run(m.scan())
    world.clock.set_et(2026, 11, 23, 9, 10)
    run(m.scan())
    assert len(gate_events()) == 1 and gate_events()[0]["stage"] == "gate"
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)  # fresh quotes
    assert run(m.scan())[0].opened  # past the gate: the state resets
    world.clock.set_et(2026, 11, 23, 17, 0)
    run(m.scan())
    run(m.scan())
    ev = gate_events()
    assert len(ev) == 2 and all(p["reason"].startswith("market gate") for p in ev)


# ------------------------------------------------------------ risk review: real clock, pre-send, budget
from stock_agent.options_manager import OptionCandidate  # noqa: E402


def _mleg(world):
    return [r for r in world.orders_sent() if r.json and r.json.get("order_class") == "mleg"]


def test_later_expiry_judged_against_real_clock(world, tmp_path):
    """First monthly rejected; 121 s pass while pricing; the next expiry's quotes (stamped at
    scan start) are stale against the real clock and must not be traded."""
    m = _mgr(world, tmp_path, [(DEC18, 10), (DEC31, 1000)])
    orig = m.broker.get_option_snapshots

    async def slow(symbols, *a, **k):
        out = await orig(symbols, *a, **k)
        world.clock.advance(seconds=121)
        return out
    m.broker.get_option_snapshots = slow
    out = run(m.scan(candidates=[OptionCandidate("AAA", source="news", story_id="st-x")]))
    assert out[0].opened is None and out[0].stage == "structure", out
    assert "2026-12-31: " in out[0].reason and "old" in out[0].reason
    assert _mleg(world) == []
    # an expiry refused only for quote age may pass on fresh quotes: not a final answer
    assert not out[0].final and not m.is_story_handled("st-x")


def test_legs_rechecked_before_claim_and_send(world, tmp_path):
    """Quotes age past the limit during the risk check: nothing is reserved or sent, the skip
    is non-final (stage pricing) and the news story stays open."""
    risk = StubRisk()
    orig = risk.evaluate_trade

    def slow_risk(*a, **k):
        world.clock.advance(seconds=121)
        return orig(*a, **k)
    risk.evaluate_trade = slow_risk
    m = _mgr(world, tmp_path, [(DEC18, 1000)], risk=risk)
    out = run(m.scan(candidates=[OptionCandidate("AAA", source="news", story_id="st-1")]))
    o = out[0]
    assert o.opened is None and o.stage == "pricing" and not o.final, o
    assert o.reason.startswith("leg ") and "old" in o.reason
    assert _mleg(world) == [] and m.structures == []
    assert not o.story_handled and not m.is_story_handled("st-1")
    assert m._news_opens_today(TODAY) == 0


def _no_fill_submit(m, world, c100, sent):
    orig_submit = m.broker.submit_raw_order

    async def submit(body):
        q = world.alpaca.quotes[c100]
        world.alpaca.set_option_quote(c100, round(q.bid + 1.0, 2), round(q.ask + 1.0, 2))  # no fill
        sent["n"] += 1
        return await orig_submit(body)
    m.broker.submit_raw_order = submit


def test_stale_later_rung_after_a_zero_fill_is_final_not_filled(world, tmp_path):
    """Rung 1 was sent and cancelled with no fill; the re-read legs for rung 2 are old, so
    rung 2 is not sent. Like master's unfilled ladder: final, stage order, story handled."""
    m = _mgr(world, tmp_path, [(DEC18, 1000)])
    c100 = occ_symbol("AAA", DEC18, "call", 100)
    sent = {"n": 0}
    _no_fill_submit(m, world, c100, sent)
    orig_snap = m.broker.get_option_snapshots

    async def snaps(symbols, *a, **k):
        out = await orig_snap(symbols, *a, **k)
        if sent["n"]:  # after the first rung the feed serves the quote from 200 s ago
            old = (world.clock.now() - timedelta(seconds=200)).isoformat().replace("+00:00", "Z")
            for v in out.values():
                if isinstance(v, dict) and v.get("latestQuote"):
                    v["latestQuote"]["t"] = old
        return out
    m.broker.get_option_snapshots = snaps
    out = run(m.scan(candidates=[OptionCandidate("AAA", source="news", story_id="st-r")]))
    o = out[0]
    assert o.opened is None and o.final and o.stage == "order", o
    assert o.reason.startswith("not filled; later rung stopped: leg ") and "old" in o.reason
    assert len(_mleg(world)) == 1 and m.is_story_handled("st-r")
    assert m.structures[0].st.value == "void" and m.structures[0].zero_fill_confirmed


def test_gate_await_after_requote_cannot_send_stale_legs(world, tmp_path):
    """The rung's market gate is awaited after the re-quote; if 121 s pass inside it, the leg
    check that follows (the last step before the send) refuses."""
    m = _mgr(world, tmp_path, [(DEC18, 1000)])
    c100 = occ_symbol("AAA", DEC18, "call", 100)
    sent = {"n": 0}
    _no_fill_submit(m, world, c100, sent)
    orig_gate = m._gate

    async def slow_gate(purpose):
        r = await orig_gate(purpose)
        if sent["n"]:
            world.clock.advance(seconds=121)
        return r
    m._gate = slow_gate
    out = run(m.scan())
    assert out[0].opened is None and out[0].stage == "order" and out[0].final, out
    assert "later rung stopped" in out[0].reason and "old" in out[0].reason
    assert len(_mleg(world)) == 1


def test_first_rung_stop_is_not_final_and_leaves_story_open(world, tmp_path):
    """The leg check at rung 0 (after the write-ahead) refuses: nothing was sent, so the skip
    is non-final, stage pricing, and the voided reservation doesn't mark the story handled."""
    m = _mgr(world, tmp_path, [(DEC18, 1000)])
    orig = m._legs_problem
    calls = {"n": 0}

    def flaky(leg_quotes):
        calls["n"] += 1
        return "leg X quote is 130s old" if calls["n"] == 2 else orig(leg_quotes)  # 1 = pre-claim
    m._legs_problem = flaky
    out = run(m.scan(candidates=[OptionCandidate("AAA", source="news", story_id="st-0")]))
    o = out[0]
    assert o.opened is None and not o.final and o.stage == "pricing" and o.reason == "leg X quote is 130s old", o
    assert _mleg(world) == []
    st = m.structures[0]
    assert st.st.value == "void" and st.zero_fill_confirmed and st.open_never_sent and not st.open_order_id
    assert not o.story_handled and not m.is_story_handled("st-0") and m._news_opens_today(TODAY) == 0
    # a later scan may answer the same story
    world.clock.advance(seconds=60)
    seed_chain(world, exp=DEC18)
    again = run(m.scan(candidates=[OptionCandidate("AAA", source="news", story_id="st-0")]))
    assert again[0].opened, again


def test_legs_problem_uses_the_clock_now(world, tmp_path):
    m = _mgr(world, tmp_path, [(DEC18, 1000)])
    q = ContractQuote(occ_symbol("AAA", DEC18, "call", 100), "AAA", DEC18, "call", 100, 2.9, 3.0,
                      world.clock.now(), 500)
    assert m._legs_problem({q.symbol: q}) is None
    world.clock.advance(seconds=121)
    assert "old" in m._legs_problem({q.symbol: q})
    assert m._legs_problem({q.symbol: None}) == f"leg {q.symbol} has no quote"


def test_expiry_budget_is_per_symbol_per_scan(world, tmp_path):
    m = _mgr(world, tmp_path, [(DEC18, 10), (JAN15, 10), (DEC31, 10), (DEC24, 10)])
    calls = _opt_snapshot_calls(m)
    cands = [OptionCandidate("AAA", source="news", story_id="st-a"),
             OptionCandidate("AAA", source="news", story_id="st-b")]
    out = run(m.scan(candidates=cands))
    assert len(calls) == 3
    assert out[0].stage == "structure" and out[0].reason.count("; 20") == 2
    assert out[1].reason == "expiry pricing budget used this scan"
    assert out[1].stage == "structure" and not out[1].final and not m.is_story_handled("st-b")
    # a new scan has a fresh budget
    run(m.scan(candidates=[OptionCandidate("AAA", source="news", story_id="st-c")]))
    assert len(calls) == 6


def test_gate_dedup_uses_a_stable_id(world, tmp_path):
    m = _mgr(world, tmp_path, [(DEC18, 1000)])
    n = {"i": 0}

    async def failing(now):
        n["i"] += 1
        raise BrokerError("server", f"upstream 503 request id {n['i']:04d}-{n['i'] * 7919:x}")
    m._reconcile_locked = failing
    o1, o2 = run(m.scan()), run(m.scan())
    assert o1[0].reason != o2[0].reason
    ev = [p for k, p in m.journal.events if k == "options.scan_skip"]
    assert len(ev) == 1 and ev[0]["symbol"] == "*" and ev[0]["reason"] == o1[0].reason


# --- R14 rung order (lead ruling 26 Nov): owner await, then market gate LAST, then sync checks ---

from stock_agent.options_book import StructState  # noqa: E402
from tests.test_options_owner_backed import ADDED, FlipGate, _owner_candidate, _strict  # noqa: E402
from tests.test_options_owner_backed import _setup as _owner_setup  # noqa: E402


def _owner_on_half_day(world, tmp_path, gate, refresh):
    m, _, _ = _owner_setup(world, tmp_path, gate=gate)
    world.clock.set_et(2026, 11, 27, 12, 49, 30)  # half day: closes 13:00, entry buffer from 12:50
    seed_chain(world)  # quotes stamped now
    m._research_refresh = refresh
    m.settings = m.settings.__class__(**{**m.settings.__dict__, "fill_wait_seconds": 0})
    return m


def test_owner_refresh_past_the_half_day_closing_buffer_sends_nothing(world, tmp_path):
    gate = _strict()
    box = {}

    def refresh(sym, now):  # the slow owner refresh before rung 0 lands at 12:50:07 ET
        if any(x.source == "owner" for x in box["m"].structures):
            world.clock.set_et(2026, 11, 27, 12, 50, 7)

    m = box["m"] = _owner_on_half_day(world, tmp_path, gate, refresh)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not world.orders_sent(), out
    o = out[0]
    assert not o.opened and "closing buffer" in o.reason and o.final is False and o.stage == "gate", o
    assert not o.story_handled
    st = next(x for x in m.structures if x.source == "owner")
    assert st.st is StructState.VOID and st.zero_fill_confirmed and st.open_never_sent
    assert st.story_id is None and gate.ledger.claims == {}  # owner shot freed, story left open


def test_owner_refresh_past_the_buffer_before_a_later_rung_sends_no_more(world, tmp_path, monkeypatch):
    gate = _strict()
    box = {"n": 0}

    def refresh(sym, now):
        if any(x.order_class == "mleg" for x in world.alpaca.orders.values()) and not box["n"]:
            box["n"] = 1  # rung 0 was sent: the refresh before rung 1 lands at 12:50:07 ET
            world.clock.set_et(2026, 11, 27, 12, 50, 7)

    m = box["m"] = _owner_on_half_day(world, tmp_path, gate, refresh)
    monkeypatch.setattr(world.alpaca, "_try_fill_mleg", lambda o, at: None)
    out = run(m.scan(candidates=[_owner_candidate()]))
    rungs = {x.client_order_id for x in world.alpaca.orders.values()
             if x.order_class == "mleg" and (x.client_order_id or "").startswith("sa-opt-open-")}
    assert len(rungs) == 1, rungs  # rung 0 only
    o = out[0]
    assert o.final and o.stage == "order" and o.reason.startswith("not filled; later rung stopped: market gate"), o
    assert gate.ledger.claims == {}  # confirmed zero fill frees the owner shot


def test_sync_owner_recheck_after_the_gate_refuses_rung_zero(world, tmp_path):
    # calls: scan start, claim, rung-0 async re-verify pass; the 4th (sync, after the gate) holds
    gate = FlipGate(good=3, change="hold")
    m, _, _ = _owner_setup(world, tmp_path, gate=gate)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not world.orders_sent() and gate.n == 4, (out, gate.n)
    o = out[0]
    assert not o.opened and o.final is False and o.stage == "owner" and not o.story_handled, o
    assert gate.ledger.claims == {}


def test_async_gate_cannot_be_rechecked_synchronously_so_nothing_is_sent(world, tmp_path):
    gate = _strict()
    sync = gate.is_entry_eligible
    box = {}

    def is_entry_eligible(symbol, now, **kw):
        el = sync(symbol, now, **kw)
        if not any(x.source == "owner" for x in box["m"].structures):
            return el

        async def later():
            return el
        return later()

    gate.is_entry_eligible = is_entry_eligible
    m = box["m"] = _owner_setup(world, tmp_path, gate=gate)[0]
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not world.orders_sent()
    assert "synchronously" in out[0].reason and out[0].final is False, out


def test_budget_cut_after_failed_expiries_stays_retryable(world, tmp_path):
    m = _mgr(world, tmp_path, [(DEC18, ""), (DEC24, ""), (DEC31, "")])
    m._expiry_tries = {"AAA": 1}  # an earlier candidate for AAA used one expiry this scan
    o = run(m._try_open(OptionCandidate("AAA", source="scan"), world.clock.now()))
    assert not o.opened and o.reason.count("open interest unknown") >= 2 and o.final is False, o
    assert not world.orders_sent()
    assert ADDED


def test_filled_reply_without_quantities_never_ladders_and_keeps_the_owner_shot_spent(world, tmp_path, monkeypatch):
    # contradictory broker reply: FILLED but no usable quantity -> unconfirmed VOID. No further
    # rung may be sent, and a later-rung market pause must not certify zero fill or free the shot.
    gate = _strict()
    m, _, _ = _owner_setup(world, tmp_path, gate=gate)
    real_wait = m._wait_or_cancel

    async def filled_without_qty(order):
        o = await real_wait(order)
        return o.model_copy(update={"status": "filled", "filled_qty": 0.0, "filled_avg_price": None, "legs": []})

    monkeypatch.setattr(m, "_wait_or_cancel", filled_without_qty)
    monkeypatch.setattr(world.alpaca, "_try_fill_mleg", lambda o, at: None)
    real_gate = m._market_gate

    async def pause_after_a_send(purpose):
        if any(x.order_class == "mleg" for x in world.alpaca.orders.values()):
            return Gate(False, "paused")
        return await real_gate(purpose)

    m._market_gate = pause_after_a_send
    m.settings = m.settings.__class__(**{**m.settings.__dict__, "fill_wait_seconds": 0})
    out = run(m.scan(candidates=[_owner_candidate()]))
    rungs = {x.client_order_id for x in world.alpaca.orders.values()
             if x.order_class == "mleg" and (x.client_order_id or "").startswith("sa-opt-open-")}
    assert len(rungs) == 1, rungs
    st = next(x for x in m.structures if x.source == "owner")
    assert st.st is StructState.VOID and not st.zero_fill_confirmed and not st.open_never_sent, st.notes
    assert gate.ledger.claims == {("AAA", ADDED): st.id}  # shot stays spent, as master
    assert not any("later rung stopped" in o.reason for o in out), out
    assert len([a for a in m.alerts.sent if "Owner option idea spent" in a[1]]) == 1  # master's alert
