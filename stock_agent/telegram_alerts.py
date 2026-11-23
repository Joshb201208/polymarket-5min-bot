from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

from stock_agent.config import Config
from stock_agent.models import DailySummary, Position, Signal, Thesis, Trade

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4000


class TelegramReporter:
    """Optional secondary alert channel. Silent when credentials are empty."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._client: httpx.AsyncClient | None = None
        self._disabled_logged = False

    @property
    def token(self) -> str:
        return getattr(self.config, "TELEGRAM_BOT_TOKEN", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")

    @property
    def chat_id(self) -> str:
        return getattr(self.config, "TELEGRAM_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _redact(self, text: str) -> str:
        tok = self.token
        if tok and tok in text:
            text = text.replace(tok, "[REDACTED]")
        return text

    async def send_message(self, text: str, *, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram if configured. Returns True on success."""
        if not self.enabled:
            if not self._disabled_logged:
                self._disabled_logged = True
                logger.debug("Telegram not configured — optional mirror stays off")
            return False

        chunks = _split_message(text)
        all_ok = True
        for chunk in chunks:
            if not await self._send_chunk(chunk, parse_mode=parse_mode):
                all_ok = False
        return all_ok

    async def _send_chunk(self, text: str, *, parse_mode: str = "HTML") -> bool:
        client = await self._get_client()
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        payload: dict[str, object] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 400 and parse_mode:
                # Only retry without HTML on 400 malformed markup, never on 429/401/5xx
                fallback_payload = dict(payload)
                fallback_payload.pop("parse_mode", None)
                resp2 = await client.post(url, json=fallback_payload)
                resp2.raise_for_status()
                return True
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            logger.error(
                "Telegram API error (%s): %s",
                e.response.status_code,
                self._redact(e.response.text[:300]),
            )
            return False
        except Exception as e:
            logger.error("Telegram send failed (%s): %s", type(e).__name__, self._redact(str(e)))
            return False

    async def send_trade_alert(
        self,
        trade: Trade,
        thesis: Thesis | None = None,
        *,
        stop_verified: bool | None = None,
    ) -> bool:
        arrow = "\U0001f4c8" if trade.action == "BUY" else "\U0001f4c9"
        cost = trade.price * trade.shares
        sym = _escape_html(str(trade.symbol))
        action = _escape_html(str(trade.action))

        lines = [
            f"{arrow} <b>{action}: {sym}</b>",
            f"Shares: {trade.shares} @ ${trade.price:.2f}",
            f"Value: ${cost:,.0f}",
        ]

        if trade.action == "BUY":
            if stop_verified is False:
                lines.append("Stop-Loss: <b>MISSING AT BROKER (UNPROTECTED)</b>")
            elif thesis and thesis.stop_loss_price:
                pct = (thesis.stop_loss_price - trade.price) / trade.price if trade.price else 0.0
                lines.append(f"Stop-Loss: ${thesis.stop_loss_price:.2f} ({pct:+.1%})")
            if thesis:
                lines.append("")
                lines.append(f"<b>Why:</b> {_escape_html(thesis.summary)}")
                lines.append(f"Conviction: {thesis.conviction}/10")
        elif trade.action == "SELL":
            if trade.pnl is not None:
                emoji = "\u2705" if trade.pnl >= 0 else "\u274c"
                if trade.pnl_pct is not None:
                    lines.append(f"Result: {emoji} ${trade.pnl:+,.0f} ({trade.pnl_pct:+.1%})")
                else:
                    lines.append(f"Result: {emoji} ${trade.pnl:+,.0f}")
            if trade.hold_days is not None:
                lines.append(f"Held: {trade.hold_days} days")
            if trade.reason:
                lines.append(f"Reason: {_escape_html(trade.reason)}")

        return await self.send_message("\n".join(lines))

    async def send_daily_summary(self, summary: DailySummary) -> bool:
        day_emoji = "\U0001f4c8" if summary.day_pnl >= 0 else "\U0001f4c9"
        lines = [
            "\U0001f4ca <b>STOCK AGENT — Daily Summary</b>",
            _escape_html(str(summary.date)),
            "",
            f"\U0001f4b0 Account Equity: ${summary.portfolio_value:,.2f} ({summary.total_pnl_pct:+.2%})",
            f"\U0001f4b5 Cash: ${summary.cash:,.0f} | Exposure: {summary.exposure_pct:.1%}",
            f"{day_emoji} Day Change: ${summary.day_pnl:+,.2f} ({summary.day_pnl_pct:+.2%})",
            "",
        ]

        if summary.positions:
            lines.append(f"<b>Positions ({summary.num_positions}):</b>")
            for p in summary.positions:
                arrow = "\u2b06\ufe0f" if p.unrealized_pnl >= 0 else "\u2b07\ufe0f"
                lines.append(
                    f"  \u2022 {_escape_html(p.symbol)}: {p.unrealized_pnl_pct:+.1%} "
                    f"(${p.market_value:,.0f}) {arrow}"
                )
            lines.append("")

        if summary.trades_today:
            lines.append(f"<b>Today's Trades ({len(summary.trades_today)}):</b>")
            for t in summary.trades_today:
                lines.append(
                    f"  \u2022 {_escape_html(t.action)} {_escape_html(t.symbol)} "
                    f"x{t.shares} @ ${t.price:.2f}"
                )
        else:
            lines.append("Today's Trades: None")

        return await self.send_message("\n".join(lines))

    async def send_weekly_report(
        self,
        portfolio_value: float,
        cash: float,
        positions: list[Position],
        signals: list[Signal],
        stats: dict,
    ) -> bool:
        lines = [
            "\U0001f4cb <b>STOCK AGENT — Weekly Scorecard</b>",
            "",
            f"\U0001f4b0 Account Equity: ${portfolio_value:,.2f}",
            f"\U0001f4b5 Cash: ${cash:,.0f}",
            f"Positions: {len(positions)}",
            "",
        ]

        if stats:
            lines.append("<b>Performance:</b>")
            lines.append(f"  Win Rate: {stats.get('win_rate', 0):.0%}")
            lines.append(f"  Total P&amp;L: ${stats.get('total_pnl', 0):+,.0f}")
            lines.append(f"  Closed Trades: {stats.get('closed_trades', 0)}")
            lines.append(f"  Max Drawdown: {stats.get('max_drawdown', 0):.1%}")
            lines.append("")

        if positions:
            lines.append("<b>Current Positions:</b>")
            for p in positions:
                lines.append(
                    f"  \u2022 {_escape_html(p.symbol)}: {p.shares} shares "
                    f"@ ${p.entry_price:.2f} ({p.unrealized_pnl_pct:+.1%})"
                )
            lines.append("")

        if signals:
            lines.append(f"<b>Queued Signals for Next Open ({len(signals)}):</b>")
            for s in signals:
                lines.append(
                    f"  \u2022 {_escape_html(s.action)} {_escape_html(s.symbol)} — Conv: {s.conviction}/10"
                )
                if s.thesis and s.thesis.summary:
                    lines.append(f"    {_escape_html(s.thesis.summary[:120])}")
        else:
            lines.append("Queued Signals: None")

        return await self.send_message("\n".join(lines))

    async def send_error_alert(self, error: str) -> bool:
        safe = _escape_html(self._redact(error)[:1500])
        msg = f"\u26a0\ufe0f <b>STOCK AGENT ALERT</b>\n\n<code>{safe}</code>"
        return await self.send_message(msg)


def _split_message(text: str) -> list[str]:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _escape_html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = ["TelegramReporter", "_escape_html", "_split_message", "datetime"]
