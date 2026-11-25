"""Adapt provider-health-aware services into the pipeline's point-in-time inputs."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from stock_agent.materiality import score_materiality
from stock_agent.research_focus import focus_symbols, owner_backed
from stock_agent.research_pipeline import NewsEvent, SecurityInputs, SourceUnavailable
from stock_agent.research_schemas import Evidence, utc_datetime

_NEWS_TTL = timedelta(minutes=20)
_QUOTE_TTL = timedelta(seconds=90)
_COMPANY_TTL = timedelta(hours=6)
_MARKET = ("SPY", "QQQ")
_NEWS_LIMIT = 5
_OWNER_HANDOFF_MIN_SCORE = 5
_REGULATORS = ("sec.gov", "bis.gov", "commerce.gov", "federalregister.gov", "justice.gov", "ftc.gov", "fda.gov")
_OWNER_CATALYST_REASONS = frozenset(
    {
        "trading_halt_or_distress",
        "earnings_or_guidance",
        "mna_or_strategic",
        "regulatory_or_legal",
        "ai_semis_or_contract",
        "capital_or_exec",
    }
)
_OWNER_DELIVERY_CATALYST_RE = re.compile(
    r"\b(?:(?:delays?|delayed|postpones?|postponed|halts?|halted|pauses?|paused|suspends?|suspended|"
    r"cancels?|canceled|cancelled|cuts?)\b[^.;]{0,50}\b(?:shipments?|deliver(?:y|ies)|rollout|launch|ramp|"
    r"production|orders?|supply)\b|"
    r"(?:shipments?|deliver(?:y|ies)|rollout|launch|ramp|production)\b[^.;]{0,40}\b(?:delays?|delayed|"
    r"pushout|pull[- ]in|halts?|halted|pauses?|paused|suspends?|suspended)\b)",
    re.I,
)
_OWNER_EXEC_CAPITAL_ACTION_RE = re.compile(
    r"\b(?:resigns?|steps?\s+down|retires?|ousted?|fires?|fired|terminat\w+|removes?|removed|"
    r"departs?|departing|departure|leaves?|leaving|appoint\w+|names?|named|replaces?|replaced|"
    r"succeeds?|buyback|share\s+repurchase|dividend\s+(?:hike|increase|cut|suspension|initiation)|"
    r"secondary\s+offering|convertible\s+notes|restructuring|layoffs?)\b",
    re.I,
)
_OWNER_WRAP = re.compile(
    r"(?:\?\s*$|"
    r"^\s*(?:why|how|what|is|are|can|could|should|will)\b|"
    r"\b(?:euphoria|roundup|commentary|opinion|editorial|column|market\s+(?:wrap|recap)|"
    r"earnings\s+call\s+summary|price[- ]move|price\s+target|"
    r"here(?:'s|\s+is)\s+(?:why|how|what|which)|"
    r"(?:shares|stock|stocks)\s+(?:are\s+|is\s+)?(?:mov(?:e|es|ing)|jump\w*|fall\w*|ris(?:e|es|ing)|"
    r"drop\w*|slump\w*|slid\w*|rall(?:y|ies|ying)|sink\w*|plung\w*|surg\w*|gain\w*|dip\w*|tumbl\w*|soar\w*)|"
    r"(?:fell|rose|jumped|dropped|slumped|gained|sank|plunged|surged|dipped)\s+\d+(?:\.\d+)?%|"
    r"give\s+back\s+(?:some\s+)?gains|"
    r"top\s+\d+\s+stocks|\d+\s+(?:ai\s+|chip\s+|tech\s+|magnificent\s+)?stocks\s+to\s+(?:buy|watch|avoid)|"
    r"stocks\s+to\s+(?:watch|buy)|if\s+you\s+invested|motley\s+fool|zacks|"
    r"left\s+in\s+the\s+dark|under\s+scrutiny\s+after)(?=\W|$))",
    re.I,
)
_MULTI_ISSUER_LEAD_RE = re.compile(
    r"^\s*(?:(?:exclusive|breaking|update)\s*:\s*)?(?:\$?[A-Za-z0-9.\-]+(?:\s+[A-Za-z0-9.\-]+){0,2})"
    r"\s*,\s*[A-Z][A-Za-z0-9.\-]+(?:\s*,\s*[A-Z][A-Za-z0-9.\-]+|\s+(?:and|&)\b)",
)
_FALLBACK_ISSUER_NAMES: dict[str, str] = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "AMD": "Advanced Micro Devices",
    "AVGO": "Broadcom",
    "MU": "Micron",
    "ANET": "Arista Networks",
    "ORCL": "Oracle",
    "PANW": "Palo Alto Networks",
    "TSM": "TSMC",
    "ASML": "ASML",
    "FIX": "Comfort Systems",
    "NVT": "nVent",
    "VST": "Vistra",
    "GOOGL": "Alphabet",
    "GOOG": "Alphabet",
    "META": "Meta",
    "AMZN": "Amazon",
    "TSLA": "Tesla",
    "LLY": "Eli Lilly",
    "UNH": "UnitedHealth",
    "ABBV": "AbbVie",
}


def _issuer_headline(symbol: str, company: str | None, headline: str, *, leading: bool = False) -> bool:
    # Provider tags alone do not make a market wrap issuer-specific. A title
    # hit is an exemption from the indirect-work quota, not proof of materiality.
    if leading:
        headline = re.sub(r"^\s*(?:(?:exclusive|breaking|update)\s*:\s*)?", "", headline, flags=re.I)
    start = "^" if leading else r"(?<!\w)"
    if re.search(rf"{start}\${re.escape(symbol)}(?!\w)", headline, re.IGNORECASE):
        return True
    if len(symbol) >= 2 and re.search(rf"{start}{re.escape(symbol)}(?!\w)", headline):
        return True
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    while words and words[-1] in {"inc", "incorporated", "corp", "corporation", "plc", "ltd", "limited", "com"}:
        words.pop()
    title = " " + " ".join(re.findall(r"[a-z0-9]+", headline.lower())) + " "
    company_match = " " + " ".join(words) + " "
    return bool(words and (title.startswith(company_match) if leading else company_match in title))


def _owner_handoff_event(item, evidence: Evidence, symbol: str, company: str | None) -> bool:
    if getattr(item, "kind", None) == "sec_filing":
        return True  # The caller already checked the filing's issuer tag.
    try:
        host = (urlsplit(evidence.url).hostname or "").lower()
    except ValueError:
        return False
    if host.endswith(".gov") or any(host == domain or host.endswith("." + domain) for domain in _REGULATORS):
        return _issuer_headline(symbol, company, evidence.title)
    if (
        _OWNER_WRAP.search(evidence.title)
        or _MULTI_ISSUER_LEAD_RE.search(evidence.title)
        or not _issuer_headline(symbol, company, evidence.title, leading=True)
    ):
        return False
    combined_text = f"{evidence.title} {evidence.snippet or ''}".strip()
    if _OWNER_DELIVERY_CATALYST_RE.search(combined_text):
        return True
    # Re-score unboosted (no focus/target boost and no multi-tag roundup penalty) so a single real catalyst reaches >= 5.
    verdict = score_materiality(evidence.title, evidence.snippet or "")
    matched = set(verdict.reasons) & _OWNER_CATALYST_REASONS
    if verdict.score < _OWNER_HANDOFF_MIN_SCORE or not matched:
        return False
    if matched == {"capital_or_exec"} and not _OWNER_EXEC_CAPITAL_ACTION_RE.search(combined_text):
        return False
    return True


class ResearchSources:
    def __init__(
        self, prices, news, *,
        list_universe: Callable[[], Awaitable[Sequence[str]]],
        fetch_company: Callable[[str], Awaitable[Any]] | None = None,
        cached_company: Callable[[str], Any | None] | None = None,
        fetch_releases: Callable[[tuple[str, ...], datetime], Awaitable[Sequence[Any]]] | None = None,
        asset_for: Callable[[str], Any] | None = None,
        health=None,
        tipranks=None,
        baseline_due: Callable[[datetime], bool] | None = None,
        fundamental_limit: int = 10,
        focus_for: Callable[..., Sequence[str]] | None = None,
        directives_for: Callable[..., Mapping[str, Any]] | None = None,
    ):
        self.prices = prices
        self.news_service = news
        self.list_universe = list_universe
        self.fetch_company = fetch_company
        self.cached_company = cached_company
        self.fetch_releases = fetch_releases
        self.asset_for = asset_for or (lambda _symbol: None)
        self.health = health
        self.tipranks = tipranks
        self.baseline_due = baseline_due
        self.fundamental_limit = max(0, fundamental_limit)
        self.focus_for = focus_for or focus_symbols
        self.directives_for = directives_for or owner_backed
        self._universe: tuple[str, ...] = ()
        self._universe_at: datetime | None = None
        self._universe_tipranks_key: tuple[Any, ...] | None = None
        self.last_universe_sources: dict[str, int] = {}
        self._tipranks_members: tuple[str, ...] = ()
        self._tipranks_members_recorded: bool = False
        self._built_tipranks_key: tuple[Any, ...] | None = None
        self._active_universe_now: datetime | None = None
        self._news_at: dict[str, datetime] = {}
        self._quote_at: dict[str, datetime] = {}
        self._companies: dict[str, tuple[datetime, Any]] = {}

    def record_universe_sources(self, counts: Mapping[str, int]) -> None:
        self.last_universe_sources = {str(k): int(v) for k, v in counts.items()}

    def record_tipranks_members(self, symbols, *, cache_key: tuple[Any, ...] | None = None) -> None:
        self._tipranks_members = tuple(symbols)
        self._tipranks_members_recorded = True
        if cache_key is not None:
            self._built_tipranks_key = tuple(cache_key)

    def _tipranks_universe_key(self, now: datetime) -> tuple[Any, ...]:
        key_fn = getattr(self.tipranks, "universe_cache_key", None)
        if key_fn is None:
            return ()
        return tuple(key_fn(now))

    async def universe(self, now: datetime) -> tuple[str, ...]:
        tr_key = self._tipranks_universe_key(now)
        ttl_expired = self._universe_at is None or now - self._universe_at >= timedelta(hours=12)
        b_due = bool(self.baseline_due(now)) if self.baseline_due is not None else False
        if ttl_expired or b_due or tr_key != self._universe_tipranks_key:
            prior_tipranks = set(self._tipranks_members)
            had_recorded = self._tipranks_members_recorded
            prior_committed_key = self._universe_tipranks_key
            built: tuple[str, ...] = ()
            candidates: tuple[str, ...] = ()
            committed_key: tuple[Any, ...] = tr_key
            for _attempt in range(3):
                pre_key = self._tipranks_universe_key(now)
                self._built_tipranks_key = None
                self._active_universe_now = now
                try:
                    built = tuple(await self.list_universe())
                finally:
                    self._active_universe_now = None
                selected_key = self._built_tipranks_key if self._built_tipranks_key is not None else pre_key
                post_key = self._tipranks_universe_key(now)
                if selected_key == post_key:
                    candidates = built
                    committed_key = selected_key
                    break
            else:
                if self._tipranks_members_recorded:
                    stale_tr = set(self._tipranks_members)
                    candidates = tuple(s for s in built if s not in stale_tr)
                    self._tipranks_members = ()
                else:
                    candidates = ()
                committed_key = ("__unstable__",)
            key_changed = prior_committed_key is not None and committed_key != prior_committed_key
            if candidates:
                self._universe = tuple(dict.fromkeys(candidates))
                if ttl_expired or b_due or self._universe_at is None:
                    self._universe_at = now
                self._universe_tipranks_key = committed_key
            else:
                if key_changed or tr_key != prior_committed_key:
                    # TipRanks membership was invalidated: clear TipRanks-sourced names (or all
                    # cached names when membership wasn't tracked per-source) even on empty rebuild.
                    if had_recorded:
                        self._universe = tuple(s for s in self._universe if s not in prior_tipranks)
                    else:
                        self._universe = ()
                    self._tipranks_members = ()
                    self._universe_tipranks_key = committed_key
                    if not self._universe:
                        self._universe_at = None
                elif prior_committed_key is None:
                    self._universe_tipranks_key = committed_key
                if self._universe_at and now - self._universe_at > timedelta(days=2):
                    self._universe = ()
                    self._universe_tipranks_key = committed_key
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
                for symbol in item.symbols:
                    if symbol not in allowed:
                        continue
                    asset = self.asset_for(symbol)
                    company = getattr(asset, "company_name", None)
                    direct = getattr(item, "kind", None) == "sec_filing" or _issuer_headline(
                        symbol, company, evidence.title,
                    )
                    events.append(NewsEvent(symbol, item.story_id, evidence, round(score), direct))
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

    async def refresh_entry_inputs(self, symbol: str, now: datetime) -> None:
        """Revalidate after model latency without replacing the analysis price."""
        if not self.quote_ok(symbol, now):
            await self._quotes((symbol,), purpose="order_sizing")
        if not self.news_ok(symbol, now):
            await self.background((symbol,))

    def _company_name(self, symbol: str) -> str | None:
        asset_name = getattr(self.asset_for(symbol), "company_name", None)
        if asset_name:
            return str(asset_name)
        stamp = self._companies.get(symbol)
        if stamp is not None:
            snap_name = getattr(stamp[1], "company_name", None) or getattr(stamp[1], "name", None)
            if snap_name:
                return str(snap_name)
        if self.cached_company is not None:
            cached = self.cached_company(symbol)
            cached_name = getattr(cached, "company_name", None) or getattr(cached, "name", None)
            if cached_name:
                return str(cached_name)
        return _FALLBACK_ISSUER_NAMES.get(symbol)

    async def handoff_material_news(
        self, symbol: str, after: datetime, now: datetime, *, owner: bool = False,
    ) -> tuple[Evidence, ...]:
        """Ordinary handoffs retain the live threshold; owner handoffs require issuer events."""
        items = await self._poll((symbol,), scope="scheduled_research", include_market=False, lookback_hours=24)
        evidence = []
        company = self._company_name(symbol) if owner else None
        for item in items:
            parsed = _news_evidence(item)
            if (symbol not in item.symbols or parsed is None or parsed.published_at is None
                    or not after < parsed.published_at <= now):
                continue
            if owner:
                if _owner_handoff_event(item, parsed, symbol, company):
                    evidence.append(parsed)
            elif (_finite(item.materiality_score) or 0) >= _NEWS_LIMIT:
                evidence.append(parsed)
        return tuple(sorted(evidence, key=lambda row: row.published_at, reverse=True)[:6])

    async def completed_close(self, symbol: str, session_date: date) -> Decimal | None:
        """An analysis anchor only, for the exact expected previous session."""
        getter = getattr(self.prices, "get_daily_bars", None)
        if getter is None:
            return None
        try:
            bars = (await getter([symbol], sessions=1, end_date=session_date)).get(symbol, ())
        except Exception:
            return None
        return _price(bars[-1].close) if bars and bars[-1].session_date == session_date else None

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

    def _normalize_with_alias(self, raw_symbols: Sequence[Any]) -> list[str]:
        out: list[str] = []
        resolve = getattr(self.news_service, "_resolve_symbol", None)
        for item in raw_symbols:
            sym = str(item or "").strip().upper()
            if not sym:
                continue
            out.append(sym)
            if callable(resolve):
                try:
                    resolved = str(resolve(sym) or "").strip().upper()
                    if resolved and resolved != sym:
                        out.append(resolved)
                except Exception:
                    pass
        return out

    def _focus_by_cadence(self, now: datetime, cadence: str) -> list[str]:
        try:
            raw = self.focus_for(now=now, cadence=cadence)
        except TypeError:
            if cadence != "live":
                return []
            try:
                raw = self.focus_for(now=now)
            except Exception:
                return []
        except Exception:
            return []
        return self._normalize_with_alias(tuple(raw))

    def _ordered_priority_symbols(self, now: datetime, *, include_daily: bool) -> tuple[str, ...]:
        """Deterministic order: owner-backed first, then live focus, then daily focus (prescan only)."""
        owner_raw: Sequence[Any] = ()
        try:
            directives = self.directives_for(now=now)
            owner_raw = tuple(directives.keys() if isinstance(directives, Mapping) else directives)
        except Exception:
            owner_raw = ()
        owner_syms = self._normalize_with_alias(owner_raw)
        live_syms = self._focus_by_cadence(now, "live")
        daily_syms = self._focus_by_cadence(now, "daily") if include_daily else []
        return tuple(dict.fromkeys([*owner_syms, *live_syms, *daily_syms]))

    async def _brief_quotes(self, symbols: Sequence[str]):
        """Pre-open analysis-only 20m quote fallback; never updates _quote_at or regular-hours price."""
        if not symbols or self.prices is None:
            return {}
        try:
            batch = await self.prices.get_quotes(
                list(symbols), purpose="brief", max_age_s=int(_NEWS_TTL.total_seconds())
            )
        except Exception:
            return {}
        return getattr(batch, "quotes", None) or {}

    async def _recent_filings_evidence(
        self, symbols: Sequence[str], now: datetime
    ) -> dict[str, tuple[Evidence, ...]]:
        fetch_recent = getattr(self.news_service, "fetch_recent_filings", None)
        if not symbols or not callable(fetch_recent):
            return {}
        try:
            batches = await fetch_recent(tuple(symbols), now=now)
        except Exception:
            return {}
        out: dict[str, tuple[Evidence, ...]] = {}
        for symbol, items in (batches or {}).items():
            parsed = tuple(ev for item in items if (ev := _news_evidence(item)) is not None)
            if parsed:
                out[symbol] = parsed
        return out

    async def securities(
        self,
        symbols: tuple[str, ...],
        now: datetime,
        *,
        preopen: bool = False,
        prescan: bool = False,
    ) -> dict[str, SecurityInputs]:
        limiter = asyncio.Semaphore(5)
        cached = {
            symbol: stamp[1] for symbol in symbols if (stamp := self._companies.get(symbol)) is not None
            and timedelta() <= now - stamp[0] <= _COMPANY_TTL
        }
        if self.cached_company is not None:
            for symbol in symbols:
                if symbol not in cached and (snapshot := self.cached_company(symbol)) is not None:
                    cached[symbol] = snapshot
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

        is_preopen = bool(preopen or prescan)
        ordered_priority = self._ordered_priority_symbols(now, include_daily=bool(prescan))
        requested_set = set(symbols)
        filing_targets = tuple(sym for sym in ordered_priority if sym in requested_set)
        fundamentals, quotes, recent_filings = await asyncio.gather(
            asyncio.gather(*(company(symbol) for symbol in symbols)),
            self._quotes(symbols, purpose="order_sizing"),
            self._recent_filings_evidence(filing_targets, now),
        )
        missing_priority_quotes = (
            [symbol for symbol in filing_targets if quotes.get(symbol) is None]
            if is_preopen
            else []
        )
        fallback_quotes = (
            await self._brief_quotes(missing_priority_quotes) if missing_priority_quotes else {}
        )
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
            bquote = fallback_quotes.get(symbol) if is_preopen else None
            ev_fn = getattr(self.tipranks, "evidence_for", None)
            tr_evidence = tuple(ev_fn(symbol, now)) if ev_fn is not None else ()
            filing_evidence = recent_filings.get(symbol, ())
            snap_evidence = tuple(e for e in getattr(snapshot, "evidence", ()) if isinstance(e, Evidence))
            out[symbol] = SecurityInputs(
                symbol,
                str(display_name),
                str(sector),
                _price(quote.price) if quote else None,
                utc_datetime(quote.as_of) if quote else None,
                tradable,
                metrics,
                (*tr_evidence, *snap_evidence)[:12],
                _provenance(raw_metrics),
                priority_evidence=filing_evidence,
                analysis_price=_price(bquote.price) if bquote else None,
                analysis_quote_at=utc_datetime(bquote.as_of) if bquote else None,
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
    if url.startswith("https://news.alpaca.invalid/"):
        return None  # The feed returned no article URL; do not present its placeholder as a citation.
    return Evidence(
        evidence_id=item.story_id, title=str(item.headline or "")[:250], url=url,
        publisher=str(item.publisher or "")[:100], published_at=published, retrieved_at=retrieved,
        snippet=str(item.snippet or "")[:700],
    )
