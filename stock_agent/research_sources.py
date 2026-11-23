"""Adapt provider-health-aware services into the pipeline's point-in-time inputs."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from stock_agent.research_pipeline import NewsEvent, SecurityInputs, SourceUnavailable
from stock_agent.research_schemas import Evidence, utc_datetime

_NEWS_TTL = timedelta(minutes=20)
_QUOTE_TTL = timedelta(seconds=90)
_COMPANY_TTL = timedelta(hours=6)
_MARKET = ("SPY", "QQQ")
_NEWS_LIMIT = 5


class ResearchSources:
    def __init__(
        self, prices, news, *,
        list_universe: Callable[[], Awaitable[Sequence[str]]],
        fetch_company: Callable[[str], Awaitable[Any]] | None = None,
        fetch_releases: Callable[[tuple[str, ...], datetime], Awaitable[Sequence[Any]]] | None = None,
        asset_for: Callable[[str], Any] | None = None,
        health=None,
        fundamental_limit: int = 10,
    ):
        self.prices = prices
        self.news_service = news
        self.list_universe = list_universe
        self.fetch_company = fetch_company
        self.fetch_releases = fetch_releases
        self.asset_for = asset_for or (lambda _symbol: None)
        self.health = health
        self.fundamental_limit = max(0, fundamental_limit)
        self._universe: tuple[str, ...] = ()
        self._universe_at: datetime | None = None
        self._news_at: dict[str, datetime] = {}
        self._quote_at: dict[str, datetime] = {}
        self._companies: dict[str, tuple[datetime, Any]] = {}

    async def universe(self, now: datetime) -> tuple[str, ...]:
        if self._universe_at is None or now - self._universe_at >= timedelta(hours=12):
            candidates = await self.list_universe()
            if candidates:
                self._universe = tuple(dict.fromkeys(candidates))
                self._universe_at = now
            elif self._universe_at and now - self._universe_at > timedelta(days=2):
                self._universe = ()
        return self._universe

    def quote_ok(self, symbol: str, now: datetime) -> bool:
        last_quote = getattr(self.prices, "last_quote", None)
        if last_quote is not None:
            quote = last_quote(symbol)
            stamp = (
                utc_datetime(quote.as_of) if quote is not None and quote.symbol == symbol
                and _price(quote.price) is not None else None
            )
        else:
            stamp = self._quote_at.get(symbol)
        return stamp is not None and timedelta() <= now - stamp <= _QUOTE_TTL

    def news_ok(self, symbol: str, now: datetime) -> bool:
        stamp = self._news_at.get(symbol)
        if stamp is None or not timedelta() <= now - stamp <= _NEWS_TTL:
            return False
        return (
            self.health is None
            or "news_missing_or_stale" not in self.health.research_entry_blockers(
                symbol, now, news_max_age_s=_NEWS_TTL.total_seconds(),
            )
        )

    async def _poll(self, symbols, *, scope, include_market, lookback_hours, focus=(), universe=()):
        if not symbols and not include_market:
            return ()
        batch = await self.news_service.poll_news(
            symbols, scope=scope, include_market=include_market, lookback_hours=lookback_hours,
            focus=focus, universe=universe, mark_seen=False,
        )
        stamp = utc_datetime(batch.as_of)
        coverage_set = tuple(dict.fromkeys((*symbols, *focus, *universe)))
        if stamp is None or not batch.ok:
            for symbol in coverage_set:
                self._news_at.pop(symbol, None)
            raise SourceUnavailable("news poll failed")
        items = tuple(batch.items)
        covered = getattr(batch, "covered_symbols", None)
        if covered is None:
            covered = coverage_set if not batch.degraded else {s for item in items for s in item.symbols}
        covered = set(covered)
        for symbol in coverage_set:
            if symbol in covered:
                self._news_at[symbol] = stamp
            else:
                self._news_at.pop(symbol, None)
        return items

    async def news(self, symbols: tuple[str, ...], now: datetime) -> tuple[NewsEvent, ...]:
        return await self.live_news(symbols, (), (), now)

    async def live_news(self, held, focus, universe, now) -> tuple[NewsEvent, ...]:
        items = await self._poll(held, scope="live", include_market=True, lookback_hours=24,
                                 focus=focus, universe=universe)
        events = []
        allowed = set((*held, *focus, *universe))
        for item in items:
            score = _finite(item.materiality_score)
            if score is None or score < _NEWS_LIMIT:
                continue
            evidence = _news_evidence(item)
            if evidence is not None:
                events.extend(NewsEvent(symbol, item.story_id, evidence, round(score))
                              for symbol in item.symbols if symbol in allowed)
        return tuple(events)

    async def background(self, symbols: tuple[str, ...]) -> dict[str, tuple[Evidence, ...]]:
        try:
            items = await self._poll(symbols, scope="scheduled_research", include_market=False, lookback_hours=72)
        except SourceUnavailable:
            return {}  # Independent news health blocks entries; held research may continue.
        result: dict[str, list[Evidence]] = {symbol: [] for symbol in symbols}
        for item in items:
            evidence = _news_evidence(item)
            if evidence is not None:
                for symbol in item.symbols:
                    if symbol in result:
                        result[symbol].append(evidence)
        return {symbol: tuple(sorted(rows, key=lambda e: e.published_at or e.retrieved_at, reverse=True)[:6])
                for symbol, rows in result.items()}

    async def releases(self, symbols: tuple[str, ...], now: datetime) -> tuple[NewsEvent, ...]:
        if self.fetch_releases is None:
            return ()
        rows = await self.fetch_releases(symbols, now)
        events = []
        allowed = set(symbols)
        for row in rows:
            stamp = utc_datetime(row.published_at)
            if row.symbol not in allowed or not row.is_published or stamp is None or stamp > now:
                continue
            # A calendar row is not a released fact unless the upstream
            # service verified it. Never promote estimates alone.
            source_url = getattr(row, "url", None) or "https://finnhub.io/docs/api/earnings-calendar"
            earnings_id = getattr(row, "story_id", None) or f"earnings:{row.symbol}:{stamp.date().isoformat()}"
            actual = _finite(getattr(row, "eps_actual", None))
            estimate = _finite(getattr(row, "eps_estimate", None))
            note = "The source confirmed published results."
            if actual is not None:
                note += f" Reported EPS: {actual}."
            if estimate is not None:
                note += f" Prior EPS estimate: {estimate}."
            evidence = Evidence(
                evidence_id=earnings_id, title=f"{row.symbol} released results", url=source_url,
                publisher=str(getattr(row, "source", "Earnings feed")), published_at=stamp,
                retrieved_at=now, snippet=note,
            )
            events.append(NewsEvent(row.symbol, earnings_id, evidence, 100))
        await self.background(tuple(dict.fromkeys(event.symbol for event in events)))
        return tuple(events)

    async def _quotes(self, symbols: tuple[str, ...], *, purpose: str):
        if not symbols:
            return {}
        batch = await self.prices.get_quotes(list(symbols), purpose=purpose, max_age_s=90)
        quotes = batch.quotes
        for symbol in symbols:
            quote = quotes.get(symbol)
            stamp = utc_datetime(quote.as_of) if quote is not None else None
            if stamp is None or _price(quote.price) is None:
                self._quote_at.pop(symbol, None)
            else:
                self._quote_at[symbol] = stamp
        return quotes

    async def securities(self, symbols: tuple[str, ...], now: datetime) -> dict[str, SecurityInputs]:
        limiter = asyncio.Semaphore(5)
        cached = {
            symbol: stamp[1] for symbol in symbols if (stamp := self._companies.get(symbol)) is not None
            and timedelta() <= now - stamp[0] <= _COMPANY_TTL
        }
        # The order is held, focus, then universe. Company data is optional:
        # don't fire several provider requests per symbol across the whole list.
        allowed = set(tuple(dict.fromkeys(symbol for symbol in symbols if symbol not in cached))[
            :self.fundamental_limit
        ])

        async def company(symbol):
            if symbol in cached:
                return cached[symbol]
            if self.fetch_company is None or symbol not in allowed:
                return None
            async with limiter:
                try:
                    snapshot = await self.fetch_company(symbol)
                except (SourceUnavailable, TimeoutError):
                    return None
            if snapshot is not None:
                self._companies[symbol] = (now, snapshot)
            return snapshot

        fundamentals = await asyncio.gather(*(company(symbol) for symbol in symbols))
        quotes = await self._quotes(symbols, purpose="order_sizing")
        out = {}
        for symbol, snapshot in zip(symbols, fundamentals, strict=True):
            asset = self.asset_for(symbol)
            display_name = (
                getattr(snapshot, "company_name", None) or getattr(snapshot, "name", None)
                or getattr(asset, "company_name", None) or symbol
            )
            sector = (
                getattr(snapshot, "canonical_sector", None) or getattr(snapshot, "sector", None)
                or getattr(asset, "canonical_sector", None) or "Unknown"
            )
            decide = getattr(asset, "tradable", getattr(snapshot, "tradable", None))
            tradable = decide if type(decide) is bool else symbol in self._universe
            if getattr(asset, "renamed_to", None):
                tradable = False
            raw_metrics = getattr(snapshot, "metrics", {})
            metrics = _metrics(raw_metrics)
            quote = quotes.get(symbol)
            out[symbol] = SecurityInputs(
                symbol, str(display_name), str(sector), _price(quote.price) if quote else None,
                utc_datetime(quote.as_of) if quote else None, tradable, metrics,
                tuple(e for e in getattr(snapshot, "evidence", ()) if isinstance(e, Evidence))[:12],
                _provenance(raw_metrics),
            )
        return out

    async def market(self, now: datetime) -> tuple[SecurityInputs, ...]:
        quotes = await self._quotes(_MARKET, purpose="order_sizing")
        result = []
        for symbol, quote in quotes.items():
            price, previous = _price(quote.price), _price(getattr(quote, "prev_close", None))
            if price is not None and previous is not None:
                result.append(SecurityInputs(
                    symbol, symbol, "ETF", price, utc_datetime(quote.as_of),
                    False, {"session_return": float(price / previous - 1)},
                ))
        return tuple(result)


def _metrics(values) -> dict[str, float]:
    result = {}
    if not isinstance(values, Mapping):
        return result
    for key, item in values.items():
        value = _finite(getattr(item, "value", item))
        if value is not None and isinstance(key, str):
            result[key] = value
    for key in ("pe_ratio", "pe_ttm"):
        if result.get(key, 0) > 0:
            result.setdefault("earnings_yield", 1 / result[key])
            break
    if "gross_margin_ttm" in result:
        result.setdefault("gross_margin", result["gross_margin_ttm"])
    return result


def _provenance(values) -> dict[str, dict]:
    if not isinstance(values, Mapping):
        return {}
    result = {}
    for key, item in values.items():
        stamp = utc_datetime(getattr(item, "as_of", None))
        source = getattr(item, "source", None)
        if isinstance(key, str) and stamp is not None and source:
            result[key] = {
                "as_of": stamp.isoformat(), "source": str(source)[:100], "unit": str(getattr(item, "unit", ""))[:40],
                "period": str(getattr(item, "period", ""))[:40],
            }
            url = getattr(item, "url", None)
            if isinstance(url, str) and url.startswith("https://"):
                result[key]["url"] = url[:500]
    return result


def _finite(value) -> float | None:
    if type(value) not in (float, int) and not isinstance(value, Decimal):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _price(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
        return price if price.is_finite() and price > 0 else None
    except (InvalidOperation, ValueError):
        return None


def _news_evidence(item) -> Evidence | None:
    published = utc_datetime(item.published_at)
    retrieved = utc_datetime(item.retrieved_at)
    url = item.url
    if published is None or retrieved is None or not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None
    return Evidence(
        evidence_id=item.story_id, title=str(item.headline or "")[:250], url=url,
        publisher=str(item.publisher or "")[:100], published_at=published, retrieved_at=retrieved,
        snippet=str(item.snippet or "")[:700],
    )
