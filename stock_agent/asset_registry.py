"""Alpaca equity tradability, canonical sectors, and verified ticker aliases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from inspect import isawaitable
from typing import TYPE_CHECKING

import httpx

from stock_agent.config import Config
from stock_agent.risk_manager import CanonicalSector, classify_symbol

if TYPE_CHECKING:
    from stock_agent.state_store import StateStore

HealthReport = Callable[[str, str, bool, str | None], Awaitable[None] | None]
_CACHE_KEY = "market_data:equity_assets:v1"


async def _json_get(http, url, *, params=None, headers=None):
    if http is None:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            return await _json_get(client, url, params=params, headers=headers)
    method = getattr(http, "get_json", None) or http.get
    result = await method(url, params=params, headers=headers)
    if isinstance(result, httpx.Response):
        result.raise_for_status()
        return result.json()
    return result


async def _notify(callback, *args):
    if callback is not None:
        result = callback(*args)
        if isawaitable(result):
            await result


def _alpaca_headers(config) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
    }


def _symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    prefix, sep, suffix = symbol.rpartition("-")
    return f"{prefix}.{suffix}" if sep and prefix and len(suffix) == 1 and suffix.isalpha() else symbol


def _text(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _now_utc() -> datetime:
    return datetime.now(UTC)


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
        self.report, self.on_rename, self.now = report, on_rename, now or _now_utc
        self._active: dict[str, AssetMeta] = {}
        self._inactive: set[str] = set()
        self._sectors: dict[str, CanonicalSector] = {}
        self._aliases: dict[str, SymbolRenameEvent] = {}
        self._announced: set[str] = set()
        self._fetched_at: datetime | None = None
        self._load_cache()

    async def refresh(
        self, symbols: Iterable[str] = (), *, force: bool = False,
    ) -> tuple[SymbolRenameEvent, ...]:
        """Refresh the active universe and check requested inactive symbols."""
        now = self.now()
        if force or self._fetched_at is None or now - self._fetched_at >= timedelta(hours=24):
            if not await self._refresh_active(now):
                return ()
        requested = {_symbol(s) for s in symbols if s.strip()}
        missing = requested - self._active.keys() - self._inactive - self._aliases.keys()
        self._inactive.update(missing)
        if missing and not await self._find_renames(missing, now.date()):
            # Keep known inactive symbols unpollable, but retry unverified
            # corporate actions on the next refresh rather than caching failure.
            self._inactive.difference_update(missing)
            self._save_cache()
            return ()
        events = []
        for old, event in self._aliases.items():
            if old in self._announced:
                continue
            await _notify(self.on_rename, event)
            self._announced.add(old)
            events.append(event)
        self._save_cache()
        return tuple(events)

    def get_asset_meta(self, symbol: str) -> AssetMeta:
        """Return metadata, or non-tradable/unknown metadata without a verified record."""
        symbol = _symbol(symbol)
        meta = self._active.get(symbol, AssetMeta(symbol))
        resolved = self.resolve_symbol(symbol)
        if resolved and resolved != symbol:
            target = self._active[resolved]
            return replace(target, symbol=symbol, tradable=False, renamed_to=resolved,
                           canonical_sector=self._sectors.get(resolved, target.canonical_sector))
        return replace(meta, canonical_sector=self._sectors.get(symbol, classify_symbol(symbol).sector))

    def resolve_symbol(self, symbol: str) -> str | None:
        """Return the pollable broker symbol, or None for known inactive symbols."""
        symbol = _symbol(symbol)
        seen: set[str] = set()
        while symbol not in self._active and symbol in self._aliases:
            if symbol in seen:
                return None
            seen.add(symbol)
            symbol = self._aliases[symbol].new_symbol
        if symbol in self._active:
            return symbol
        return None if not symbol or self._fetched_at is not None or seen else symbol

    def set_sector(self, symbol: str, sector: str | None) -> CanonicalSector:
        """Attach provider sector metadata using the canonical risk vocabulary."""
        symbol = self.resolve_symbol(symbol) or _symbol(symbol)
        result = classify_symbol(symbol, sector).sector
        if result != CanonicalSector.UNKNOWN or symbol not in self._sectors:
            self._sectors[symbol] = result
            self._save_cache()
        return self._sectors[symbol]

    @staticmethod
    def provider_symbol(symbol: str, provider: str) -> str:
        """Render internal class-share syntax for a provider."""
        value = _symbol(symbol)
        return value.replace(".", "-") if provider.lower() in {"sec", "fmp"} else value

    async def _refresh_active(self, now: datetime) -> bool:
        try:
            rows = await _json_get(
                self.http, f"{self.config.ALPACA_BASE_URL.rstrip('/')}/v2/assets",
                params={"status": "active", "asset_class": "us_equity"},
                headers=_alpaca_headers(self.config),
            )
            if not isinstance(rows, list) or not rows:
                raise ValueError("invalid asset universe")
            ciks = await self._sec_ciks()
            active = {}
            for item in rows:
                symbol = _symbol(item["symbol"])
                if (not symbol or item.get("status") != "active"
                        or item.get("class", "us_equity") != "us_equity"):
                    continue
                active[symbol] = AssetMeta(
                    symbol, _text(item.get("id")), _text(item.get("name")),
                    _text(item.get("exchange")), item.get("tradable") is True,
                    ciks.get(symbol) or (self._active[symbol].cik if symbol in self._active else None),
                )
            if not active:
                raise ValueError("no active assets")
        except Exception:
            await _notify(self.report, "alpaca", "assets", False, "asset_universe_unavailable")
            return False
        self._active, self._fetched_at = active, now
        self._inactive.clear()
        await _notify(self.report, "alpaca", "assets", True, None)
        return True

    async def _sec_ciks(self) -> dict[str, str]:
        agent = getattr(self.config, "SEC_USER_AGENT", "")
        if not agent:
            return {}
        try:
            payload = await _json_get(
                self.http, "https://www.sec.gov/files/company_tickers_exchange.json",
                headers={"User-Agent": agent},
            )
            fields = payload["fields"]
            ticker, cik = fields.index("ticker"), fields.index("cik")
            return {_symbol(row[ticker]): str(int(row[cik])).zfill(10) for row in payload["data"]}
        except Exception:
            return {}

    async def _find_renames(self, missing: set[str], today: date) -> bool:
        try:
            for symbols in _chunks(sorted(missing)):
                rows = await self._name_change_rows(symbols, today)
                for item in sorted(rows, key=lambda row: row.get("process_date") or ""):
                    old, new = _symbol(item.get("old_symbol") or ""), _symbol(item.get("new_symbol") or "")
                    try:
                        day = date.fromisoformat(item.get("process_date", ""))
                    except (ValueError, TypeError):
                        continue
                    if old not in missing or old == new or new not in self._active or day > today:
                        continue
                    self._aliases[old] = SymbolRenameEvent(old, new, item.get("new_cusip"), day)
                    if old in self._sectors and new not in self._sectors:
                        self._sectors[new] = self._sectors[old]
        except Exception:
            await _notify(self.report, "alpaca", "corporate_actions", False, "corporate_actions_unavailable")
            return False
        await _notify(self.report, "alpaca", "corporate_actions", True, None)
        return True

    async def _name_change_rows(self, symbols: list[str], today: date) -> list[dict]:
        params = {
            "symbols": ",".join(symbols), "types": "name_change",
            "start": (today - timedelta(days=180)).isoformat(), "end": today.isoformat(),
            "limit": 1000,
        }
        output, tokens = [], set()
        while True:
            payload = await _json_get(
                self.http, f"{self.config.ALPACA_DATA_URL.rstrip('/')}/v1/corporate-actions",
                params=params, headers=_alpaca_headers(self.config),
            )
            output.extend(payload["corporate_actions"].get("name_changes", []))
            token = payload.get("next_page_token")
            if not token:
                return output
            if token in tokens or len(tokens) >= 20:
                raise ValueError("unbounded corporate actions paging")
            tokens.add(token)
            params["page_token"] = token

    def _load_cache(self) -> None:
        if self.store is None:
            return
        try:
            cache = self.store.kv_get(_CACHE_KEY, {})
            if not cache:
                return
            active = {row["symbol"]: AssetMeta(**{**row, "canonical_sector": CanonicalSector(
                row["canonical_sector"],
            )}) for row in cache["active"]}
            aliases = {
                row["old_symbol"]: SymbolRenameEvent(
                    row["old_symbol"], row["new_symbol"], row["cusip"], date.fromisoformat(row["process_date"]),
                )
                for row in cache["aliases"]
            }
            self._sectors = {key: CanonicalSector(value) for key, value in cache["sectors"].items()}
            self._fetched_at = datetime.fromisoformat(cache["fetched_at"]) if cache.get("fetched_at") else None
            self._active, self._aliases = active, aliases
            self._inactive, self._announced = set(cache["inactive"]), set(cache["announced"])
        except (ValueError, TypeError, KeyError):
            return

    def _save_cache(self) -> None:
        if self.store is not None:
            self.store.kv_set(_CACHE_KEY, {
                "fetched_at": self._fetched_at, "active": [asdict(meta) for meta in self._active.values()],
                "inactive": sorted(self._inactive), "announced": sorted(self._announced),
                "aliases": [asdict(event) for event in self._aliases.values()], "sectors": self._sectors,
            })


def _chunks(symbols: list[str], size: int = 100):
    for index in range(0, len(symbols), size):
        yield symbols[index:index + size]
