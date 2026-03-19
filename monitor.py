"""
monitor.py - P&L tracking, trade logging, and performance metrics.

Responsibilities:
  - Write every trade to a CSV file (persistent)
  - Track running performance statistics
  - Generate hourly and daily reports
  - Persist stats to JSON for restart recovery
  - Emit Telegram alerts for important events
"""

import csv
import json
import math
import time
import logging
import threading
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from config import MonitorConfig, TelegramConfig
from utils import format_usd, format_pct, send_telegram_message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trade log entry schema (matches CSV columns)
# ---------------------------------------------------------------------------

@dataclass
class TradeLogEntry:
    timestamp: str
    unix_ts: float
    market_slug: str
    asset: str
    strategy: str
    direction: str          # "YES" or "NO"
    size_usd: float
    entry_price: float
    exit_price: float
    shares: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    balance_after: float
    edge: float
    confidence: float
    won: bool


CSV_FIELDS = [
    "timestamp", "unix_ts", "market_slug", "asset", "strategy",
    "direction", "size_usd", "entry_price", "exit_price", "shares",
    "gross_pnl", "fees_paid", "net_pnl", "balance_after",
    "edge", "confidence", "won",
]


# ---------------------------------------------------------------------------
# PerformanceMonitor
# ---------------------------------------------------------------------------

class PerformanceMonitor:
    """
    Logs and tracks all trade activity.

    Usage:
        monitor = PerformanceMonitor(config, telegram_config)
        monitor.log_trade(entry)
        print(monitor.get_summary())
    """

    def __init__(
        self,
        config: MonitorConfig,
        telegram_config: Optional[TelegramConfig] = None,
    ):
        self._config = config
        self._telegram = telegram_config
        self._lock = threading.Lock()

        # Running trade history (in-memory)
        self._trades: List[TradeLogEntry] = []

        # Stats cache
        self._stats: Dict = {}
        self._stats_dirty = True

        # Hourly report tracking
        self._last_hourly_report = time.time()
        self._last_daily_report = time.time()

        # Set up CSV file
        self._csv_file = config.trade_log_file
        self._stats_file = config.stats_file
        self._ensure_csv_header()

        # Load any existing stats for continuity
        self._load_persisted_stats()

        logger.info(
            "PerformanceMonitor initialised (csv=%s, stats=%s)",
            self._csv_file, self._stats_file,
        )

    # ------------------------------------------------------------------
    # Trade Logging
    # ------------------------------------------------------------------

    def log_trade(self, entry: TradeLogEntry) -> None:
        """
        Record a resolved trade.  Writes to CSV, updates in-memory stats,
        and optionally sends a Telegram notification.
        """
        with self._lock:
            self._trades.append(entry)
            self._stats_dirty = True

        # Append to CSV
        try:
            with open(self._csv_file, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                row = asdict(entry)
                row["won"] = "1" if entry.won else "0"
                writer.writerow(row)
        except Exception as exc:
            logger.error("Failed to write trade to CSV: %s", exc)

        # Save stats JSON
        stats = self._compute_stats()
        self._persist_stats(stats)

        # Log to console
        emoji = "✓" if entry.won else "✗"
        logger.info(
            "%s Trade [%s] %s %s entry=%.3f exit=%.3f pnl=%s bal=%s",
            emoji,
            entry.asset,
            entry.direction,
            entry.strategy,
            entry.entry_price,
            entry.exit_price,
            format_usd(entry.net_pnl),
            format_usd(entry.balance_after),
        )

        # Telegram alert
        self._maybe_send_trade_alert(entry)

    def log_event(self, message: str, level: str = "info") -> None:
        """Log a non-trade event (e.g., circuit breaker, startup)."""
        getattr(logger, level, logger.info)(message)
        if self._telegram and self._telegram.is_configured:
            send_telegram_message(
                self._telegram.bot_token,
                self._telegram.chat_id,
                f"[Bot] {message}",
            )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def _compute_stats(self) -> Dict:
        """Compute all performance statistics from trade history."""
        with self._lock:
            trades = list(self._trades)

        total = len(trades)
        if total == 0:
            return {"total_trades": 0}

        wins = [t for t in trades if t.won]
        losses = [t for t in trades if not t.won]
        net_pnls = [t.net_pnl for t in trades]

        total_pnl = sum(net_pnls)
        win_rate = len(wins) / total
        avg_trade = total_pnl / total
        best_trade = max(net_pnls)
        worst_trade = min(net_pnls)
        total_fees = sum(t.fees_paid for t in trades)

        # Sharpe estimate: daily Sharpe using per-trade returns
        if len(net_pnls) >= 2:
            mean = avg_trade
            variance = sum((x - mean) ** 2 for x in net_pnls) / (len(net_pnls) - 1)
            std = math.sqrt(variance) if variance > 0 else 1e-9
            sharpe = mean / std * math.sqrt(len(net_pnls))
        else:
            sharpe = 0.0

        # Drawdown (running max)
        running_pnl = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in net_pnls:
            running_pnl += pnl
            if running_pnl > peak:
                peak = running_pnl
            dd = (peak - running_pnl) / (peak if peak != 0 else 1)
            if dd > max_drawdown:
                max_drawdown = dd

        # Profit factor
        gross_wins = sum(t.net_pnl for t in wins) if wins else 0.0
        gross_losses = abs(sum(t.net_pnl for t in losses)) if losses else 1e-9
        profit_factor = gross_wins / gross_losses

        # Average win / loss
        avg_win = gross_wins / len(wins) if wins else 0.0
        avg_loss = gross_losses / len(losses) if losses else 0.0

        # By asset
        assets = {}
        for t in trades:
            a = t.asset
            if a not in assets:
                assets[a] = {"trades": 0, "wins": 0, "pnl": 0.0}
            assets[a]["trades"] += 1
            if t.won:
                assets[a]["wins"] += 1
            assets[a]["pnl"] += t.net_pnl

        # Last balance
        last_balance = trades[-1].balance_after if trades else 0.0

        stats = {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_trade_pnl": avg_trade,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "total_fees": total_fees,
            "sharpe_estimate": sharpe,
            "max_drawdown": max_drawdown,
            "last_balance": last_balance,
            "by_asset": assets,
        }

        with self._lock:
            self._stats = stats
            self._stats_dirty = False

        return stats

    def get_stats(self) -> Dict:
        """Return current performance stats (cached unless dirty)."""
        if self._stats_dirty or not self._stats:
            return self._compute_stats()
        with self._lock:
            return dict(self._stats)

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def get_summary(self) -> str:
        """Return a concise performance summary string."""
        s = self.get_stats()
        if s.get("total_trades", 0) == 0:
            return "No trades recorded yet."

        lines = [
            "═══════════════ Performance Summary ═══════════════",
            f"  Trades:      {s['total_trades']} ({s['wins']}W / {s['losses']}L)",
            f"  Win Rate:    {format_pct(s['win_rate'])}",
            f"  Total P&L:   {format_usd(s['total_pnl'])}",
            f"  Avg Trade:   {format_usd(s['avg_trade_pnl'])}",
            f"  Best Trade:  {format_usd(s['best_trade'])}",
            f"  Worst Trade: {format_usd(s['worst_trade'])}",
            f"  Profit Fac:  {s['profit_factor']:.2f}",
            f"  Fees Paid:   {format_usd(s.get('total_fees', 0))}",
            f"  Max DD:      {format_pct(s['max_drawdown'])}",
            f"  Sharpe Est:  {s['sharpe_estimate']:.2f}",
            f"  Balance:     {format_usd(s.get('last_balance', 0))}",
        ]

        asset_stats = s.get("by_asset", {})
        if asset_stats:
            lines.append("  ── By Asset ──────────────────────────────────────")
            for asset, d in asset_stats.items():
                wr = d["wins"] / d["trades"] if d["trades"] else 0
                lines.append(
                    f"  {asset:4s}: {d['trades']} trades  WR={format_pct(wr)}  "
                    f"PnL={format_usd(d['pnl'])}"
                )

        lines.append("═══════════════════════════════════════════════════")
        return "\n".join(lines)

    def get_hourly_report(self) -> str:
        """Return a summary of the last hour of activity."""
        cutoff = time.time() - 3600
        with self._lock:
            recent = [t for t in self._trades if t.unix_ts >= cutoff]

        if not recent:
            return "No trades in the last hour."

        wins = sum(1 for t in recent if t.won)
        total = len(recent)
        pnl = sum(t.net_pnl for t in recent)
        wr = wins / total

        lines = [
            f"[Hourly] {total} trades | WR={format_pct(wr)} | P&L={format_usd(pnl)}",
        ]
        for t in recent[-5:]:  # last 5 trades
            lines.append(
                f"  {'✓' if t.won else '✗'} {t.asset} {t.direction} "
                f"entry={t.entry_price:.3f} exit={t.exit_price:.3f} "
                f"pnl={format_usd(t.net_pnl)}"
            )
        return "\n".join(lines)

    def get_daily_report(self) -> str:
        """Return a full daily stats report."""
        today_midnight = time.time() - (time.time() % 86400)
        with self._lock:
            today_trades = [t for t in self._trades if t.unix_ts >= today_midnight]

        s = self.get_stats()
        daily_trades = len(today_trades)
        daily_pnl = sum(t.net_pnl for t in today_trades)
        daily_wins = sum(1 for t in today_trades if t.won)
        daily_wr = daily_wins / daily_trades if daily_trades else 0

        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        lines = [
            f"═══════════════ Daily Report ({date_str}) ═══════════════",
            f"  Today's Trades:  {daily_trades}",
            f"  Today's Win Rate: {format_pct(daily_wr)}",
            f"  Today's P&L:     {format_usd(daily_pnl)}",
            "",
            "  ── All-Time ──",
            f"  {s['total_trades']} trades | WR={format_pct(s.get('win_rate', 0))} "
            f"| P&L={format_usd(s.get('total_pnl', 0))}",
            f"  Max Drawdown: {format_pct(s.get('max_drawdown', 0))} "
            f"| Sharpe: {s.get('sharpe_estimate', 0):.2f}",
            "═══════════════════════════════════════════════════════════",
        ]
        return "\n".join(lines)

    def should_print_hourly_report(self) -> bool:
        """Returns True once per hour."""
        if not self._config.hourly_report:
            return False
        if time.time() - self._last_hourly_report >= 3600:
            self._last_hourly_report = time.time()
            return True
        return False

    def should_print_daily_report(self) -> bool:
        """Returns True once per day."""
        if not self._config.daily_report:
            return False
        if time.time() - self._last_daily_report >= 86400:
            self._last_daily_report = time.time()
            return True
        return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _ensure_csv_header(self) -> None:
        """Write CSV header if the file does not exist."""
        if not os.path.exists(self._csv_file):
            try:
                with open(self._csv_file, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                    writer.writeheader()
            except Exception as exc:
                logger.warning("Could not create CSV file: %s", exc)

    def _persist_stats(self, stats: Dict) -> None:
        """Write stats to JSON file."""
        try:
            data = {
                "updated": datetime.now(tz=timezone.utc).isoformat(),
                "stats": stats,
            }
            with open(self._stats_file, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as exc:
            logger.warning("Could not persist stats: %s", exc)

    def _load_persisted_stats(self) -> None:
        """Load previous session stats from JSON if available."""
        if not os.path.exists(self._stats_file):
            return
        try:
            with open(self._stats_file) as f:
                data = json.load(f)
            logger.info(
                "Loaded persisted stats from %s (updated %s)",
                self._stats_file,
                data.get("updated", "unknown"),
            )
        except Exception as exc:
            logger.warning("Could not load persisted stats: %s", exc)

    # ------------------------------------------------------------------
    # Telegram Alerts
    # ------------------------------------------------------------------

    def _maybe_send_trade_alert(self, entry: TradeLogEntry) -> None:
        """Send Telegram alert for notable trades."""
        if not self._telegram or not self._telegram.is_configured:
            return

        # Only alert on significant wins/losses
        if abs(entry.net_pnl) < 0.50:
            return

        emoji = "🟢" if entry.won else "🔴"
        msg = (
            f"{emoji} *{entry.asset}* {entry.direction} [{entry.strategy}]\n"
            f"Entry: {entry.entry_price:.3f} → Exit: {entry.exit_price:.3f}\n"
            f"P&L: {format_usd(entry.net_pnl)}\n"
            f"Balance: {format_usd(entry.balance_after)}"
        )
        send_telegram_message(
            self._telegram.bot_token,
            self._telegram.chat_id,
            msg,
        )
