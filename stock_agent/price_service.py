"""Timestamped equity quotes and completed split-adjusted daily bars."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from stock_agent.config import Config

if TYPE_CHECKING:
    from stock_agent.asset_registry import AssetRegistry, HealthReport
    from stock_agent.state_store import StateStore

QuotePurpose = Literal["order_sizing", "stop_check", "valuation", "brief"]


@dataclass(frozen=True)
class Quote:
    """Decimal prices; change_pct is in percentage points, not a ratio."""

    symbol: str
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    prev_close: Decimal | None
    change_pct: Decimal | None
    as_of: datetime
    retrieved_at: datetime
    source: str
    is_market_hours: bool


@dataclass(frozen=True)
class PriceBatchResult:
    quotes: dict[str, Quote]
    missing: dict[str, str]


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None = None
    source: str = "alpaca_sip"


class PriceService:
    def __init__(
        self, http=None, *, config=Config, store: StateStore | None = None,
        calendar=None, assets: AssetRegistry | None = None,
        report: HealthReport | None = None, now: Callable[[], datetime] | None = None,
    ):
        """Inject a shared async HTTP client; the service does not own it."""
        self.http, self.config, self.store = http, config, store
        self.calendar, self.assets, self.report, self.now = calendar, assets, report, now

    async def get_quotes(
        self, symbols: Iterable[str], *, purpose: QuotePurpose = "brief", max_age_s: int | None = None,
    ) -> PriceBatchResult:
        """Return one fresh quote or one missing reason for each requested symbol."""
        raise NotImplementedError

    async def get_daily_bars(
        self, symbols: Iterable[str], *, sessions: int = 60,
        start_date: date | None = None, end_date: date | None = None,
    ) -> dict[str, list[DailyBar]]:
        """Return completed split-adjusted sessions, oldest first."""
        raise NotImplementedError
