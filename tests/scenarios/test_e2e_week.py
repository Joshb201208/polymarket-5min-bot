"""End-to-end simulated week (Mon 2026-11-23 .. Fri 2026-11-27) on the new components.

One run of the week (module fixture), many assertions. The world is scripted
by `E2EWeek`; the model replies by `ScriptedModels`; the agent is
`week_agent.WeekAgent` until the real scheduler loop is wired.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pytest

from tests.fakes.clock import ET, UTC
from tests.fakes.providers import LLMReply
from tests.scenarios import ScenarioRunner
from tests.scenarios.week import E2EWeek
from tests.scenarios.week_agent import runner_hooks

STOP_GRACE = timedelta(minutes=10)   # "every long has a stop within N minutes of its fill"
CONFIRM_MODEL = "gpt-5.6-luna"


@dataclass
class Plan:
    symbol: str
    direction: str
    conviction: int
    verdict: str | None = None  # for the blind exit confirmation


@dataclass
class ScriptedModels:
    """Answers OpenAI Responses calls from the evidence id in the prompt."""

    plans: dict[str, Plan] = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)  # (model, evidence_id, answer)

    def on_news(self, world, symbol, headline):
        story = world.alpaca.news[-1]
        eid = f"news-{story['id']}"
        if "guidance" in headline or "contract" in headline:
            self.plans[eid] = Plan(symbol, "BUY", 9)
        elif "Second report" in headline:
            self.plans[eid] = Plan(symbol, "EXIT_PROPOSED", 8, "CONFIRM_BREAK")
        elif "probe" in headline:
            self.plans[eid] = Plan(symbol, "EXIT_PROPOSED", 8, "DISAGREE")

    def __call__(self, body: dict):
        text = json.dumps(body)
        eid = next((e for e in self.plans if f'"{e}"' in text or e in text), None)
        model = body.get("model", "")
        if eid is None:
            self.calls.append((model, "", "no evidence"))
            return LLMReply(text="{}")
        p = self.plans[eid]
        if model == CONFIRM_MODEL:
            self.calls.append((model, eid, p.verdict or "DISAGREE"))
            return LLMReply(text=json.dumps({"symbol": p.symbol, "verdict": p.verdict or "DISAGREE",
                                             "conviction": 8, "reason": "Independent read of the story",
                                             "evidence_ids": [eid]}))
        self.calls.append((model, eid, p.direction))
        held = p.direction == "EXIT_PROPOSED"
        draft = {
            "symbol": p.symbol, "direction": p.direction, "conviction": p.conviction,
            "summary": "Scripted thesis", "bull_case": "Upside", "bear_case": "Downside",
            "claims": [{"text": "The story changes the case.", "evidence_ids": [eid]}],
            "catalysts": [], "risks": [], "thesis_break": "The reported change reverses",
            "target_price": None, "thesis_change": "broken" if held else "new", "horizon_days": 20,
        }
        n = eid.split("-")[1]
        return LLMReply(text=json.dumps(draft),
                        sources=[{"url": f"https://example.org/news/{n}", "title": "Story"}])


@dataclass
class Invariants:
    """Checked after every simulated minute."""

    first_unprotected: dict[str, datetime] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    def __call__(self, world, runner) -> bool:
        now = world.clock.now()
        a = world.alpaca
        for sym, pos in a.positions.items():
            if pos.qty < 0 and len(sym) <= 6:
                self.violations.append(f"{now:%a %H:%M} short stock {sym} {pos.qty}")
            if pos.qty <= 0 or len(sym) > 6:
                continue
            stops = [o for o in a.open_orders(sym) if o.side == "sell" and o.type in ("stop", "stop_limit")]
            working_exit = [o for o in a.open_orders(sym) if o.side == "sell" and o not in stops]
            if len(stops) > 1:
                self.violations.append(f"{now:%a %H:%M} {sym} has {len(stops)} stops")
            covered = len(stops) == 1 and all(o.time_in_force == "gtc" for o in stops)
            if covered or working_exit:
                self.first_unprotected.pop(sym, None)
            else:
                first = self.first_unprotected.setdefault(sym, now)
                if now - first > STOP_GRACE and world.clock.calendar.is_open(now):
                    self.violations.append(f"{now:%a %H:%M} {sym} unprotected since {first:%H:%M}")
        return False  # never "fires"; only records


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    from tests.fakes.world import FakeWorld

    mp = pytest.MonkeyPatch()
    data = tmp_path_factory.mktemp("e2e_week")
    world = FakeWorld(start=datetime(2026, 11, 23, 7, 0, tzinfo=ET))
    world.install(mp, data_dir=data)
    models = ScriptedModels()
    world.openai.respond_with(models)
    script = E2EWeek(on_news=models.on_news)
    runner = ScenarioRunner(world, **runner_hooks())
    inv = Invariants()
    runner.when(inv, lambda w, r: None, "invariants", once=False)
    try:
        script.run(runner)
    finally:
        mp.undo()
    return world, runner, script, models, inv


def _orders(world, *, day=None, symbol=None, side=None):
    out = []
    for q in world.net.requests("alpaca", "POST", "/v2/orders"):
        body = q.json or {}
        at = q.at.astimezone(ET)
        if day and at.date() != day:
            continue
        if symbol and body.get("symbol") != symbol:
            continue
        if side and body.get("side") != side:
            continue
        out.append((at, body, q))
    return out


def _agents(runner):
    return runner.agent


def test_week_runs_without_tick_errors(week):
    world, runner, *_ = week
    assert runner.steps > 2000
    assert not runner.errors, "\n".join(e.trace for e in runner.errors[:3])


def test_never_a_short_stock_position_and_stops_always_in_place(week):
    *_, inv = week
    assert not inv.violations, inv.violations[:10]


def test_never_a_sell_above_free_shares(week):
    """The fake refuses (403) any sell above qty_available; none may be refused."""
    world, *_ = week
    refused = [(at, b) for at, b, q in _orders(world, side="sell") if q.status in (403, 422)]
    assert not refused, refused


def test_first_run_normalise_completes_before_any_entry(week):
    world, runner, *_ = week
    st = runner.agent.engine._norm_state()
    assert st.get("state") == "done", st
    done_at = datetime.fromisoformat(st["done_at"])
    buys = [at for at, b, _ in _orders(world, side="buy") if b.get("position_intent") == "buy_to_open"]
    assert buys, "no entry all week"
    assert min(buys) > done_at.astimezone(ET)
    # the stale ZZZ order was cleaned or left harmless, the AAA holding has a stop
    assert world.alpaca.position("AAA") is None or any(o.type == "stop" for o in world.alpaca.open_orders("AAA"))
    no_short = world.net.requests("alpaca", "PATCH", "/v2/account/configurations")
    assert no_short and no_short[0].json.get("no_shorting") is True


def test_news_driven_buy_with_conviction_8_plus_sized_within_limits(week):
    world, runner, script, models, _ = week
    from stock_agent.config import Config

    assert any(m == "gpt-6-luna" and e and a == "BUY" for m, e, a in models.calls)
    bbb = [o for o in world.alpaca.orders_for("BBB") if o.side == "buy" and o.filled_qty > 0]
    assert len(bbb) == 1, bbb
    fill_value = bbb[0].filled_qty * bbb[0].filled_avg_price
    assert fill_value <= Config.MAX_POSITION_PCT * 100_000 * 1.001


def test_stop_fill_does_not_trigger_a_second_sale(week):
    world, *_ = week
    sells = [o for o in world.alpaca.orders_for("BBB") if o.side == "sell" and o.filled_qty > 0]
    assert len(sells) == 1 and sells[0].type == "stop", [(o.type, o.filled_qty) for o in sells]
    assert world.alpaca.position("BBB") is None


def test_bad_news_exit_needs_the_second_confirmation(week):
    world, runner, script, models, _ = week
    t = script.times
    verdicts = [(e, a) for m, e, a in models.calls if m == CONFIRM_MODEL]
    assert ("DISAGREE" in [a for _, a in verdicts]) and ("CONFIRM_BREAK" in [a for _, a in verdicts]), verdicts
    sells = [at for at, b, _ in _orders(world, symbol="AAA", side="sell") if b.get("type") != "stop"]
    assert sells, "AAA never exited"
    assert min(sells) >= t["aaa_confirm"], "sold on the first, unconfirmed story"
    assert world.alpaca.position("AAA") is None


def test_nothing_trades_on_thanksgiving(week):
    world, *_ = week
    assert not _orders(world, day=date(2026, 11, 26))


def test_lost_order_reply_and_restart_produce_no_duplicate(week):
    world, runner, *_ = week
    assert runner.builds >= 2, "restart never happened (no DDD order?)"
    buys = [o for o in world.alpaca.orders_for("DDD") if o.side == "buy"]
    assert len(buys) == 1, [(o.status, o.qty) for o in buys]
    pos = world.alpaca.position("DDD")
    assert pos is not None and pos.qty == buys[0].filled_qty


def test_ledger_reconciles_to_the_broker_at_each_close(week):
    world, runner, *_ = week
    closes = world.e2e_shared["closes"]
    assert [c.session for c in closes] == ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27"]
    bad = [c for c in closes if not c.ok]
    assert not bad, bad


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="R-02: one explained stop fill in a 2-position book raises a CRITICAL mass_close alert")
def test_alerts_fire_for_real_failures_only(week):
    world, runner, *_ = week
    alerts = world.e2e_shared["alerts"]
    crit = [a for a in alerts if a[0] == "critical"]
    allowed = [a for a in crit if a[1].startswith("DDD")]
    assert crit == allowed, crit
    assert any(a[1] == "Account clean-up plan" for a in alerts)
    assert any(a[1] == "Bought BBB" for a in alerts) and any(a[1] == "Sold AAA" for a in alerts)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="R-01: the bot's own filled buy is booked as an 'imported' position with a warning alert")
def test_own_fills_are_not_reported_as_imports(week):
    world, *_ = week
    noisy = [a for a in world.e2e_shared["alerts"] if a[1] in ("Books: imported BBB", "Books: imported DDD")]
    assert not noisy, noisy


def test_option_spread_opened_atomically_and_closed_at_t_minus_2(week):
    pytest.importorskip("stock_agent.options_manager", reason="options manager not merged into revamp/build yet")
    world, runner, script, *_ = week
    legs = script.ccc_contracts()
    mleg = [b for _, b, _ in _orders(world) if b.get("order_class") == "mleg"]
    assert mleg, "no spread order"
    single = [b for _, b, _ in _orders(world) if b.get("symbol") in legs.values()]
    assert not single, "a spread leg was sent as a single order"
    close_at = script.times["ccc_t_minus_2_close"]
    for occ in legs.values():
        assert world.alpaca.position(occ) is None or world.clock.now() < close_at.astimezone(UTC)

