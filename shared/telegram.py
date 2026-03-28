"""Shared Telegram reporting — combined NBA + NHL daily/weekly summaries.

This module provides functions for sending combined reports that show
both NBA and NHL performance. Individual trade alerts stay in each
agent's own telegram module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from shared.config import SharedConfig
from shared import line_tracker, whale_detector
from nba_agent.utils import format_dollars, format_pct

logger = logging.getLogger(__name__)


class CombinedTelegramReporter:
    """Sends combined NBA + NHL reports to Telegram."""

    def __init__(self, config: SharedConfig | None = None) -> None:
        self.config = config or SharedConfig()
        self.token = self.config.TELEGRAM_BOT_TOKEN
        self.chat_id = self.config.TELEGRAM_CHAT_ID
        self.api_base = self.config.TELEGRAM_API_BASE

    async def send_message(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            return False

        url = f"{self.api_base}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return False

    async def send_combined_daily_summary(
        self,
        bankroll: float,
        nba_stats: dict,
        nhl_stats: dict,
        mode: str,
        auto_hedge_count: int = 0,
        stop_loss_count: int = 0,
    ) -> bool:
        """Send combined daily P&L summary for both sports."""
        nba_pnl = nba_stats.get("daily_pnl", 0)
        nhl_pnl = nhl_stats.get("daily_pnl", 0)
        total_pnl = nba_pnl + nhl_pnl

        pnl_sign = "+" if total_pnl >= 0 else ""
        date_str = nba_stats.get("date_str", datetime.now(timezone.utc).strftime("%B %d, %Y"))

        nba_open = nba_stats.get("open_positions", 0)
        nhl_open = nhl_stats.get("open_positions", 0)
        nba_trades = nba_stats.get("trades_today", 0)
        nhl_trades = nhl_stats.get("trades_today", 0)

        text = (
            f"<b>DAILY P&L SUMMARY — {date_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>COMBINED</b>\n"
            f"Today's P&L: {pnl_sign}{format_dollars(total_pnl)}\n"
            f"Bankroll: {format_dollars(bankroll)}\n"
            f"Open Positions: {nba_open + nhl_open}\n\n"
        )

        if nba_trades > 0 or nba_open > 0:
            nba_sign = "+" if nba_pnl >= 0 else ""
            text += (
                f"<b>NBA</b>\n"
                f"P&L: {nba_sign}{format_dollars(nba_pnl)} | "
                f"Trades: {nba_trades} | Open: {nba_open}\n\n"
            )

        if nhl_trades > 0 or nhl_open > 0:
            nhl_sign = "+" if nhl_pnl >= 0 else ""
            text += (
                f"<b>NHL</b>\n"
                f"P&L: {nhl_sign}{format_dollars(nhl_pnl)} | "
                f"Trades: {nhl_trades} | Open: {nhl_open}\n\n"
            )

        # Line movement summary
        line_summary = line_tracker.get_summary()
        whale_count = whale_detector.get_movement_count(days=1)

        text += "<b>SIGNALS</b>\n"
        text += (
            f"Line Movements: {line_summary['moving_toward_us']} toward / "
            f"{line_summary['moving_against_us']} against\n"
        )
        if auto_hedge_count > 0 or stop_loss_count > 0:
            text += f"Auto-Exits: {auto_hedge_count} hedge / {stop_loss_count} stop-loss\n"
        if whale_count > 0:
            text += f"Whale Alerts: {whale_count}\n"
        text += "\n"

        text += f"Mode: {mode.upper()}"

        return await self.send_message(text)

    async def send_combined_weekly_summary(
        self,
        bankroll: float,
        nba_stats: dict,
        nhl_stats: dict,
        mode: str,
    ) -> bool:
        """Send combined weekly summary."""
        nba_pnl = nba_stats.get("total_pnl", 0)
        nhl_pnl = nhl_stats.get("total_pnl", 0)
        total_pnl = nba_pnl + nhl_pnl
        pnl_sign = "+" if total_pnl >= 0 else ""

        nba_bets = nba_stats.get("total_bets", 0)
        nhl_bets = nhl_stats.get("total_bets", 0)
        nba_wins = nba_stats.get("wins", 0)
        nhl_wins = nhl_stats.get("wins", 0)
        total_bets = nba_bets + nhl_bets
        total_wins = nba_wins + nhl_wins
        total_losses = total_bets - total_wins
        wr = (total_wins / total_bets * 100) if total_bets > 0 else 0

        text = (
            f"<b>WEEKLY REPORT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>COMBINED</b>\n"
            f"Bets: {total_bets} ({total_wins}W / {total_losses}L)\n"
            f"Win Rate: {wr:.1f}%\n"
            f"P&L: {pnl_sign}{format_dollars(total_pnl)}\n"
            f"Bankroll: {format_dollars(bankroll)}\n\n"
        )

        if nba_bets > 0:
            nba_sign = "+" if nba_pnl >= 0 else ""
            nba_wr = (nba_wins / nba_bets * 100) if nba_bets > 0 else 0
            text += (
                f"<b>NBA</b>: {nba_bets} bets, {nba_wr:.0f}% WR, "
                f"{nba_sign}{format_dollars(nba_pnl)}\n"
            )

        if nhl_bets > 0:
            nhl_sign = "+" if nhl_pnl >= 0 else ""
            nhl_wr = (nhl_wins / nhl_bets * 100) if nhl_bets > 0 else 0
            text += (
                f"<b>NHL</b>: {nhl_bets} bets, {nhl_wr:.0f}% WR, "
                f"{nhl_sign}{format_dollars(nhl_pnl)}\n"
            )

        text += f"\nDashboard: http://139.59.26.34\nMode: {mode.upper()}"

        return await self.send_message(text)
