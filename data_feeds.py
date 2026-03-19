"""
data_feeds.py - Real-time crypto price data via Crypto.com REST API.

Polls Crypto.com every ~5 seconds for ticker data and periodically
fetches 1-minute candles for indicator calculations. Thread-safe.

Provides:
  - get_current_price(asset) -> float
  - get_price_history(asset, minutes) -> [(ts, price), ...]
  - get_momentum(asset, window_seconds) -> float
  - get_volatility(asset, window_seconds) -> float
  - get_rsi(asset, period, minutes) -> float
  - get_macd(asset, ...) -> (macd, signal, histogram)
  - get_bollinger_bands(asset, ...) -> (upper, mid, lower)
"""

import json
import time
import logging
import threading
import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict

from utils import calculate_rsi, calculate_macd, calculate_bollinger_bands

logger = logging.getLogger(__name__)

# Crypto.com instrument names for each asset
INSTRUMENT_MAP = {
    "BTC": "BTC_USDT",
    "ETH": "ETH_USDT",
    "SOL": "SOL_USDT",
}


def _call_crypto_com(tool_name: str, arguments: dict) -> Optional[dict]:
    """Call Crypto.com via the external-tool CLI."""
    try:
        payload = json.dumps({
            "source_id": "crypto_com",
            "tool_name": tool_name,
            "arguments": arguments,
        })
        result = subprocess.run(
            ["external-tool", "call", payload],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.debug("external-tool error: %s", result.stderr[:200])
            return None

        raw = result.stdout.strip()
        # The output is a JSON string wrapping the result
        outer = json.loads(raw)
        if isinstance(outer, str):
            # Sometimes double-encoded; extract JSON from the text
            # Format: "Here is the Crypto.com Exchange ... {JSON}"
            brace_idx = outer.find("{")
            if brace_idx >= 0:
                return json.loads(outer[brace_idx:])
            return None
        return outer
    except Exception as e:
        logger.debug("_call_crypto_com(%s) failed: %s", tool_name, e)
        return None


def _call_crypto_com_http(tool_name: str, arguments: dict) -> Optional[dict]:
    """Fallback: call Crypto.com public API directly via HTTP."""
    import httpx
    base = "https://api.crypto.com/exchange/v1/public"
    instrument = arguments.get("instrument_name", "")

    try:
        with httpx.Client(timeout=10) as client:
            if tool_name == "get_ticker":
                resp = client.get(f"{base}/get-tickers", params={"instrument_name": instrument})
            elif tool_name == "get_candlestick":
                resp = client.get(
                    f"{base}/get-candlestick",
                    params={
                        "instrument_name": instrument,
                        "timeframe": arguments.get("timeframe", "1m"),
                    },
                )
            elif tool_name == "get_trades":
                resp = client.get(
                    f"{base}/get-trades",
                    params={
                        "instrument_name": instrument,
                        "count": arguments.get("count", 50),
                    },
                )
            else:
                return None

            if resp.status_code == 200:
                data = resp.json()
                result = data.get("result", data)
                # For get-tickers, extract single ticker from data list
                if tool_name == "get_ticker" and isinstance(result, dict):
                    ticker_list = result.get("data", [])
                    if ticker_list:
                        return ticker_list[0]
                return result
    except Exception as e:
        logger.debug("HTTP fallback for %s failed: %s", tool_name, e)
    return None


def call_crypto_com(tool_name: str, arguments: dict) -> Optional[dict]:
    """Try external-tool CLI first, fall back to direct HTTP."""
    result = _call_crypto_com(tool_name, arguments)
    if result:
        return result
    return _call_crypto_com_http(tool_name, arguments)


@dataclass
class PricePoint:
    timestamp: float
    price: float


class PriceFeed:
    """
    Polls Crypto.com for real-time price data for BTC, ETH, SOL.
    Stores a rolling window of prices and candle history.
    """

    POLL_INTERVAL = 5.0          # seconds between ticker polls
    CANDLE_INTERVAL = 60.0       # seconds between candle fetches
    MAX_HISTORY_POINTS = 1800    # max price points to keep (~30 min at 1/s)
    MAX_CANDLES = 120            # max 1-min candles to keep (2 hours)

    def __init__(self, assets: List[str] = None, history_minutes: int = 30):
        self._assets = [a.upper() for a in (assets or ["BTC", "ETH", "SOL"])]
        self._history_minutes = history_minutes
        self._lock = threading.Lock()

        # Price history: asset -> deque of PricePoint
        self._prices: Dict[str, deque] = {
            a: deque(maxlen=self.MAX_HISTORY_POINTS) for a in self._assets
        }
        # 1-min candle close prices: asset -> deque of (ts, close)
        self._candles: Dict[str, deque] = {
            a: deque(maxlen=self.MAX_CANDLES) for a in self._assets
        }
        # Latest price per asset
        self._latest: Dict[str, float] = {a: 0.0 for a in self._assets}
        self._latest_ts: Dict[str, float] = {a: 0.0 for a in self._assets}
        # 24h change from exchange (decimal, e.g. -0.04 = -4%)
        self._change_24h: Dict[str, float] = {a: 0.0 for a in self._assets}

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._candle_thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._use_cli = True  # Try CLI first, may fall back

    def start(self) -> None:
        """Start polling threads."""
        if self._running:
            return
        self._running = True

        logger.info(
            "PriceFeed: starting polling for %s (interval=%ss)",
            self._assets, self.POLL_INTERVAL,
        )

        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="price-poll")
        self._thread.start()

        self._candle_thread = threading.Thread(target=self._candle_loop, daemon=True, name="candle-poll")
        self._candle_thread.start()

    def stop(self) -> None:
        """Stop polling."""
        self._running = False
        logger.info("PriceFeed: stopped.")

    def wait_for_data(self, timeout: float = 20) -> bool:
        """Wait until at least one price point is available."""
        return self._ready.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Polling loops
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Fetch ticker data every POLL_INTERVAL seconds."""
        while self._running:
            for asset in self._assets:
                self._fetch_ticker(asset)
            time.sleep(self.POLL_INTERVAL)

    def _candle_loop(self) -> None:
        """Fetch 1-min candles every CANDLE_INTERVAL seconds."""
        # Initial fetch
        for asset in self._assets:
            self._fetch_candles(asset)

        while self._running:
            time.sleep(self.CANDLE_INTERVAL)
            for asset in self._assets:
                self._fetch_candles(asset)

    def _fetch_ticker(self, asset: str) -> None:
        """Fetch current ticker for an asset."""
        instrument = INSTRUMENT_MAP.get(asset)
        if not instrument:
            return

        try:
            data = call_crypto_com("get_ticker", {"instrument_name": instrument})
            if not data:
                return

            # Crypto.com uses 'a' for last price, 'b' for bid, 'k' for ask
            price_str = data.get("a") or data.get("last")
            if not price_str:
                return

            price = float(price_str)
            ts = time.time()

            # 24h change: Crypto.com field 'c' is 24h price change as decimal
            change_24h_str = data.get("c") or data.get("change") or "0"
            try:
                change_24h = float(change_24h_str)
            except (ValueError, TypeError):
                change_24h = 0.0

            with self._lock:
                self._prices[asset].append(PricePoint(ts, price))
                self._latest[asset] = price
                self._latest_ts[asset] = ts
                self._change_24h[asset] = change_24h

            if not self._ready.is_set():
                self._ready.set()
                logger.info("PriceFeed: first price received for %s: $%.2f", asset, price)

        except Exception as e:
            logger.debug("PriceFeed: ticker fetch failed for %s: %s", asset, e)

    def _fetch_candles(self, asset: str) -> None:
        """Fetch recent 1-min candles for indicator calculation."""
        instrument = INSTRUMENT_MAP.get(asset)
        if not instrument:
            return

        try:
            data = call_crypto_com("get_candlestick", {
                "instrument_name": instrument,
                "timeframe": "1m",
            })
            if not data:
                return

            candles_raw = data.get("data", [])
            if not candles_raw:
                return

            with self._lock:
                self._candles[asset].clear()
                for c in sorted(candles_raw, key=lambda x: x.get("t", 0)):
                    # Crypto.com uses short keys: 'c' for close, 't' for timestamp (ms)
                    close = float(c.get("c", 0) or c.get("close", 0))
                    ts_raw = c.get("t", 0) or c.get("timestamp", 0)
                    try:
                        ts_val = float(ts_raw)
                        # Convert ms to seconds if needed
                        ts = ts_val / 1000.0 if ts_val > 1e12 else ts_val
                    except (ValueError, TypeError):
                        ts = time.time()
                    self._candles[asset].append((ts, close))

        except Exception as e:
            logger.debug("PriceFeed: candle fetch failed for %s: %s", asset, e)

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def has_data(self, asset: str) -> bool:
        asset = asset.upper()
        with self._lock:
            return self._latest.get(asset, 0) > 0

    def data_age_seconds(self, asset: str) -> float:
        asset = asset.upper()
        with self._lock:
            ts = self._latest_ts.get(asset, 0)
        return time.time() - ts if ts > 0 else 9999

    def get_current_price(self, asset: str) -> float:
        asset = asset.upper()
        with self._lock:
            return self._latest.get(asset, 0.0)

    def get_price_history(self, asset: str, minutes: int = 15) -> List[Tuple[float, float]]:
        """Return list of (timestamp, price) tuples for the last N minutes."""
        asset = asset.upper()
        cutoff = time.time() - minutes * 60
        with self._lock:
            return [(p.timestamp, p.price) for p in self._prices.get(asset, []) if p.timestamp >= cutoff]

    def get_24h_change(self, asset: str) -> float:
        """Return the 24h price change as a decimal (e.g., -0.04 = -4%)."""
        asset = asset.upper()
        with self._lock:
            return self._change_24h.get(asset, 0.0)

    def get_momentum(self, asset: str, window: int = 30) -> float:
        """Price change % over the last `window` seconds."""
        asset = asset.upper()
        cutoff = time.time() - window
        with self._lock:
            points = self._prices.get(asset, deque())
            if len(points) < 2:
                return 0.0
            current = points[-1].price
            old_points = [p for p in points if p.timestamp <= cutoff]
            if not old_points:
                return 0.0
            old_price = old_points[-1].price
        if old_price <= 0:
            return 0.0
        return (current - old_price) / old_price

    def get_volatility(self, asset: str, window: int = 300) -> float:
        """Standard deviation of returns over the last `window` seconds."""
        asset = asset.upper()
        cutoff = time.time() - window
        with self._lock:
            points = [(p.timestamp, p.price) for p in self._prices.get(asset, []) if p.timestamp >= cutoff]
        if len(points) < 5:
            return 0.0
        returns = []
        for i in range(1, len(points)):
            if points[i - 1][1] > 0:
                returns.append((points[i][1] - points[i - 1][1]) / points[i - 1][1])
        if len(returns) < 3:
            return 0.0
        import math
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)

    def get_rsi(self, asset: str, period: int = 14, minutes: int = 15) -> float:
        """RSI using 1-min candle close prices."""
        prices = self._get_candle_closes(asset, minutes)
        if len(prices) < period + 1:
            return 50.0  # neutral
        return calculate_rsi(prices, period)

    def get_macd(
        self, asset: str, fast: int = 12, slow: int = 26, signal: int = 9, minutes: int = 30,
    ) -> Tuple[float, float, float]:
        """MACD line, signal line, histogram."""
        prices = self._get_candle_closes(asset, minutes)
        if len(prices) < slow + signal:
            return 0.0, 0.0, 0.0
        return calculate_macd(prices, fast, slow, signal)

    def get_bollinger_bands(
        self, asset: str, period: int = 20, std: float = 2.0, minutes: int = 30,
    ) -> Tuple[float, float, float]:
        """Upper, middle, lower Bollinger Bands."""
        prices = self._get_candle_closes(asset, minutes)
        if len(prices) < period:
            return 0.0, 0.0, 0.0
        return calculate_bollinger_bands(prices, period, std)

    def _get_candle_closes(self, asset: str, minutes: int) -> List[float]:
        """Get close prices from candles."""
        asset = asset.upper()
        cutoff = time.time() - minutes * 60
        with self._lock:
            candles = self._candles.get(asset, deque())
            return [close for ts, close in candles if ts >= cutoff]
