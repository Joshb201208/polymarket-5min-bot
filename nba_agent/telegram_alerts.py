"""All Telegram messaging — trade alerts, P&L summaries, etc."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from nba_agent.config import Config
from nba_agent.models import Confidence, EdgeResult, Position, ResearchData, Trade
from nba_agent.utils import format_dollars, format_edge, format_pct, format_price

logger = logging.getLogger(__name__)


class TelegramBot:
    """Sends formatted alerts to Telegram."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.token = self.config.TELEGRAM_BOT_TOKEN
        self.chat_id = self.config.TELEGRAM_CHAT_ID
        self.api_base = self.config.TELEGRAM_API_BASE

    async def send_message(self, text: str) -> bool:
        """Send an HTML-formatted message to Telegram."""
        if not self.token or not self.chat_id:
            logger.warning("Telegram not configured — skipping message")
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
                if resp.status_code == 200:
                    return True
                logger.error("Telegram send failed: %d %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return False

    async def send_test(self) -> bool:
        """Send a test message."""
        return await self.send_message("🏀 NBA Agent — Test message. Bot is connected!")

    async def send_trade_alert(
        self,
        position: Position,
        edge_result: EdgeResult,
        remaining_balance: float,
    ) -> bool:
        """Send a trade execution alert."""
        mode_label = "Paper" if position.mode == "paper" else "Live"
        market = edge_result.market

        # Build research section
        research_lines = self._format_research(edge_result.research)

        # Market type label
        mtype = market.market_type.value.title()

        text = (
            f"🏀 <b>TRADE EXECUTED [{mode_label}]</b>\n\n"
            f"{market.question} — {mtype}\n"
            f"BUY {position.side} @ {format_price(position.entry_price)} | {format_dollars(position.cost)}\n\n"
            f"Edge: {format_edge(edge_result.edge)} | Confidence: {edge_result.confidence.value}\n"
            f"Our Fair Price: {format_pct(edge_result.our_fair_price)}\n"
        )

        if research_lines:
            text += "\n📊 <b>Research:</b>\n"
            for line in research_lines:
                text += f"• {line}\n"

        text += (
            f"\nMode: {mode_label.upper()} | Order: {position.id}\n"
            f"Balance: {format_dollars(remaining_balance)} remaining\n"
        )

        # Link to market
        if market.event_slug:
            text += f"\npolymarket.com/event/{market.event_slug}"

        return await self.send_message(text)

    async def send_exit_alert(
        self,
        position: Position,
        trade: Trade,
        balance: float,
    ) -> bool:
        """Send an early exit alert."""
        mode_label = "Paper" if position.mode == "paper" else "Live"
        pnl = position.pnl or 0.0
        pnl_pct = (pnl / position.cost * 100) if position.cost > 0 else 0.0
        pnl_sign = "+" if pnl >= 0 else ""

        text = (
            f"🔄 <b>EARLY EXIT [{mode_label}]</b>\n\n"
            f"{position.market_question}\n"
            f"SELL {position.side} @ {format_price(position.exit_price or 0)} | {position.shares:.2f} shares\n\n"
            f"Entry: {format_price(position.entry_price)} → Exit: {format_price(position.exit_price or 0)}\n"
            f"P&L: {pnl_sign}{format_dollars(pnl)} ({pnl_sign}{pnl_pct:.1f}%)\n"
            f"Reason: {position.exit_reason}\n\n"
            f"Mode: {mode_label.upper()} | Balance: {format_dollars(balance)}"
        )

        return await self.send_message(text)

    async def send_daily_summary(
        self,
        date_str: str,
        open_positions: int,
        trades_today: int,
        buys_today: int,
        sells_today: int,
        daily_pnl: float,
        bankroll: float,
        best_trade: str,
        worst_trade: str,
        win_rate: float,
        avg_edge: float,
        mode: str,
    ) -> bool:
        """Send daily P&L summary."""
        pnl_sign = "+" if daily_pnl >= 0 else ""
        pnl_pct = (daily_pnl / (bankroll - daily_pnl)) * 100 if (bankroll - daily_pnl) > 0 else 0.0

        text = (
            f"📊 <b>DAILY P&L SUMMARY — {date_str}</b>\n\n"
            f"Open Positions: {open_positions}\n"
            f"Trades Today: {trades_today} ({buys_today} buys, {sells_today} sells)\n\n"
            f"Today's P&L: {pnl_sign}{format_dollars(daily_pnl)} ({pnl_sign}{pnl_pct:.1f}%)\n"
            f"Bankroll: {format_dollars(bankroll)}\n"
        )

        if best_trade:
            text += f"\n🏆 Best Trade: {best_trade}"
        if worst_trade:
            text += f"\n💀 Worst Trade: {worst_trade}"

        text += (
            f"\n\nWin Rate: {win_rate:.1f}% ({int(win_rate * trades_today / 100) if trades_today > 0 else 0}/{trades_today})\n"
            f"Avg Edge: {format_edge(avg_edge)}\n"
            f"Mode: {mode.upper()}"
        )

        return await self.send_message(text)

    async def send_weekly_summary(
        self,
        week_str: str,
        total_bets: int,
        wins: int,
        total_pnl: float,
        roi: float,
        bankroll: float,
        biggest_win: str,
        biggest_loss: str,
        expected_wr: float,
        actual_wr: float,
        mode: str,
    ) -> bool:
        """Send weekly performance summary."""
        pnl_sign = "+" if total_pnl >= 0 else ""
        win_rate = (wins / total_bets * 100) if total_bets > 0 else 0.0

        # Calibration assessment
        wr_diff = actual_wr - expected_wr
        if abs(wr_diff) < 5:
            calibration = "GOOD"
        elif wr_diff > 0:
            calibration = "GOOD (model slightly underestimates)"
        else:
            calibration = "NEEDS REVIEW (model overestimates)"

        losses = total_bets - wins
        gross_wins = total_pnl + abs(total_pnl) if total_pnl > 0 else 0  # approximate
        profit_factor_str = f"{roi:.1f}%" if total_bets > 0 else "--"

        text = (
            f"📈 <b>WEEKLY REPORT — {week_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Performance</b>\n"
            f"Bets: {total_bets} ({wins}W / {losses}L)\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"P&L: {pnl_sign}{format_dollars(total_pnl)}\n"
            f"ROI: {pnl_sign}{roi:.1f}%\n"
            f"Bankroll: {format_dollars(bankroll)}\n"
        )

        if biggest_win:
            text += f"\n🏆 Best: {biggest_win}"
        if biggest_loss:
            text += f"\n💀 Worst: {biggest_loss}"

        text += (
            f"\n\n<b>Model Health</b>\n"
            f"Expected WR: {expected_wr:.1f}%\n"
            f"Actual WR: {actual_wr:.1f}%\n"
            f"Calibration: {calibration}\n\n"
            f"Dashboard: http://139.59.26.34\n"
            f"Mode: {mode.upper()}"
        )

        return await self.send_message(text)

    async def send_stop_loss_alert(self, bankroll: float, peak: float) -> bool:
        """Send stop-loss alert."""
        text = (
            f"🚨 <b>STOP LOSS TRIGGERED</b>\n\n"
            f"Bankroll: {format_dollars(bankroll)}\n"
            f"Peak: {format_dollars(peak)}\n"
            f"Drawdown: {format_pct((peak - bankroll) / peak)}\n\n"
            f"<b>Trading paused until manual review.</b>"
        )
        return await self.send_message(text)

    async def send_startup_message(self, mode: str, bankroll: float) -> bool:
        """Send agent startup notification."""
        text = (
            f"🚀 <b>NBA Agent Started</b>\n\n"
            f"Mode: {mode.upper()}\n"
            f"Bankroll: {format_dollars(bankroll)}\n"
            f"Scan interval: {self.config.SCAN_INTERVAL} min\n"
            f"Exit check: {self.config.EXIT_CHECK_INTERVAL} min\n"
            f"Season: {self.config.NBA_SEASON}"
        )
        return await self.send_message(text)

    def _format_research(self, research: ResearchData | None) -> list[str]:
        """Format research data into display lines."""
        if not research:
            return []

        home = research.home_team
        away = research.away_team
        lines = []

        # Records
        lines.append(
            f"{home.team_name}: {home.wins}-{home.losses} ({format_pct(home.win_pct)}) | L10: {home.last_10}"
        )
        lines.append(
            f"{away.team_name}: {away.wins}-{away.losses} ({format_pct(away.win_pct)}) | L10: {away.last_10}"
        )

        # H2H
        if research.h2h and (research.h2h.team_a_wins + research.h2h.team_b_wins) > 0:
            h = research.h2h
            if h.team_a_wins > h.team_b_wins:
                lines.append(f"H2H this season: {home.team_name} leads {h.team_a_wins}-{h.team_b_wins}")
            elif h.team_b_wins > h.team_a_wins:
                lines.append(f"H2H this season: {away.team_name} leads {h.team_b_wins}-{h.team_a_wins}")
            else:
                lines.append(f"H2H this season: Tied {h.team_a_wins}-{h.team_b_wins}")

        # Home/away records
        lines.append(f"{home.team_name}: home record {home.home_record}")

        # Rest
        rest_line = f"Rest: {home.team_name} {home.rest_days} days"
        if home.is_b2b:
            rest_line += " (B2B)"
        rest_line += f", {away.team_name} {away.rest_days} days"
        if away.is_b2b:
            rest_line += " (B2B)"
        lines.append(rest_line)

        # Ratings
        if home.off_rating > 0:
            lines.append(
                f"Off Rating: {home.team_name} {home.off_rating:.1f} | {away.team_name} {away.off_rating:.1f}"
            )
        if home.def_rating > 0:
            lines.append(
                f"Def Rating: {home.team_name} {home.def_rating:.1f} | {away.team_name} {away.def_rating:.1f}"
            )

        # Injuries
        for inj in research.home_injuries:
            lines.append(f"⚠️ {inj}")
        for inj in research.away_injuries:
            lines.append(f"⚠️ {inj}")

        return lines
