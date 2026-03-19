"""
utils.py - Helper functions for the Polymarket trading bot.

Covers:
  - 5-minute window math
  - Technical indicators (RSI, MACD, Bollinger Bands)
  - USD formatting
  - Telegram notifications
  - Async retry helper
"""

import time
import math
import logging
import asyncio
import functools
from typing import List, Tuple, Optional, Callable, Any

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time / Window Helpers
# ---------------------------------------------------------------------------

WINDOW_SECONDS = 300  # 5 minutes = 300 seconds


def timestamp_to_5min_window(ts: float) -> Tuple[int, int]:
    """
    Given a Unix timestamp (float), return the (window_start, window_end)
    for the 5-minute window that contains it.

    Example:
        ts=1700000150  →  window_start=1700000100, window_end=1700000400
    """
    window_start = int(math.floor(ts / WINDOW_SECONDS) * WINDOW_SECONDS)
    window_end = window_start + WINDOW_SECONDS
    return window_start, window_end


def current_window() -> Tuple[int, int]:
    """Return the 5-minute window for the current time."""
    return timestamp_to_5min_window(time.time())


def seconds_until_window_end() -> float:
    """Return seconds remaining in the current 5-minute window."""
    _, window_end = current_window()
    return max(0.0, window_end - time.time())


def next_window_start() -> int:
    """Return the Unix timestamp of the start of the NEXT 5-minute window."""
    _, window_end = current_window()
    return window_end


# ---------------------------------------------------------------------------
# Technical Indicators
# ---------------------------------------------------------------------------

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """
    Calculate the Relative Strength Index for a list of closing prices.

    Args:
        prices: List of prices, ordered oldest → newest.
        period:  RSI period (default 14).

    Returns:
        RSI value between 0 and 100, or 50.0 if insufficient data.
    """
    if len(prices) < period + 1:
        return 50.0  # neutral when not enough data

    arr = np.array(prices, dtype=float)
    deltas = np.diff(arr)

    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Initial average using simple mean over first `period` values
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    # Wilder's smoothing for remaining values
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi)


def calculate_macd(
    prices: List[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> Tuple[float, float, float]:
    """
    Calculate MACD, signal line, and histogram.

    Args:
        prices:        List of prices, ordered oldest → newest.
        fast:          Fast EMA period (default 12).
        slow:          Slow EMA period (default 26).
        signal_period: Signal EMA period (default 9).

    Returns:
        Tuple of (macd_line, signal_line, histogram).
        Returns (0.0, 0.0, 0.0) if insufficient data.
    """
    if len(prices) < slow + signal_period:
        return 0.0, 0.0, 0.0

    arr = np.array(prices, dtype=float)

    def ema(data: np.ndarray, period: int) -> np.ndarray:
        k = 2.0 / (period + 1)
        result = np.empty(len(data))
        result[0] = data[0]
        for i in range(1, len(data)):
            result[i] = data[i] * k + result[i - 1] * (1 - k)
        return result

    ema_fast = ema(arr, fast)
    ema_slow = ema(arr, slow)
    macd_line = ema_fast - ema_slow

    signal_line_arr = ema(macd_line, signal_period)
    histogram = macd_line - signal_line_arr

    return float(macd_line[-1]), float(signal_line_arr[-1]), float(histogram[-1])


def calculate_bollinger_bands(
    prices: List[float],
    period: int = 20,
    std_multiplier: float = 2.0,
) -> Tuple[float, float, float]:
    """
    Calculate Bollinger Bands.

    Args:
        prices:         List of prices, ordered oldest → newest.
        period:         Rolling average period (default 20).
        std_multiplier: Standard deviation multiplier (default 2.0).

    Returns:
        Tuple of (upper_band, middle_band, lower_band).
        Returns (0.0, 0.0, 0.0) if insufficient data.
    """
    if len(prices) < period:
        if prices:
            p = prices[-1]
            return p, p, p
        return 0.0, 0.0, 0.0

    window = np.array(prices[-period:], dtype=float)
    middle = float(np.mean(window))
    std = float(np.std(window, ddof=1))

    upper = middle + std_multiplier * std
    lower = middle - std_multiplier * std

    return upper, middle, lower


def calculate_momentum(prices: List[float], window_count: int = 10) -> float:
    """
    Calculate simple price momentum as percentage change.

    Args:
        prices:       List of prices, ordered oldest → newest.
        window_count: Number of most recent price points to use.

    Returns:
        Percentage change (e.g., 0.005 = 0.5% gain). 0.0 if insufficient data.
    """
    if len(prices) < 2:
        return 0.0
    relevant = prices[-min(window_count, len(prices)):]
    if relevant[0] == 0:
        return 0.0
    return (relevant[-1] - relevant[0]) / relevant[0]


def calculate_volatility(prices: List[float]) -> float:
    """
    Calculate annualised volatility as std-dev of log returns.

    Returns:
        Std-dev of log returns (not annualised, for intraday use).
        Returns 0.0 if insufficient data.
    """
    if len(prices) < 2:
        return 0.0
    arr = np.array(prices, dtype=float)
    # guard against zero prices
    arr = arr[arr > 0]
    if len(arr) < 2:
        return 0.0
    log_returns = np.diff(np.log(arr))
    return float(np.std(log_returns))


# ---------------------------------------------------------------------------
# Fee Calculation (Polymarket Crypto)
# ---------------------------------------------------------------------------

def polymarket_fee(shares: float, price: float) -> float:
    """
    Calculate Polymarket crypto fee per the published formula:
        fee = C × p × 0.25 × (p × (1 - p))^2

    Args:
        shares: Number of shares (contracts) traded.
        price:  Price per share (0–1).

    Returns:
        Fee in USD.
    """
    return shares * price * 0.25 * (price * (1.0 - price)) ** 2


def exchange_implied_probability(
    start_price: float,
    current_price: float,
    volatility: float,
    seconds_remaining: float,
) -> float:
    """
    Estimate the probability that the asset will close UP at window end
    using a simple log-normal model.

    Args:
        start_price:      Price at start of the 5-min window.
        current_price:    Current price.
        volatility:       Per-second log-return std-dev.
        seconds_remaining: Seconds until window close.

    Returns:
        Probability in [0, 1].
    """
    if start_price <= 0 or current_price <= 0 or seconds_remaining <= 0:
        return 0.5

    log_return = math.log(current_price / start_price)
    # Drift towards log_return; uncertainty grows with sqrt(time)
    sigma = volatility * math.sqrt(seconds_remaining) if volatility > 0 else 1e-6

    from scipy.stats import norm  # type: ignore  # optional import
    try:
        prob = float(norm.cdf(log_return / sigma))
    except Exception:
        # Fallback: linear approximation
        prob = 0.5 + (log_return / (sigma * 2.5))
        prob = max(0.01, min(0.99, prob))

    return prob


# ---------------------------------------------------------------------------
# USD Formatting
# ---------------------------------------------------------------------------

def format_usd(amount: float) -> str:
    """Format a float as a USD string, e.g. '$1,234.56'."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def format_pct(fraction: float) -> str:
    """Format a fraction as a percentage string, e.g. '3.45%'."""
    return f"{fraction * 100:.2f}%"


# ---------------------------------------------------------------------------
# Telegram Notifications
# ---------------------------------------------------------------------------

def send_telegram_message(token: str, chat_id: str, message: str) -> bool:
    """
    Send a message via Telegram Bot API.

    Args:
        token:    Telegram bot token.
        chat_id:  Target chat/channel ID.
        message:  Message text (Markdown supported).

    Returns:
        True on success, False on failure.
    """
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            # If Markdown parsing fails, retry without parse_mode
            if resp.status_code == 400 and "can't parse entities" in resp.text:
                logger.debug("Telegram Markdown failed, retrying as plain text")
                payload.pop("parse_mode", None)
                resp2 = client.post(url, json=payload)
                if resp2.status_code == 200:
                    return True
                logger.warning("Telegram error %s: %s", resp2.status_code, resp2.text)
                return False
            logger.warning("Telegram error %s: %s", resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


async def send_telegram_message_async(token: str, chat_id: str, message: str) -> bool:
    """Async version of send_telegram_message."""
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("Telegram async send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Async Retry Decorator
# ---------------------------------------------------------------------------

def async_retry(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple = (Exception,),
):
    """
    Decorator: retry an async function up to `max_retries` times with
    exponential back-off.

    Usage:
        @async_retry(max_retries=3, delay=0.5)
        async def fetch_data():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        logger.debug(
                            "%s attempt %d/%d failed (%s). Retrying in %.1fs…",
                            func.__name__, attempt, max_retries, exc, current_delay,
                        )
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.warning(
                            "%s failed after %d attempts: %s",
                            func.__name__, max_retries, exc,
                        )
            raise last_exc
        return wrapper
    return decorator


def sync_retry(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple = (Exception,),
):
    """
    Decorator: retry a synchronous function up to `max_retries` times.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        logger.debug(
                            "%s attempt %d/%d failed (%s). Retrying in %.1fs…",
                            func.__name__, attempt, max_retries, exc, current_delay,
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.warning(
                            "%s failed after %d attempts: %s",
                            func.__name__, max_retries, exc,
                        )
            raise last_exc
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Misc Helpers
# ---------------------------------------------------------------------------

def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value between lo and hi."""
    return max(lo, min(hi, value))


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that returns `default` instead of raising ZeroDivisionError."""
    if denominator == 0:
        return default
    return numerator / denominator


def round_to_tick(price: float, tick_size: float = 0.01) -> float:
    """Round price to the nearest tick size (Polymarket default 0.01)."""
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 10)
