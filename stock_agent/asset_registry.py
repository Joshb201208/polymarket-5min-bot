"""Alpaca equity tradability, canonical sectors, and verified ticker aliases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from stock_agent.config import Config
from stock_agent.risk_manager import CanonicalSector

if TYPE_CHECKING:
    from stock_agent.state_store import StateStore

HealthReport = Callable[[str, str, bool, str | None], Awaitable[None] | None]


@dataclass(frozen=True)
class AssetMeta:
    symbol: str
    asset_id: str | None = None
    company_name: str | None = None
    exchange: str | None = None
    tradable: bool = False
    cik: str | None = None
    canonical_sector: CanonicalSector = CanonicalSector.UNKNOWN
    renamed_to: str | None = None


@dataclass(frozen=True)
class SymbolRenameEvent:
    old_symbol: str
    new_symbol: str
    cusip: str | None
    process_date: date


class AssetRegistry:
    def __init__(
        self, http=None, *, config=Config, store: StateStore | None = None,
        report: HealthReport | None = None,
        on_rename: Callable[[SymbolRenameEvent], Awaitable[None] | None] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        """Inject a shared async HTTP client; the registry does not own it."""
        self.http, self.config, self.store = http, config, store
        self.report, self.on_rename, self.now = report, on_rename, now

    async def refresh(
        self, symbols: Iterable[str] = (), *, force: bool = False,
    ) -> tuple[SymbolRenameEvent, ...]:
        """Refresh the active universe and check requested inactive symbols."""
        raise NotImplementedError

    def get_asset_meta(self, symbol: str) -> AssetMeta:
        """Return metadata, or non-tradable/unknown metadata without a verified record."""
        raise NotImplementedError

    def resolve_symbol(self, symbol: str) -> str | None:
        """Return the pollable broker symbol, or None for known inactive symbols."""
        raise NotImplementedError

    def set_sector(self, symbol: str, sector: str | None) -> CanonicalSector:
        """Attach provider sector metadata using the canonical risk vocabulary."""
        raise NotImplementedError

    @staticmethod
    def provider_symbol(symbol: str, provider: str) -> str:
        """Render internal class-share syntax for a provider."""
        raise NotImplementedError
