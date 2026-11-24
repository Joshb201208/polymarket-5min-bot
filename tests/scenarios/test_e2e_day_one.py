"""A14 day one on the real StockAgentScheduler, minute by minute.

A synthetic mixed long/short account needs first-run clean-up, Perplexity answers 401
(out of credit), FMP is on the free tier (402), the other providers work and
the regime is cautious. A14: at least one buy is proposed, sized, ordered and
filled on the first session, and the daily summary shows the entry funnel.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pytest

from stock_agent.config import Config
from tests.fakes import ET
from tests.scenarios import ScenarioRunner

_START = datetime(2026, 11, 23, 7, 0, tzinfo=ET)
_RESEARCH = ("AAPL", "MSFT", "JPM")
_BOOK = {"KO": (150, 70.0), "PG": (80, 160.0), "XOM": (110, 115.0), "UNH": (25, 500.0), "HD": (30, 380.0)}


def _shape_account(world):
    a = world.alpaca
    a.cash = 40_000
    for sym, (qty, px) in _BOOK.items():         # each long under MAX_POSITION_PCT
        world.price(sym, px, spread=0.02)
        a.seed_position(sym, qty, px * 0.95)
    world.price("SHRT", 50.0, spread=0.02)
    a.seed_position("SHRT", -200, 52.0)          # covered by the first-run clean-up
    for sym in _RESEARCH:
        world.price(sym, 100.0, spread=0.02)
    _cautious_macro(world)


def _cautious_macro(world):
    """210 sessions of ETF bars scoring -4 (cautious, macro_monitor.py:340):
    SPY/QQQ under their 50d and 200d averages (-2), SPY ~8% under its 90-day
    high (-1), VIXY ~11% over its 50d average (-1), TLT/USO/UUP flat (0)."""
    falling = [110.0] * 120 + [round(110 - 9 * i / 89, 2) for i in range(90)]
    for sym in ("SPY", "QQQ"):
        world.alpaca.seed_daily_bars(sym, falling, end=_START.date())
        world.price(sym, 100.0, spread=0.02)
    world.alpaca.seed_daily_bars("VIXY", [20.0] * 190 + [24.0] * 20, end=_START.date())
    world.price("VIXY", 24.0, spread=0.02)
    for sym in ("TLT", "USO", "UUP"):
        world.alpaca.seed_daily_bars(sym, [100.0] * 210, end=_START.date())
        world.price(sym, 100.0, spread=0.02)


def _providers(world):
    world.perplexity.fail("/v1/agent", "status", status=401, body={"error": {"code": "insufficient_credits"}},
                          times=None)
    world.fmp.fail("/stable/profile", "status", status=402, times=None)
    world.finnhub.on("GET", "/api/v1/stock/metric",
                     {"metric": {"peTTM": 20, "grossMarginTTM": 50, "revenueGrowthTTMYoy": 10}})
    for sym in _RESEARCH:
        world.news([sym], f"{sym} raises quarterly guidance", summary="The issuer publishes new guidance.",
                   at=_START - timedelta(hours=2))

    def draft(body):
        text = body["messages"][0]["content"] if "messages" in body else body["input"]
        data = json.loads(text.split("INPUT_JSON:\n", 1)[1])
        sym = data["symbol"]
        story = next((e for e in data.get("evidence", []) if e.get("published_at")), None)
        return {
            "symbol": sym, "direction": "BUY", "conviction": 9 if sym == "AAPL" else 7,
            "summary": "The issuer raised its outlook.", "bull_case": "Improving demand.",
            "bear_case": "Demand may weaken.",
            "claims": [{"text": "The issuer raised guidance.", "evidence_ids": [story["evidence_id"]]}] if story else [],
            "catalysts": [], "risks": [], "thesis_break": "Next release contradicts the update.",
            "target_price": None, "thesis_change": "new", "horizon_days": 15,
        }

    world.openai.respond_with(draft)
    world.anthropic.respond_with(draft)


def _run_day_one(world, monkeypatch):
    monkeypatch.setattr(Config, "PRIORITY_SYMBOLS", _RESEARCH)
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "test-anthropic")
    _shape_account(world)
    _providers(world)
    runner = ScenarioRunner(world)
    runner.run_until(datetime.combine(_START.date(), time(17, 30), tzinfo=ET))
    return runner


def _daily_close(world):
    return [m for m in world.alerts_sent() if "Daily Close Summary" in json.dumps(m.json)]


@pytest.mark.world_start(_START)
def test_day_one_buys_with_perplexity_out_and_fmp_limited(world, monkeypatch):
    runner = _run_day_one(world, monkeypatch)
    sched, log = runner.agent, runner.summary()

    briefs = [m for m in world.alerts_sent() if "Macro Brief" in json.dumps(m.json)]
    assert briefs and "Cautious" in json.dumps(briefs[0].json), log   # floor 8 applies (A14)
    assert sched.engine.normalised, log
    assert "SHRT" not in world.alpaca.positions                     # clean-up covered the short
    buys = [r for r in world.orders_sent() if r.method == "POST" and r.status == 200
            and r.json.get("side") == "buy" and r.json.get("symbol") in _RESEARCH]
    assert buys, ("no research buy was ordered on day one", log, _funnel_events(sched))
    assert {r.json["symbol"] for r in buys} == {"AAPL"}             # conviction 7 stays below the cautious floor
    filled = [s for s in _RESEARCH if s in world.alpaca.positions and world.alpaca.positions[s].qty > 0]
    assert filled == ["AAPL"], "a buy was ordered but nothing filled"
    assert world.alpaca.positions["AAPL"].qty * 100.0 <= 0.15 * world.alpaca.equity() + 1e-6
    assert world.net.requests(service="perplexity", path="/v1/agent")[0].status == 401
    assert _daily_close(world), "no daily close summary was delivered"


@pytest.mark.xfail(strict=True, reason="A14d: Daily Close Summary has no entry funnel yet; the stages are only "
                   "journalled (research_pipeline.py research.entry_funnel, execution.py:543 execution.buy_funnel). "
                   "Owner: alerts_reporting")
@pytest.mark.world_start(_START)
def test_day_one_daily_summary_shows_the_entry_funnel(world, monkeypatch):
    _run_day_one(world, monkeypatch)
    close = _daily_close(world)
    assert close
    text = json.dumps(close[-1].json).lower()
    for stage in ("screened", "thesis", "conviction", "risk", "ordered", "filled"):
        assert stage in text, stage


def _funnel_events(sched):
    try:
        return list(sched.journal.read(kinds={"research.entry_funnel"}))[-3:]
    except Exception as e:  # noqa: BLE001 - diagnostics only
        return repr(e)
