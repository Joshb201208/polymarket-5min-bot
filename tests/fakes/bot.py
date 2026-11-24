"""Builders for TODAY's bot models (stock_agent.models), for characterisation
tests and scenarios. When the builders replace these models, adapt here."""

from __future__ import annotations

from datetime import datetime, timezone


def thesis(symbol: str, *, direction: str = "BUY", conviction: int = 8, target: float | None = None):
    from stock_agent.models import Thesis

    return Thesis(
        symbol=symbol, direction=direction, conviction=conviction, summary=f"Test thesis for {symbol}",
        bull_case="b", bear_case="b", catalysts=[], risks=[], target_price=target,
        generated_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
    )


def buy_signal(symbol: str, *, conviction: int = 8, entry_price: float | None = None):
    from stock_agent.models import Signal

    return Signal(
        symbol=symbol, action="BUY", conviction=conviction, thesis=thesis(symbol, conviction=conviction),
        entry_price=entry_price, position_size_pct=0.07, generated_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
    )


def position(symbol: str, shares: int, entry: float, *, stop: float | None = None, sector: str = "Technology",
             price: float | None = None):
    from stock_agent.models import Position

    px = price if price is not None else entry
    when = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    return Position(
        symbol=symbol, shares=shares, entry_price=entry, entry_date=when, current_price=px,
        market_value=px * shares, stop_loss=stop if stop is not None else round(entry * 0.93, 2),
        thesis=thesis(symbol), sector=sector, last_updated=when,
    )
