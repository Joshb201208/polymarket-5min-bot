"""Combined midnight Telegram digest — merges all agents into one summary."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import SharedConfig
from nba_agent.utils import load_json, utcnow

logger = logging.getLogger(__name__)


class CombinedDigest:
    """Sends a single combined daily summary for all agents at midnight SGT (16:00 UTC)."""

    def __init__(self, config: SharedConfig | None = None) -> None:
        self.config = config or SharedConfig()
        self.token = self.config.TELEGRAM_BOT_TOKEN
        self.chat_id = self.config.TELEGRAM_CHAT_ID
        self.api_base = self.config.TELEGRAM_API_BASE
        self._last_sent: datetime | None = None

    def should_send(self) -> bool:
        """Check if it's time to send the daily digest (4pm UTC = midnight SGT)."""
        now = utcnow()
        if now.hour != 16:
            return False
        if self._last_sent and (now - self._last_sent) < timedelta(hours=12):
            return False
        return True

    async def send_combined_digest(self) -> bool:
        """Generate and send the combined daily digest."""
        now = utcnow()
        data_dir = self.config.DATA_DIR

        # Read bankroll
        bankroll_data = load_json(data_dir / "bankroll.json", {})
        bankroll = bankroll_data.get("current_bankroll", 0)
        starting = bankroll_data.get("starting_bankroll", 0)
        is_paused = bankroll_data.get("is_paused", False)

        # Read NBA data
        nba_stats = self._get_agent_stats(
            data_dir / "positions.json",
            data_dir / "trades.json",
        )

        # Read Events data
        events_stats = self._get_agent_stats(
            data_dir / "events_positions.json",
            data_dir / "events_trades.json",
        )

        # Combined stats
        total_open = nba_stats["open"] + events_stats["open"]
        total_trades = nba_stats["trades_today"] + events_stats["trades_today"]
        total_pnl = nba_stats["daily_pnl"] + events_stats["daily_pnl"]
        total_roi = (total_pnl / starting * 100) if starting > 0 else 0

        # Build message
        date_str = now.strftime("%B %d, %Y")
        pnl_sign = "+" if total_pnl >= 0 else ""
        pnl_emoji = "+" if total_pnl >= 0 else ""

        text = (
            f"<b>DAILY DIGEST — {date_str}</b>\n"
            f"{'=' * 30}\n\n"
        )

        # Portfolio overview
        text += (
            f"<b>Portfolio</b>\n"
            f"Bankroll: ${bankroll:,.2f}\n"
            f"Open Positions: {total_open}\n"
            f"Today's Trades: {total_trades}\n"
            f"Today's P&L: {pnl_sign}${total_pnl:,.2f}\n"
        )

        if is_paused:
            text += "STATUS: PAUSED (stop-loss)\n"

        # NBA section
        text += f"\n<b>NBA Agent</b>\n"
        if nba_stats["trades_today"] > 0 or nba_stats["open"] > 0:
            nba_pnl_sign = "+" if nba_stats["daily_pnl"] >= 0 else ""
            text += (
                f"Open: {nba_stats['open']} | Trades: {nba_stats['trades_today']}\n"
                f"P&L: {nba_pnl_sign}${nba_stats['daily_pnl']:.2f}\n"
            )
            if nba_stats["wins"] + nba_stats["losses"] > 0:
                wr = nba_stats["wins"] / (nba_stats["wins"] + nba_stats["losses"]) * 100
                text += f"Win Rate: {wr:.0f}% ({nba_stats['wins']}W/{nba_stats['losses']}L)\n"
        else:
            text += "No activity today\n"

        # Events section
        text += f"\n<b>Events Agent</b>\n"
        if events_stats["trades_today"] > 0 or events_stats["open"] > 0:
            evt_pnl_sign = "+" if events_stats["daily_pnl"] >= 0 else ""
            text += (
                f"Open: {events_stats['open']} | Trades: {events_stats['trades_today']}\n"
                f"P&L: {evt_pnl_sign}${events_stats['daily_pnl']:.2f}\n"
            )
            if events_stats["wins"] + events_stats["losses"] > 0:
                wr = events_stats["wins"] / (events_stats["wins"] + events_stats["losses"]) * 100
                text += f"Win Rate: {wr:.0f}% ({events_stats['wins']}W/{events_stats['losses']}L)\n"
        else:
            text += "No activity today\n"

        text += f"\nDashboard: http://139.59.26.34"

        success = await self._send_message(text)
        if success:
            self._last_sent = now
        return success

    def _get_agent_stats(self, positions_path, trades_path) -> dict:
        """Get daily stats for an agent from its data files."""
        now = utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        positions = load_json(positions_path, {"positions": []}).get("positions", [])
        trades = load_json(trades_path, {"trades": []}).get("trades", [])

        open_count = sum(1 for p in positions if p.get("status") == "open")

        today_trades = []
        for t in trades:
            try:
                ts = datetime.fromisoformat(t.get("timestamp", "").replace("Z", "+00:00"))
                if day_start <= ts < day_end:
                    today_trades.append(t)
            except (ValueError, AttributeError):
                continue

        sells = [t for t in today_trades if t.get("action") == "SELL"]
        daily_pnl = sum(t.get("pnl", 0) or 0 for t in sells)
        wins = sum(1 for t in sells if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in sells if (t.get("pnl") or 0) <= 0)

        return {
            "open": open_count,
            "trades_today": len(today_trades),
            "daily_pnl": daily_pnl,
            "wins": wins,
            "losses": losses,
        }

    async def _send_message(self, text: str) -> bool:
        """Send an HTML-formatted message to Telegram."""
        if not self.token or not self.chat_id:
            logger.warning("Telegram not configured — skipping digest")
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
                    logger.info("Combined daily digest sent")
                    return True
                logger.error("Telegram send failed: %d %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return False
