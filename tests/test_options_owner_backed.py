"""Owner-backed bullish option idea (lead rulings 25 Nov 2026): one defined-risk bullish
structure per directive generation, proved by the research gate's typed owner proof,
inside the unchanged option budgets, with the origin "owner" in book and journal."""

import asyncio
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from stock_agent.options_book import StructState
from stock_agent.options_manager import OptionCandidate
from stock_agent.options_policy import ExitReason
from stock_agent.research_gate import Eligibility
from stock_agent.research_schemas import Direction
from tests.options_support import StubGate, StubRisk, make_manager, seed_chain

ADDED = date(2026, 11, 25)


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class Proof:  # shape of research_gate.OwnerEntryProof
    symbol: str
    added: date
    expires: date
    thesis_id: str
    raw_conviction: int
    effective_conviction: int
    generated_at: datetime


class Ledger:
    def __init__(self):
        self.claims: dict[tuple[str, date], str] = {}
        self.vetoed = False
        self.used_calls: list = []

    def owner_directive_used(self, symbol, added, *, lane="option"):
        self.used_calls.append((symbol, added, lane))
        return self.vetoed or (symbol, added) in self.claims

    def claim_owner_option(self, symbol, added, structure_id):
        if self.vetoed:
            return False
        held = self.claims.setdefault((symbol, added), structure_id)
        return held == structure_id


class OwnerGate(StubGate):
    """Ordinary calls see the model's own (low) grade and fail; the owner option lane
    gets the owner grade plus a typed proof."""

    def __init__(self, raw=3, owner=8, added=ADDED):
        super().__init__()
        self.ledger = Ledger()
        self.raw, self.owner, self.added = raw, owner, added
        self.calls: list = []
        self.reservations: list = []

    def is_entry_eligible(self, symbol, now, *, allow_owner=False, owner_lane="stock", reservation_id=None):
        self.calls.append((symbol, allow_owner, owner_lane))
        self.reservations.append(reservation_id)
        gen = now - timedelta(hours=1)
        if not (allow_owner and owner_lane == "option") or self.added is None:
            return Eligibility(False, ("conviction_below_floor_or_invalid",), f"th-{symbol}",
                               self.raw, self.owner, Direction.BUY, gen)
        proof = Proof(symbol, self.added, date(2027, 1, 25), f"th-{symbol}", self.raw, self.owner, gen)
        return Eligibility(True, (), f"th-{symbol}", self.raw, self.owner, Direction.BUY, gen, proof)


def _owner_candidate(**kw):
    return OptionCandidate("AAA", source="owner", story_id=f"owner:AAA:{ADDED}:2026-11-23",
                           direction_hint="BUY", thesis_id="th-AAA", owner_directive_added=ADDED.isoformat(), **kw)


def _setup(world, tmp_path, gate=None, risk=None):
    world.clock.set_et(2026, 11, 23, 10, 0)
    seed_chain(world)
    gate = gate or OwnerGate()
    risk = risk or StubRisk()
    return make_manager(world, tmp_path, gate=gate, risk=risk), gate, risk


def test_owner_idea_opens_one_bull_call_spread_with_owner_origin(world, tmp_path):
    m, gate, risk = _setup(world, tmp_path)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert out[0].opened, out
    st = m.get(out[0].opened)
    assert st.strategy == "bull_call_spread" and st.source == "owner"
    assert st.owner_directive_added == ADDED.isoformat() and st.st is StructState.OPEN
    assert any("owner-backed" in n for n in st.notes)
    # typed proof reaches final risk; raw model grade stays the model's
    assert risk.controls[-1].owner_entry.added == ADDED
    assert risk.intents[-1].raw_conviction == 3 and risk.intents[-1].effective_conviction == 8
    assert gate.ledger.claims == {("AAA", ADDED): st.id}
    assert ("AAA", True, "option") in gate.calls
    ev = {k: f for k, f in m.journal.events}
    assert ev["options.open_submitted"]["source"] == "owner"
    assert ev["options.open_submitted"]["owner_directive_added"] == ADDED.isoformat()
    assert ev["options.opened"]["source"] == "owner"
    assert risk.intents[-1].signal_at == risk.controls[-1].owner_entry.generated_at


def test_ordinary_scan_never_inherits_the_owner_grade(world, tmp_path):
    m, gate, _ = _setup(world, tmp_path)
    out = run(m.scan())  # daily scan over the universe, same symbol
    assert not out[0].opened and "research gate" in out[0].reason
    assert all(not allow for _, allow, _ in gate.calls)
    assert not world.orders_sent()


def test_one_shot_per_generation_close_does_not_rearm(world, tmp_path):
    m, gate, _ = _setup(world, tmp_path)
    first = run(m.scan(candidates=[_owner_candidate()]))
    st = m.get(first[0].opened)
    run(m._close(st, ExitReason.LOSS_LIMIT, world.clock.now()))  # stopped out: never re-arms
    assert st.st is StructState.CLOSED and not m.live()
    m.cooldowns.clear()  # even without the loss cool-down the generation stays used
    seed_chain(world)
    n = len(world.orders_sent())
    again = run(m.scan(candidates=[dataclasses.replace(_owner_candidate(), story_id=f"owner:AAA:{ADDED}:2026-11-24")]))
    assert not again[0].opened and "already used" in again[0].reason
    assert len(world.orders_sent()) == n


def test_new_generation_rearms(world, tmp_path):
    m, gate, _ = _setup(world, tmp_path)
    st = m.get(run(m.scan(candidates=[_owner_candidate()]))[0].opened)
    run(m._close(st, ExitReason.MANUAL, world.clock.now()))
    assert st.st is StructState.CLOSED
    m.cooldowns.clear()  # the loss cool-down still applies to owner ideas; cleared here to test re-arming
    seed_chain(world)
    gate.added = date(2026, 12, 1)  # owner changed "added"
    c = dataclasses.replace(_owner_candidate(), story_id="owner:AAA:2026-12-01:2026-11-24",
                            owner_directive_added="2026-12-01")
    out = run(m.scan(candidates=[c]))
    assert out[0].opened and m.get(out[0].opened).owner_directive_added == "2026-12-01"


def test_veto_or_refused_claim_sends_nothing(world, tmp_path):
    gate = OwnerGate()
    gate.ledger.vetoed = True
    m, _, _ = _setup(world, tmp_path, gate=gate)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and ("vetoed" in out[0].reason or "refused" in out[0].reason)
    assert not world.orders_sent() and not m.live()


def test_missing_claim_api_is_conservative(world, tmp_path):
    gate = OwnerGate()
    gate.ledger = object()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and not world.orders_sent()


def test_idea_must_match_the_current_directive(world, tmp_path):
    m, _, _ = _setup(world, tmp_path)
    stale = dataclasses.replace(_owner_candidate(), owner_directive_added="2026-10-01")
    out = run(m.scan(candidates=[stale]))
    assert not out[0].opened and "does not match" in out[0].reason
    other = dataclasses.replace(_owner_candidate(), thesis_id="th-old")
    assert not run(m.scan(candidates=[other]))[0].opened
    assert not world.orders_sent()


def test_no_proof_no_entry(world, tmp_path):
    gate = OwnerGate(added=None)  # directive expired / removed: gate issues no proof
    m, _, _ = _setup(world, tmp_path, gate=gate)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and not world.orders_sent()


def test_owner_eight_never_falls_back_to_a_lone_option(world, tmp_path):
    world.alpaca.options_level = 2  # account allows single options only
    m, _, _ = _setup(world, tmp_path)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and "no spread" in out[0].reason
    assert not world.orders_sent()


def test_owner_idea_respects_budgets_and_risk_verdict(world, tmp_path):
    m, gate, _ = _setup(world, tmp_path, risk=StubRisk(ok=False, reasons=("option_total_budget",)))
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and "option_total_budget" in out[0].reason
    assert gate.ledger.claims == {}  # claim only after a passing final risk verdict
    assert not world.orders_sent()


def test_one_live_structure_per_underlying_still_applies(world, tmp_path):
    gate = OwnerGate()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    ordinary = StubGate()
    ordinary.set("AAA", Direction.BUY, 8)
    m.gate = ordinary
    assert run(m.scan())[0].opened
    m.gate = gate
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and "already holds" in out[0].reason


# ------------------------------------------------ lead ruling 2: zero fill frees the shot
class ReleasingLedger(Ledger):
    def __init__(self):
        super().__init__()
        self.released: list = []

    def release_owner_option(self, symbol, added, structure_id):
        if self.claims.get((symbol, added)) != structure_id or self.vetoed:
            return False
        del self.claims[(symbol, added)]
        self.released.append(structure_id)
        return True

    def owner_directive_vetoed(self, symbol, added):
        return self.vetoed


class StrictOwnerGate(OwnerGate):
    """Like the real gate: no proof while the durable option claim stands."""

    def is_entry_eligible(self, symbol, now, *, allow_owner=False, owner_lane="stock", reservation_id=None):
        el = super().is_entry_eligible(symbol, now, allow_owner=allow_owner, owner_lane=owner_lane,
                                       reservation_id=reservation_id)
        held = self.ledger.claims.get((symbol, self.added)) if self.added else None
        if el.ok and (self.ledger.vetoed or (held is not None and held != reservation_id)):
            return Eligibility(False, ("owner_option_used",), el.thesis_id, el.raw_conviction,
                               el.effective_conviction, el.direction, el.generated_at)
        return el


def _strict():
    gate = StrictOwnerGate()
    gate.ledger = ReleasingLedger()
    return gate


def _never_fill(world, monkeypatch, m):
    monkeypatch.setattr(world.alpaca, "_try_fill_mleg", lambda o, at: None)
    m.settings = dataclasses.replace(m.settings, fill_wait_seconds=0)  # no real-time polling


def _next_session(m, world):
    m.cooldowns.clear()
    seed_chain(world)
    return dataclasses.replace(_owner_candidate(), story_id=f"owner:AAA:{ADDED}:2026-11-24")


def test_confirmed_zero_fill_frees_the_option_shot(world, tmp_path, monkeypatch):
    gate = _strict()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    _never_fill(world, monkeypatch, m)
    out = run(m.scan(candidates=[_owner_candidate()]))
    voided = [x for x in m.structures if x.source == "owner"]
    assert not out[0].opened and len(voided) == 1
    v = voided[0]
    assert v.st is StructState.VOID and v.zero_fill_confirmed, v.notes
    monkeypatch.undo()
    again = run(m.scan(candidates=[_next_session(m, world)]))
    assert again[0].opened, again
    assert gate.ledger.released == [v.id] and v.owner_claim_released
    assert gate.ledger.claims == {("AAA", ADDED): again[0].opened}
    assert any(k == "options.owner_claim_released" for k, _ in m.journal.events)


def test_unconfirmed_void_stays_spent_and_alerts_once(world, tmp_path, monkeypatch):
    gate = _strict()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    _never_fill(world, monkeypatch, m)
    run(m.scan(candidates=[_owner_candidate()]))
    v = next(x for x in m.structures if x.source == "owner")
    v.zero_fill_confirmed = False  # e.g. voided by reconcile after an ambiguous submit
    v.owner_claim_released = False
    gate.ledger.claims[("AAA", ADDED)] = v.id
    gate.ledger.released.clear()
    monkeypatch.undo()
    for day in ("2026-11-24", "2026-11-25"):
        c = dataclasses.replace(_next_session(m, world), story_id=f"owner:AAA:{ADDED}:{day}")
        out = run(m.scan(candidates=[c]))
        assert not out[0].opened and ("already used" in out[0].reason or "owner_option_used" in out[0].reason)
    assert gate.ledger.released == [] and (("AAA", ADDED) in gate.ledger.claims)
    spent = [a for a in m.alerts.sent if "Owner option idea spent" in a[1]]
    assert len(spent) == 1


def test_claim_without_book_structure_stays_spent_and_alerts_once(world, tmp_path):
    gate = _strict()
    gate.ledger.claims[("AAA", ADDED)] = "opt-crashed"  # claimed, then crashed before the book save
    m, _, _ = _setup(world, tmp_path, gate=gate)
    for _ in range(2):
        out = run(m.scan(candidates=[_owner_candidate()]))
        assert not out[0].opened
    assert not world.orders_sent() and gate.ledger.claims[("AAA", ADDED)] == "opt-crashed"
    assert len([a for a in m.alerts.sent if "Owner option idea spent" in a[1]]) == 1


def test_veto_does_not_raise_the_spent_alert(world, tmp_path):
    gate = _strict()
    gate.ledger.vetoed = True
    m, _, _ = _setup(world, tmp_path, gate=gate)
    assert not run(m.scan(candidates=[_owner_candidate()]))[0].opened
    assert not [a for a in m.alerts.sent if "Owner option idea spent" in a[1]]


# ------------------------------------- held owner option-only thesis: HOLD + proof grades
class HeldOwnerGate(OwnerGate):
    """Held review keeps the model's grades on the thesis (raw 3, eff 3, HOLD); only the
    typed proof carries the owner grade."""

    def is_entry_eligible(self, symbol, now, *, allow_owner=False, owner_lane="stock", reservation_id=None):
        el = super().is_entry_eligible(symbol, now, allow_owner=allow_owner, owner_lane=owner_lane,
                                       reservation_id=reservation_id)
        return dataclasses.replace(el, direction=Direction.HOLD, effective_conviction=self.raw)


def test_held_owner_thesis_uses_proof_grades_and_hold_direction(world, tmp_path):
    m, gate, risk = _setup(world, tmp_path, gate=HeldOwnerGate())
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert out[0].opened, out
    st = m.get(out[0].opened)
    assert st.direction == "bullish" and st.strategy == "bull_call_spread" and st.conviction == 8
    assert risk.intents[-1].raw_conviction == 3 and risk.intents[-1].effective_conviction == 8


def test_ordinary_hold_thesis_is_still_not_tradeable(world, tmp_path):
    m, _, _ = _setup(world, tmp_path, gate=HeldOwnerGate())
    out = run(m.scan())
    assert not out[0].opened and not world.orders_sent()


def test_owner_idea_with_bearish_hint_is_rejected(world, tmp_path):
    m, _, _ = _setup(world, tmp_path)
    c = dataclasses.replace(_owner_candidate(), direction_hint="BEARISH")
    out = run(m.scan(candidates=[c]))
    assert not out[0].opened and "bullish" in out[0].reason and not world.orders_sent()


# ------------------------------------------- F2 eager release / reconcile, F3 re-verify
def test_zero_fill_releases_the_shot_eagerly(world, tmp_path, monkeypatch):
    gate = _strict()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    _never_fill(world, monkeypatch, m)
    run(m.scan(candidates=[_owner_candidate()]))
    v = next(x for x in m.structures if x.source == "owner")
    assert v.st is StructState.VOID and v.zero_fill_confirmed
    assert v.owner_claim_released and gate.ledger.released == [v.id] and gate.ledger.claims == {}


def _pending_owner(m, gate, world, monkeypatch):
    _never_fill(world, monkeypatch, m)
    run(m.scan(candidates=[_owner_candidate()]))
    v = next(x for x in m.structures if x.source == "owner")
    v.state = StructState.PENDING.value  # as if the process crashed after an ambiguous submit
    v.zero_fill_confirmed = v.owner_claim_released = False
    gate.ledger.claims[("AAA", ADDED)] = v.id
    gate.ledger.released.clear()
    return v


def test_reconcile_void_without_broker_order_alerts_once_and_keeps_the_shot(world, tmp_path, monkeypatch):
    gate = _strict()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    v = _pending_owner(m, gate, world, monkeypatch)
    v.open_client_id = "sa-opt-open-never-sent"
    run(m.reconcile())
    run(m.reconcile())
    assert v.st is StructState.VOID and not v.zero_fill_confirmed
    assert gate.ledger.claims == {("AAA", ADDED): v.id} and gate.ledger.released == []
    assert len([a for a in m.alerts.sent if "Owner option idea spent" in a[1]]) == 1


def test_reconcile_confirmed_zero_fill_releases_eagerly(world, tmp_path, monkeypatch):
    gate = _strict()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    v = _pending_owner(m, gate, world, monkeypatch)  # client id is the cancelled rung's
    run(m.reconcile())
    assert v.st is StructState.VOID and v.zero_fill_confirmed and v.owner_claim_released
    assert gate.ledger.claims == {} and not [a for a in m.alerts.sent if "spent" in a[1]]


class FlipGate(StrictOwnerGate):
    """Issues the proof for the first `good` owner-lane calls, then applies `change`."""

    def __init__(self, good, change):
        super().__init__()
        self.ledger = ReleasingLedger()
        self.good, self.change, self.n = good, change, 0

    def is_entry_eligible(self, symbol, now, *, allow_owner=False, owner_lane="stock", reservation_id=None):
        if allow_owner and owner_lane == "option":
            self.n += 1
            if self.n > self.good:
                if self.change == "hold":
                    gen = now - timedelta(hours=1)
                    return Eligibility(False, ("owner_directive_session_hold",), f"th-{symbol}",
                                       self.raw, self.owner, Direction.BUY, gen)
                self.added = date(2026, 12, 2)  # owner edited the directive mid-open
        return super().is_entry_eligible(symbol, now, allow_owner=allow_owner, owner_lane=owner_lane,
                                         reservation_id=reservation_id)


def test_session_hold_before_claim_sends_nothing_and_consumes_nothing(world, tmp_path):
    gate = FlipGate(good=1, change="hold")  # proof at scan start, hold by claim time
    m, _, _ = _setup(world, tmp_path, gate=gate)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and "session_hold" in out[0].reason
    assert gate.ledger.claims == {} and not world.orders_sent() and not m.structures


def test_directive_edited_before_claim_is_rejected(world, tmp_path):
    gate = FlipGate(good=1, change="edit")
    m, _, _ = _setup(world, tmp_path, gate=gate)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and "changed" in out[0].reason
    assert gate.ledger.claims == {} and not world.orders_sent()


def test_hold_before_rung_zero_voids_without_sending_and_frees_the_shot(world, tmp_path):
    gate = FlipGate(good=2, change="hold")  # passes scan start and claim, held before rung 0
    m, _, _ = _setup(world, tmp_path, gate=gate)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and not world.orders_sent()
    v = next(x for x in m.structures if x.source == "owner")
    assert v.st is StructState.VOID and v.zero_fill_confirmed and v.owner_claim_released
    assert gate.ledger.claims == {}


def test_definitive_broker_rejection_releases_the_shot_eagerly(world, tmp_path, monkeypatch):
    from stock_agent.broker import BrokerError

    gate = _strict()
    m, _, _ = _setup(world, tmp_path, gate=gate)

    async def refuse(body):
        raise BrokerError("rejected", "synthetic broker rejection", status=403)

    monkeypatch.setattr(m.broker, "submit_raw_order", refuse)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and "rejected" in out[0].reason
    v = next(x for x in m.structures if x.source == "owner")
    assert v.st is StructState.VOID and v.zero_fill_confirmed and v.owner_claim_released
    assert gate.ledger.claims == {} and gate.ledger.released == [v.id]


class QuoteTTLGate(StrictOwnerGate):
    """Every owner-lane proof needs a quote refreshed within a short TTL (a ladder outlives
    the gate's quote TTL). The refresh time is the fake clock's, read at refresh."""

    TTL = timedelta(seconds=5)

    def __init__(self, clock=None):
        super().__init__()
        self.ledger = ReleasingLedger()
        self.clock = clock
        self.refreshed_at = None
        self.refreshes = 0

    def refresh(self, symbol, now):
        self.refreshes += 1
        self.refreshed_at = self.clock.now() if self.clock is not None else now

    def is_entry_eligible(self, symbol, now, *, allow_owner=False, owner_lane="stock", reservation_id=None):
        if allow_owner and owner_lane == "option":
            at = self.refreshed_at
            if at is None or not (timedelta() <= now - at <= self.TTL):
                gen = now - timedelta(hours=1)
                return Eligibility(False, ("quote_stale_or_unavailable",), f"th-{symbol}",
                                   self.raw, self.owner, Direction.BUY, gen)
        return super().is_entry_eligible(symbol, now, allow_owner=allow_owner, owner_lane=owner_lane,
                                         reservation_id=reservation_id)


def test_owner_ladder_refreshes_the_quote_before_each_reverify(world, tmp_path, monkeypatch):
    gate = QuoteTTLGate()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    m._research_refresh = gate.refresh
    m.settings = dataclasses.replace(m.settings, fill_wait_seconds=0)
    fills = {"n": 0}
    real = world.alpaca._try_fill_mleg

    def fill_from_rung_two(o, at):  # rung 0 dies unfilled; a later rung fills
        fills["n"] += 1
        if len({x.id for x in world.alpaca.orders.values() if x.order_class == "mleg"}) >= 2:
            real(o, at)

    monkeypatch.setattr(world.alpaca, "_try_fill_mleg", fill_from_rung_two)
    out = run(m.scan(candidates=[_owner_candidate()]))
    st = next(x for x in m.structures if x.source == "owner")
    assert out[0].opened == st.id and st.st is StructState.OPEN, st.notes
    assert len([x for x in world.alpaca.orders.values() if x.order_class == "mleg"]) >= 2  # really laddered
    assert gate.refreshes >= 4  # scan start, before the lock, rung 0, rung 1
    assert gate.ledger.claims == {("AAA", ADDED): st.id}


def test_owner_without_refresh_hook_stops_on_a_stale_quote(world, tmp_path):
    gate = QuoteTTLGate()
    m, _, _ = _setup(world, tmp_path, gate=gate)
    out = run(m.scan(candidates=[_owner_candidate()]))
    assert not out[0].opened and "quote_stale" in out[0].reason and not world.orders_sent()
