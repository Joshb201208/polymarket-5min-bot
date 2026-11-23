"""Small helpers for state_ledger tests: a dict-level broker stub and a ledger factory.

The stub returns Alpaca-shaped JSON dicts (strings for numbers), the same
shapes the real API and tests/fakes/alpaca.py produce.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from stock_agent.journal import Journal
from stock_agent.ledger import Ledger
from stock_agent.state_store import StateStore


class StubBroker:
    def __init__(self, cash: float = 100000.0):
        self.cash = cash
        self.positions: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.activities: list[dict] = []
        self.fail: set[str] = set()      # method names that raise
        self.calls: list[str] = []
        self._aid = 0
        self.positions_replies: list[list[dict]] = []   # scripted replies for get_positions

    def pos(self, symbol: str, qty: float, avg: float, price: float | None = None, *,
            asset_class: str | None = None, mult: int = 1, lastday: float | None = None) -> None:
        price = avg if price is None else price
        ac = asset_class or ("us_option" if len(symbol) > 10 else "us_equity")
        m = 100 if ac == "us_option" else mult
        self.positions[symbol] = {
            "symbol": symbol, "asset_class": ac, "qty": str(qty), "qty_available": str(qty),
            "avg_entry_price": str(avg), "current_price": None if price is None else str(price),
            "market_value": str(qty * (price or 0) * m), "lastday_price": str(lastday or price or 0),
            "side": "short" if qty < 0 else "long", "asset_id": f"asset-{symbol}",
        }

    def act(self, activity_type: str, symbol: str | None = None, ts: str = "2026-11-02T15:00:00Z",
            **fields) -> dict:
        self._aid += 1
        a = {"id": f"{ts[:10].replace('-', '')}000000000::{self._aid:08d}", "activity_type": activity_type,
             "symbol": symbol, **fields}
        if activity_type == "FILL":
            a["transaction_time"] = ts
        else:
            a["date"] = ts[:10]
        self.activities.append(a)
        return a

    def fill(self, symbol: str, side: str, qty: float, price: float, ts: str = "2026-11-02T15:00:00Z") -> dict:
        return self.act("FILL", symbol, ts, side=side, qty=str(qty), price=str(price), order_id=f"o{self._aid}")

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} failed (stub)")

    def equity(self) -> float:
        return self.cash + sum(float(p["market_value"]) for p in self.positions.values())

    async def get_account(self):
        self._check("get_account")
        long_mv = sum(float(p["market_value"]) for p in self.positions.values()
                      if p["asset_class"] == "us_equity" and float(p["qty"]) > 0)
        short_mv = sum(float(p["market_value"]) for p in self.positions.values()
                       if p["asset_class"] == "us_equity" and float(p["qty"]) < 0)
        return {"equity": str(self.equity()), "cash": str(self.cash), "last_equity": str(self.equity()),
                "long_market_value": str(long_mv), "short_market_value": str(short_mv),
                "buying_power": str(self.cash * 2), "shorting_enabled": True, "status": "ACTIVE"}

    async def get_positions(self):
        self._check("get_positions")
        if self.positions_replies:
            return self.positions_replies.pop(0)
        return [dict(p) for p in self.positions.values()]

    async def list_orders(self, status="open", **kw):
        self._check("list_orders")
        return list(self.orders)

    async def get_activities(self, activity_types=None, *, after=None, page_size=100, max_pages=20):
        self._check("get_activities")
        return list(reversed(self.activities))


class Clock:
    def __init__(self, t: datetime | None = None):
        self.t = t or datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t


def make_ledger(tmp: Path, broker=None, clock=None, alerts: list | None = None):
    clock = clock or Clock()
    j = Journal(tmp / "journal", clock=clock)
    s = StateStore(tmp / "state.db", j, clock=clock)
    s.open()
    sink = alerts if alerts is not None else []
    led = Ledger(s, j, broker=broker, clock=clock,
                 alert=lambda sev, title, body, key=None: sink.append((sev, title, body, key)))
    return led


def run(coro):
    return asyncio.run(coro)
