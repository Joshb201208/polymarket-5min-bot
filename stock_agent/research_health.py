"""Read-only evidence eligibility, independent of market/risk/order authorization."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from stock_agent.ledger import StaleBook
from stock_agent.research_schemas import utc_datetime

InputCheck = Callable[[str, datetime], bool]


class ResearchInputHealth:
    def __init__(self, ledger, llm, *, quote_ok: InputCheck, news_ok: InputCheck, research_store=None):
        self.ledger = ledger
        self.llm = llm
        self.quote_ok = quote_ok
        self.news_ok = news_ok
        self.research_store = research_store

    def research_entry_blockers(self, symbol: str, now: datetime) -> tuple[str, ...]:
        blockers = []
        try:
            book = self.ledger.snapshot()
            if self.ledger.trading_block() or symbol in book.blocked_symbols:
                blockers.append("broker_book_blocked")
        except StaleBook:
            blockers.append("broker_book_unavailable")
        if not self.quote_ok(symbol, now):
            blockers.append("quote_stale_or_unavailable")
        if not self.news_ok(symbol, now):
            blockers.append("news_stale_or_unavailable")
        if not self.llm.is_healthy():
            blockers.append("llm_unavailable")
        if self.research_store is not None and self.research_store.has_unqueued_exit(symbol, now):
            blockers.append("confirmed_exit_pending")
        return tuple(blockers)

    def extra_conviction_floor(self, now: datetime) -> int:
        if self.research_store is not None:
            market = self.research_store.store.kv_get("research_market_regime") or {}
            stamp = utc_datetime(market.get("as_of"))
            if stamp is not None and timedelta() <= now - stamp <= timedelta(days=1) and market.get("regime") == "cautious":
                return 8
        return 7
