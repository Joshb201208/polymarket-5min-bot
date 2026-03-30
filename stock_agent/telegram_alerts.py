import logging
from datetime import datetime

import httpx

from stock_agent.config import Config
from stock_agent.models import DailySummary, Position, Signal, Thesis, Trade

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4000


class TelegramReporter:
    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def send_message(self, text: str):
        """Send a message to the configured Telegram chat, splitting if too long."""
        if not self.config.TELEGRAM_BOT_TOKEN or not self.config.TELEGRAM_CHAT_ID:
            logger.warning("Telegram not configured — skipping message")
            return

        chunks = _split_message(text)
        for chunk in chunks:
            await self._send_chunk(chunk)

    async def _send_chunk(self, text: str):
        """Send a single message chunk to Telegram."""
        client = await self._get_client()
        url = f"{TELEGRAM_API}/bot{self.config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": self.config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error("Telegram API error: %s — %s", e.response.status_code, e.response.text[:300])
            # Retry without HTML parsing if it fails (malformed HTML)
            try:
                payload["parse_mode"] = ""
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            except Exception:
                logger.error("Telegram fallback also failed")
        except Exception as e:
            logger.error("Telegram send failed: %s", e)

    async def send_trade_alert(self, trade: Trade, thesis: Thesis | None = None):
        """Send a formatted trade alert."""
        arrow = "\U0001f4c8" if trade.action == "BUY" else "\U0001f4c9"
        cost = trade.price * trade.shares

        lines = [
            f"{arrow} <b>{trade.action}: {trade.symbol}</b>",
            f"Shares: {trade.shares} @ ${trade.price:.2f}",
            f"Value: ${cost:,.0f}",
        ]

        if trade.action == "BUY" and thesis:
            stop_loss = thesis.stop_loss_price
            if stop_loss:
                pct = (stop_loss - trade.price) / trade.price
                lines.append(f"Stop-Loss: ${stop_loss:.2f} ({pct:+.1%})")
            lines.append("")
            lines.append(f"<b>Thesis:</b> {thesis.summary}")
            lines.append(f"Conviction: {thesis.conviction}/10")
        elif trade.action == "SELL":
            if trade.pnl is not None:
                emoji = "\u2705" if trade.pnl >= 0 else "\u274c"
                lines.append(f"P&L: {emoji} ${trade.pnl:+,.0f} ({trade.pnl_pct:+.1%})" if trade.pnl_pct else f"P&L: {emoji} ${trade.pnl:+,.0f}")
            if trade.hold_days is not None:
                lines.append(f"Held: {trade.hold_days} days")
            lines.append(f"Reason: {trade.reason}")

        await self.send_message("\n".join(lines))

    async def send_daily_summary(self, summary: DailySummary):
        """Send end-of-day portfolio summary."""
        day_emoji = "\U0001f4c8" if summary.day_pnl >= 0 else "\U0001f4c9"
        total_emoji = "\U0001f4c8" if summary.total_pnl >= 0 else "\U0001f4c9"

        lines = [
            "\U0001f4ca <b>STOCK AGENT — Daily Summary</b>",
            f"{summary.date}",
            "",
            f"\U0001f4b0 Portfolio: ${summary.portfolio_value:,.0f} ({summary.total_pnl_pct:+.2%})",
            f"\U0001f4b5 Cash: ${summary.cash:,.0f} | Exposure: {summary.exposure_pct:.1%}",
            f"{day_emoji} Day P&L: ${summary.day_pnl:+,.0f} ({summary.day_pnl_pct:+.2%})",
            "",
        ]

        if summary.positions:
            lines.append(f"<b>Positions ({summary.num_positions}):</b>")
            for p in summary.positions:
                arrow = "\u2b06\ufe0f" if p.unrealized_pnl >= 0 else "\u2b07\ufe0f"
                lines.append(
                    f"  \u2022 {p.symbol}: {p.unrealized_pnl_pct:+.1%} (${p.market_value:,.0f}) {arrow}"
                )
            lines.append("")

        if summary.trades_today:
            lines.append(f"<b>Today's Trades ({len(summary.trades_today)}):</b>")
            for t in summary.trades_today:
                lines.append(f"  \u2022 {t.action} {t.symbol} x{t.shares} @ ${t.price:.2f}")
        else:
            lines.append("Today's Trades: None")

        await self.send_message("\n".join(lines))

    async def send_weekly_report(
        self,
        portfolio_value: float,
        cash: float,
        positions: list[Position],
        signals: list[Signal],
        stats: dict,
    ):
        """Send weekly analysis report."""
        lines = [
            "\U0001f4cb <b>STOCK AGENT — Weekly Report</b>",
            "",
            f"\U0001f4b0 Portfolio Value: ${portfolio_value:,.0f}",
            f"\U0001f4b5 Cash: ${cash:,.0f}",
            f"Positions: {len(positions)}",
            "",
        ]

        if stats:
            lines.append("<b>Performance:</b>")
            lines.append(f"  Win Rate: {stats.get('win_rate', 0):.0%}")
            lines.append(f"  Total P&L: ${stats.get('total_pnl', 0):+,.0f}")
            lines.append(f"  Closed Trades: {stats.get('closed_trades', 0)}")
            lines.append(f"  Max Drawdown: {stats.get('max_drawdown', 0):.1%}")
            lines.append("")

        if positions:
            lines.append("<b>Current Positions:</b>")
            for p in positions:
                lines.append(f"  \u2022 {p.symbol}: {p.shares} shares @ ${p.entry_price:.2f} ({p.unrealized_pnl_pct:+.1%})")
            lines.append("")

        if signals:
            lines.append(f"<b>New Signals ({len(signals)}):</b>")
            for s in signals:
                lines.append(f"  \u2022 {s.action} {s.symbol} — Conv: {s.conviction}/10")
                if s.thesis:
                    lines.append(f"    {s.thesis.summary[:120]}")
        else:
            lines.append("New Signals: None this week")

        await self.send_message("\n".join(lines))

    async def send_error_alert(self, error: str):
        """Send critical error notification."""
        msg = f"\u26a0\ufe0f <b>STOCK AGENT ERROR</b>\n\n<code>{_escape_html(error[:1500])}</code>"
        await self.send_message(msg)


def _split_message(text: str) -> list[str]:
    """Split a long message into chunks that fit within Telegram's limit."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    chunks = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break

        # Try to split at a newline
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
