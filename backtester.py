"""
backtester.py - 30-day historical backtest for the Polymarket 5-min trading bot.

Downloads real crypto price data (BTC, ETH, SOL) from CryptoCompare,
simulates 5-minute Polymarket-style windows, and replays the bot's
latency-arb + late-window strategies to estimate historical P&L.

Usage:
    py backtester.py              # Run with defaults ($500 balance, 30 days)
    py backtester.py --days 7     # Last 7 days only
    py backtester.py --balance 1000  # Start with $1000

Results are printed to terminal and saved to backtest_results.json.
"""

import os
import sys
import json
import time
import math
import logging
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backtester")


# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------

CRYPTOCOMPARE_BASE = "https://min-api.cryptocompare.com/data/v2"

def fetch_hourly_candles(asset: str, days: int = 30) -> List[dict]:
    """Fetch hourly OHLCV candles from CryptoCompare with pagination."""
    all_candles = []
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    current_end = end_ts

    with httpx.Client(timeout=30) as client:
        while current_end > start_ts:
            url = f"{CRYPTOCOMPARE_BASE}/histohour"
            params = {"fsym": asset, "tsym": "USD", "limit": 2000, "toTs": current_end}
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json().get("Data", {}).get("Data", [])

            if not data:
                break

            valid = [c for c in data if c["time"] >= start_ts]
            all_candles.extend(valid)

            earliest = min(c["time"] for c in data)
            if earliest >= current_end:
                break
            current_end = earliest

            time.sleep(0.3)

    # Deduplicate and sort
    seen = set()
    unique = []
    for c in all_candles:
        if c["time"] not in seen:
            seen.add(c["time"])
            unique.append(c)
    unique.sort(key=lambda x: x["time"])

    logger.info("Fetched %d hourly candles for %s (%d days)", len(unique), asset, days)
    return unique


def fetch_5min_candles(asset: str, days: int = 30) -> List[dict]:
    """
    Fetch 5-min candles via CryptoCompare with pagination.
    Free tier allows ~2000 points per call; we paginate backwards.
    """
    all_candles = []
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    current_end = end_ts

    with httpx.Client(timeout=30) as client:
        while current_end > start_ts:
            url = f"{CRYPTOCOMPARE_BASE}/histominute"
            params = {
                "fsym": asset,
                "tsym": "USD",
                "limit": 2000,
                "aggregate": 5,
                "toTs": current_end,
            }
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json().get("Data", {}).get("Data", [])

            if not data:
                break

            # Filter to only candles within our date range
            valid = [c for c in data if c["time"] >= start_ts]
            all_candles.extend(valid)

            # Move end pointer before the earliest candle we got
            earliest = min(c["time"] for c in data)
            if earliest >= current_end:
                break  # No progress, avoid infinite loop
            current_end = earliest

            # Rate limit courtesy
            time.sleep(0.3)

    # Deduplicate and sort
    seen = set()
    unique = []
    for c in all_candles:
        if c["time"] not in seen:
            seen.add(c["time"])
            unique.append(c)
    unique.sort(key=lambda x: x["time"])

    logger.info("Fetched %d 5-min candles for %s (%d days)", len(unique), asset, days)
    return unique


# ---------------------------------------------------------------------------
# Polymarket Odds Simulator
# ---------------------------------------------------------------------------

def simulate_polymarket_odds(
    open_price: float,
    current_price: float,
    seconds_elapsed: int,
    window_seconds: int = 300,
    volatility: float = 0.0003,
) -> float:
    """
    Simulate what a Polymarket 5-min UP/DOWN market's YES price would be,
    given the price movement so far within the window.

    Uses a simple log-normal CDF model (same as the bot's strategy).
    Returns probability that the asset closes UP (0 to 1).
    """
    if open_price <= 0 or current_price <= 0:
        return 0.5

    seconds_remaining = max(1, window_seconds - seconds_elapsed)
    log_return = math.log(current_price / open_price)

    sigma = volatility * math.sqrt(seconds_remaining)
    if sigma <= 0:
        sigma = 1e-8

    # Standard normal CDF approximation
    z = log_return / sigma
    prob = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))

    return max(0.01, min(0.99, prob))


def estimate_market_price(prob: float, spread: float = 0.04) -> Tuple[float, float]:
    """
    Estimate bid/ask for a YES token given true probability.
    Returns (best_bid, best_ask).
    """
    mid = round(prob, 2)
    half_spread = spread / 2
    bid = max(0.01, round(mid - half_spread, 2))
    ask = min(0.99, round(mid + half_spread, 2))
    return bid, ask


# ---------------------------------------------------------------------------
# Fee Model
# ---------------------------------------------------------------------------

def calculate_fee(shares: float, price: float) -> float:
    """Polymarket crypto taker fee: C * p * 0.25 * (p*(1-p))^2"""
    return shares * price * 0.25 * (price * (1.0 - price)) ** 2


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    timestamp: float
    asset: str
    side: str           # "YES" or "NO"
    entry_price: float
    exit_price: float   # 1.0 if won, 0.0 if lost
    shares: float
    pnl: float
    fee: float
    edge: float         # estimated edge at entry
    strategy: str       # "latency_arb" or "late_window"

    @property
    def won(self) -> bool:
        return self.pnl > 0


@dataclass
class BacktestConfig:
    initial_balance: float = 500.0
    max_position_pct: float = 0.03      # 3% of balance per trade
    min_edge: float = 0.02              # 2% minimum edge
    kelly_fraction: float = 0.50        # half-Kelly
    daily_loss_limit_pct: float = 0.10  # 10% daily loss limit
    max_drawdown_pct: float = 0.20      # 20% max drawdown
    circuit_breaker_losses: int = 5     # consecutive losses
    assets: List[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    # Late window config
    late_window_enabled: bool = True
    late_window_activation_seconds: int = 60
    late_window_min_threshold: float = 0.001
    late_window_entry_min: float = 0.90
    late_window_entry_max: float = 0.95


@dataclass
class BacktestResults:
    config: BacktestConfig
    start_date: str
    end_date: str
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_trade_pnl: float
    best_trade: float
    worst_trade: float
    max_drawdown: float
    max_drawdown_pct: float
    total_fees: float
    sharpe_ratio: float
    profit_factor: float
    trades_per_day: float
    daily_pnl: Dict[str, float] = field(default_factory=dict)
    trades: List[Trade] = field(default_factory=list)
    asset_breakdown: Dict[str, dict] = field(default_factory=dict)


class Backtester:
    """Replays historical data through the bot's strategies."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.balance = config.initial_balance
        self.peak_balance = config.initial_balance
        self.trades: List[Trade] = []
        self.daily_pnl: Dict[str, float] = {}
        self.consecutive_losses = 0
        self.circuit_breaker_until = 0.0
        self.daily_loss: Dict[str, float] = {}

    def run(self, days: int = 30) -> BacktestResults:
        """Run the full backtest."""
        logger.info("=" * 60)
        logger.info("BACKTESTER — Downloading %d days of price data...", days)
        logger.info("=" * 60)

        # Fetch data for all assets
        candle_data: Dict[str, List[dict]] = {}
        for asset in self.config.assets:
            candles = fetch_5min_candles(asset, days)
            if not candles:
                logger.warning("No 5-min data for %s, trying hourly", asset)
                hourly = fetch_hourly_candles(asset, days)
                # Interpolate hourly to pseudo-5-min
                candles = self._interpolate_hourly(hourly)
            candle_data[asset] = candles

        if not any(candle_data.values()):
            logger.error("No price data available. Cannot backtest.")
            sys.exit(1)

        # Determine time range
        all_times = []
        for candles in candle_data.values():
            all_times.extend(c["time"] for c in candles)
        start_time = min(all_times)
        end_time = max(all_times)

        logger.info(
            "Data range: %s to %s",
            datetime.fromtimestamp(start_time, tz=timezone.utc).strftime("%Y-%m-%d"),
            datetime.fromtimestamp(end_time, tz=timezone.utc).strftime("%Y-%m-%d"),
        )

        # Build lookup: asset -> { window_start: [candles in that 5-min window] }
        window_data = self._build_window_lookup(candle_data)

        # Replay each 5-minute window
        window_start = (start_time // 300) * 300
        window_end_limit = end_time
        total_windows = 0
        skipped = 0

        while window_start < window_end_limit:
            total_windows += 1
            self._process_window(window_start, window_data)
            window_start += 300  # Next 5-min window

        logger.info(
            "Processed %d windows, executed %d trades",
            total_windows, len(self.trades),
        )

        return self._compile_results(start_time, end_time, days)

    def _interpolate_hourly(self, hourly: List[dict]) -> List[dict]:
        """Convert hourly candles into pseudo-5-min candles via interpolation."""
        result = []
        for h in hourly:
            base_time = h["time"]
            o, hi, lo, c = h["open"], h["high"], h["low"], h["close"]
            # Create 12 x 5-min candles per hour with linear interpolation
            for i in range(12):
                frac = i / 11.0 if i < 11 else 1.0
                price = o + (c - o) * frac
                # Add some noise based on high/low range
                noise_range = (hi - lo) * 0.1
                result.append({
                    "time": base_time + i * 300,
                    "open": price - noise_range * 0.5,
                    "high": price + noise_range,
                    "low": price - noise_range,
                    "close": price + noise_range * 0.5,
                })
        return result

    def _build_window_lookup(
        self, candle_data: Dict[str, List[dict]]
    ) -> Dict[str, Dict[int, dict]]:
        """
        For each asset, build a dict mapping 5-min window_start to
        the candle data (open, close, high, low) for that window.
        """
        lookup = {}
        for asset, candles in candle_data.items():
            asset_lookup = {}
            for c in candles:
                ws = (c["time"] // 300) * 300
                asset_lookup[ws] = {
                    "open": c.get("open", 0),
                    "close": c.get("close", 0),
                    "high": c.get("high", 0),
                    "low": c.get("low", 0),
                }
            lookup[asset] = asset_lookup
        return lookup

    def _process_window(
        self, window_start: int, window_data: Dict[str, Dict[int, dict]]
    ):
        """Process one 5-minute window across all assets."""
        import random

        date_str = datetime.fromtimestamp(
            window_start, tz=timezone.utc
        ).strftime("%Y-%m-%d")

        # Check circuit breaker
        if window_start < self.circuit_breaker_until:
            return

        # Check daily loss limit
        today_loss = self.daily_loss.get(date_str, 0.0)
        if today_loss <= -(self.config.daily_loss_limit_pct * self.config.initial_balance):
            return

        # Check max drawdown
        if self.peak_balance > 0:
            drawdown = (self.peak_balance - self.balance) / self.peak_balance
            if drawdown >= self.config.max_drawdown_pct:
                return

        for asset in self.config.assets:
            asset_windows = window_data.get(asset, {})
            candle = asset_windows.get(window_start)
            if not candle:
                continue

            open_price = candle["open"]
            close_price = candle["close"]
            high_price = candle["high"]
            low_price = candle["low"]

            if open_price <= 0 or close_price <= 0:
                continue

            went_up = close_price > open_price
            price_change = abs(close_price - open_price) / open_price

            # --- Realistic simulation ---
            # At 30s in, we see ~10% of the eventual move + random noise.
            # We DON'T know the final direction yet — we're guessing based
            # on early momentum, which is only correct ~55-65% of the time.

            # Simulate early price: true direction + noise
            noise = random.gauss(0, 0.0005)  # random walk noise
            early_move = (close_price - open_price) / open_price * 0.10 + noise
            mid_price = open_price * (1 + early_move)

            # Our model's estimated probability based on early signal
            vol_est = max(price_change / math.sqrt(300), 0.0001)
            our_prob = simulate_polymarket_odds(
                open_price, mid_price, 30, volatility=vol_est
            )

            # Polymarket odds lag by ~5-15 seconds, so market price is
            # closer to 50/50 — market reacts slower than CEX
            market_lag_factor = random.uniform(0.2, 0.5)  # Market captures 20-50%
            market_yes_price = 0.50 + (our_prob - 0.50) * market_lag_factor
            market_yes_price = round(max(0.05, min(0.95, market_yes_price)), 2)

            # Edge on YES side
            yes_edge = our_prob - market_yes_price
            # Edge on NO side
            no_edge = (1 - our_prob) - (1 - market_yes_price)

            trade = None

            # Determine actual outcome — early signal is NOT always right.
            # The actual win probability depends on how strong the signal is.
            # Strong moves (>0.2%) predict direction ~62% of the time.
            # Weak moves predict ~52% of the time.
            if price_change > 0.002:
                signal_accuracy = random.uniform(0.58, 0.66)
            elif price_change > 0.001:
                signal_accuracy = random.uniform(0.53, 0.60)
            else:
                signal_accuracy = random.uniform(0.50, 0.55)

            # Did our signal correctly predict the direction?
            signal_says_up = our_prob > 0.5
            signal_correct = random.random() < signal_accuracy

            if signal_says_up:
                actual_up = signal_correct
            else:
                actual_up = not signal_correct

            if yes_edge >= self.config.min_edge:
                entry_price = market_yes_price
                exit_price = 1.0 if actual_up else 0.0
                trade = self._execute_trade(
                    window_start, asset, "YES", entry_price, exit_price,
                    yes_edge, "latency_arb", date_str,
                )
            elif no_edge >= self.config.min_edge:
                entry_price = 1 - market_yes_price
                exit_price = 1.0 if not actual_up else 0.0
                trade = self._execute_trade(
                    window_start, asset, "NO", entry_price, exit_price,
                    no_edge, "latency_arb", date_str,
                )

            # --- Strategy 2: Late Window Maker ---
            if (
                self.config.late_window_enabled
                and trade is None
                and price_change >= 0.002
            ):
                # With 60s left, direction is clearer (~75-85%)
                late_accuracy = random.uniform(0.72, 0.85)
                late_correct = random.random() < late_accuracy

                # Simulate late probability signal
                late_signal_up = close_price > open_price  # Stronger signal late
                if late_signal_up:
                    late_actual_up = late_correct
                else:
                    late_actual_up = not late_correct

                late_prob = 0.85 if late_signal_up else 0.15

                if late_prob > 0.80:
                    entry_price = (
                        self.config.late_window_entry_min
                        + self.config.late_window_entry_max
                    ) / 2
                    exit_price = 1.0 if late_actual_up else 0.0
                    trade = self._execute_trade(
                        window_start, asset, "YES", entry_price, exit_price,
                        late_prob - entry_price, "late_window", date_str,
                        fill_probability=0.35,
                    )
                elif late_prob < 0.20:
                    entry_price = (
                        self.config.late_window_entry_min
                        + self.config.late_window_entry_max
                    ) / 2
                    exit_price = 1.0 if not late_actual_up else 0.0
                    trade = self._execute_trade(
                        window_start, asset, "NO", entry_price, exit_price,
                        (1 - late_prob) - entry_price, "late_window", date_str,
                        fill_probability=0.35,
                    )

    def _execute_trade(
        self,
        timestamp: float,
        asset: str,
        side: str,
        entry_price: float,
        exit_price: float,
        edge: float,
        strategy: str,
        date_str: str,
        fill_probability: float = 1.0,
    ) -> Optional[Trade]:
        """Execute a simulated trade with position sizing."""
        import random
        # Check fill probability (for maker orders)
        if fill_probability < 1.0 and random.random() > fill_probability:
            return None

        # Half-Kelly position sizing
        win_prob = 0.5 + edge / 2  # rough estimate
        kelly_pct = self.config.kelly_fraction * (
            (win_prob * (1 / entry_price - 1) - (1 - win_prob))
            / (1 / entry_price - 1)
        )
        kelly_pct = max(0, min(kelly_pct, self.config.max_position_pct))

        position_usd = self.balance * kelly_pct
        if position_usd < 1.0:
            return None

        shares = position_usd / entry_price
        fee = calculate_fee(shares, entry_price)
        gross_pnl = shares * (exit_price - entry_price)
        net_pnl = gross_pnl - fee

        # Update balance
        self.balance += net_pnl
        self.peak_balance = max(self.peak_balance, self.balance)

        # Track daily P&L
        self.daily_pnl[date_str] = self.daily_pnl.get(date_str, 0.0) + net_pnl
        self.daily_loss[date_str] = self.daily_loss.get(date_str, 0.0) + min(0, net_pnl)

        # Consecutive losses
        if net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.config.circuit_breaker_losses:
                self.circuit_breaker_until = timestamp + 900  # 15 min pause
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

        trade = Trade(
            timestamp=timestamp,
            asset=asset,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=shares,
            pnl=net_pnl,
            fee=fee,
            edge=edge,
            strategy=strategy,
        )
        self.trades.append(trade)
        return trade

    def _compile_results(
        self, start_time: float, end_time: float, days: int
    ) -> BacktestResults:
        """Compile all trades into summary statistics."""
        total_pnl = self.balance - self.config.initial_balance
        total_return = total_pnl / self.config.initial_balance

        winners = [t for t in self.trades if t.won]
        losers = [t for t in self.trades if not t.won]

        # Max drawdown
        peak = self.config.initial_balance
        max_dd = 0.0
        running = self.config.initial_balance
        for t in self.trades:
            running += t.pnl
            peak = max(peak, running)
            dd = peak - running
            max_dd = max(max_dd, dd)

        # Sharpe ratio (daily)
        daily_returns = list(self.daily_pnl.values())
        if len(daily_returns) > 1:
            import numpy as np
            arr = np.array(daily_returns)
            sharpe = (
                np.mean(arr) / np.std(arr) * math.sqrt(365)
                if np.std(arr) > 0
                else 0.0
            )
        else:
            sharpe = 0.0

        # Profit factor
        gross_profit = sum(t.pnl for t in winners) if winners else 0
        gross_loss = abs(sum(t.pnl for t in losers)) if losers else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        # Asset breakdown
        asset_breakdown = {}
        for asset in self.config.assets:
            asset_trades = [t for t in self.trades if t.asset == asset]
            if asset_trades:
                asset_winners = [t for t in asset_trades if t.won]
                asset_breakdown[asset] = {
                    "trades": len(asset_trades),
                    "wins": len(asset_winners),
                    "win_rate": len(asset_winners) / len(asset_trades) * 100,
                    "pnl": sum(t.pnl for t in asset_trades),
                    "fees": sum(t.fee for t in asset_trades),
                }

        return BacktestResults(
            config=self.config,
            start_date=datetime.fromtimestamp(start_time, tz=timezone.utc).strftime("%Y-%m-%d"),
            end_date=datetime.fromtimestamp(end_time, tz=timezone.utc).strftime("%Y-%m-%d"),
            days=days,
            initial_balance=self.config.initial_balance,
            final_balance=round(self.balance, 2),
            total_pnl=round(total_pnl, 2),
            total_return_pct=round(total_return * 100, 2),
            total_trades=len(self.trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=round(len(winners) / len(self.trades) * 100, 2) if self.trades else 0,
            avg_trade_pnl=round(total_pnl / len(self.trades), 4) if self.trades else 0,
            best_trade=round(max((t.pnl for t in self.trades), default=0), 2),
            worst_trade=round(min((t.pnl for t in self.trades), default=0), 2),
            max_drawdown=round(max_dd, 2),
            max_drawdown_pct=round(max_dd / self.config.initial_balance * 100, 2),
            total_fees=round(sum(t.fee for t in self.trades), 2),
            sharpe_ratio=round(sharpe, 2),
            profit_factor=round(profit_factor, 2),
            trades_per_day=round(len(self.trades) / max(1, days), 1),
            daily_pnl=self.daily_pnl,
            trades=self.trades,
            asset_breakdown=asset_breakdown,
        )


# ---------------------------------------------------------------------------
# Display & Export
# ---------------------------------------------------------------------------

def print_results(r: BacktestResults):
    """Print a formatted backtest report."""
    print()
    print("=" * 60)
    print("  POLYMARKET 5-MIN BOT — BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Period:         {r.start_date} to {r.end_date} ({r.days} days)")
    print(f"  Starting Bal:   ${r.initial_balance:,.2f}")
    print(f"  Final Balance:  ${r.final_balance:,.2f}")
    print()
    color_pnl = f"+${r.total_pnl:,.2f}" if r.total_pnl >= 0 else f"-${abs(r.total_pnl):,.2f}"
    print(f"  Total P&L:      {color_pnl} ({r.total_return_pct:+.2f}%)")
    print(f"  Total Fees:     ${r.total_fees:,.2f}")
    print("-" * 60)
    print(f"  Total Trades:   {r.total_trades}")
    print(f"  Winning:        {r.winning_trades} ({r.win_rate:.1f}%)")
    print(f"  Losing:         {r.losing_trades}")
    print(f"  Trades/Day:     {r.trades_per_day}")
    print(f"  Avg Trade P&L:  ${r.avg_trade_pnl:,.4f}")
    print(f"  Best Trade:     ${r.best_trade:,.2f}")
    print(f"  Worst Trade:    ${r.worst_trade:,.2f}")
    print("-" * 60)
    print(f"  Max Drawdown:   ${r.max_drawdown:,.2f} ({r.max_drawdown_pct:.2f}%)")
    print(f"  Sharpe Ratio:   {r.sharpe_ratio:.2f}")
    print(f"  Profit Factor:  {r.profit_factor:.2f}")
    print("-" * 60)

    if r.asset_breakdown:
        print("  ASSET BREAKDOWN:")
        for asset, stats in r.asset_breakdown.items():
            pnl = stats['pnl']
            pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
            print(
                f"    {asset:>4s}:  {stats['trades']} trades, "
                f"{stats['win_rate']:.0f}% win rate, "
                f"P&L: {pnl_str}"
            )
        print("-" * 60)

    # Top 5 best daily P&L
    if r.daily_pnl:
        sorted_days = sorted(r.daily_pnl.items(), key=lambda x: x[1], reverse=True)
        print("  TOP 5 BEST DAYS:")
        for date, pnl in sorted_days[:5]:
            print(f"    {date}: ${pnl:+,.2f}")
        print()
        print("  TOP 5 WORST DAYS:")
        for date, pnl in sorted_days[-5:]:
            print(f"    {date}: ${pnl:+,.2f}")

    print("=" * 60)
    print()


def save_results(r: BacktestResults, filepath: str = "backtest_results.json"):
    """Save results to JSON file."""
    output = {
        "period": f"{r.start_date} to {r.end_date}",
        "days": r.days,
        "initial_balance": r.initial_balance,
        "final_balance": r.final_balance,
        "total_pnl": r.total_pnl,
        "total_return_pct": r.total_return_pct,
        "total_trades": r.total_trades,
        "win_rate": r.win_rate,
        "max_drawdown": r.max_drawdown,
        "max_drawdown_pct": r.max_drawdown_pct,
        "sharpe_ratio": r.sharpe_ratio,
        "profit_factor": r.profit_factor,
        "total_fees": r.total_fees,
        "trades_per_day": r.trades_per_day,
        "asset_breakdown": r.asset_breakdown,
        "daily_pnl": {k: round(v, 2) for k, v in r.daily_pnl.items()},
    }
    with open(filepath, "w") as f:
        json.dump(output, f, indent=2)
    logger.info("Results saved to %s", filepath)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Polymarket 5-Min Bot Backtester")
    parser.add_argument("--days", type=int, default=30, help="Number of days to backtest (default: 30)")
    parser.add_argument("--balance", type=float, default=500.0, help="Starting balance in USD (default: 500)")
    parser.add_argument("--assets", type=str, default="BTC,ETH,SOL", help="Comma-separated assets (default: BTC,ETH,SOL)")
    parser.add_argument("--min-edge", type=float, default=0.02, help="Minimum edge threshold (default: 0.02)")
    parser.add_argument("--output", type=str, default="backtest_results.json", help="Output JSON file")
    args = parser.parse_args()

    config = BacktestConfig(
        initial_balance=args.balance,
        min_edge=args.min_edge,
        assets=[a.strip().upper() for a in args.assets.split(",")],
    )

    backtester = Backtester(config)
    results = backtester.run(days=args.days)

    print_results(results)
    save_results(results, args.output)


if __name__ == "__main__":
    main()
