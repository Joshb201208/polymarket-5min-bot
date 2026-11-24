"""Timestamped equity quotes and completed split-adjusted daily bars."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from stock_agent.asset_registry import (
    AssetRegistry, _alpaca_headers, _chunks, _json_get, _notify, _now_utc, _symbol,
)
from stock_agent.config import Config

if TYPE_CHECKING:
    from stock_agent.asset_registry import HealthReport
    from stock_agent.state_store import StateStore

QuotePurpose = Literal["order_sizing", "stop_check", "valuation", "brief"]
_ET = ZoneInfo("America/New_York")
_QUOTE_AGES = {"order_sizing": 90, "stop_check": 300, "valuation": 900, "brief": 900}
_MAX_HISTORY = 504
_BAR_CACHE_AGE = timedelta(hours=2)
_REGULAR_OPEN = time(9, 30)


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
        self.calendar, self.assets, self.report, self.now = calendar, assets, report, now or _now_utc
        self._quotes: OrderedDict[str, Quote] = OrderedDict()
        self._calendar: tuple[date, date, date, dict[date, datetime]] | None = None

    def last_quote(self, symbol: str) -> Quote | None:
        """Cache only. The caller must still check the provider timestamp."""
        resolved = self.assets.resolve_symbol(symbol) if self.assets else _symbol(symbol)
        return self._quotes.get(resolved) if resolved else None

    async def get_quotes(
        self, symbols: Iterable[str], *, purpose: QuotePurpose = "brief", max_age_s: int | None = None,
    ) -> PriceBatchResult:
        """Return one fresh quote or one missing reason for each requested symbol."""
        if purpose not in _QUOTE_AGES or (max_age_s is not None and max_age_s <= 0):
            raise ValueError("unknown quote purpose or nonpositive freshness limit")
        effective, missing = self._requested(symbols)
        output: dict[str, Quote] = {}
        if effective:
            market_open = await self._market_open(self.now())
            max_age = (max_age_s if max_age_s is not None else
                       (96 * 3600 if not market_open and purpose in {"brief", "valuation"}
                        else _QUOTE_AGES[purpose]))
            feed = getattr(self.config, "ALPACA_DATA_FEED", "iex")
            found, reasons = await self._snapshots(list(effective), feed, market_open, max_age)
            for symbol in effective:
                quote = found.get(symbol)
                if quote is None:
                    quote, reason = await self._fallback(symbol, market_open, max_age, reasons[symbol])
                for requested in effective[symbol]:
                    if quote is None:
                        missing[requested] = reason
                    else:
                        output[requested] = quote
        for symbol in missing:
            resolved = self.assets.resolve_symbol(symbol) if self.assets else _symbol(symbol)
            if resolved:
                self._quotes.pop(resolved, None)
        for quote in output.values():
            self._quotes[quote.symbol] = quote
            self._quotes.move_to_end(quote.symbol)
        while len(self._quotes) > 2000:
            self._quotes.popitem(last=False)
        if effective or missing:
            await _notify(self.report, "price_service", "live_quotes", not missing,
                          "unresolved_quotes" if missing else None)
        return PriceBatchResult(output, missing)

    async def get_daily_bars(
        self, symbols: Iterable[str], *, sessions: int = 60,
        start_date: date | None = None, end_date: date | None = None,
    ) -> dict[str, list[DailyBar]]:
        """Return completed split-adjusted sessions, oldest first."""
        if type(sessions) is not int or not 1 <= sessions <= _MAX_HISTORY:
            raise ValueError(f"sessions must be in [1, {_MAX_HISTORY}]")
        effective, missing = self._requested(symbols)
        output: dict[str, list[DailyBar]] = {name: [] for name in missing}
        output.update((name, []) for names in effective.values() for name in names)
        if not effective:
            return output
        now = self.now().astimezone(UTC)
        today = now.astimezone(_ET).date()
        cutoff = now - timedelta(minutes=16)
        end_day = min(end_date or today, today)
        start_day = start_date or end_day - timedelta(days=2 * sessions + 14)
        if start_day > end_day:
            return output
        try:
            calendar = await self._session_closes(start_day, end_day, today)
            days = [day for day, close in sorted(calendar.items()) if close <= cutoff]
            if start_date is None:
                days = days[-sessions:]
            if days:
                bars = await self._bars_for_sessions(list(effective), days, cutoff, now)
                for symbol, requested in effective.items():
                    complete = self._contiguous_tail(bars.get(symbol, {}), days)
                    if (start_date is not None and len(complete) != len(days)
                            or len(complete) < min(len(days), min(15, sessions))):
                        continue
                    for name in requested:
                        output[name] = complete
        except Exception:
            await _notify(self.report, "price_service", "daily_bars", False, "daily_bars_unavailable")
            return output
        await _notify(self.report, "price_service", "daily_bars", all(output.values()),
                      None if all(output.values()) else "incomplete_daily_bars")
        return output

    def _requested(self, symbols: Iterable[str]) -> tuple[dict[str, list[str]], dict[str, str]]:
        effective: dict[str, list[str]] = {}
        missing: dict[str, str] = {}
        for raw in dict.fromkeys(symbols):
            requested = raw.strip().upper()
            symbol = self.assets.resolve_symbol(requested) if self.assets else _symbol(requested)
            if not symbol:
                missing[requested] = "inactive_or_unknown_symbol"
            elif requested not in effective.setdefault(symbol, []):
                effective[symbol].append(requested)
        return effective, missing

    async def _market_open(self, now: datetime) -> bool:
        if self.calendar is not None and hasattr(self.calendar, "is_open"):
            try:
                return self.calendar.is_open(now)
            except Exception:
                return True
        try:
            clock = await self._trading_get("/v2/clock")
            return clock.get("is_open") is True
        except Exception:
            # Never extend freshness on an unknown clock.
            return True

    async def _snapshots(self, symbols, feed, market_open, max_age):
        found, missing = {}, {}
        for batch in _chunks(symbols):
            try:
                rows = await self._alpaca_get("/v2/stocks/snapshots", {"symbols": ",".join(batch), "feed": feed})
                if not isinstance(rows, dict):
                    raise ValueError("invalid snapshot envelope")
                rows = rows.get("snapshots", rows)
                if not isinstance(rows, dict):
                    raise ValueError("invalid snapshot records")
            except Exception:
                missing.update(dict.fromkeys(batch, "alpaca_unavailable"))
                continue
            for symbol in batch:
                row = rows.get(symbol)
                if not isinstance(row, dict):
                    missing[symbol] = "alpaca_missing_snapshot"
                    continue
                trade, book = row.get("latestTrade") or {}, row.get("latestQuote") or {}
                if not isinstance(trade, dict) or not isinstance(book, dict):
                    missing[symbol] = "alpaca_invalid_snapshot"
                    continue
                bid, ask = _positive(book.get("bp")), _positive(book.get("ap"))
                if bid is not None and ask is not None and bid > ask:
                    missing[symbol] = "alpaca_crossed_quote"
                    continue
                if not _is_recent(_timestamp(book.get("t")), self.now(), max_age):
                    bid = ask = None
                previous = _previous_close(row, self.now())
                quote, reason = self._quote(symbol, trade.get("p"), trade.get("t"), bid, ask,
                                            previous, f"alpaca_{feed}", market_open, max_age)
                if quote is None:
                    missing[symbol] = reason
                else:
                    found[symbol] = quote
        return found, missing

    async def _fallback(self, symbol, market_open, max_age, primary_reason):
        cfg, failures = self.config, [primary_reason]
        if token := getattr(cfg, "FINNHUB_API_KEY", ""):
            try:
                data = await _json_get(
                    self.http, "https://finnhub.io/api/v1/quote", params={"symbol": symbol},
                    headers={"X-Finnhub-Token": token},
                )
                quote, reason = self._quote(symbol, data.get("c"), data.get("t"), None, None,
                                            data.get("pc"), "finnhub_quote", market_open, max_age)
                if quote is not None:
                    return quote, ""
                failures.append(reason)
            except Exception:
                failures.append("finnhub_unavailable")
        if (token := getattr(cfg, "FMP_API_KEY", "")) and getattr(cfg, "FMP_QUOTES_ENABLED", True):
            try:
                alias = AssetRegistry.provider_symbol(symbol, "fmp")
                rows = await _json_get(
                    self.http, "https://financialmodelingprep.com/stable/quote", params={"symbol": alias},
                    headers={"apikey": token},
                )
                data = next((entry for entry in rows if entry.get("symbol") == alias), {})
                quote, reason = self._quote(symbol, data.get("price"), data.get("timestamp"), None, None,
                                            data.get("previousClose"), "fmp_quote", market_open, max_age,
                                            data.get("changePercentage"))
                if quote is not None:
                    return quote, ""
                failures.append(reason)
            except Exception:
                failures.append("fmp_unavailable")
        return None, ";".join(dict.fromkeys(failures))

    def _quote(self, symbol, raw_price, raw_time, bid, ask, prev, source, market_open,
               max_age, change_pct=None) -> tuple[Quote | None, str]:
        now = self.now().astimezone(UTC)
        price, as_of, prev = _positive(raw_price), _timestamp(raw_time), _positive(prev)
        if price is None:
            return None, f"{source}_invalid_price"
        if as_of is None:
            return None, f"{source}_missing_timestamp"
        if not _is_recent(as_of, now, max_age):
            return None, f"{source}_stale_or_future"
        pct = (price / prev - 1) * 100 if prev is not None else _number(change_pct)
        return Quote(symbol, price, bid, ask, prev, pct, as_of, now, source, market_open), ""

    async def _trading_get(self, path, params=None):
        return await _json_get(self.http, f"{self.config.ALPACA_BASE_URL.rstrip('/')}{path}",
                               params=params, headers=_alpaca_headers(self.config))

    async def _alpaca_get(self, path, params):
        return await _json_get(self.http, f"{self.config.ALPACA_DATA_URL.rstrip('/')}{path}",
                               params=params, headers=_alpaca_headers(self.config))

    async def _session_closes(self, start: date, end: date, today: date) -> dict[date, datetime]:
        cached = self._calendar
        if cached and cached[0] <= start <= end <= cached[1] and cached[2] == today:
            return {day: close for day, close in cached[3].items() if start <= day <= end}
        broker = getattr(self.calendar, "broker", self.calendar)
        if broker is not None and hasattr(broker, "get_calendar"):
            closes = {row.date: _close(row.date, row.close) for row in await broker.get_calendar(start, end)}
        else:
            rows = await self._trading_get("/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})
            closes = {_date(row["date"]): _close(_date(row["date"]), row["close"]) for row in rows}
        self._calendar = start, end, today, closes
        return {day: close for day, close in closes.items() if start <= day <= end}

    async def _bars_for_sessions(self, symbols, days, cutoff, now):
        collected, requests = {}, {}
        for symbol in symbols:
            cached_at, cached = self._read_cache(symbol)
            if _same_bar_basis_window(cached_at, now) and all(day in cached for day in days):
                collected[symbol] = cached
                continue
            missing = [day for day in days if day not in cached]
            query_from = min(missing) if missing else days[-1]
            # Re-fetch the previous close to detect changed split adjustment.
            if overlap := [day for day in days if day < query_from and day in cached]:
                query_from = overlap[-1]
            requests.setdefault(query_from, []).append(symbol)
            collected[symbol] = cached
        for query_from, subset in requests.items():
            fetched = await self._fetch_bars(subset, query_from, days[-1], cutoff, set(days))
            rebase = []
            for symbol in subset:
                cached, recent = collected[symbol], fetched[symbol]
                overlap = {day for day in days if day >= query_from and day in cached}
                # A missing overlap cannot verify the cache's split basis. A
                # paginated retry of the whole window must stand on its own.
                changed_or_unverified = any(day not in recent or cached[day] != recent[day] for day in overlap)
                if changed_or_unverified or (cached and not overlap):
                    collected[symbol] = recent
                    if query_from > days[0]:
                        rebase.append(symbol)
                else:
                    collected[symbol] = {**cached, **recent}
            if rebase:
                collected.update(await self._fetch_bars(rebase, days[0], days[-1], cutoff, set(days)))
            for symbol in subset:
                self._write_cache(symbol, now, collected[symbol])
        return collected

    async def _fetch_bars(self, symbols, start, end, cutoff, session_days):
        bars: dict[str, dict[date, DailyBar]] = {symbol: {} for symbol in symbols}
        for batch in _chunks(symbols):
            start_at = datetime.combine(start, time.min, _ET).astimezone(UTC)
            end_at = min(cutoff, datetime.combine(end + timedelta(days=1), time.min, _ET)
                         .astimezone(UTC) - timedelta(seconds=1))
            params = {"symbols": ",".join(batch), "timeframe": "1Day", "feed": "sip", "adjustment": "split",
                      "start": start_at.isoformat(), "end": end_at.isoformat(), "limit": 10000}
            tokens = set()
            while True:
                data = await self._alpaca_get("/v2/stocks/bars", params)
                for symbol, rows in (data.get("bars") or {}).items():
                    if symbol not in bars:
                        continue
                    for row in rows:
                        if bar := _daily_bar(symbol, row):
                            if bar.session_date in session_days:
                                bars[symbol][bar.session_date] = bar
                token = data.get("next_page_token")
                if not token:
                    break
                if token in tokens or len(tokens) >= 50:
                    raise ValueError("unbounded daily data paging")
                tokens.add(token)
                params["page_token"] = token
        return bars

    @staticmethod
    def _contiguous_tail(bars, days):
        result = []
        for day in reversed(days):
            if day not in bars:
                break
            result.append(bars[day])
        return list(reversed(result))

    def _read_cache(self, symbol):
        if self.store is not None:
            try:
                data = self.store.kv_get(f"market_data:split_bars:v1:{symbol}", {})
                if data:
                    rows = (_daily_bar(symbol, row) for row in data["bars"])
                    return _timestamp(data["as_of"]), {bar.session_date: bar for bar in rows if bar}
            except (ValueError, TypeError, KeyError):
                pass
        return None, {}

    def _write_cache(self, symbol, now, bars):
        if self.store is not None:
            self.store.kv_set(f"market_data:split_bars:v1:{symbol}", {
                "as_of": now,
                "bars": [asdict(bars[day]) for day in sorted(bars)[-_MAX_HISTORY:]],
            })


def _same_bar_basis_window(cached_at, now):
    if cached_at is None or not timedelta(0) <= now - cached_at < _BAR_CACHE_AGE:
        return False
    cached_et, now_et = cached_at.astimezone(_ET), now.astimezone(_ET)
    return (cached_et.date() == now_et.date()
            and (cached_et.time() < _REGULAR_OPEN) == (now_et.time() < _REGULAR_OPEN))


def _number(value) -> Decimal | None:
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _positive(value) -> Decimal | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _timestamp(value) -> datetime | None:
    try:
        if isinstance(value, str):
            instant = datetime.fromisoformat(value)
        elif type(value) in {int, float} and value > 0:
            instant = datetime.fromtimestamp(value, UTC)
        else:
            return None
        return instant.astimezone(UTC) if instant.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _is_recent(stamp, now, seconds) -> bool:
    return stamp is not None and 0 <= (now - stamp).total_seconds() <= seconds


def _previous_close(snapshot, now):
    current = snapshot.get("dailyBar") or {}
    stamp = _timestamp(current.get("t")) if isinstance(current, dict) else None
    if stamp is not None and stamp.astimezone(_ET).date() < now.astimezone(_ET).date():
        return current.get("c")
    previous = snapshot.get("prevDailyBar") or {}
    return previous.get("c") if isinstance(previous, dict) else None


def _date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _close(day, close) -> datetime:
    return datetime.combine(day, time.fromisoformat(close) if isinstance(close, str) else close, _ET)


def _daily_bar(symbol, row) -> DailyBar | None:
    try:
        if "session_date" in row:
            day = _date(row["session_date"])
            opening, high, low, close, volume, vwap = (row[key] for key in
                                                      ("open", "high", "low", "close", "volume", "vwap"))
        else:
            stamp = _timestamp(row.get("t"))
            if stamp is None:
                return None
            day = stamp.astimezone(_ET).date()
            opening, high, low, close, volume, vwap = (row.get(key) for key in ("o", "h", "l", "c", "v", "vw"))
        opening, high, low, close = map(_positive, (opening, high, low, close))
        if any(value is None for value in (opening, high, low, close)):
            return None
        if not low <= min(opening, close) <= max(opening, close) <= high:
            return None
        if type(volume) is not int or volume < 0:
            return None
        return DailyBar(symbol, day, opening, high, low, close, volume, _positive(vwap))
    except (ValueError, TypeError, KeyError):
        return None
