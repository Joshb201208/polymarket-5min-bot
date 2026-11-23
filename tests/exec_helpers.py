"""Test wiring for ExecutionEngine on the fake world (no network).

Uses the real StateStore, Ledger, reconcile, RiskManager and MarketCalendar.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from stock_agent.broker import AlpacaClient
from stock_agent.journal import Journal
from stock_agent.ledger import Ledger, NewIntent
from stock_agent.market_calendar import MarketCalendar
from stock_agent.risk_manager import RiskManager
from stock_agent.config import Config
from stock_agent.execution import ExecSettings, ExecutionEngine


@dataclass
class Q:
    price: float
    bid: float | None
    ask: float | None
    as_of: datetime
    source: str = "fake"


@dataclass
class Batch:
    quotes: dict
    missing: dict


class FakePrices:
    def __init__(self, world):
        self.world = world

    async def get_quotes(self, symbols, *, purpose, max_age_s=None):
        out, missing = {}, {}
        for s in symbols:
            q = self.world.alpaca.quotes.get(s)
            if q is None:
                missing[s] = "no quote"
            else:
                out[s] = Q(q.last, q.bid, q.ask, q.at)
        return Batch(out, missing)


def make_engine(world, tmp_path, monkeypatch, *, normalised=True, options=None, settings=None):
    from stock_agent.state_store import StateStore

    clock = world.clock
    journal = Journal(tmp_path / "journal")
    store = StateStore(tmp_path / "state.db", journal, clock=clock.now)
    store.open()
    broker = AlpacaClient("https://paper-api.alpaca.markets", "k", "s")
    ledger = Ledger(store, journal, broker=broker, clock=clock.now)

    cal = MarketCalendar(broker, tmp_path / "cal.json", now=clock.now)
    alerts = []

    def alert(sev, title, body, key=None):
        alerts.append((sev, title, body))

    eng = ExecutionEngine(broker, ledger, RiskManager(Config), cal, FakePrices(world), alert=alert,
                          options=options, now=clock.now, settings=settings or ExecSettings())
    eng.alerts = alerts
    if normalised:
        store.kv_set(ExecutionEngine.NORMALISE_KEY, {"state": "done"})
    return eng


def enqueue(eng, symbol, side, *, ref=None, key=None, **meta):
    snap_meta = eng.ledger.open_position_meta(symbol)
    ids = eng.ledger.enqueue_intents([NewIntent(
        symbol=symbol, side=side, dedupe_key=key or f"t:{symbol}:{side}:{uuid.uuid4().hex[:6]}",
        reference_price=None if ref is None else Decimal(str(ref)),
        position_id=snap_meta.position_id if (snap_meta and side == "sell") else None,
        kind="cover" if meta.pop("cover", False) else "equity",
        meta={"effective_conviction": 8, "raw_conviction": 8, "sector": "Technology", **meta})])
    return eng.ledger.get_intent(ids[0])


def wired_scheduler(world, tmp_path, monkeypatch, **kw):
    """Today's StockAgentScheduler with the new services injected (engine, ledger, ...)."""
    from types import SimpleNamespace

    from stock_agent.scheduler import StockAgentScheduler

    eng = make_engine(world, tmp_path, monkeypatch, **kw)
    services = SimpleNamespace(broker=eng.broker, ledger=eng.ledger, risk=eng.risk, calendar=eng.calendar,
                               prices=eng.prices, engine=eng)
    return StockAgentScheduler(services=services), eng
