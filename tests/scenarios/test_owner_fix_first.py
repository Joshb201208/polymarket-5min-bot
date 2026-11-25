"""FIX FIRST on 16483e0 (plan_coverage_reviewer; lead ruling F1-F5 plus stock-first): the reviewer's
concrete scenarios driven through the real StockAgentScheduler -> ResearchPipeline -> ResearchStore /
Ledger -> OptionsManager wiring on simulated time. Harness: tests/scenarios/owner_support.py.

Timeline (ET) unless a test says otherwise: owner directive NVDA added Tue 24 Nov 2026; Wed 25 Nov 08:00
prescan (quote 10 minutes old, so the owner BUY is staged as a handoff plus an owner option idea); events
at 08:30; real scheduler ticks from the 09:35 handoff to 09:50. Prices are synthetic round numbers.
"""

import asyncio
import json
from datetime import date, timedelta

import pytest

from stock_agent.config import Config
from stock_agent.options_book import StructState

from tests.scenarios.owner_support import ADDED, OwnerHarness, et, owner_item

START = et(2026, 11, 25, 7, 0)
SESSION = date(2026, 11, 25)
pending = pytest.mark.xfail(strict=False, reason="FIX FIRST fix pending; drop once the owner commits land")


def drive(h, scenario):
    """Run `scenario()` (setup, steps and assertions) on the live scheduler, then shut it down."""

    async def wrapper():
        try:
            await scenario()
        finally:
            await h.stop()

    asyncio.run(wrapper())


async def stage(h, day: int = 25, *, start: bool = True, option_ideas: int = 1):
    """Start, one dated issuer story, a 10-minute-old quote, then the 08:00 prescan stages the owner BUY."""
    if start:
        await h.start()
    h.headline("NVIDIA presents at an industry conference", minutes_ago=30)
    await h.advance(day, 7, 50)
    h.fresh_quote()
    await h.advance(day, 8, 0)
    row = await h.run_research("prescan")
    assert row.detail["entry_funnel"].get("handoff_staged") == 1, row.detail
    assert row.detail["entry_funnel"]["option_ideas"] == option_ideas, row.detail
    return row


async def open_session(h, day: int = 25, *, at=(9, 35), until=(9, 50)):
    await h.advance(day, *at)
    h.fresh_quote()
    # In session the market quotes continuously; the base harness freezes quote time for its
    # pre-open age checks, which would let a spread ladder's own fill wait stale the quote.
    h.world.alpaca.behaviour.quotes_stay_fresh_in_session = True
    h.requote_chain(100.0)
    minutes = (until[0] * 60 + until[1]) - (at[0] * 60 + at[1])
    await h.execute(2 * minutes)
    await h.scheduler.alerts.flush()  # the heartbeat loop delivers queued alerts in production


def handoff_outcomes(h):
    out = {}
    for row in h.journal("research.entry_funnel"):
        if row.get("job") == "entry_handoff":
            for code, n in row.get("handoff_outcomes", {}).items():
                out[code] = out.get(code, 0) + n
    return out


def lanes_unspent(h, added=ADDED):
    ledger = h.scheduler.ledger
    return (not ledger.owner_directive_used(h.symbol, added)
            and not ledger.owner_directive_used(h.symbol, added, lane="option")
            and not ledger.owner_directive_vetoed(h.symbol, added))


def held_for_session(h, session=SESSION):
    probe = getattr(h.scheduler.ledger, "owner_directive_held", None)
    return probe is not None and probe(h.symbol, session)


def owner_alerts(h, needle=""):
    """Owner-directive alerts as delivered on Discord (Telegram mirrors some; count one channel)."""
    return [t for t in h.world.discord.texts()
            if "owner directive" in t.lower() and h.symbol in t and needle.lower() in t.lower()]


def rearm_alerts(h):
    return [t for t in h.world.discord.texts() if "re-arm" in t.lower() and h.symbol in t]


def ok(requests):
    return [r for r in requests if r.status == 200]


def reject(predicate, status=403, message="synthetic broker rejection"):
    """A definitive broker refusal (nothing placed) for requests matching predicate(json)."""
    import httpx

    def hook(request):
        if request.method == "POST" and request.url.path == "/v2/orders":
            body = json.loads(request.content or b"{}")
            if predicate(body):
                return httpx.Response(status, json={"code": 40310000, "message": message})
        return None

    return hook


def structures(h):
    return [x for x in h.scheduler.options.structures if x.underlying == h.symbol]


def opened(h):
    """Owner structures that reached OPEN; the ladder may send several rungs for one structure."""
    return [x for x in structures(h) if x.st.value == "open"]


def is_mleg(body):
    return body.get("order_class") == "mleg"


# ---------------------------------------------------------------- stock first (lead ruling on the 09:35 race)
@pending
@pytest.mark.world_start(START)
def test_stock_lane_goes_first_then_the_option_lane_opens(world, monkeypatch, tmp_path):
    h = OwnerHarness(world, monkeypatch).install(tmp_path=tmp_path)

    async def scenario():
        await stage(h)
        await open_session(h)
        stock, options = ok(h.buys()), ok(h.option_orders())
        assert stock and options, (h.intent_states(), h.option_orders())
        sent = h.world.net.recorded
        assert sent.index(stock[0]) < sent.index(options[0]), "the owner option waits for the stock attempt"
        assert h.scheduler.ledger.list_intents(symbol=h.symbol)[0].status.value == "filled", h.intent_states()
        assert h.scheduler.ledger.owner_directive_used(h.symbol, ADDED)
        assert h.scheduler.ledger.owner_directive_used(h.symbol, ADDED, lane="option")

    drive(h, scenario)


@pending
@pytest.mark.world_start(START)
def test_day1_stock_refused_and_spread_opened_day2_stock_still_enters(world, monkeypatch, tmp_path):
    """Day 1 the broker refuses the stock buy, so the option lane proceeds and opens the spread. Day 2 the
    same generation's own open spread must not make the owner stock buy a scope mismatch."""
    h = OwnerHarness(world, monkeypatch).install(tmp_path=tmp_path)
    refusing = {"on": True}
    stock_reject = reject(lambda b: refusing["on"] and not is_mleg(b) and b.get("side") == "buy"
                          and b.get("symbol") == h.symbol)
    h.intercept_broker(stock_reject)

    async def scenario():
        await stage(h)
        await open_session(h)
        assert not ok(h.buys()) and h.buys(), h.intent_states()
        assert len(opened(h)) == 1, [(x.st, x.notes) for x in structures(h)]
        refusing["on"] = False
        await stage(h, day=27, start=False, option_ideas=0)  # the option lane is already spent
        await open_session(h, day=27, until=(9, 45))
        assert len(ok(h.buys())) == 1, h.intent_states()
        assert len(opened(h)) == 1 and len(structures(h)) == 1, "one structure per generation"
        assert h.scheduler.ledger.owner_directive_used(h.symbol, ADDED)

    drive(h, scenario)


# ---------------------------------------------------------------- 1. headline between scan and handoff (F1a, F1c)
BLOCKING = ("NVIDIA cuts Q4 revenue guidance", "NVIDIA delays Rubin shipments", "NVIDIA CEO Jensen Huang steps down")
NOISE = (
    (("DELL", "NVDA"), "Dell reports Q3 earnings; AI server backlog rises"),
    (("NVDA",), "US weighs broad tariff hike on imports"),
    (("NVDA",), "S&P 500 rises as Nvidia leads chip stocks higher"),
)


@pending
@pytest.mark.parametrize("headline", BLOCKING)
@pytest.mark.world_start(START)
def test_single_issuer_catalyst_between_scan_and_handoff_blocks_both_lanes(world, monkeypatch, tmp_path, headline):
    h = OwnerHarness(world, monkeypatch).install(tmp_path=tmp_path)

    async def scenario():
        await stage(h)
        await h.advance(25, 8, 30)
        h.headline(headline, minutes_ago=0)
        await open_session(h)
        assert handoff_outcomes(h).get("handoff_owner_news_deferred") == 1, handoff_outcomes(h)
        assert not h.buys() and not h.option_orders(), (h.buys(), h.option_orders())
        assert held_for_session(h)
        assert lanes_unspent(h)

    drive(h, scenario)


@pytest.mark.parametrize(("symbols", "headline"), NOISE)
@pytest.mark.world_start(START)
def test_24_nov_noise_does_not_defer_the_owner_handoff(world, monkeypatch, tmp_path, symbols, headline):
    h = OwnerHarness(world, monkeypatch).install(tmp_path=tmp_path)

    async def scenario():
        await stage(h)
        await h.advance(25, 8, 30)
        h.headline(headline, minutes_ago=0, symbols=list(symbols))
        await open_session(h)
        assert "handoff_owner_news_deferred" not in handoff_outcomes(h), handoff_outcomes(h)
        assert handoff_outcomes(h).get("handoff_promoted") == 1, handoff_outcomes(h)
        assert not owner_alerts(h, "handoff deferred") and not held_for_session(h)

    drive(h, scenario)


# ---------------------------------------------------------------- 2. 8-K deferral, then the options lane (F1c)
@pending
@pytest.mark.world_start(START)
def test_8k_deferral_also_stops_the_owner_option_lane(world, monkeypatch, tmp_path):
    h = OwnerHarness(world, monkeypatch).install(tmp_path=tmp_path)

    async def scenario():
        await stage(h)
        await h.advance(25, 8, 30)
        h.filing("8-K", items="2.02", accepted=et(2026, 11, 25, 8, 30), seq=123)
        await open_session(h)
        assert handoff_outcomes(h).get("handoff_owner_news_deferred") == 1, handoff_outcomes(h)
        assert not h.buys() and not h.option_orders(), h.option_orders()
        assert held_for_session(h)
        assert lanes_unspent(h)
        assert len(owner_alerts(h, "handoff deferred")) == 1

    drive(h, scenario)


# ---------------------------------------------------------------- 3. live review while staged (F1b, F1c)
LIVE_STORY = "US weighs new AI chip export curbs"  # material (score 7) but not an issuer-led NVDA event
@pending
@pytest.mark.parametrize("verdict", ["AVOID", "BEARISH"])
@pytest.mark.world_start(START)
def test_live_bearish_review_while_staged_holds_owner_idea_for_the_session(world, monkeypatch, tmp_path, verdict):
    h = OwnerHarness(world, monkeypatch, ordinary=verdict).install(tmp_path=tmp_path)

    async def scenario():
        await stage(h)
        # The live news lane runs from the 09:30 open; the staged handoff waits until 09:35.
        await h.advance(25, 9, 30)
        h.headline(LIVE_STORY, minutes_ago=0)
        await open_session(h, at=(9, 30))
        live = [p for p in h.prompts if p[1] == "ordinary" and p[2] == verdict]
        assert live and live[0][0] < et(2026, 11, 25, 9, 35), h.prompts
        assert not h.buys() and not h.option_orders(), (h.intent_states(), h.option_orders())
        assert held_for_session(h)
        assert lanes_unspent(h), "a session hold consumes nothing and does not latch"
        assert len(owner_alerts(h)) == 1, h.world.discord.texts()
        # The next session's prescan reviews the owner idea afresh.
        h.ordinary = "HOLD"
        await h.advance(27, 7, 50)
        h.fresh_quote()
        await h.advance(27, 8, 0)
        await h.run_research("prescan")
        assert len(h.owner_prompts()) == 2, h.prompts
        assert not held_for_session(h, date(2026, 11, 27))

    drive(h, scenario)


@pytest.mark.parametrize("verdict", ["HOLD", "INSUFFICIENT_EVIDENCE"])
@pytest.mark.world_start(START)
def test_live_hold_or_insufficient_review_keeps_the_staged_owner_thesis(world, monkeypatch, tmp_path, verdict):
    h = OwnerHarness(world, monkeypatch, ordinary=verdict).install(tmp_path=tmp_path)

    async def scenario():
        await stage(h)
        thesis = h.scheduler.ledger.current_thesis(h.symbol).thesis_id
        await h.advance(25, 9, 30)
        h.headline(LIVE_STORY, minutes_ago=0)
        await h.execute(9)  # live review before the 09:35 handoff
        assert [p for p in h.prompts if p[1] == "ordinary"], h.prompts
        assert h.scheduler.ledger.current_thesis(h.symbol).thesis_id == thesis
        await open_session(h)
        assert handoff_outcomes(h).get("handoff_promoted") == 1, handoff_outcomes(h)
        assert not held_for_session(h)

    drive(h, scenario)


# ---------------------------------------------------------------- 4. day-1 option zero fill, day-2 retry (F2)
@pending
@pytest.mark.world_start(START)
def test_day1_option_zero_fill_releases_the_shot_and_day2_opens_the_spread(world, monkeypatch, tmp_path):
    h = OwnerHarness(world, monkeypatch, ordinary="HOLD").install(tmp_path=tmp_path)
    refusing = {"on": True}
    h.intercept_broker(reject(lambda b: refusing["on"] and is_mleg(b)))

    async def scenario():
        await stage(h)
        await open_session(h)
        assert len(ok(h.buys())) == 1, h.intent_states()
        assert h.option_orders() and not ok(h.option_orders())
        ledger = h.scheduler.ledger
        assert not ledger.owner_directive_used(h.symbol, ADDED, lane="option"), "zero-fill void releases eagerly"
        assert [(x.st, x.zero_fill_confirmed) for x in structures(h)] == [(StructState.VOID, True)]
        refusing["on"] = False
        await h.advance(27, 7, 50)
        h.fresh_quote()
        await h.advance(27, 8, 0)  # NVDA is held now; the held review is HOLD / unchanged
        row = await h.run_job("prescan")
        assert row.detail["entry_funnel"]["option_ideas"] == 1, row.detail
        await open_session(h, day=27, until=(10, 15))
        assert len(opened(h)) == 1, [(x.st, x.notes) for x in structures(h)]
        assert ledger.owner_directive_used(h.symbol, ADDED, lane="option")

    drive(h, scenario)


# ---------------------------------------------------------------- 5. uncertain option submit, reconcile, alert (F2)
@pending
@pytest.mark.world_start(START)
def test_uncertain_option_submit_voided_by_reconcile_alerts_once_and_stays_spent(world, monkeypatch, tmp_path):
    import httpx

    h = OwnerHarness(world, monkeypatch, ordinary="HOLD").install(tmp_path=tmp_path)
    lost = []

    def timeout(request):
        if request.method == "POST" and request.url.path == "/v2/orders" and is_mleg(json.loads(request.content)):
            lost.append(request)
            raise httpx.ReadTimeout("synthetic timeout; nothing reached the broker", request=request)
        return None

    h.intercept_broker(timeout)

    async def scenario():
        await stage(h)
        await open_session(h, until=(10, 30))  # several reconcile passes after the lost submit
        assert len(lost) == 1
        ledger = h.scheduler.ledger
        assert ledger.owner_directive_used(h.symbol, ADDED, lane="option")
        assert [(x.st, x.zero_fill_confirmed) for x in structures(h)] == [(StructState.VOID, False)]
        spent = rearm_alerts(h)
        assert len(spent) == 1, h.world.alert_texts()
        await h.advance(27, 7, 50)
        h.fresh_quote()
        await h.advance(27, 8, 0)
        await h.run_job("prescan")
        await open_session(h, day=27, until=(9, 45))
        assert len(lost) == 1 and not ok(h.option_orders()), "no second structure unless re-armed"
        assert len(rearm_alerts(h)) == 1

    drive(h, scenario)


# ---------------------------------------------------------------- 6. directive edited mid-open (F3)
@pending
@pytest.mark.parametrize("edit", ["removed", "conviction_6"])
@pytest.mark.world_start(START)
def test_directive_edited_during_the_option_open_submits_nothing(world, monkeypatch, tmp_path, edit):
    h = OwnerHarness(world, monkeypatch, ordinary="HOLD").install(tmp_path=tmp_path)
    edited = []

    def edit_on_chain_read(request):
        if request.method == "GET" and "options" in request.url.path and not edited:
            edited.append(h.world.clock.now_et())
            h.edit_focus(*(() if edit == "removed" else (owner_item(owner_conviction=6),)))
        return None

    async def scenario():
        await stage(h)
        h.intercept_broker(edit_on_chain_read)  # after the scan: the edit lands inside the option open
        await open_session(h)
        assert edited, "the option lane never read the chain"
        assert not h.option_orders(), h.option_orders()
        assert not h.scheduler.ledger.owner_directive_used(h.symbol, ADDED, lane="option")

    drive(h, scenario)


# ---------------------------------------------------------------- 7. mixed-date veto (F5a, F5b)
@pytest.mark.parametrize("cites", [pytest.param("all", marks=pending), "oldest"])
@pytest.mark.world_start(START)
def test_pre_added_evidence_cannot_veto_and_does_not_block_other_entries(world, monkeypatch, tmp_path, cites):
    """NVDA owner review answers AVOID citing a 20 Nov 10-Q (before "added") with or without a 25 Nov item.
    The veto is invalid: NVDA is not latched, and AAPL's ordinary staged BUY still enters at 09:35."""
    h = OwnerHarness(world, monkeypatch, others=("AAPL",), veto_cites=cites,
                     owner="AVOID", ordinary=lambda body, data: "BUY" if data["symbol"] == "AAPL" else "INSUFFICIENT_EVIDENCE",
                     ).install(tmp_path=tmp_path)
    monkeypatch.setattr(Config, "UNIVERSE_SIZE", 2)

    async def scenario():
        h.filing("10-Q", accepted=et(2026, 11, 20, 7, 0), seq=77)
        await h.start()
        h.headline("NVIDIA presents at an industry conference", minutes_ago=30, symbols=["NVDA", "AAPL"])
        await h.advance(25, 7, 50)
        h.fresh_quote()
        await h.advance(25, 8, 0)
        row = await h.run_job("prescan")
        owner = h.owner_prompts()
        assert owner and len(owner[0][4]) >= 2, ("both dated items reach the owner review", owner)
        ledger = h.scheduler.ledger
        assert not ledger.owner_directive_vetoed(h.symbol, ADDED)
        assert h.scheduler.research.analyst.llm.is_healthy()
        await open_session(h)
        aapl = [r for r in ok(h.world.orders_sent()) if r.method == "POST" and r.json.get("symbol") == "AAPL"
                and r.json.get("side") == "buy"]
        assert len(aapl) == 1, (handoff_outcomes(h), h.scheduler.ledger.list_intents(symbol="AAPL"))
        assert not h.buys() and not h.option_orders()

    drive(h, scenario)


# ---------------------------------------------------------------- 8. grounding for daily names (F4a, F4b)
@pending
@pytest.mark.world_start(et(2026, 11, 25, 11, 0))
def test_organic_midweek_review_of_a_daily_focus_name_keeps_web_grounding(world, monkeypatch, tmp_path):
    h = OwnerHarness(world, monkeypatch, symbol="MSFT", ordinary="AVOID")
    h.install(tmp_path=tmp_path, focus=({"symbol": "MSFT", "cadence": "daily", "note": "Owner watchlist",
                                        "added": "2026-11-23", "expires": "2027-01-25"},))

    async def scenario():
        h.filing("10-Q", accepted=et(2026, 10, 11, 16, 0), symbol="MSFT", seq=45)
        await h.start()
        h.headline("Microsoft outlines cloud capacity plans", minutes_ago=30)
        await h.advance(25, 12, 15)
        await h.run_research("midweek")
        reviews = [p for p in h.prompts if p[1] == "ordinary"]
        assert reviews and all(p[3] for p in reviews), ("organic reviews stay grounded", h.prompts)

    drive(h, scenario)


@pending
@pytest.mark.world_start(START)
def test_owner_prescan_with_only_an_old_edgar_stub_is_grounded(world, monkeypatch, tmp_path):
    h = OwnerHarness(world, monkeypatch).install(tmp_path=tmp_path)

    async def scenario():
        h.filing("10-Q", accepted=et(2026, 10, 11, 16, 0), seq=45)
        await stage(h)
        assert h.owner_prompts() and all(p[3] for p in h.owner_prompts()), h.prompts

    drive(h, scenario)


# ---------------------------------------------------------------- 9. regular-hours quote rules (F4c)
@pytest.mark.world_start(et(2026, 11, 25, 11, 0))
def test_regular_hours_unheld_buy_with_a_ten_minute_quote_is_skipped_as_on_master(
        world, monkeypatch, tmp_path):
    h = OwnerHarness(world, monkeypatch, symbol="AMD", ordinary="BUY")
    h.install(tmp_path=tmp_path, focus=({"symbol": "AMD", "note": "Needs a dated order.",
                                        "added": "2026-11-23", "expires": "2026-12-09"},))

    async def scenario():
        await h.start()
        h.quotes_go_stale()
        await h.advance(25, 13, 20)
        h.fresh_quote()  # 10 minutes old at 13:30: inside 20 minutes, outside 90 seconds
        h.headline("AMD confirms a dated accelerator order", minutes_ago=5)
        await h.advance(25, 13, 30)
        row = await h.run_research("midweek")
        funnel = row.detail["entry_funnel"]
        # Same as origin/master 2d972aa on this fixture: the 90-second rule, no brief-quote anchor, no alert.
        assert funnel["rejected_by_reason"] == {"quote_stale_or_unavailable": 1}, (funnel, h.intent_states())
        assert not [i for i in h.intents() if i.reference_price is not None], h.intent_states()
        assert not [t for t in h.world.alert_texts() if "quote" in t.lower()], h.world.alert_texts()

    drive(h, scenario)


@pytest.mark.world_start(et(2026, 11, 25, 11, 0))
def test_regular_hours_exit_on_a_held_daily_name_carries_no_brief_quote(world, monkeypatch, tmp_path):
    h = OwnerHarness(world, monkeypatch, symbol="MSFT", ordinary="EXIT_PROPOSED")
    h.install(tmp_path=tmp_path, focus=({"symbol": "MSFT", "cadence": "daily", "note": "Owner watchlist",
                                        "added": "2026-11-23", "expires": "2027-01-25"},))
    world.alpaca.seed_position("MSFT", 10, 90.0)

    async def scenario():
        await h.start()
        h.quotes_go_stale()
        await h.advance(25, 13, 20)
        h.fresh_quote()
        h.headline("Microsoft discloses a cloud contract loss", minutes_ago=5)
        await h.advance(25, 13, 30)
        await h.run_research("midweek")
        exits = [i for i in h.intents() if i.side == "sell" or "exit" in str(i.origin).lower()
                 or "exit" in str(getattr(i, "kind", "")).lower()]
        assert all(i.reference_price is None for i in exits), [(i.origin, i.reference_price) for i in exits]

    drive(h, scenario)
