from datetime import timedelta
from decimal import Decimal

import pytest

from stock_agent.ledger import DiffKind, Severity, ThesisRole
from stock_agent.reconcile import ReconcileError, parse_occ
from tests.ledger_helpers import Clock, StubBroker, make_ledger, run


def kinds(snap):
    return sorted((d.kind.value, d.symbol) for d in snap.diffs)


def test_reconcile_read_error_leaves_state_untouched(tmp_path):
    b = StubBroker()
    b.pos("AAA", 10, 100)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("first"))
    meta_before = led.open_position_meta("AAA")
    for name in ("get_account", "get_positions", "list_orders", "get_activities"):
        b.fail = {name}
        with pytest.raises(ReconcileError):
            run(led.refresh("again"))
    assert led.open_position_meta("AAA") == meta_before


def test_reconcile_empty_account_is_real_empty_only_on_success(tmp_path):
    b = StubBroker()
    b.pos("AAA", 10, 100)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("first"))
    b.fail = {"get_positions"}
    with pytest.raises(ReconcileError):
        run(led.refresh("fail"))
    assert led.open_position_meta("AAA") is not None
    b.fail = set()
    b.fill("AAA", "sell", 10, 101, ts="2026-11-23T14:59:00Z")
    del b.positions["AAA"]
    snap = run(led.refresh("sold"))
    assert ("closed", "AAA") in kinds(snap)
    assert led.open_position_meta("AAA") is None


def test_reconcile_imports_short_and_alerts_critical(tmp_path):
    b = StubBroker()
    b.pos("PPP", -85, 85.7, 84.5)
    alerts = []
    led = make_ledger(tmp_path, broker=b, alerts=alerts)
    snap = run(led.refresh("start"))
    assert snap.holding("PPP").qty == Decimal("-85") and snap.holding("PPP").is_short
    assert "PPP" in snap.blocked_symbols
    sev = [d.severity for d in snap.diffs if d.kind == DiffKind.SHORT_STOCK]
    assert sev == [Severity.CRITICAL]
    assert any(a[0] == "critical" and "short_stock" in a[1] for a in alerts)
    snap2 = run(led.refresh("again"))          # still short -> still blocked and still critical
    assert "PPP" in snap2.blocked_symbols


def test_reconcile_keeps_fractional_qty(tmp_path):
    b = StubBroker()
    b.pos("MMM", 0.5, 1800, 1345)
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("start"))
    h = snap.holding("MMM")
    assert h.qty == Decimal("0.5") and h.is_fractional
    assert ("fractional", "MMM") in kinds(snap)
    assert ("fractional", "MMM") not in kinds(run(led.refresh("again")))   # reported once


def test_reconcile_preserves_metadata_on_qty_change(tmp_path):
    b = StubBroker()
    b.pos("AAA", 10, 100)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("first"))
    m = led.open_position_meta("AAA")
    tid = led.record_thesis("AAA", {"c": 9}, ThesisRole.ENTRY_CANDIDATE, activate=True)
    led.set_entry_thesis(m.position_id, tid)
    led.set_sector(m.position_id, "Technology")
    led.set_stop(m.position_id, 92.5, "stop-1")
    b.pos("AAA", 15, 102)
    snap = run(led.refresh("topup"))
    h = snap.holding("AAA")
    assert h.qty == 15 and h.avg_cost == Decimal("102")
    assert h.meta.position_id == m.position_id and h.meta.sector == "Technology"
    assert h.meta.entry_thesis_id == tid and h.meta.stop_order_id == "stop-1"
    assert ("qty_changed", "AAA") in kinds(snap)


def test_reconcile_close_explained_by_stop_fill(tmp_path):
    b = StubBroker()
    b.pos("AAA", 10, 100)
    alerts = []
    led = make_ledger(tmp_path, broker=b, alerts=alerts)
    run(led.refresh("first"))
    b.fill("AAA", "sell", 10, 92.0, ts="2026-11-23T14:40:00Z")
    del b.positions["AAA"]
    snap = run(led.refresh("after stop"))
    [d] = [d for d in snap.diffs if d.kind == DiffKind.CLOSED]
    assert d.severity == Severity.WARNING and "sell fill" in d.explanation


def test_reconcile_unexplained_close_is_critical(tmp_path):
    b = StubBroker()
    b.pos("AAA", 10, 100)
    b.pos("BBB", 10, 50)
    b.pos("CCC", 10, 50)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("first"))
    del b.positions["AAA"]
    snap = run(led.refresh("gone"))
    [d] = [d for d in snap.diffs if d.kind == DiffKind.CLOSED]
    assert d.severity == Severity.CRITICAL


def test_reconcile_close_explained_by_assignment_and_delivery(tmp_path):
    b = StubBroker()
    occ = "AAA261127P00100000"
    b.pos(occ, -1, 2.0, 3.0)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("first"))
    b.act("OPASN", occ, ts="2026-11-27T21:00:00Z", qty="1")
    b.act("OPTRD", "AAA", ts="2026-11-27T21:00:00Z", qty="100", price="100", net_amount="-10000")
    del b.positions[occ]
    b.pos("AAA", 100, 100, 99)
    snap = run(led.refresh("after assignment"))
    closed = [d for d in snap.diffs if d.kind == DiffKind.CLOSED]
    assert closed and "assigned" in closed[0].explanation
    assert led.open_position_meta("AAA").opened_source == "assignment"


def test_reconcile_name_change_moves_meta(tmp_path):
    b = StubBroker()
    b.pos("OLD", 78, 100)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("first"))
    pid = led.open_position_meta("OLD").position_id
    led.set_sector(pid, "Financials")
    b.act("NC", "NEW", ts="2026-06-09T08:00:00Z", qty="78", group_id="g1")
    b.act("NC", "OLD", ts="2026-06-09T08:00:00Z", qty="-78", group_id="g1")
    del b.positions["OLD"]
    b.pos("NEW", 78, 100)
    snap = run(led.refresh("rename"))
    assert ("renamed", "NEW") in kinds(snap)
    m = led.open_position_meta("NEW")
    assert m.position_id == pid and m.sector == "Financials"
    assert not any(d.kind == DiffKind.CLOSED for d in snap.diffs)


def test_reconcile_mass_close_guard_rereads(tmp_path):
    b = StubBroker()
    for s in ("AAA", "BBB", "CCC"):
        b.pos(s, 10, 50)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("first"))
    full = [dict(p) for p in b.positions.values()]
    b.positions_replies = [[], full]          # first read empty (glitch), re-read good
    snap = run(led.refresh("glitch"))
    assert b.calls.count("get_positions") == 3
    assert not any(d.kind == DiffKind.CLOSED for d in snap.diffs)
    assert {m.symbol for m in led.all_open_meta()} == {"AAA", "BBB", "CCC"}


def test_reconcile_mass_close_confirmed_is_critical(tmp_path):
    b = StubBroker()
    for s in ("AAA", "BBB"):
        b.pos(s, 10, 50)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("first"))
    b.positions.clear()
    snap = run(led.refresh("liquidated"))
    assert any(d.kind == DiffKind.MASS_CLOSE and d.severity == Severity.CRITICAL for d in snap.diffs)


def test_reconcile_option_legs_parsed_and_ungrouped_flagged(tmp_path):
    b = StubBroker()
    b.pos("GSX261127C00920000", 1, 17.55, 1.06)
    b.pos("GSX261127C00940000", -1, 6.55, 0.75)
    led = make_ledger(tmp_path, broker=b)
    led.option_leg_grouper = lambda occ: "s1" if occ.endswith("920000") else None
    snap = run(led.refresh("start"))
    assert len(snap.legs) == 2 and snap.holdings == ()
    short = [x for x in snap.legs if x.qty < 0][0]
    assert short.side == "short" and short.strike == Decimal("940") and short.multiplier == 100
    assert snap.options_mv == Decimal("106") - Decimal("75")
    assert [d.symbol for d in snap.diffs if d.kind == DiffKind.OPTION_LEG_UNGROUPED] == ["GSX261127C00940000"]


def test_reconcile_invalid_price_flagged_not_used(tmp_path):
    b = StubBroker()
    b.pos("AAA", 10, 100, None)
    b.pos("GSX261127C00920000", 1, 17.55, 0)              # long leg marked 0 pre-open
    b.pos("GSX261127C00940000", -1, 6.55, 4.30)           # short leg priced above a lower strike
    b.pos("GSX261127C00900000", 1, 20, 3.0)             # lower-strike call below it: crossed
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("pre-open"))
    assert snap.holding("AAA").price is None and not snap.holding("AAA").price_ok
    assert snap.quality == "indicative"
    bad = {x.occ_symbol for x in snap.legs if not x.price_ok}
    assert bad == {"GSX261127C00920000", "GSX261127C00940000", "GSX261127C00900000"}


def test_reconcile_imports_entry_date_from_fills(tmp_path):
    b = StubBroker()
    b.fill("HHH", "buy", 38, 214.63, ts="2026-04-23T17:56:16Z")
    b.pos("HHH", 38, 214.63, 218)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("start", rebuild=True))
    m = led.open_position_meta("HHH")
    assert m.opened_at.date().isoformat() == "2026-04-23" and m.opened_source == "import"


def test_reconcile_records_cash_flows_and_open_orders(tmp_path):
    b = StubBroker()
    b.act("JNLC", None, ts="2026-03-30T00:00:00Z", net_amount="100000")
    b.orders = [{"id": "o1", "client_order_id": "sa-x", "symbol": "AAA", "side": "buy", "type": "limit",
                 "qty": "10", "filled_qty": "4", "limit_price": "10.5", "time_in_force": "day",
                 "asset_class": "us_equity", "position_intent": "buy_to_open"}]
    led = make_ledger(tmp_path, broker=b)
    snap = run(led.refresh("start"))
    [cf] = led.cash_flows()
    assert cf.amount == 100000 and cf.kind == "JNLC"
    [o] = snap.open_orders
    assert o.remaining_qty == 6 and o.position_intent == "buy_to_open"
    run(led.refresh("again"))
    assert len(led.cash_flows()) == 1                      # idempotent ingest


def test_rebuild_clears_trading_block_and_alerts(tmp_path):
    (tmp_path / "state.db").write_bytes(b"junk" * 500)
    b = StubBroker()
    b.pos("AAA", 10, 100)
    alerts = []
    led = make_ledger(tmp_path, broker=b, alerts=alerts)
    assert led.trading_block()
    run(led.refresh("rebuild", rebuild=True))
    assert led.trading_block() is None
    assert any(a[3] == "state:rebuilt" for a in alerts)


def test_reconcile_journals_every_diff(tmp_path):
    b = StubBroker()
    b.pos("PPP", -5, 10)
    led = make_ledger(tmp_path, broker=b)
    run(led.refresh("start"))
    recs = list(led.journal.read(kinds={"reconcile_diff"}))
    assert {r["kind"] for r in recs} == {"reconcile_diff"}
    assert any(r.get("diff_kind") == "short_stock" for r in recs)


def test_parse_occ():
    und, exp, right, strike = parse_occ("JPM261211C00340000")
    assert und == "JPM" and exp.isoformat() == "2026-12-11" and right == "call" and strike == Decimal("340")
    assert parse_occ("AAPL") is None


def test_snapshot_age(tmp_path):
    clock = Clock()
    led = make_ledger(tmp_path, broker=StubBroker(), clock=clock)
    snap = run(led.refresh("x"))
    clock.t += timedelta(seconds=30)
    assert snap.age_seconds(clock.t) == 30
