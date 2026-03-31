import asyncio
import logging
from datetime import datetime, date

import httpx

from stock_agent.config import Config
from stock_agent.models import CompanyData

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"
FINNHUB_BASE = "https://finnhub.io/api/v1"
SEC_BASE = "https://data.sec.gov"


class DataFeed:
    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._fmp_semaphore = asyncio.Semaphore(5)
        self._finnhub_semaphore = asyncio.Semaphore(3)
        self._sec_semaphore = asyncio.Semaphore(5)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── FMP helpers ──────────────────────────────────────────────────

    async def _fmp_get(self, path: str, extra_params: dict | None = None) -> dict | list | None:
        async with self._fmp_semaphore:
            try:
                client = await self._get_client()
                params = {"apikey": self.config.FMP_API_KEY}
                if extra_params:
                    params.update(extra_params)
                url = f"{FMP_BASE}{path}"
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                await asyncio.sleep(0.15)  # rate-limit courtesy
                return data
            except Exception as e:
                logger.error("FMP request failed for %s: %s", path, e)
                return None

    # ── Finnhub helpers ──────────────────────────────────────────────

    async def _finnhub_get(self, path: str, extra_params: dict | None = None) -> dict | list | None:
        async with self._finnhub_semaphore:
            try:
                client = await self._get_client()
                params = {"token": self.config.FINNHUB_API_KEY}
                if extra_params:
                    params.update(extra_params)
                url = f"{FINNHUB_BASE}{path}"
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                await asyncio.sleep(1.1)  # 60/min → ~1s between calls
                return data
            except Exception as e:
                logger.error("Finnhub request failed for %s: %s", path, e)
                return None

    # ── SEC EDGAR helpers ────────────────────────────────────────────

    async def _sec_get(self, url: str) -> dict | None:
        async with self._sec_semaphore:
            try:
                client = await self._get_client()
                headers = {"User-Agent": "StockAgent josh@example.com"}
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                await asyncio.sleep(0.12)  # 10 req/sec
                return data
            except Exception as e:
                logger.error("SEC EDGAR request failed for %s: %s", url, e)
                return None

    # ── Alpaca data helpers ──────────────────────────────────────────

    async def _alpaca_data_get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            client = await self._get_client()
            headers = {
                "APCA-API-KEY-ID": self.config.ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": self.config.ALPACA_SECRET_KEY,
            }
            url = f"{self.config.ALPACA_DATA_URL}{path}"
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Alpaca data request failed for %s: %s", path, e)
            return None

    # ── Public API methods ───────────────────────────────────────────

    async def get_company_fundamentals(self, symbol: str) -> CompanyData | None:
        """Fetch comprehensive fundamental data for a single symbol."""
        profile_data, metrics_data, ratios_data, estimates_data, dcf_data, peers_data, segments_data = (
            await asyncio.gather(
                self._fmp_get("/profile", {"symbol": symbol}),
                self._fmp_get("/key-metrics", {"symbol": symbol, "period": "quarterly", "limit": "8"}),
                self._fmp_get("/ratios", {"symbol": symbol, "period": "quarterly", "limit": "8"}),
                self._fmp_get("/analyst-estimates", {"symbol": symbol, "period": "quarterly", "limit": "4"}),
                self._fmp_get("/discounted-cash-flow", {"symbol": symbol}),
                self._fmp_get("/stock-peers", {"symbol": symbol}),
                self._fmp_get("/revenue-product-segmentation", {"symbol": symbol, "period": "annual"}),
            )
        )

        if not profile_data:
            logger.warning("No profile data for %s", symbol)
            return None

        profile = profile_data[0] if isinstance(profile_data, list) and profile_data else profile_data
        if isinstance(profile, list):
            return None

        # Extract latest metrics
        latest_metrics = {}
        if isinstance(metrics_data, list) and metrics_data:
            latest_metrics = metrics_data[0]

        latest_ratios = {}
        if isinstance(ratios_data, list) and ratios_data:
            latest_ratios = ratios_data[0]

        # Extract growth data from profile
        growth_data = await self._fmp_get("/income-statement-growth", {"symbol": symbol, "period": "quarterly", "limit": "4"})
        rev_growth = None
        earn_growth = None
        if isinstance(growth_data, list) and growth_data:
            g = growth_data[0]
            rev_growth = g.get("growthRevenue")
            earn_growth = g.get("growthNetIncome")

        # Price target
        pt_data = await self._fmp_get("/price-target-summary", {"symbol": symbol})
        target_price = None
        analyst_rating = None
        if isinstance(pt_data, list) and pt_data:
            pt = pt_data[0]
            target_price = pt.get("targetConsensus") or pt.get("targetMedian")
        elif isinstance(pt_data, dict):
            target_price = pt_data.get("targetConsensus") or pt_data.get("targetMedian")

        # DCF
        dcf_value = None
        if isinstance(dcf_data, list) and dcf_data:
            dcf_value = dcf_data[0].get("dcf")
        elif isinstance(dcf_data, dict):
            dcf_value = dcf_data.get("dcf")

        # Peers
        peers = None
        if isinstance(peers_data, list) and peers_data:
            if isinstance(peers_data[0], dict):
                peers = peers_data[0].get("peersList", [])
            else:
                peers = peers_data

        # Revenue segments
        segments = None
        if isinstance(segments_data, list) and segments_data:
            segments = segments_data[0] if isinstance(segments_data[0], dict) else None
        elif isinstance(segments_data, dict):
            segments = segments_data

        # Earnings calendar for next date
        next_earnings = None
        earnings_cal = await self._finnhub_get(
            "/calendar/earnings",
            {"symbol": symbol, "from": date.today().isoformat(), "to": ""},
        )
        if isinstance(earnings_cal, dict):
            ec = earnings_cal.get("earningsCalendar", [])
            for e in ec:
                if e.get("symbol") == symbol:
                    next_earnings = e.get("date")
                    break

        try:
            return CompanyData(
                symbol=symbol,
                name=profile.get("companyName", symbol),
                sector=profile.get("sector", "Unknown"),
                industry=profile.get("industry", "Unknown"),
                market_cap=float(profile.get("marketCap") or profile.get("mktCap") or 0),
                price=float(profile.get("price", 0)),
                pe_ratio=_safe_float(latest_metrics.get("peRatio") or latest_ratios.get("priceEarningsRatio") or profile.get("peRatio")),
                forward_pe=_safe_float(latest_ratios.get("priceEarningsToGrowthRatio")),
                pb_ratio=_safe_float(latest_metrics.get("pbRatio")),
                ps_ratio=_safe_float(latest_metrics.get("priceToSalesRatio")),
                ev_ebitda=_safe_float(latest_metrics.get("enterpriseValueOverEBITDA")),
                revenue_growth_yoy=_safe_float(rev_growth),
                earnings_growth_yoy=_safe_float(earn_growth),
                gross_margin=_safe_float(latest_ratios.get("grossProfitMargin")),
                operating_margin=_safe_float(latest_ratios.get("operatingProfitMargin")),
                net_margin=_safe_float(latest_ratios.get("netProfitMargin")),
                roe=_safe_float(latest_ratios.get("returnOnEquity")),
                roic=_safe_float(latest_metrics.get("roic")),
                fcf_yield=_safe_float(latest_metrics.get("freeCashFlowYield")),
                debt_to_equity=_safe_float(latest_ratios.get("debtEquityRatio") or latest_metrics.get("debtToEquity")),
                current_ratio=_safe_float(latest_ratios.get("currentRatio")),
                revenue_ttm=_safe_float(latest_metrics.get("revenuePerShare")),
                net_income_ttm=_safe_float(latest_metrics.get("netIncomePerShare")),
                fcf_ttm=_safe_float(latest_metrics.get("freeCashFlowPerShare")),
                analyst_target_price=_safe_float(target_price),
                analyst_rating=analyst_rating,
                dcf_value=_safe_float(dcf_value),
                next_earnings_date=next_earnings,
                revenue_segments=segments,
                peers=peers,
            )
        except Exception as e:
            logger.error("Failed to build CompanyData for %s: %s", symbol, e)
            return None

    async def get_stock_quote(self, symbol: str) -> dict | None:
        """Get real-time quote for a symbol."""
        data = await self._fmp_get("/quote", {"symbol": symbol})
        if isinstance(data, list) and data:
            q = data[0]
            return {
                "symbol": symbol,
                "price": q.get("price", 0),
                "change": q.get("change", 0),
                "change_pct": q.get("changesPercentage", 0),
                "volume": q.get("volume", 0),
            }
        return None

    async def get_batch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Get quotes for multiple symbols."""
        results = {}
        # FMP supports comma-separated symbols in quote endpoint
        batch_size = 20
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            sym_str = ",".join(batch)
            data = await self._fmp_get("/quote", {"symbol": sym_str})
            if isinstance(data, list):
                for q in data:
                    sym = q.get("symbol", "")
                    results[sym] = {
                        "symbol": sym,
                        "price": q.get("price", 0),
                        "change": q.get("change", 0),
                        "change_pct": q.get("changesPercentage") or q.get("changePercentage") or 0,
                        "volume": q.get("volume", 0),
                    }
        return results

    async def get_earnings_calendar(self, from_date: str, to_date: str) -> list[dict]:
        """Get upcoming earnings from Finnhub."""
        data = await self._finnhub_get("/calendar/earnings", {"from": from_date, "to": to_date})
        if isinstance(data, dict):
            return data.get("earningsCalendar", [])
        return []

    async def screen_universe(
        self,
        min_market_cap: float | None = None,
        sectors: list[str] | None = None,
    ) -> list[str]:
        """Screen for symbols using FMP company screener."""
        if min_market_cap is None:
            min_market_cap = self.config.MIN_MARKET_CAP

        all_symbols: list[str] = []
        target_sectors = sectors or [
            "Technology",
            "Healthcare",
            "Financial Services",
            "Consumer Cyclical",
            "Industrials",
            "Consumer Defensive",
            "Communication Services",
        ]

        per_sector = max(self.config.UNIVERSE_SIZE // len(target_sectors), 10)

        for sector in target_sectors:
            data = await self._fmp_get(
                "/company-screener",
                {
                    "marketCapMoreThan": str(int(min_market_cap)),
                    "sector": sector,
                    "limit": str(per_sector),
                    "exchange": "NYSE,NASDAQ",
                },
            )
            if isinstance(data, list):
                for item in data:
                    sym = item.get("symbol", "")
                    if sym and sym not in all_symbols:
                        all_symbols.append(sym)

            await asyncio.sleep(0.2)

        logger.info("Screened %d symbols across %d sectors", len(all_symbols), len(target_sectors))
        return all_symbols[: self.config.UNIVERSE_SIZE]

    async def get_company_news(self, symbol: str, limit: int = 10) -> list[dict]:
        """Get recent news for a symbol from FMP."""
        data = await self._fmp_get("/news/stock", {"tickers": symbol, "limit": str(limit)})
        if isinstance(data, list):
            return data
        return []

    async def get_analyst_recommendations(self, symbol: str) -> list[dict]:
        """Get analyst recommendations from Finnhub."""
        data = await self._finnhub_get("/stock/recommendation", {"symbol": symbol})
        if isinstance(data, list):
            return data
        return []

    async def get_sec_filings(self, cik: str) -> dict | None:
        """Get SEC filing history for a CIK."""
        padded = cik.zfill(10)
        return await self._sec_get(f"{SEC_BASE}/submissions/CIK{padded}.json")

    async def get_recent_earnings(
        self, symbol: str, lookback_days: int = 2
    ) -> dict | None:
        """Check if a symbol reported earnings in the last N days.

        Returns dict with beat/miss info, or None if no recent earnings.
        Uses Finnhub earnings calendar.
        """
        from datetime import datetime, timedelta

        today = datetime.now().strftime("%Y-%m-%d")
        lookback = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        try:
            data = await self._finnhub_get(
                "/calendar/earnings",
                {"from": lookback, "to": today, "symbol": symbol},
            )
            if not isinstance(data, dict):
                return None

            calendar = data.get("earningsCalendar", [])
            for entry in calendar:
                if entry.get("symbol", "").upper() == symbol.upper():
                    actual = entry.get("epsActual")
                    estimate = entry.get("epsEstimate")
                    if actual is not None and estimate is not None:
                        surprise = actual - estimate
                        surprise_pct = (
                            (surprise / abs(estimate) * 100) if estimate != 0 else 0
                        )
                        return {
                            "symbol": symbol,
                            "date": entry.get("date"),
                            "eps_actual": actual,
                            "eps_estimate": estimate,
                            "surprise": surprise,
                            "surprise_pct": surprise_pct,
                            "beat": actual > estimate,
                            "revenue_actual": entry.get("revenueActual"),
                            "revenue_estimate": entry.get("revenueEstimate"),
                        }
                    # Earnings date exists but no actual yet
                    return {
                        "symbol": symbol,
                        "date": entry.get("date"),
                        "pending": True,
                        "beat": False,
                        "surprise_pct": 0,
                    }
        except Exception as e:
            logger.debug("Earnings check failed for %s: %s", symbol, e)

        return None

    async def get_historical_prices(
        self, symbol: str, from_date: str, to_date: str
    ) -> list[dict]:
        """Get historical EOD prices from FMP."""
        data = await self._fmp_get(
            f"/historical-price-eod/full",
            {"symbol": symbol, "from": from_date, "to": to_date},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("historical", [])
        return []


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        if f != f:  # NaN check
            return None
        return f
    except (ValueError, TypeError):
        return None
