"""Universe discovery, company fundamentals and earnings releases on free sources.

Design: revamp notes news_data section 4 item 5, DECISIONS A9/A12.

- Universe: owner focus symbols first (always), then a versioned catalogue of
  liquid US large caps across all 11 GICS sectors, then Alpaca's most-actives
  and movers screeners. Everything except focus symbols must be tradable in the
  AssetRegistry. There is no hard-coded four-symbol fallback.
- Company snapshot: Finnhub ``/stock/metric`` ratios, SEC XBRL
  ``companyconcept`` dollar TTM figures (never per-share values), optional FMP
  ``/stable/profile`` (skipped while plan-blocked), Finnhub insider open-market
  trades (codes P/S, dollar weighted) and recommendation trend. Each metric
  carries its own provenance.
- Earnings: Finnhub ``/calendar/earnings``; dates are US/Eastern with
  bmo/amc/dmh times; a row with ``epsActual`` None is never a release; tiny
  estimates are dropped; one stable story id per fiscal quarter.

Provider facts (checked 2026-11-23):
- https://finnhub.io/docs/api/earnings-calendar (hour bmo/amc/dmh, epsActual,
  epsEstimate, quarter, year; free tier: 1 month history)
- https://finnhub.io/docs/api/insider-transactions (change, transactionCode,
  transactionPrice, filingDate, transactionDate; 100 rows per call)
- https://finnhub.io/docs/api/company-basic-financials (``metric=all``; key
  names such as peTTM/grossMarginTTM are as observed in responses: unverified
  against a published schema, so every key is optional)
- https://finnhub.io/docs/api/recommendation-trends
- https://www.sec.gov/search-filings/edgar-application-programming-interfaces
  (companyconcept/CIK##########/{taxonomy}/{tag}.json, facts per unit)
- https://docs.alpaca.markets/us/openapi/market-data-api.json
  (/v1beta1/screener/stocks/most-actives by=volume|trades top<=100;
  /v1beta1/screener/{market_type}/movers top<=50, gainers/losers with price)
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import re
import time as _time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from stock_agent.config import Config
from stock_agent.research_pipeline import SourceUnavailable
from stock_agent.research_schemas import Evidence
from stock_agent.risk_manager import CanonicalSector, classify_symbol, normalize_sector

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
FINNHUB = "https://finnhub.io/api/v1"
SEC_CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
FMP_PROFILE = "https://financialmodelingprep.com/stable/profile"
_SYMBOL = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
_HOURS = {"bmo": time(8, 0), "dmh": time(12, 0), "amc": time(16, 5)}
_TINY_ESTIMATE = 0.05
_COMPANY_TTL = timedelta(hours=12)
_MIN_SCREENER_PRICE = 5.0

# --------------------------------------------------------------------------
# Versioned catalogue. Bump the version whenever the list changes. Membership
# is a starting point only: every name is re-checked against the broker's
# asset list (tradable, not renamed) before it enters the universe.
UNIVERSE_CATALOGUE_VERSION = "2026-11-23.1"
UNIVERSE_CATALOGUE: dict[CanonicalSector, tuple[str, ...]] = {
    CanonicalSector.INFORMATION_TECHNOLOGY: (
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE", "CSCO", "ACN", "IBM", "INTU",
        "QCOM", "TXN", "NOW", "AMAT", "MU", "LRCX", "KLAC", "ADI", "PANW", "ANET", "SNPS", "CDNS",
    ),
    CanonicalSector.COMMUNICATION_SERVICES: (
        "GOOGL", "META", "NFLX", "TMUS", "DIS", "VZ", "T", "CMCSA", "EA", "TTWO",
    ),
    CanonicalSector.CONSUMER_DISCRETIONARY: (
        "AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "SBUX", "NKE", "CMG", "ORLY", "GM", "MAR",
    ),
    CanonicalSector.CONSUMER_STAPLES: (
        "WMT", "COST", "PG", "KO", "PEP", "PM", "MDLZ", "MO", "CL", "KMB", "GIS",
    ),
    CanonicalSector.ENERGY: (
        "XOM", "CVX", "COP", "EOG", "SLB", "PSX", "MPC", "OXY", "WMB", "KMI", "VLO",
    ),
    CanonicalSector.FINANCIALS: (
        "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "BLK", "SCHW", "C", "SPGI", "CB", "PGR",
        "BRK.B",
    ),
    CanonicalSector.HEALTH_CARE: (
        "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "ISRG", "AMGN", "DHR", "PFE", "GILD",
        "VRTX", "BSX", "SYK", "MDT",
    ),
    CanonicalSector.INDUSTRIALS: (
        "GE", "CAT", "RTX", "HON", "UNP", "BA", "DE", "LMT", "UPS", "ETN", "ADP", "WM", "GD", "NOC",
    ),
    CanonicalSector.MATERIALS: (
        "LIN", "SHW", "APD", "ECL", "FCX", "NEM", "DD", "NUE",
    ),
    CanonicalSector.REAL_ESTATE: (
        "PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "CCI",
    ),
    CanonicalSector.UTILITIES: (
        "NEE", "SO", "DUK", "CEG", "AEP", "SRE", "D", "EXC",
    ),
}

_CATALOGUE_SECTOR = {s: sector for sector, names in UNIVERSE_CATALOGUE.items() for s in names}

# Finnhub profile2 ``finnhubIndustry`` is an industry label, not a GICS sector.
# Keyword mapping (labels observed in responses; no published list: unverified).
_INDUSTRY_KEYWORDS: tuple[tuple[tuple[str, ...], CanonicalSector], ...] = (
    (("bank", "insurance", "financial", "capital market"), CanonicalSector.FINANCIALS),
    (("real estate", "reit"), CanonicalSector.REAL_ESTATE),
    (("utilit",), CanonicalSector.UTILITIES),
    (("oil", "gas", "energy", "coal"), CanonicalSector.ENERGY),
    (("pharma", "biotech", "health", "life science", "medical"), CanonicalSector.HEALTH_CARE),
    (("semiconductor", "technology", "software", "electronic", "communications equipment"),
     CanonicalSector.INFORMATION_TECHNOLOGY),
    (("media", "telecom", "entertainment"), CanonicalSector.COMMUNICATION_SERVICES),
    (("chemical", "metals", "mining", "packaging", "construction material"), CanonicalSector.MATERIALS),
    (("aerospace", "defense", "machinery", "airline", "logistics", "industrial", "building", "road",
      "rail", "marine", "trading companies", "professional services", "commercial services"),
     CanonicalSector.INDUSTRIALS),
    (("beverage", "food", "tobacco", "personal products", "household"), CanonicalSector.CONSUMER_STAPLES),
    (("retail", "auto", "hotel", "restaurant", "leisure", "textile", "apparel", "consumer"),
     CanonicalSector.CONSUMER_DISCRETIONARY),
)


def finnhub_industry_sector(raw: str | None) -> CanonicalSector:
    direct = normalize_sector(raw)
    if direct != CanonicalSector.UNKNOWN or not raw:
        return direct
    low = raw.casefold()
    for words, sector in _INDUSTRY_KEYWORDS:
        if any(w in low for w in words):
            return sector
    return CanonicalSector.UNKNOWN


# Finnhub metric key -> (our key, unit, scale). Finnhub reports margins,
# growth, ROE and yield in percent; we store ratios. Market cap is in millions.
_FINNHUB_METRICS: tuple[tuple[tuple[str, ...], str, str, float], ...] = (
    (("peTTM", "peBasicExclExtraTTM", "peExclExtraTTM"), "pe_ttm", "x", 1.0),
    (("pbQuarterly", "pbAnnual"), "pb", "x", 1.0),
    (("psTTM", "psAnnual"), "ps_ttm", "x", 1.0),
    (("roeTTM", "roeRfy"), "roe_ttm", "ratio", 0.01),
    (("grossMarginTTM", "grossMarginAnnual"), "gross_margin_ttm", "ratio", 0.01),
    (("netProfitMarginTTM", "netProfitMarginAnnual"), "net_margin_ttm", "ratio", 0.01),
    (("revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy"), "revenue_growth_yoy", "ratio", 0.01),
    (("epsGrowthTTMYoy", "epsGrowthQuarterlyYoy"), "eps_growth_yoy", "ratio", 0.01),
    (("totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual"), "debt_to_equity", "x", 1.0),
    (("beta",), "beta", "x", 1.0),
    (("marketCapitalization",), "market_cap_usd", "USD", 1_000_000.0),
    (("currentDividendYieldTTM", "dividendYieldIndicatedAnnual"), "dividend_yield", "ratio", 0.01),
)

# SEC us-gaap tags tried in order; values are dollars (unit USD), never per-share.
_SEC_CONCEPTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("revenue_ttm_usd", ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")),
    ("net_income_ttm_usd", ("NetIncomeLoss",)),
    ("operating_cash_flow_ttm_usd", ("NetCashProvidedByUsedInOperatingActivities",)),
)


@dataclass(frozen=True)
class MetricProvenance:
    value: float
    unit: str
    period: str            # "TTM", "FY", "MRQ", "current", "90d", "month"
    as_of: datetime | None
    source: str            # "finnhub", "sec", "fmp"
    url: str


@dataclass(frozen=True)
class CompanySnapshot:
    symbol: str
    company_name: str | None
    canonical_sector: str
    tradable: bool | None
    metrics: dict[str, MetricProvenance] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class EarningsRelease:
    symbol: str
    published_at: datetime        # UTC
    is_published: bool
    url: str
    story_id: str                 # earnings:SYM:YYYY-Qn (fiscal)
    eps_actual: float | None
    eps_estimate: float | None
    source: str = "Finnhub earnings calendar"
    hour: str | None = None


def _finite(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _norm(symbol: str) -> str:
    return symbol.strip().upper().replace("-", ".")


def _date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def release_time(day: date, hour: str | None) -> datetime:
    """UTC timestamp of a release; unknown hour is treated as after the close."""
    return datetime.combine(day, _HOURS.get((hour or "").lower(), _HOURS["amc"]), ET).astimezone(UTC)


def sec_ttm(units: Mapping[str, Any], today: date) -> tuple[float, str, date] | None:
    """Trailing-twelve-month dollars from companyconcept ``units`` -> (value, period, end).

    Uses four contiguous quarters when present, else latest FY + current YTD -
    prior-year YTD, else the latest FY. Ignores anything ending > 15 months ago.
    """
    facts: dict[tuple[date, date], tuple[str, float]] = {}
    for row in units.get("USD") or ():
        start, end, val = _date(row.get("start")), _date(row.get("end")), _finite(row.get("val"))
        if start is None or end is None or val is None or end < start:
            continue
        filed = str(row.get("filed") or "")
        key = (start, end)
        if key not in facts or filed > facts[key][0]:
            facts[key] = (filed, val)
    if not facts:
        return None
    span = {k: (k[1] - k[0]).days for k in facts}
    quarters = sorted((k for k, d in span.items() if 80 <= d <= 100), key=lambda k: k[1])
    annuals = sorted((k for k, d in span.items() if 350 <= d <= 380), key=lambda k: k[1])
    latest_end = max(k[1] for k in facts)
    if latest_end < today - timedelta(days=460):
        return None
    if len(quarters) >= 4:
        last4 = quarters[-4:]
        contiguous = all(0 <= (b[0] - a[1]).days <= 7 for a, b in zip(last4, last4[1:], strict=False))
        if contiguous and last4[-1][1] >= latest_end - timedelta(days=7):
            return sum(facts[k][1] for k in last4), "TTM", last4[-1][1]
    if not annuals:
        return None
    fy = annuals[-1]
    if latest_end > fy[1] + timedelta(days=7):
        ytd = [k for k in facts if abs((k[0] - fy[1]).days - 1) <= 7 and abs((k[1] - latest_end).days) <= 3]
        if ytd:
            cur = ytd[0]
            prior = [k for k in facts if abs((k[0] - fy[0]).days) <= 7
                     and abs((k[1] - (cur[1] - timedelta(days=365))).days) <= 7]
            if prior:
                return facts[fy][1] + facts[cur][1] - facts[prior[0]][1], "TTM", cur[1]
    return facts[fy][1], "FY", fy[1]


class FundamentalsService:
    """Free-source fundamentals, universe and earnings for the research pipeline."""

    def __init__(
        self, http, *, health=None, assets=None, prices=None, config=Config,
        focus_symbols: Iterable[str] | Callable[[], Iterable[str]] = (),
        now: Callable[[], datetime] | None = None,
        universe_size: int = 150, release_lookback_days: int = 3, finnhub_per_minute: int = 45,
    ):
        self.http, self.health, self.assets, self.prices, self.config = http, health, assets, prices, config
        self._focus = focus_symbols
        self.now = now or (lambda: datetime.now(UTC))
        self.universe_size = universe_size
        self.release_lookback_days = release_lookback_days
        self._companies: dict[str, CompanySnapshot] = {}
        # Finnhub free tier allows 60 calls/minute; stay under it, leaving room for news.
        self.finnhub_per_minute = finnhub_per_minute
        self._finnhub_calls: deque[float] = deque()
        self._finnhub_lock = asyncio.Lock()

    def cached_company(self, symbol: str, max_age: timedelta = timedelta(days=3)) -> CompanySnapshot | None:
        """Last snapshot for ``symbol`` without any request; None if absent or older than ``max_age``."""
        snap = self._companies.get(_norm(symbol))
        if snap is None or snap.fetched_at is None or self.now() - snap.fetched_at > max_age:
            return None
        return snap

    async def _finnhub_slot(self) -> None:
        async with self._finnhub_lock:
            while True:
                t = _time.monotonic()
                while self._finnhub_calls and t - self._finnhub_calls[0] >= 60.0:
                    self._finnhub_calls.popleft()
                if len(self._finnhub_calls) < self.finnhub_per_minute:
                    self._finnhub_calls.append(t)
                    return
                await asyncio.sleep(min(5.0, 60.0 - (t - self._finnhub_calls[0]) + 0.05))

    # ------------------------------------------------------------ plumbing
    def _report(self, provider: str, capability: str, ok: bool, error: str | None = None, *,
                status_code: int | None = None, symbol: str | None = None) -> None:
        if self.health is None:
            return
        try:
            self.health.report(provider, capability, ok, error, status_code=status_code, symbol=symbol)
        except Exception:  # health bookkeeping must never break data fetches
            logger.exception("health report failed")

    def _blocked(self, provider: str, capability: str, symbol: str | None = None) -> bool:
        if self.health is None:
            return False
        try:
            return bool(self.health.is_blocked(provider, capability, symbol))
        except Exception:
            return False

    async def _json(self, provider: str, capability: str, url: str, *, params=None,
                    symbol: str | None = None) -> Any | None:
        """GET JSON; report health; None on any failure (status, transport, bad JSON)."""
        if provider == "finnhub":
            await self._finnhub_slot()
        try:
            resp = await self.http.get(url, params=params)
        except Exception as exc:
            self._report(provider, capability, False, f"transport: {type(exc).__name__}", symbol=symbol)
            return None
        code = resp.status_code
        if code == 404 and provider == "sec":
            self._report(provider, capability, True)
            return {}      # concept not filed by this company: a healthy empty answer
        if code >= 400:
            self._report(provider, capability, False, f"HTTP {code}", status_code=code, symbol=symbol)
            return None
        try:
            data = resp.json()
        except ValueError:
            self._report(provider, capability, False, "invalid JSON", status_code=code, symbol=symbol)
            return None
        self._report(provider, capability, True)
        return data

    def focus(self) -> tuple[str, ...]:
        raw = self._focus() if callable(self._focus) else self._focus
        if not raw:
            from stock_agent.research_focus import focus_symbols
            raw = focus_symbols(now=self.now())
        return tuple(dict.fromkeys(_norm(s) for s in raw if s and s.strip()))

    def _meta(self, symbol: str):
        if self.assets is None:
            return None
        try:
            return self.assets.get_asset_meta(symbol)
        except Exception:
            return None

    def _registry_ready(self) -> bool:
        try:
            return self.assets is not None and bool(self.assets.active_symbols())
        except Exception:
            return False

    def _tradable(self, symbol: str) -> str | None:
        """Broker symbol if tradable and not renamed, else None."""
        try:
            resolved = self.assets.resolve_symbol(symbol)
        except Exception:
            return None
        if not resolved:
            return None
        meta = self._meta(resolved)
        return resolved if meta is not None and meta.tradable and not meta.renamed_to else None

    # ------------------------------------------------------------ universe
    async def screener_symbols(self) -> tuple[str, ...]:
        base = f"{self.config.ALPACA_DATA_URL.rstrip('/')}/v1beta1/screener/stocks"
        actives = await self._json("alpaca", "screener", f"{base}/most-actives", params={"by": "volume", "top": 50})
        movers = await self._json("alpaca", "screener", f"{base}/movers", params={"top": 20})
        out: list[str] = []
        for row in (actives or {}).get("most_actives") or ():
            out.append(str(row.get("symbol") or ""))
        for side in ("gainers", "losers"):
            for row in (movers or {}).get(side) or ():
                px = _finite(row.get("price"))
                if px is not None and px >= _MIN_SCREENER_PRICE:   # movers lists penny stocks and warrants
                    out.append(str(row.get("symbol") or ""))
        return tuple(dict.fromkeys(s for s in (_norm(x) for x in out if x) if _SYMBOL.match(s)))

    async def _price_ok(self, symbols: tuple[str, ...]) -> set[str]:
        if not symbols or self.prices is None:
            return set(symbols)
        try:
            batch = await self.prices.get_quotes(list(symbols), purpose="brief")
        except Exception:
            return set()
        return {s for s, q in batch.quotes.items() if (_finite(q.price) or 0.0) >= _MIN_SCREENER_PRICE}

    async def list_universe_with_baseline(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        focus = self.focus()
        catalogue_fixed = tuple(s for names in UNIVERSE_CATALOGUE.values() for s in names)
        screened = await self.screener_symbols()
        ready = self._registry_ready()
        picked_rr: list[str] = []
        picked_fixed: list[str] = []
        if ready:
            sector_queues = [
                [t for s in names if (t := self._tradable(s))]
                for names in UNIVERSE_CATALOGUE.values()
            ]
            picked_rr.extend(
                t for row in itertools.zip_longest(*sector_queues) for t in row if t is not None
            )
            picked_fixed.extend(t for s in catalogue_fixed if (t := self._tradable(s)))
            seen_cat = set(picked_fixed)
            extra = tuple(t for t in (self._tradable(s) for s in screened) if t and t not in seen_cat)
            ok = await self._price_ok(extra)
            tail = [s for s in extra if s in ok]
            picked_rr.extend(tail)
            picked_fixed.extend(tail)
        else:
            # Registry not loaded yet: keep the catalogue in sector round-robin order so research
            # continues; AssetRegistry still decides tradability before any order.
            picked_rr.extend(
                s for row in itertools.zip_longest(*UNIVERSE_CATALOGUE.values()) for s in row if s is not None
            )
            picked_fixed.extend(catalogue_fixed)
            logger.warning("asset registry empty: universe catalogue %s not validated", UNIVERSE_CATALOGUE_VERSION)
        cap = max(0, self.universe_size)
        rest_rr = [s for s in dict.fromkeys(picked_rr) if s not in focus][:cap]
        rest_fixed = [s for s in dict.fromkeys(picked_fixed) if s not in focus][:cap]
        return (
            tuple(dict.fromkeys((*focus, *rest_rr))),
            tuple(dict.fromkeys((*focus, *rest_fixed))),
        )

    async def list_universe(self) -> tuple[str, ...]:
        return (await self.list_universe_with_baseline())[0]

    # ------------------------------------------------------------ company
    async def _finnhub_metrics(self, symbol: str, now: datetime) -> tuple[dict, Evidence | None]:
        if not self.config.FINNHUB_API_KEY:
            return {}, None
        url = f"{FINNHUB}/stock/metric"
        data = await self._json("finnhub", "fundamentals", url, params={"symbol": symbol, "metric": "all"},
                                symbol=symbol)
        metric = (data or {}).get("metric") if isinstance(data, dict) else None
        if not isinstance(metric, dict):
            return {}, None
        out: dict[str, MetricProvenance] = {}
        public = f"https://finnhub.io/docs/api/company-basic-financials?symbol={symbol}"
        for keys, ours, unit, scale in _FINNHUB_METRICS:
            for k in keys:
                v = _finite(metric.get(k))
                if v is not None:
                    period = "TTM" if "TTM" in k else ("MRQ" if "Quarterly" in k else "FY")
                    if ours in ("beta", "market_cap_usd"):
                        period = "current"
                    out[ours] = MetricProvenance(v * scale, unit, period, now, "finnhub", public)
                    break
        if not out:
            return {}, None
        ev = Evidence(evidence_id=f"finnhub:metric:{symbol}:{now.date().isoformat()}", url=public,
                      title=f"{symbol} basic financials", published_at=None, retrieved_at=now,
                      publisher="Finnhub", snippet=_snippet(out, ("pe_ttm", "gross_margin_ttm", "revenue_growth_yoy")))
        return out, ev

    async def _sec_dollars(self, symbol: str, cik: str | None, now: datetime) -> tuple[dict, Evidence | None]:
        if not cik:
            return {}, None
        cik = str(cik).zfill(10)
        out: dict[str, MetricProvenance] = {}
        answered = False
        for ours, tags in _SEC_CONCEPTS:
            for tag in tags:
                url = SEC_CONCEPT.format(cik=cik, tag=tag)
                data = await self._json("sec", "edgar", url, symbol=symbol)
                if data is None:
                    return out, _sec_evidence(symbol, cik, out, now) if out else None  # SEC down: stop early
                answered = True
                got = sec_ttm((data or {}).get("units") or {}, now.date()) if isinstance(data, dict) else None
                if got:
                    value, period, end = got
                    out[ours] = MetricProvenance(value, "USD", period, datetime.combine(end, time(), UTC), "sec", url)
                    break
        if not answered or not out:
            return out, None
        return out, _sec_evidence(symbol, cik, out, now)

    async def _fmp_profile(self, symbol: str) -> dict:
        if not self.config.FMP_API_KEY or self._blocked("fmp", "fundamentals", symbol):
            return {}
        data = await self._json("fmp", "fundamentals", FMP_PROFILE,
                                params={"symbol": symbol.replace(".", "-")}, symbol=symbol)
        return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}

    async def _finnhub_profile(self, symbol: str) -> dict:
        if not self.config.FINNHUB_API_KEY:
            return {}
        data = await self._json("finnhub", "fundamentals", f"{FINNHUB}/stock/profile2", params={"symbol": symbol},
                                symbol=symbol)
        return data if isinstance(data, dict) else {}

    async def insider_net_usd(self, symbol: str, now: datetime) -> float | None:
        """Net open-market insider dollars over 90 days: buys (P) minus sells (S)."""
        if not self.config.FINNHUB_API_KEY:
            return None
        start = (now - timedelta(days=90)).date()
        data = await self._json("finnhub", "insider", f"{FINNHUB}/stock/insider-transactions",
                                params={"symbol": symbol, "from": start.isoformat(), "to": now.date().isoformat()},
                                symbol=symbol)
        if not isinstance(data, dict):
            return None
        net = 0.0
        for row in data.get("data") or ():
            code = str(row.get("transactionCode") or "").upper()
            shares, price = _finite(row.get("change")), _finite(row.get("transactionPrice"))
            day = _date(row.get("transactionDate") or row.get("filingDate"))
            if code not in ("P", "S") or not shares or not price or price <= 0 or day is None or day < start:
                continue   # awards, option exercises, gifts and tax withholding are not conviction trades
            net += (abs(shares) if code == "P" else -abs(shares)) * price
        return net

    async def recommendation(self, symbol: str) -> tuple[float, float | None] | None:
        """(buy ratio of latest month, change vs prior month)."""
        if not self.config.FINNHUB_API_KEY:
            return None
        data = await self._json("finnhub", "recommendation", f"{FINNHUB}/stock/recommendation",
                                params={"symbol": symbol}, symbol=symbol)
        if not isinstance(data, list):
            return None
        ratios = []
        for row in sorted((r for r in data if isinstance(r, dict)), key=lambda r: str(r.get("period")), reverse=True):
            counts = [_finite(row.get(k)) or 0.0 for k in ("strongBuy", "buy", "hold", "sell", "strongSell")]
            total = sum(counts)
            if total > 0:
                ratios.append((counts[0] + counts[1]) / total)
        if not ratios:
            return None
        return ratios[0], (ratios[0] - ratios[1]) if len(ratios) > 1 else None

    async def fetch_company(self, symbol: str) -> CompanySnapshot:
        symbol = _norm(symbol)
        now = self.now()
        cached = self._companies.get(symbol)
        if cached and cached.fetched_at and now - cached.fetched_at < _COMPANY_TTL:
            return cached
        meta = self._meta(symbol)
        cik = getattr(meta, "cik", None)
        (fin, fin_ev), (sec, sec_ev), profile, fmp = await asyncio.gather(
            self._finnhub_metrics(symbol, now), self._sec_dollars(symbol, cik, now),
            self._finnhub_profile(symbol), self._fmp_profile(symbol))
        if not fin and not sec:
            raise SourceUnavailable(f"no fundamentals source answered for {symbol}")
        metrics = {**fin, **sec}
        if fmp:
            url = f"https://site.financialmodelingprep.com/developer/docs/stable/profile-symbol?symbol={symbol}"
            for key, raw, unit in (("market_cap_usd", "marketCap", "USD"), ("beta", "beta", "x")):
                v = _finite(fmp.get(raw))
                if key not in metrics and v is not None:
                    metrics[key] = MetricProvenance(v, unit, "current", now, "fmp", url)
        insider, rec = await asyncio.gather(self.insider_net_usd(symbol, now), self.recommendation(symbol))
        ins_url = f"https://finnhub.io/docs/api/insider-transactions?symbol={symbol}"
        if insider is not None:
            metrics["insider_net_usd_90d"] = MetricProvenance(insider, "USD", "90d", now, "finnhub", ins_url)
        if rec is not None:
            rec_url = f"https://finnhub.io/docs/api/recommendation-trends?symbol={symbol}"
            metrics["analyst_buy_ratio"] = MetricProvenance(rec[0], "ratio", "month", now, "finnhub", rec_url)
            if rec[1] is not None:
                metrics["analyst_trend_delta"] = MetricProvenance(rec[1], "ratio", "month", now, "finnhub", rec_url)
        name = (getattr(meta, "company_name", None) or profile.get("name") or fmp.get("companyName") or None)
        sector = getattr(meta, "canonical_sector", CanonicalSector.UNKNOWN) or CanonicalSector.UNKNOWN
        if sector == CanonicalSector.UNKNOWN:
            sector = _CATALOGUE_SECTOR.get(symbol, CanonicalSector.UNKNOWN)
        if sector == CanonicalSector.UNKNOWN:
            sector = classify_symbol(symbol, fmp.get("sector")).sector
        if sector == CanonicalSector.UNKNOWN:
            sector = finnhub_industry_sector(profile.get("finnhubIndustry"))
        snap = CompanySnapshot(
            symbol=symbol, company_name=name, canonical_sector=str(getattr(sector, "value", sector)),
            tradable=(bool(meta.tradable and not meta.renamed_to) if meta is not None else None),
            metrics=metrics, evidence=tuple(e for e in (fin_ev, sec_ev) if e is not None), fetched_at=now)
        self._companies[symbol] = snap
        return snap

    # ------------------------------------------------------------ earnings
    async def fetch_releases(self, symbols: tuple[str, ...], now: datetime) -> tuple[EarningsRelease, ...]:
        """Verified, already-published releases within the lookback window."""
        wanted = {_norm(s) for s in symbols}
        if not wanted or not self.config.FINNHUB_API_KEY:
            return ()
        start = (now.astimezone(ET) - timedelta(days=self.release_lookback_days)).date()
        data = await self._json("finnhub", "earnings_calendar", f"{FINNHUB}/calendar/earnings",
                                params={"from": start.isoformat(), "to": now.astimezone(ET).date().isoformat()})
        if not isinstance(data, dict):
            raise SourceUnavailable("earnings calendar unavailable")
        out: dict[str, EarningsRelease] = {}
        for row in data.get("earningsCalendar") or ():
            symbol = _norm(str(row.get("symbol") or ""))
            day = _date(row.get("date"))
            actual = _finite(row.get("epsActual"))
            if symbol not in wanted or day is None or actual is None:
                continue   # no actual EPS = not released, whatever the date says
            published = release_time(day, row.get("hour"))
            if published > now or published < now - timedelta(days=self.release_lookback_days + 1):
                continue
            estimate = _finite(row.get("epsEstimate"))
            if estimate is not None and abs(estimate) < _TINY_ESTIMATE:
                estimate = None   # surprise vs a near-zero estimate is meaningless
            year, quarter = row.get("year"), row.get("quarter")
            period = f"{int(year)}-Q{int(quarter)}" if _finite(year) and _finite(quarter) else day.isoformat()
            story_id = f"earnings:{symbol}:{period}"
            if story_id in out:
                continue
            out[story_id] = EarningsRelease(
                symbol=symbol, published_at=published, is_published=True,
                url="https://finnhub.io/docs/api/earnings-calendar", story_id=story_id,
                eps_actual=actual, eps_estimate=estimate, hour=row.get("hour") or None)
        return tuple(sorted(out.values(), key=lambda r: (r.published_at, r.symbol)))


def _snippet(metrics: Mapping[str, MetricProvenance], keys: Iterable[str]) -> str:
    parts = [f"{k}={metrics[k].value:.4g}" for k in keys if k in metrics]
    return "; ".join(parts) or "basic financial ratios"


def _sec_evidence(symbol: str, cik: str, metrics: Mapping[str, MetricProvenance], now: datetime) -> Evidence:
    first = next(iter(metrics.values()))
    return Evidence(evidence_id=f"sec:xbrl:{symbol}:{now.date().isoformat()}", url=first.url,
                    title=f"{symbol} SEC XBRL financial data", published_at=first.as_of, retrieved_at=now,
                    publisher="SEC EDGAR", snippet=_snippet(metrics, (k for k, _ in _SEC_CONCEPTS)))
