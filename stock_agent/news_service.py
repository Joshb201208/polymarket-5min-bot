"""Live news polling and cross-provider deduplication service (Decisions A5 & A12).

Sources (in order):
1. Alpaca News REST (`/v1beta1/news`) — primary timestamped stream for held, focus,
   universe, and broad-market (`SPY`, `QQQ`) symbols with `since` cursor in `StateStore.kv`.
2. Finnhub `/api/v1/company-news` — secondary company news for held + focus symbols.
3. SEC EDGAR `/submissions/CIK{10}.json` — authoritative `8-K`, `10-Q`, `10-K` filings
   for symbols with known CIK mappings.

Guarantees:
- Cross-provider story deduplication + persisted seen-set in `StateStore.kv` so a story
  is surfaced as `is_new` at most once (unless revised) across process restarts.
- `mark_evaluated(story_ids)` persists evaluated story IDs so the LLM reaction path
  evaluates each material story at most once (Decision A5).
- Failed providers NEVER masquerade as "no news": when all active providers fail (or
  none are configured), `NewsFeedBatch.ok` is `False` and `health` records the failure.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from stock_agent.config import Config
from stock_agent.health import ProviderHealthRegistry
from stock_agent.http_safety import DataHttpClient, configure_safe_http_logging, scrub_secrets
from stock_agent.materiality import (
    MATERIALITY_THRESHOLD,
    sanitize_news_text,
    score_materiality,
)
from stock_agent.research_focus import focus_symbols
from stock_agent.research_schemas import Evidence, utc_datetime
from stock_agent.state_store import StateStore, StateUnavailable

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
ALPACA_DATA_BASE = "https://data.alpaca.markets"
FINNHUB_BASE = "https://finnhub.io/api/v1"
SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"

BROAD_MARKET_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")

_KV_SEEN_KEY = "news:seen_stories:v1"
_KV_EVALUATED_KEY = "news:evaluated_stories:v1"
_KV_CURSOR_PREFIX = "news:cursor:v1:"
_MAX_PERSISTED_STORIES = 2000
_CURSOR_OVERLAP = timedelta(minutes=2)
_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")

# Built-in CIK fallback for major US equities when AssetRegistry has not yet populated CIKs
DEFAULT_CIK_MAP: dict[str, str] = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
    "AMD": "0000002488",
    "AVGO": "0001730168",
    "MU": "0000723125",
    "ANET": "0001596532",
    "GOOGL": "0001652044",
    "GOOG": "0001652044",
    "META": "0001326801",
    "AMZN": "0001018724",
    "TSLA": "0001318605",
    "JPM": "0000019617",
    "GS": "0000886982",
    "BNY": "0001390777",
    "BK": "0001390777",
    "LLY": "0000059478",
    "UNH": "0000731766",
    "ABBV": "0001551152",
    "XOM": "0000034088",
    "CVX": "0000093410",
    "HON": "0000773840",
    "CAT": "0000018230",
    "WMT": "0000104169",
    "PG": "0000080424",
    "KO": "0000021344",
}


def story_fingerprint(headline: str, symbols: Sequence[str], published_at: datetime) -> str:
    """Deterministic vendor-neutral story ID so duplicate wire pickups collapse to one ID."""
    norm_head = _PUNCT_RE.sub(" ", headline.lower())
    words = [w for w in norm_head.split() if len(w) > 2][:10]
    head_key = " ".join(words)
    sym_key = ",".join(sorted({s.strip().upper() for s in symbols if s and s.strip()}))[:40]
    day_bucket = published_at.astimezone(UTC).strftime("%Y-%m-%d")
    digest = hashlib.sha256(f"{day_bucket}|{sym_key}|{head_key}".encode()).hexdigest()[:16]
    return f"story-{digest}"


@dataclass(frozen=True)
class NewsItem:
    """Canonical timestamped news/filing item consumed by `research_pipeline`."""

    id: str
    symbols: tuple[str, ...]
    headline: str
    summary: str
    source: str
    published_at: datetime
    url: str = ""
    updated_at: datetime | None = None
    first_seen_at: datetime | None = None
    retrieved_at: datetime | None = None
    revision: int = 1
    provider: str = "alpaca"
    provider_id: str = ""
    kind: Literal["news", "sec_filing"] = "news"
    sec_form: str | None = None
    sec_items: tuple[str, ...] = ()
    materiality: int = 0
    materiality_reasons: tuple[str, ...] = ()
    is_new: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "symbols",
            tuple(s.strip().upper() for s in self.symbols if s and s.strip()),
        )
        object.__setattr__(self, "published_at", utc_datetime(self.published_at))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at", utc_datetime(self.updated_at))
        if self.first_seen_at is not None:
            object.__setattr__(self, "first_seen_at", utc_datetime(self.first_seen_at))
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at", utc_datetime(self.retrieved_at))
        if not self.url or not self.url.startswith(("https://", "http://")):
            object.__setattr__(self, "url", f"https://news.alpaca.invalid/story/{self.id}")

    @property
    def story_id(self) -> str:
        return self.id

    @property
    def publisher(self) -> str:
        return self.source

    @property
    def snippet(self) -> str:
        return self.summary

    @property
    def materiality_score(self) -> int:
        return self.materiality

    @property
    def is_material(self) -> bool:
        return self.materiality >= MATERIALITY_THRESHOLD

    def to_evidence(self) -> Evidence:
        """Convert to `research_schemas.Evidence` for grounded LLM citations."""
        ev_url = self.url or f"https://news.alpaca.invalid/story/{self.id}"
        return Evidence(
            evidence_id=self.id,
            url=ev_url,
            title=self.headline or f"{self.source} item",
            published_at=self.published_at,
            retrieved_at=self.retrieved_at or self.published_at,
            publisher=self.source,
            snippet=self.summary or None,
        )


@dataclass(frozen=True)
class NewsFeedBatch:
    ok: bool
    degraded: bool
    as_of: datetime
    items: tuple[NewsItem, ...]
    new_items: tuple[NewsItem, ...]
    material_new_items: tuple[NewsItem, ...]
    errors: dict[str, str]
    covered_symbols: tuple[str, ...] = ()


NewsPollResult = NewsFeedBatch


def _safe_utc(raw: Any, *, fallback: datetime) -> datetime:
    if isinstance(raw, str) and raw:
        try:
            return utc_datetime(raw)
        except ValueError:
            return fallback
    return fallback


class NewsService:
    """Timed live news polling service with cursor persistence and materiality filtering."""

    MATERIALITY_THRESHOLD: int = MATERIALITY_THRESHOLD

    def __init__(
        self,
        http: DataHttpClient | httpx.AsyncClient | None = None,
        *,
        config: Any = Config,
        store: StateStore | None = None,
        defer_load: bool = False,
        health: ProviderHealthRegistry | None = None,
        assets: Any = None,
        cik_lookup: Callable[[str], str | None] | None = None,
        enable_sec_filings: bool = False,
        now: Callable[[], datetime] | None = None,
        alpaca_data_base: str = ALPACA_DATA_BASE,
        finnhub_base: str = FINNHUB_BASE,
        sec_submissions_base: str = SEC_SUBMISSIONS_BASE,
    ) -> None:
        configure_safe_http_logging()
        self.config = config
        self.store = store
        self._now = now or (lambda: datetime.now(UTC))
        self.health = health or ProviderHealthRegistry(
            config, store=store, defer_load=defer_load, now=self._now
        )
        self.assets = assets
        self._cik_lookup = cik_lookup
        self.enable_sec_filings = enable_sec_filings or (cik_lookup is not None)
        self.alpaca_data_base = alpaca_data_base.rstrip("/")
        self.finnhub_base = finnhub_base.rstrip("/")
        self.sec_submissions_base = sec_submissions_base.rstrip("/")

        if isinstance(http, DataHttpClient):
            self.http = http
            self._owns_http = False
        elif isinstance(http, httpx.AsyncClient):
            self.http = DataHttpClient(config, client=http)
            self._owns_http = False
        else:
            self.http = DataHttpClient(config)
            self._owns_http = True

        self._seen: dict[str, dict[str, Any]] = {}
        self._evaluated: dict[str, str] = {}
        self._cursors: dict[str, datetime] = {}
        self._latest_batch: NewsFeedBatch | None = None
        self._state_loaded: bool = store is None
        if not defer_load:
            self.load_state()

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    def latest_batch(self) -> NewsFeedBatch | None:
        return self._latest_batch

    # ------------------------------------------------- Asset / CIK helpers

    def _resolve_symbol(self, symbol: str) -> str:
        clean = symbol.strip().upper()
        if self.assets is not None and hasattr(self.assets, "resolve_symbol"):
            try:
                resolved = self.assets.resolve_symbol(clean)
                if resolved:
                    return str(resolved).strip().upper()
            except NotImplementedError:
                pass
            except Exception:
                pass
        if clean == "BK":
            return "BNY"
        return clean

    def _lookup_cik(self, symbol: str) -> str | None:
        clean = symbol.strip().upper()
        if self._cik_lookup is not None:
            cik = self._cik_lookup(clean)
            if cik:
                return str(cik)
        if self.assets is not None and hasattr(self.assets, "get_asset_meta"):
            try:
                meta = self.assets.get_asset_meta(clean)
                if getattr(meta, "cik", None):
                    return str(meta.cik)
            except NotImplementedError:
                pass
            except Exception:
                pass
        return DEFAULT_CIK_MAP.get(clean)

    # ------------------------------------------------- Cursor & Seen Persistence

    def load_state(self) -> bool:
        """Hydrate persisted news cursors and seen-set from StateStore once open."""
        if self.store is None:
            self._state_loaded = True
            return True
        try:
            seen_raw = self.store.kv_get(_KV_SEEN_KEY)
            eval_raw = self.store.kv_get(_KV_EVALUATED_KEY)
        except StateUnavailable:
            return False
        self._state_loaded = True
        if isinstance(seen_raw, dict):
            for k, v in seen_raw.items():
                if isinstance(v, dict):
                    self._seen.setdefault(str(k), v)
        if isinstance(eval_raw, dict):
            for k, v in eval_raw.items():
                self._evaluated.setdefault(str(k), str(v))
        return True

    _load_persisted = load_state

    def _ensure_loaded(self) -> None:
        if not self._state_loaded:
            self.load_state()

    def get_cursor(self, scope: str = "live") -> datetime | None:
        self._ensure_loaded()
        if scope in self._cursors:
            return self._cursors[scope]
        if self.store is None:
            return None
        try:
            raw = self.store.kv_get(f"{_KV_CURSOR_PREFIX}{scope}")
        except StateUnavailable:
            return None
        if isinstance(raw, str) and raw:
            try:
                dt = utc_datetime(raw)
                self._cursors[scope] = dt
                return dt
            except ValueError:
                return None
        return None

    def _save_state(self, scope: str, cursor_ts: datetime) -> None:
        self._ensure_loaded()
        self._cursors[scope] = cursor_ts
        if len(self._seen) > _MAX_PERSISTED_STORIES:
            ordered = sorted(
                self._seen.items(),
                key=lambda kv: str(kv[1].get("seen_at", "")),
                reverse=True,
            )[:_MAX_PERSISTED_STORIES]
            self._seen = dict(ordered)

        if self.store is None:
            return
        try:
            self.store.kv_set(f"{_KV_CURSOR_PREFIX}{scope}", cursor_ts.isoformat())
            self.store.kv_set(_KV_SEEN_KEY, self._seen)
        except StateUnavailable:
            return
        except Exception as exc:
            logger.warning("Failed persisting news cursor/seen state: %s", scrub_secrets(str(exc)))

    def is_evaluated(self, story_id: str) -> bool:
        self._ensure_loaded()
        return story_id in self._evaluated

    def mark_evaluated(self, story_ids: Iterable[str], *, at: datetime | None = None) -> None:
        self._ensure_loaded()
        ts = utc_datetime(at or self._now()).isoformat()
        changed = False
        for sid in story_ids:
            if sid and str(sid) not in self._evaluated:
                self._evaluated[str(sid)] = ts
                changed = True
        if not changed:
            return
        if len(self._evaluated) > _MAX_PERSISTED_STORIES:
            ordered = sorted(self._evaluated.items(), key=lambda kv: kv[1], reverse=True)[
                :_MAX_PERSISTED_STORIES
            ]
            self._evaluated = dict(ordered)
        if self.store is not None:
            try:
                self.store.kv_set(_KV_EVALUATED_KEY, self._evaluated)
            except StateUnavailable:
                return
            except Exception as exc:
                logger.warning("Failed persisting evaluated stories: %s", scrub_secrets(str(exc)))

    # --------------------------------------------------------- Polling Interface

    async def poll_news(
        self,
        symbols: Sequence[str] = (),
        *,
        focus: Sequence[str] | None = None,
        universe: Sequence[str] = (),
        include_focus: bool = True,
        include_market: bool = True,
        lookback_hours: float = 24.0,
        materiality_threshold: int = MATERIALITY_THRESHOLD,
        scope: str = "live",
        mark_seen: bool = True,
    ) -> NewsFeedBatch:
        """Poll Alpaca (primary), Finnhub (secondary), and optional SEC filings."""
        now_utc = utc_datetime(self._now())
        owner_focus: tuple[str, ...] = ()
        if focus is not None:
            owner_focus = tuple(self._resolve_symbol(str(s)) for s in focus if s and str(s).strip())
        elif include_focus:
            try:
                owner_focus = tuple(self._resolve_symbol(s) for s in focus_symbols(now=now_utc))
            except Exception:
                owner_focus = ()

        held_list = [self._resolve_symbol(str(s)) for s in symbols if s and str(s).strip()]
        uni_list = [self._resolve_symbol(str(s)) for s in universe if s and str(s).strip()]
        market_list = list(BROAD_MARKET_SYMBOLS) if include_market else []

        polled_symbols = list(dict.fromkeys([*held_list, *owner_focus, *uni_list, *market_list]))
        focus_set = set(owner_focus)
        target_set = set(held_list) | focus_set | set(uni_list)

        prev_cursor = self.get_cursor(scope)
        default_start = now_utc - timedelta(hours=max(0.25, float(lookback_hours)))
        alpaca_start = (
            max(default_start, prev_cursor - _CURSOR_OVERLAP)
            if prev_cursor is not None
            else default_start
        )

        raw_items: list[NewsItem] = []
        errors: dict[str, str] = {}
        alpaca_ok = False
        finnhub_covered: tuple[str, ...] = ()

        # 1. Primary: Alpaca News REST (/v1beta1/news)
        if not (getattr(self.config, "ALPACA_API_KEY", "") and getattr(self.config, "ALPACA_SECRET_KEY", "")):
            errors["alpaca"] = "Alpaca news credentials not configured"
        elif self.health.is_blocked("alpaca", "news", now=now_utc):
            errors["alpaca"] = "Alpaca news temporarily plan-blocked"
        else:
            alp_items, alp_err = await self._fetch_alpaca_news(
                polled_symbols,
                start_utc=alpaca_start,
                end_utc=now_utc,
                now_utc=now_utc,
                target_set=target_set,
                focus_set=focus_set,
                threshold=materiality_threshold,
            )
            if alp_err is None:
                alpaca_ok = True
                raw_items.extend(alp_items)
            else:
                errors["alpaca"] = alp_err

        # 2. Secondary: Finnhub /company-news for held + focus symbols (capped to protect 60/min limit)
        priority_symbols = list(dict.fromkeys([*held_list, *owner_focus]))
        if not priority_symbols and errors.get("alpaca"):
            priority_symbols = polled_symbols[:12]
        else:
            priority_symbols = priority_symbols[:12]

        if (
            priority_symbols
            and bool(getattr(self.config, "FINNHUB_API_KEY", ""))
            and not self.health.is_blocked("finnhub", "company_news", now=now_utc)
        ):
            fh_items, finnhub_covered, fh_err = await self._fetch_finnhub_news(
                priority_symbols,
                start_utc=default_start,
                end_utc=now_utc,
                now_utc=now_utc,
                target_set=target_set,
                focus_set=focus_set,
                threshold=materiality_threshold,
            )
            raw_items.extend(fh_items)
            if fh_err is not None:
                errors["finnhub"] = fh_err

        # 3. Tertiary: SEC EDGAR filings (8-K, 10-Q, 10-K) when enabled
        if self.enable_sec_filings and priority_symbols and not self.health.is_blocked(
            "sec", "edgar", now=now_utc
        ):
            sec_items = await self._fetch_sec_filings(
                priority_symbols[:10],
                since_utc=default_start,
                now_utc=now_utc,
                target_set=target_set,
                focus_set=focus_set,
                threshold=materiality_threshold,
            )
            raw_items.extend(sec_items)

        if not alpaca_ok and not finnhub_covered:
            self.health.record_news_poll(polled_symbols, now_utc, source="failed", ok=False)
            batch = NewsFeedBatch(
                ok=False,
                degraded=False,
                as_of=now_utc,
                items=(),
                new_items=(),
                material_new_items=(),
                errors=errors,
            )
            self._latest_batch = batch
            return batch

        degraded = bool(errors)
        merged = self._dedupe_and_apply_seen(raw_items, now_utc=now_utc, mark_seen=mark_seen)
        new_items = tuple(item for item in merged if item.is_new)
        material_new = tuple(
            item
            for item in new_items
            if item.materiality >= materiality_threshold and not self.is_evaluated(item.id)
        )

        if mark_seen:
            newest_ts = max((i.published_at for i in merged), default=now_utc)
            self._save_state(scope, max(now_utc, newest_ts))

        source_label = "alpaca" if alpaca_ok else "finnhub"
        covered_syms = tuple(polled_symbols) if alpaca_ok else finnhub_covered
        covered_set = set(covered_syms)
        self.health.record_news_poll(
            covered_syms,
            now_utc,
            source=source_label,
            ok=True,
            degraded=degraded,
            include_market=include_market and alpaca_ok,
            uncovered_symbols=(symbol for symbol in polled_symbols if symbol not in covered_set),
        )

        batch = NewsFeedBatch(
            ok=True,
            degraded=degraded,
            as_of=now_utc,
            items=merged,
            new_items=new_items,
            material_new_items=material_new,
            errors=errors,
            covered_symbols=covered_syms,
        )
        self._latest_batch = batch
        return batch

    # Convenient alias
    poll = poll_news

    async def get_symbol_news(
        self,
        symbol: str,
        *,
        lookback_hours: float = 72.0,
        mark_seen: bool = False,
    ) -> NewsFeedBatch:
        sym = self._resolve_symbol(symbol)
        return await self.poll_news(
            [sym],
            focus=(),
            universe=(),
            include_focus=False,
            include_market=False,
            lookback_hours=lookback_hours,
            scope=f"symbol:{sym}",
            mark_seen=mark_seen,
        )

    # ---------------------------------------------------- Provider Implementations

    async def _fetch_alpaca_news(
        self,
        symbols: Sequence[str],
        *,
        start_utc: datetime,
        end_utc: datetime,
        now_utc: datetime,
        target_set: set[str],
        focus_set: set[str],
        threshold: int,
    ) -> tuple[list[NewsItem], str | None]:
        items: list[NewsItem] = []
        start_iso = start_utc.isoformat().replace("+00:00", "Z")
        end_iso = end_utc.isoformat().replace("+00:00", "Z")

        symbol_batches = [symbols[i : i + 45] for i in range(0, len(symbols), 45)] or [[]]
        for batch in symbol_batches:
            page_token: str | None = None
            for _page in range(3):
                params: dict[str, Any] = {
                    "start": start_iso,
                    "end": end_iso,
                    "limit": 50,
                    "sort": "desc",
                }
                if batch:
                    params["symbols"] = ",".join(batch)
                if page_token:
                    params["page_token"] = page_token

                try:
                    resp = await self.http.get(
                        f"{self.alpaca_data_base}/v1beta1/news",
                        params=params,
                    )
                except httpx.HTTPError as exc:
                    msg = scrub_secrets(str(exc))
                    self.health.record_failure(
                        "alpaca", "news", summary=msg, required=True, at=now_utc
                    )
                    return items, msg

                if resp.status_code != 200:
                    msg = f"Alpaca news HTTP {resp.status_code}"
                    self.health.record_failure(
                        "alpaca",
                        "news",
                        status_code=resp.status_code,
                        summary=msg,
                        required=True,
                        at=now_utc,
                    )
                    return items, msg

                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
                rows = payload.get("news") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    msg = "Alpaca news returned an invalid payload"
                    self.health.record_failure("alpaca", "news", summary=msg, required=True, at=now_utc)
                    return items, msg
                self.health.record_ok("alpaca", "news", required=True, at=now_utc)
                for row in rows:
                    parsed = self._parse_alpaca_item(
                        row,
                        now_utc=now_utc,
                        target_set=target_set,
                        focus_set=focus_set,
                        threshold=threshold,
                    )
                    if parsed is not None:
                        items.append(parsed)

                nxt = payload.get("next_page_token") if isinstance(payload, dict) else None
                if not nxt:
                    break
                page_token = str(nxt)

        return items, None

    @staticmethod
    def _parse_alpaca_item(
        row: Any,
        *,
        now_utc: datetime,
        target_set: set[str],
        focus_set: set[str],
        threshold: int,
    ) -> NewsItem | None:
        if not isinstance(row, dict):
            return None
        headline = sanitize_news_text(row.get("headline"), max_chars=300)
        if not headline:
            return None
        summary = sanitize_news_text(row.get("summary") or row.get("content"), max_chars=600)
        created_raw = row.get("created_at")
        if not created_raw:
            return None
        try:
            published_at = utc_datetime(str(created_raw))
        except ValueError:
            return None
        updated_at: datetime | None = None
        if row.get("updated_at"):
            try:
                updated_at = utc_datetime(str(row["updated_at"]))
            except ValueError:
                updated_at = None

        raw_syms = row.get("symbols") or []
        syms = tuple(str(s).strip().upper() for s in raw_syms if s and str(s).strip())
        sid = story_fingerprint(headline, syms, published_at)
        verdict = score_materiality(
            headline,
            summary,
            symbols=syms,
            held_or_target=target_set,
            focus_set=focus_set,
            threshold=threshold,
        )
        revision = 2 if (updated_at and updated_at > published_at + timedelta(seconds=30)) else 1
        return NewsItem(
            id=sid,
            symbols=syms,
            headline=headline,
            summary=summary,
            source=str(row.get("source") or "alpaca").strip(),
            published_at=published_at,
            url=str(row.get("url") or "").strip(),
            updated_at=updated_at,
            retrieved_at=now_utc,
            revision=revision,
            provider="alpaca",
            provider_id=str(row.get("id") or ""),
            kind="news",
            materiality=verdict.score,
            materiality_reasons=verdict.reasons,
        )

    async def _fetch_finnhub_news(
        self,
        symbols: Sequence[str],
        *,
        start_utc: datetime,
        end_utc: datetime,
        now_utc: datetime,
        target_set: set[str],
        focus_set: set[str],
        threshold: int,
    ) -> tuple[list[NewsItem], tuple[str, ...], str | None]:
        items: list[NewsItem] = []
        from_str = start_utc.astimezone(ET).date().isoformat()
        to_str = end_utc.astimezone(ET).date().isoformat()
        covered: list[str] = []
        last_err: str | None = None

        for sym in symbols:
            if self.health.is_blocked("finnhub", "company_news", symbol=sym, now=now_utc):
                last_err = "Finnhub company-news temporarily plan-blocked for a requested symbol"
                continue
            try:
                resp = await self.http.get(
                    f"{self.finnhub_base}/company-news",
                    params={"symbol": sym, "from": from_str, "to": to_str},
                )
            except httpx.HTTPError as exc:
                last_err = scrub_secrets(str(exc))
                self.health.record_failure(
                    "finnhub", "company_news", summary=last_err, at=now_utc
                )
                break

            if resp.status_code != 200:
                last_err = f"Finnhub company-news HTTP {resp.status_code}"
                self.health.record_failure(
                    "finnhub",
                    "company_news",
                    status_code=resp.status_code,
                    symbol=sym,
                    summary=last_err,
                    at=now_utc,
                )
                if resp.status_code in (401, 429):
                    break
                continue

            try:
                rows = resp.json()
            except ValueError:
                rows = None
            if not isinstance(rows, list):
                last_err = "Finnhub company-news returned an invalid payload"
                self.health.record_failure("finnhub", "company_news", symbol=sym, summary=last_err, at=now_utc)
                continue
            covered.append(sym)
            self.health.record_ok("finnhub", "company_news", required=False, at=now_utc)
            for row in rows[:25]:
                if not isinstance(row, dict):
                    continue
                headline = sanitize_news_text(row.get("headline"), max_chars=300)
                if not headline:
                    continue
                ts_val = row.get("datetime")
                if isinstance(ts_val, (int, float)) and ts_val > 0:
                    published_at = datetime.fromtimestamp(float(ts_val), tz=UTC)
                else:
                    continue
                if published_at < start_utc - timedelta(hours=12) or published_at > now_utc + timedelta(minutes=5):
                    continue
                summary = sanitize_news_text(row.get("summary"), max_chars=600)
                related = [
                    s.strip().upper()
                    for s in str(row.get("related") or sym).split(",")
                    if s.strip()
                ]
                syms = tuple(dict.fromkeys([sym, *related]))
                sid = story_fingerprint(headline, syms, published_at)
                verdict = score_materiality(
                    headline,
                    summary,
                    symbols=syms,
                    held_or_target=target_set,
                    focus_set=focus_set,
                    threshold=threshold,
                )
                items.append(
                    NewsItem(
                        id=sid,
                        symbols=syms,
                        headline=headline,
                        summary=summary,
                        source=str(row.get("source") or "Finnhub").strip(),
                        published_at=published_at,
                        url=str(row.get("url") or "").strip(),
                        retrieved_at=now_utc,
                        provider="finnhub",
                        provider_id=str(row.get("id") or ""),
                        kind="news",
                        materiality=verdict.score,
                        materiality_reasons=verdict.reasons,
                    )
                )

        return items, tuple(covered), last_err

    async def _fetch_sec_filings(
        self,
        symbols: Sequence[str],
        *,
        since_utc: datetime,
        now_utc: datetime,
        target_set: set[str],
        focus_set: set[str],
        threshold: int,
    ) -> list[NewsItem]:
        out: list[NewsItem] = []
        since_day = since_utc.astimezone(ET).date()

        for sym in symbols:
            cik = self._lookup_cik(sym)
            if not cik:
                continue
            cik_padded = str(cik).zfill(10)
            try:
                resp = await self.http.get(
                    f"{self.sec_submissions_base}/CIK{cik_padded}.json"
                )
            except httpx.HTTPError as exc:
                self.health.record_failure("sec", "edgar", summary=scrub_secrets(str(exc)), at=now_utc)
                break
            if resp.status_code != 200:
                self.health.record_failure(
                    "sec",
                    "edgar",
                    status_code=resp.status_code,
                    summary=f"SEC submissions HTTP {resp.status_code}",
                    at=now_utc,
                )
                continue
            self.health.record_ok("sec", "edgar", required=False, at=now_utc)
            payload = resp.json()
            recent = (
                payload.get("filings", {}).get("recent", {})
                if isinstance(payload, dict)
                else {}
            )
            forms = recent.get("form") or []
            dates = recent.get("filingDate") or []
            accessions = recent.get("accessionNumber") or []
            items_col = recent.get("items") or []
            acceptance_col = recent.get("acceptanceDateTime") or []

            for idx, form in enumerate(forms[:20]):
                form_str = str(form or "").strip().upper()
                if form_str not in {"8-K", "8-K/A", "10-Q", "10-K", "6-K"}:
                    continue
                filing_date_str = str(dates[idx]) if idx < len(dates) else ""
                if not filing_date_str or filing_date_str < since_day.isoformat():
                    continue
                acc_raw = str(accessions[idx]) if idx < len(accessions) else f"{sym}-{idx}"
                acc_nodash = acc_raw.replace("-", "")
                raw_items_entry = items_col[idx] if idx < len(items_col) else ""
                if isinstance(raw_items_entry, list):
                    sec_item_codes = tuple(str(x).strip() for x in raw_items_entry if str(x).strip())
                else:
                    sec_item_codes = tuple(
                        x.strip() for x in str(raw_items_entry or "").split(",") if x.strip()
                    )
                published_at = datetime.fromisoformat(f"{filing_date_str}T12:00:00+00:00")
                if idx < len(acceptance_col) and acceptance_col[idx]:
                    try:
                        published_at = utc_datetime(str(acceptance_col[idx]))
                    except ValueError:
                        pass
                items_desc = f" (Items {', '.join(sec_item_codes)})" if sec_item_codes else ""
                headline = f"{sym} filed SEC Form {form_str}{items_desc}"
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_padded)}/{acc_nodash}/{acc_raw}-index.htm"
                verdict = score_materiality(
                    headline,
                    "",
                    symbols=(sym,),
                    held_or_target=target_set,
                    focus_set=focus_set,
                    sec_form=form_str,
                    sec_items=sec_item_codes,
                    threshold=threshold,
                )
                out.append(
                    NewsItem(
                        id=f"sec-{sym.lower()}-{acc_nodash}",
                        symbols=(sym,),
                        headline=headline,
                        summary=f"SEC EDGAR Form {form_str} filed on {filing_date_str}{items_desc}.",
                        source="SEC EDGAR",
                        published_at=published_at,
                        url=url,
                        retrieved_at=now_utc,
                        provider="sec",
                        provider_id=acc_raw,
                        kind="sec_filing",
                        sec_form=form_str,
                        sec_items=sec_item_codes,
                        materiality=verdict.score,
                        materiality_reasons=verdict.reasons,
                    )
                )
        return out

    def _dedupe_and_apply_seen(
        self,
        items: Sequence[NewsItem],
        *,
        now_utc: datetime,
        mark_seen: bool,
    ) -> tuple[NewsItem, ...]:
        by_id: dict[str, NewsItem] = {}
        for item in items:
            existing = by_id.get(item.id)
            if existing is None:
                by_id[item.id] = item
                continue
            merged_syms = tuple(dict.fromkeys([*existing.symbols, *item.symbols]))
            preferred = existing if existing.provider in {"sec", "alpaca"} else item
            by_id[item.id] = NewsItem(
                id=preferred.id,
                symbols=merged_syms,
                headline=preferred.headline,
                summary=preferred.summary or existing.summary or item.summary,
                source=preferred.source,
                published_at=min(existing.published_at, item.published_at),
                url=preferred.url or existing.url or item.url,
                updated_at=preferred.updated_at or existing.updated_at,
                retrieved_at=now_utc,
                revision=max(existing.revision, item.revision),
                provider=preferred.provider,
                provider_id=preferred.provider_id,
                kind=preferred.kind,
                sec_form=preferred.sec_form or existing.sec_form,
                sec_items=preferred.sec_items or existing.sec_items,
                materiality=max(existing.materiality, item.materiality),
                materiality_reasons=tuple(
                    dict.fromkeys([*existing.materiality_reasons, *item.materiality_reasons])
                ),
            )

        final_list: list[NewsItem] = []
        for sid, item in by_id.items():
            seen_meta = self._seen.get(sid)
            if seen_meta is None:
                first_seen = now_utc
                is_new = True
            else:
                prev_rev = int(seen_meta.get("revision", 1) or 1)
                first_seen = _safe_utc(seen_meta.get("first_seen_at"), fallback=item.published_at)
                is_new = item.revision > prev_rev

            enriched = NewsItem(
                id=item.id,
                symbols=item.symbols,
                headline=item.headline,
                summary=item.summary,
                source=item.source,
                published_at=item.published_at,
                url=item.url,
                updated_at=item.updated_at,
                first_seen_at=first_seen,
                retrieved_at=item.retrieved_at or now_utc,
                revision=item.revision,
                provider=item.provider,
                provider_id=item.provider_id,
                kind=item.kind,
                sec_form=item.sec_form,
                sec_items=item.sec_items,
                materiality=item.materiality,
                materiality_reasons=item.materiality_reasons,
                is_new=is_new,
            )
            final_list.append(enriched)
            if mark_seen and (seen_meta is None or is_new):
                self._seen[sid] = {
                    "first_seen_at": first_seen.isoformat(),
                    "seen_at": now_utc.isoformat(),
                    "revision": item.revision,
                }

        final_list.sort(key=lambda x: (x.published_at, x.materiality, x.id), reverse=True)
        return tuple(final_list)
