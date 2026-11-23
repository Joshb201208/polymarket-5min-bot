"""First run against live-shaped holdings (synthetic symbols), the legacy importer,
and the coverage points from review: long put, bad marks, no-shorting assignment,
rejection alerts."""

import asyncio
import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from stock_agent.migrate_v0 import migrate_v0
from stock_agent.options_book import KvOptionsStore, StructState, import_legacy_options, legacy_hints_from_rows
from stock_agent.research_schemas import Direction
from tests.fakes import occ_symbol
from tests.ledger_helpers import make_ledger
from tests.options_support import EXP, StubGate, make_manager, requote, seed_chain

FRI = date(2026, 11, 27)  # day after Thanksgiving, 13:00 close
DEC11 = date(2026, 12, 11)
C100 = occ_symbol("AAA", EXP, "call", 100)
C105 = occ_symbol("AAA", EXP, "call", 105)
P100 = occ_symbol("AAA", EXP, "put", 100)


def run(coro):
    return asyncio.run(coro)


def _live_shaped(world):
    """Two debit spreads expiring Fri 27 Nov and one short call (11 Dec) against an
    over-size share holding."""
    a = world.alpaca
    world.price("GGG", 930, spread=0.1)
    world.price("HHH", 200, spread=0.05)
    g_l, g_s = occ_symbol("GGG", FRI, "call", 920), occ_symbol("GGG", FRI, "call", 940)
    h_l, h_s = occ_symbol("HHH", FRI, "put", 205), occ_symbol("HHH", FRI, "put", 195)
    for und, kind, k in (("GGG", "call", 920), ("GGG", "call", 940), ("HHH", "put", 205), ("HHH", "put", 195)):
        a.add_option_contract(und, FRI, kind, k)
    world.price("JJJ", 350, spread=0.05)
    a.seed_position("JJJ", 300, 300.0)  # over-size holding
    a.add_option_contract("JJJ", DEC11, "call", 340)
    jc = occ_symbol("JJJ", DEC11, "call", 340)
    a.seed_position(g_l, 1, 12.0)
    a.seed_position(g_s, -1, 6.0)
    a.seed_position(h_l, 2, 7.0)
    a.seed_position(h_s, -2, 3.0)
    a.seed_position(jc, -1, 8.0)
    legs = dict(g_l=g_l, g_s=g_s, h_l=h_l, h_s=h_s, jc=jc)
    _quote_all(world, legs)
    return legs


def _quote_all(world, legs):
    a = world.alpaca
    a.set_option_quote(legs["g_l"], 14.0, 14.4)
    a.set_option_quote(legs["g_s"], 5.0, 5.4)
    a.set_option_quote(legs["h_l"], 6.0, 6.2)
    a.set_option_quote(legs["h_s"], 1.0, 1.1)
    a.set_option_quote(legs["jc"], 12.0, 12.4)
    for und, px in (("GGG", 930), ("HHH", 200), ("JJJ", 350)):
        world.price(und, px, spread=0.05)


def test_first_run_from_broker_positions_alone(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    legs = _live_shaped(world)
    m = make_manager(world, tmp_path, universe=())
    res = run(m.monitor())
    g = next(s for s in m.structures if s.underlying == "GGG")
    h = next(s for s in m.structures if s.underlying == "HHH")
    j = next(s for s in m.structures if s.underlying == "JJJ")
    assert (g.strategy, g.qty, g.entry_debit) == ("bull_call_spread", 1, pytest.approx(6.0))
    assert (h.strategy, h.qty, h.entry_debit) == ("bear_put_spread", 2, pytest.approx(4.0))
    # Monday: 24, 25 and 27 Nov are left (the half day counts) -> spreads stay open
    assert g.st is StructState.OPEN and h.st is StructState.OPEN
    # the lone short call is repaired (bought back); the shares are left to the trim policy
    assert j.st is StructState.CLOSED and world.alpaca.position(legs["jc"]) is None
    assert world.alpaca.position("JJJ").qty == 300
    assert res.repairs and not m.covered
    # Tuesday 24 Nov after open + 5: T-2 time exit for both spreads
    world.clock.set_et(2026, 11, 24, 9, 33)
    _quote_all(world, legs)
    assert run(m.monitor()).exits == []  # inside the opening buffer
    world.clock.set_et(2026, 11, 24, 9, 36)
    _quote_all(world, legs)
    run(m.monitor())
    assert g.st is StructState.CLOSED and h.st is StructState.CLOSED
    assert all(world.alpaca.position(legs[k]) is None for k in ("g_l", "g_s", "h_l", "h_s"))
    assert all(p.qty >= 0 for p in world.alpaca.positions.values()), "no short anything left"


def test_normalise_buys_back_the_covered_call_once(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    legs = _live_shaped(world)
    m = make_manager(world, tmp_path, universe=())
    run(m.reconcile())
    r = run(m.close_short_call(legs["jc"]))
    assert r.filled and r.filled_qty == 1
    run(m.monitor())  # the repair structure is already closed: no second buy
    buys = [o for o in world.orders_sent() if o.json.get("symbol") == legs["jc"]]
    assert len(buys) == 1
    assert world.alpaca.position(legs["jc"]) is None  # not flipped to a long call


def test_legacy_importer_hints_steer_pairing(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    a = world.alpaca
    world.price("KKK", 100, spread=0.02)
    s = {k: occ_symbol("KKK", EXP, "call", k) for k in (100, 105, 110, 115)}
    for k, sym in s.items():
        a.add_option_contract("KKK", EXP, "call", k)
        a.set_option_quote(sym, 3.0, 3.1)
    for k, q in ((100, 1), (105, 1), (110, -1), (115, -1)):
        a.seed_position(s[k], q, 2.0 if q > 0 else 1.0)
    rows = [dict(position_id="g1", strategy="BULL_CALL_SPREAD", contract_symbol=s[100], side="buy",
                 leg2_contract_symbol=s[115], leg2_side="sell", qty=1, entry_price=1.0, is_open=True),
            dict(position_id="g2", strategy="BULL_CALL_SPREAD", contract_symbol=s[105], side="buy",
                 leg2_contract_symbol=s[110], leg2_side="sell", qty=1, entry_price=1.0, is_open=True)]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
    assert import_legacy_options(conn, rows) == 2
    book = json.loads(conn.execute("SELECT value_json FROM kv").fetchone()[0])
    assert book["structures"] == [] and set(book["meta"]["legacy_hints"]) == set(s.values())

    class _Kv:
        def kv_get(self, key, default=None):
            r = conn.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(r[0]) if r else default

        def kv_set(self, key, value):
            conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value, default=str), "t"))

    m = make_manager(world, tmp_path, universe=(), store=KvOptionsStore(_Kv()))
    run(m.reconcile())
    pairs = sorted(tuple(lg.strike for lg in st.legs) for st in m.structures)
    assert pairs == [(100, 115), (105, 110)]  # by legacy group, not by order
    assert all("legacy group" in " ".join(st.notes) for st in m.structures)


def test_migrate_v0_runs_the_options_importer(tmp_path):
    fix = Path(__file__).parent / "fixtures" / "state_v0"
    for f in fix.iterdir():
        shutil.copy(f, tmp_path / f.name)
    led = make_ledger(tmp_path)
    rep = migrate_v0(tmp_path, led.store, options_importer=import_legacy_options)
    assert rep.options_open == 1 and rep.options_open_imported == 1
    book = led.store.kv_get(KvOptionsStore.KEY)
    assert "AAA261127C00100000" in book["meta"]["legacy_hints"]


def test_hints_ignore_junk_rows():
    assert legacy_hints_from_rows([{"contract_symbol": "nonsense"}, "x", {}]) == {}


# ------------------------------------------------------------- review coverage
def test_long_put_for_high_conviction_bearish(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA", Direction.BEARISH, 9)
    m = make_manager(world, tmp_path, gate=gate)
    out = run(m.scan())
    st = m.get(out[0].opened)
    assert st.strategy == "long_put"
    body = world.orders_sent()[-1].json
    assert body["symbol"] == P100 and body["type"] == "limit" and body["position_intent"] == "buy_to_open"
    assert world.alpaca.position("AAA") is None


@pytest.mark.parametrize("bid,ask", [(0.0, 5.0), (0.0, 0.0), (3.0, 2.0)])
def test_zero_or_crossed_marks_are_not_marks(world, tmp_path, bid, ask):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA")
    m = make_manager(world, tmp_path, gate=gate)
    st = m.get(run(m.scan())[0].opened)
    world.clock.advance(minutes=20)
    seed_chain(world)
    world.alpaca.set_option_quote(C100, bid, ask)  # long leg marked at nothing / crossed
    res = run(m.monitor())
    assert res.exits == [] and st.st is StructState.OPEN and st.realized_pnl is None
    assert "options.mark_rejected" in m.journal.kinds()


def test_early_assignment_with_shorting_disabled(world, tmp_path):
    world.alpaca.shorting_enabled = False
    world.alpaca.configurations["no_shorting"] = True
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA")
    covers = []
    m = make_manager(world, tmp_path, gate=gate, covers=covers)
    st = m.get(run(m.scan())[0].opened)
    world.clock.advance(minutes=20)
    world.price("AAA", 112, spread=0.02)
    requote(world, C100, 12.3)
    requote(world, C105, 7.3)
    world.alpaca.assign_early(C105)
    run(m.monitor())
    assert ("AAA", "assignment") in covers
    assert st.st is StructState.CLOSED and world.alpaca.position(C100) is None
    stock_sells = [o for o in world.orders_sent() if o.json.get("symbol") == "AAA" and o.json.get("side") == "sell"]
    assert stock_sells == []  # the manager never trades the stock itself


def test_rejected_open_alerts(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA")
    m = make_manager(world, tmp_path, gate=gate)
    world.alpaca.fail("/v2/orders", "status", method="POST", status=403,
                      body={"code": 40310000, "message": "insufficient options buying power"})
    out = run(m.scan())
    assert out[0].opened is None and "rejected" in out[0].reason
    assert any("rejected" in title for _, title, _ in m.alerts.sent)
    assert m.structures[0].st is StructState.VOID


def test_capital_lock_guards_the_reservation(world, tmp_path):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = StubGate()
    gate.set("AAA")
    m = make_manager(world, tmp_path, gate=gate)

    async def go():
        lock = asyncio.Lock()
        m._capital_lock = lock
        await lock.acquire()
        task = asyncio.create_task(m.scan())
        await asyncio.sleep(0.5)
        assert world.orders_sent() == [] and m.structures == []  # waits for the equity lane
        lock.release()
        out = await task
        return out
    out = run(go())
    assert out[0].opened
    c = m.risk_commitments()
    assert len(c) == 1 and c[0].cash_reserved == 0  # filled: cash already at the broker


def test_pending_structure_reserves_cash(world, tmp_path):
    from stock_agent.options_book import Leg, Structure
    m = make_manager(world, tmp_path)
    st = Structure(id="os-x", underlying="AAA", strategy="bull_call_spread", direction="bullish", qty=2,
                   expiry=EXP.isoformat(), legs=[Leg(C100, "buy", "call", 100, EXP.isoformat())],
                   state=StructState.PENDING.value, source="scan", max_loss=140.0, open_client_id="sa-opt-open-x")
    m.structures.append(st)
    (c,) = m.risk_commitments()
    assert float(c.max_loss) == 140.0 and float(c.cash_reserved) == 140.0 and "sa-opt-open-x" in c.order_ids


def test_deferred_load(world, tmp_path):
    from stock_agent.options_manager import OptionsError

    class NotOpen:
        ready = False
        data = None

        def kv_get(self, key, default=None):
            if not self.ready:
                raise RuntimeError("store not open")
            return self.data

        def kv_set(self, key, value):
            self.data = value

    kv = NotOpen()
    m = make_manager(world, tmp_path, store=KvOptionsStore(kv), defer_load=True)
    assert m.open_blocker() == "options book not loaded"
    with pytest.raises(OptionsError):
        m.commitments()
    kv.ready = True
    m.load_state()
    assert m.commitments() == () and m.open_blocker() is None
