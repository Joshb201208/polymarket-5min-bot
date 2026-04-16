import asyncio
import logging
from datetime import datetime, timezone

import httpx

from stock_agent.config import Config
from stock_agent.models import DailySummary, Position, Signal, Thesis, Trade

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
MAX_EMBED_LENGTH = 6000
MAX_FIELD_VALUE = 1024


class DiscordReporter:
    def __init__(self, config: Config):
        self.config = config
        self.token = config.DISCORD_BOT_TOKEN
        self.channels = config.DISCORD_CHANNELS
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "Authorization": f"Bot {self.token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def send_embed(self, channel_id: str, embed: dict) -> bool:
        """POST an embed to a Discord channel. Handles 429 rate limits with retry."""
        if not self.token:
            logger.warning("Discord not configured — skipping embed")
            return False

        # Ensure timestamp
        if "timestamp" not in embed:
            embed["timestamp"] = datetime.now(timezone.utc).isoformat()

        # Ensure footer
        mode = getattr(self.config, "MODE", "PAPER")
        if "footer" not in embed:
            embed["footer"] = {"text": f"Stock Agent • {mode} Mode"}

        client = await self._get_client()
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        payload = {"embeds": [embed]}

        for attempt in range(4):
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 429:
                    retry_after = resp.json().get("retry_after", 1.0)
                    logger.warning("Discord rate limited — retrying in %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return True
            except httpx.HTTPStatusError as e:
                logger.error(
                    "Discord API error: %s — %s",
                    e.response.status_code,
                    e.response.text[:300],
                )
                return False
            except Exception as e:
                logger.error("Discord send failed (attempt %d): %s", attempt + 1, e)
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return False

        return False

    async def _send_multi_embed(self, channel_id: str, embeds: list[dict]) -> bool:
        """Send multiple embeds in one message (Discord allows up to 10)."""
        if not self.token:
            return False

        mode = getattr(self.config, "MODE", "PAPER")
        for embed in embeds:
            if "timestamp" not in embed:
                embed["timestamp"] = datetime.now(timezone.utc).isoformat()
            if "footer" not in embed:
                embed["footer"] = {"text": f"Stock Agent • {mode} Mode"}

        client = await self._get_client()
        url = f"{DISCORD_API}/channels/{channel_id}/messages"

        # Discord allows max 10 embeds per message
        for i in range(0, len(embeds), 10):
            batch = embeds[i : i + 10]
            payload = {"embeds": batch}

            for attempt in range(4):
                try:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 429:
                        retry_after = resp.json().get("retry_after", 1.0)
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    break
                except httpx.HTTPStatusError as e:
                    logger.error("Discord API error: %s", e.response.status_code)
                    return False
                except Exception as e:
                    logger.error("Discord send failed: %s", e)
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return False

        return True

    # ── ANNOUNCEMENTS ─────────────────────────────────────────────────

    async def send_startup(self, mode: str, cash: float):
        """Green embed to #announcements on startup."""
        embed = {
            "title": "\U0001f680 Stock Agent Online",
            "color": 0x10B981,
            "fields": [
                {"name": "Mode", "value": f"`{mode}`", "inline": True},
                {"name": "Starting Capital", "value": f"${cash:,.0f}", "inline": True},
                {"name": "Max Position", "value": f"{self.config.MAX_POSITION_PCT:.0%}", "inline": True},
                {"name": "Max Exposure", "value": f"{self.config.MAX_TOTAL_EXPOSURE:.0%}", "inline": True},
                {"name": "Stop-Loss", "value": f"{self.config.STOP_LOSS_PCT:.0%}", "inline": True},
                {"name": "Min Conviction", "value": f"{self.config.MIN_CONVICTION}/10", "inline": True},
            ],
            "footer": {"text": f"Stock Agent • {mode} Mode"},
        }
        await self.send_embed(self.channels["announcements"], embed)

    async def send_shutdown(self, reason: str):
        """Red embed to #announcements on shutdown."""
        embed = {
            "title": "\U0001f6d1 Stock Agent Offline",
            "description": reason,
            "color": 0xEF4444,
        }
        await self.send_embed(self.channels["announcements"], embed)

    # ── TRADE EXECUTIONS ──────────────────────────────────────────────

    async def send_trade_alert(self, trade: Trade, thesis: Thesis | None = None):
        """Rich embed to #trade-executions. Also archives to #trade-history."""
        is_buy = trade.action == "BUY"
        color = 0x22C55E if is_buy else 0xEF4444
        emoji = "\U0001f4c8" if is_buy else "\U0001f4c9"
        cost = trade.price * trade.shares

        description = ""
        if is_buy and thesis:
            description = thesis.summary
        elif not is_buy and trade.reason:
            description = trade.reason

        fields = [
            {"name": "Entry Price" if is_buy else "Exit Price", "value": f"${trade.price:.2f}", "inline": True},
            {"name": "Shares", "value": str(trade.shares), "inline": True},
            {"name": "Position Size", "value": f"${cost:,.0f}", "inline": True},
        ]

        if is_buy and thesis:
            if thesis.stop_loss_price:
                pct = (thesis.stop_loss_price - trade.price) / trade.price
                fields.append({"name": "Stop-Loss", "value": f"${thesis.stop_loss_price:.2f} ({pct:+.1%})", "inline": True})
            fields.append({"name": "Conviction", "value": _conviction_badge(thesis.conviction), "inline": True})
            if thesis.catalysts:
                cat_text = "\n".join(f"\u2022 {c}" for c in thesis.catalysts[:5])
                fields.append({"name": "Key Catalysts", "value": cat_text[:MAX_FIELD_VALUE], "inline": False})
        elif not is_buy:
            if trade.pnl is not None:
                pnl_emoji = "\u2705" if trade.pnl >= 0 else "\u274c"
                pnl_text = f"{pnl_emoji} ${trade.pnl:+,.2f}"
                if trade.pnl_pct is not None:
                    pnl_text += f" ({trade.pnl_pct:+.1%})"
                fields.append({"name": "P&L", "value": pnl_text, "inline": True})
            if trade.hold_days is not None:
                fields.append({"name": "Hold Period", "value": f"{trade.hold_days} days", "inline": True})

        embed = {
            "title": f"{emoji} {trade.action} — {trade.symbol}",
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": f"Stock Agent • Paper Mode • {trade.symbol}"},
        }

        # Send to both channels in parallel
        await asyncio.gather(
            self.send_embed(self.channels["trades"], embed),
            self.send_embed(self.channels["trade_history"], embed),
            return_exceptions=True,
        )

    # ── THESIS BOARD ──────────────────────────────────────────────────

    async def send_thesis(self, thesis: Thesis):
        """Detailed embed to #thesis-board."""
        color = 0xF59E0B if thesis.conviction >= 8 else 0x3B82F6

        fields = [
            {"name": "Direction", "value": f"**{thesis.direction}**", "inline": True},
            {"name": "Conviction", "value": _conviction_badge(thesis.conviction), "inline": True},
            {"name": "Time Horizon", "value": thesis.time_horizon, "inline": True},
        ]

        if thesis.target_price:
            fields.append({"name": "Target Price", "value": f"${thesis.target_price:.2f}", "inline": True})
        if thesis.stop_loss_price:
            fields.append({"name": "Stop-Loss", "value": f"${thesis.stop_loss_price:.2f}", "inline": True})

        fields.append({"name": "Bull Case", "value": thesis.bull_case[:MAX_FIELD_VALUE], "inline": False})
        fields.append({"name": "Bear Case", "value": thesis.bear_case[:MAX_FIELD_VALUE], "inline": False})

        if thesis.catalysts:
            cat_text = "\n".join(f"\u2022 {c}" for c in thesis.catalysts[:6])
            fields.append({"name": "Catalysts", "value": cat_text[:MAX_FIELD_VALUE], "inline": True})
        if thesis.risks:
            risk_text = "\n".join(f"\u2022 {r}" for r in thesis.risks[:6])
            fields.append({"name": "Risks", "value": risk_text[:MAX_FIELD_VALUE], "inline": True})

        if thesis.sources:
            src_text = "\n".join(f"\u2022 {s}" for s in thesis.sources[:5])
            fields.append({"name": "Sources", "value": src_text[:MAX_FIELD_VALUE], "inline": False})

        embed = {
            "title": f"\U0001f4d1 Thesis — {thesis.symbol}",
            "description": thesis.summary,
            "color": color,
            "fields": fields,
            "footer": {"text": f"Stock Agent • Paper Mode • {thesis.symbol}"},
        }
        await self.send_embed(self.channels["thesis"], embed)

    # ── DAILY P&L ─────────────────────────────────────────────────────

    async def send_daily_summary(self, summary: DailySummary):
        """End-of-day summary embed to #daily-pnl."""
        color = 0x22C55E if summary.day_pnl >= 0 else 0xEF4444

        # Stats row
        stats = self.config
        win_rate = ""
        trades = summary.trades_today
        if trades:
            wins = sum(1 for t in trades if t.pnl and t.pnl > 0)
            total = len(trades)
            win_rate = f"{wins}/{total}"
        else:
            win_rate = "N/A"

        fields = [
            {"name": "Portfolio Value", "value": f"```${summary.portfolio_value:,.2f}```", "inline": True},
            {"name": "Day P&L", "value": f"```{'+' if summary.day_pnl >= 0 else ''}${summary.day_pnl:,.2f} ({summary.day_pnl_pct:+.2%})```", "inline": True},
            {"name": "Total P&L", "value": f"```{'+' if summary.total_pnl >= 0 else ''}${summary.total_pnl:,.2f} ({summary.total_pnl_pct:+.2%})```", "inline": True},
            {"name": "Cash", "value": f"${summary.cash:,.0f}", "inline": True},
            {"name": "Exposure", "value": f"{summary.exposure_pct:.1%}", "inline": True},
            {"name": "Trades Today", "value": win_rate, "inline": True},
        ]

        # Position table as code block
        if summary.positions:
            header = f"{'Symbol':<6} {'Entry':>9} {'Now':>9} {'P&L':>7} {'Conv':>4}"
            rows = [header]
            for p in summary.positions:
                conv = p.thesis.conviction if p.thesis else 0
                rows.append(
                    f"{p.symbol:<6} ${p.entry_price:>8,.2f} ${p.current_price:>8,.2f} {p.unrealized_pnl_pct:>+6.1%} {conv:>4}"
                )
            table_text = "```\n" + "\n".join(rows) + "\n```"
            if len(table_text) <= MAX_FIELD_VALUE:
                fields.append({"name": "\U0001f4cc Positions", "value": table_text, "inline": False})
            else:
                # Split into chunks
                chunk_rows = [header]
                for row in rows[1:]:
                    chunk_rows.append(row)
                    test = "```\n" + "\n".join(chunk_rows) + "\n```"
                    if len(test) > MAX_FIELD_VALUE - 50:
                        fields.append({"name": "\U0001f4cc Positions", "value": "```\n" + "\n".join(chunk_rows[:-1]) + "\n```", "inline": False})
                        chunk_rows = [header, row]
                if len(chunk_rows) > 1:
                    fields.append({"name": "\u200b", "value": "```\n" + "\n".join(chunk_rows) + "\n```", "inline": False})

        # Today's trades
        if summary.trades_today:
            trade_lines = []
            for t in summary.trades_today:
                pnl_str = ""
                if t.pnl is not None:
                    pnl_str = f" → ${t.pnl:+,.0f}"
                trade_lines.append(f"\u2022 **{t.action}** {t.symbol} x{t.shares} @ ${t.price:.2f}{pnl_str}")
            fields.append({"name": "Executed Trades", "value": "\n".join(trade_lines)[:MAX_FIELD_VALUE], "inline": False})

        embed = {
            "title": f"\U0001f4ca Daily Summary — {summary.date}",
            "color": color,
            "fields": fields,
            "footer": {"text": f"Stock Agent • Paper Mode • {summary.num_positions} positions"},
        }
        await self.send_embed(self.channels["daily_pnl"], embed)

    # ── PORTFOLIO OVERVIEW ────────────────────────────────────────────

    async def send_weekly_report(
        self,
        portfolio_value: float,
        cash: float,
        positions: list[Position],
        signals: list[Signal],
        stats: dict,
    ):
        """Detailed embed(s) to #portfolio-overview."""
        embeds: list[dict] = []

        # Main stats embed
        fields = [
            {"name": "Portfolio Value", "value": f"```${portfolio_value:,.2f}```", "inline": True},
            {"name": "Cash", "value": f"```${cash:,.0f}```", "inline": True},
            {"name": "Positions", "value": f"```{len(positions)}```", "inline": True},
        ]

        if stats:
            fields.extend([
                {"name": "Win Rate", "value": f"{stats.get('win_rate', 0):.0%}", "inline": True},
                {"name": "Total P&L", "value": f"${stats.get('total_pnl', 0):+,.0f}", "inline": True},
                {"name": "Max Drawdown", "value": f"{stats.get('max_drawdown', 0):.1%}", "inline": True},
                {"name": "Closed Trades", "value": str(stats.get("closed_trades", 0)), "inline": True},
            ])
            if stats.get("sharpe_ratio") is not None:
                fields.append({"name": "Sharpe Ratio", "value": f"{stats['sharpe_ratio']:.2f}", "inline": True})

        embeds.append({
            "title": "\U0001f4cb Weekly Report",
            "color": 0x3B82F6,
            "fields": fields,
        })

        # Positions embed
        if positions:
            pos_fields = []
            for p in positions:
                conv = p.thesis.conviction if p.thesis else 0
                pnl_emoji = "\u2705" if p.unrealized_pnl >= 0 else "\u274c"
                pos_fields.append({
                    "name": f"{pnl_emoji} {p.symbol}",
                    "value": (
                        f"Entry: ${p.entry_price:.2f} → ${p.current_price:.2f}\n"
                        f"P&L: {p.unrealized_pnl_pct:+.1%} (${p.unrealized_pnl:+,.0f})\n"
                        f"Conviction: {conv}/10 | {p.sector}"
                    ),
                    "inline": True,
                })

            embeds.append({
                "title": f"\U0001f4bc Positions ({len(positions)})",
                "color": 0x3B82F6,
                "fields": pos_fields,
            })

        # Signals embed
        if signals:
            sig_fields = []
            for s in signals:
                desc = s.thesis.summary[:200] if s.thesis else ""
                sig_fields.append({
                    "name": f"\U0001f7e2 {s.action} {s.symbol} — {s.conviction}/10",
                    "value": desc,
                    "inline": False,
                })

            embeds.append({
                "title": f"\U0001f4e1 New Signals ({len(signals)})",
                "color": 0xF59E0B,
                "fields": sig_fields,
            })

        # Check total size — split if needed
        if len(embeds) <= 10:
            await self._send_multi_embed(self.channels["portfolio"], embeds)
        else:
            for embed in embeds:
                await self.send_embed(self.channels["portfolio"], embed)

    # ── RISK ALERTS ───────────────────────────────────────────────────

    async def send_risk_alert(self, alert_type: str, details: str, symbol: str | None = None):
        """Red/orange embed to #risk-alerts."""
        type_config = {
            "STOP_LOSS": ("\U0001f6a8 Stop-Loss Triggered", 0xEF4444),
            "THESIS_BREAK": ("\u26a0\ufe0f Thesis Broken", 0xEF4444),
            "EXPOSURE_WARNING": ("\U0001f4c9 Exposure Warning", 0xF59E0B),
            "SECTOR_LIMIT": ("\U0001f4ca Sector Limit", 0xF59E0B),
            "PDT_WARNING": ("\u26a0\ufe0f PDT Compliance", 0xF59E0B),
        }

        title, color = type_config.get(alert_type, (f"\u26a0\ufe0f {alert_type}", 0xEF4444))

        fields = [{"name": "Details", "value": details[:MAX_FIELD_VALUE], "inline": False}]
        if symbol:
            fields.insert(0, {"name": "Symbol", "value": f"**{symbol}**", "inline": True})

        embed = {
            "title": title,
            "color": color,
            "fields": fields,
        }
        await self.send_embed(self.channels["risk"], embed)

    # ── MARKET MONITOR ────────────────────────────────────────────────

    async def send_material_event(self, symbol: str, event: str, impact: str, severity: int):
        """Event embed to #market-monitor."""
        color_map = {"positive": 0x10B981, "negative": 0xEF4444, "neutral": 0x3B82F6}
        color = color_map.get(impact, 0x6B7280)

        sev_bar = "\U0001f7e5" * min(severity, 10) + "\u2b1c" * max(0, 10 - severity)

        embed = {
            "title": f"\U0001f4e2 Material Event — {symbol}",
            "color": color,
            "fields": [
                {"name": "Event", "value": event[:MAX_FIELD_VALUE], "inline": False},
                {"name": "Impact", "value": f"**{impact.title()}**", "inline": True},
                {"name": "Severity", "value": f"{severity}/10\n{sev_bar}", "inline": True},
            ],
        }
        await self.send_embed(self.channels["market_monitor"], embed)

    # ── SCREENER OUTPUT ───────────────────────────────────────────────

    async def send_screener_results(self, ranked_companies: list):
        """Table of top candidates to #screener-output."""
        if not ranked_companies:
            return

        header = f"{'#':<3} {'Symbol':<6} {'Score':>6} {'Name':<20}"
        rows = [header, "-" * 40]
        for i, r in enumerate(ranked_companies[:25], 1):
            sym = r.get("symbol", "?")
            score = r.get("score", r.get("composite_score", 0))
            name = r.get("name", "")[:20]
            rows.append(f"{i:<3} {sym:<6} {score:>6.1f} {name:<20}")

        table = "```\n" + "\n".join(rows) + "\n```"

        embed = {
            "title": f"\U0001f50d Screener Results — Top {min(len(ranked_companies), 25)}",
            "description": table[:4096],
            "color": 0x3B82F6,
        }
        await self.send_embed(self.channels["screener"], embed)

    # ── PERPLEXITY ANALYST ────────────────────────────────────────────

    async def send_analysis_report(self, symbol: str, analysis: str, sources: list):
        """Full analysis to #perplexity-analyst. Splits if needed."""
        source_text = ""
        if sources:
            source_text = "\n\n**Sources:**\n" + "\n".join(f"\u2022 {s}" for s in sources[:10])

        full_text = analysis + source_text

        # Split into multiple embeds if needed
        if len(full_text) <= 4096:
            embed = {
                "title": f"\U0001f9e0 Analysis — {symbol}",
                "description": full_text,
                "color": 0x3B82F6,
            }
            await self.send_embed(self.channels["analyst"], embed)
        else:
            chunks = _split_text(full_text, 4096)
            embeds = []
            for i, chunk in enumerate(chunks):
                embeds.append({
                    "title": f"\U0001f9e0 Analysis — {symbol}" + (f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""),
                    "description": chunk,
                    "color": 0x3B82F6,
                })
            for embed in embeds:
                await self.send_embed(self.channels["analyst"], embed)

    # ── MACRO CONTEXT ─────────────────────────────────────────────────

    async def send_macro_update(self, title: str, content: str):
        """Macro update to #macro-context."""
        embed = {
            "title": f"\U0001f30d {title}",
            "description": content[:4096],
            "color": 0x8B5CF6,
        }
        await self.send_embed(self.channels["macro"], embed)

    # ── SYSTEM LOGS ───────────────────────────────────────────────────

    async def send_system_log(self, level: str, message: str):
        """Log embed to #system-logs."""
        color_map = {"INFO": 0x6B7280, "WARNING": 0xF59E0B, "ERROR": 0xEF4444}
        color = color_map.get(level.upper(), 0x6B7280)

        embed = {
            "title": f"{level.upper()}",
            "description": f"```{message[:4000]}```",
            "color": color,
        }
        await self.send_embed(self.channels["system_logs"], embed)

    async def send_error(self, error: str, context: str = ""):
        """Red error embed to #system-logs."""
        desc = f"```{error[:3500]}```"
        if context:
            desc = f"**Context:** {context}\n\n" + desc

        embed = {
            "title": "\u274c Error",
            "description": desc[:4096],
            "color": 0xEF4444,
        }
        await self.send_embed(self.channels["system_logs"], embed)

    # ── Explained Simply channels ─────────────────────────────────

    async def send_eli5_trade(self, trade, thesis, is_buy: bool = True):
        """Plain English explanation of a trade to #what-the-bot-did."""
        ch = self.channels.get("what_bot_did")
        if not ch:
            return

        if is_buy:
            desc = (
                f"**The bot just bought {getattr(trade, 'symbol', 'a stock')}.**\n\n"
                f"**In plain English:** {getattr(thesis, 'summary', 'No summary available.')}\n\n"
                f"**How much?** {getattr(trade, 'shares', '?')} shares at "
                f"${getattr(trade, 'price', 0):,.2f} — that's "
                f"{getattr(trade, 'shares', 0) * getattr(trade, 'price', 0):,.0f} dollars, "
                f"a small piece of the portfolio.\n\n"
                f"**Safety net:** If the stock drops 5%, the bot will automatically sell to limit the loss.\n\n"
                f"**What to watch for:** The bot will keep monitoring this stock and sell "
                f"if the original reason for buying no longer holds true."
            )
            color = 0x22c55e
            title = f"🛒 Bought {getattr(trade, 'symbol', '???')}"
        else:
            pnl = getattr(trade, 'pnl', 0) or 0
            pnl_word = "profit" if pnl >= 0 else "loss"
            desc = (
                f"**The bot just sold {getattr(trade, 'symbol', 'a stock')}.**\n\n"
                f"**Why?** {getattr(trade, 'reason', 'No reason provided.')}\n\n"
                f"**Result:** ${abs(pnl):,.2f} {pnl_word} on this trade.\n\n"
                f"That's investing — sometimes you win, sometimes you cut your losses early. "
                f"The important thing is the bot followed its rules."
            )
            color = 0xef4444 if pnl < 0 else 0x22c55e
            title = f"{'📈' if pnl >= 0 else '📉'} Sold {getattr(trade, 'symbol', '???')}"

        await self.send_embed(ch, {
            "title": title,
            "description": desc,
            "color": color,
        })

    async def send_eli5_weekly(self, portfolio_state, signals):
        """Weekly coffee briefing to #weekly-eli5."""
        ch = self.channels.get("weekly_eli5")
        if not ch:
            return

        positions = getattr(portfolio_state, 'positions', []) or []
        cash = getattr(portfolio_state, 'cash', 100000)
        starting = getattr(portfolio_state, 'starting_capital', 100000)
        total_value = cash + sum(getattr(p, 'market_value', 0) for p in positions)
        total_pnl = total_value - starting
        pnl_pct = (total_pnl / starting * 100) if starting else 0

        # Build a conversational summary
        if not positions:
            positions_text = "The bot doesn't hold any stocks right now — it's watching and waiting for the right opportunities."
        else:
            lines = []
            for p in positions:
                sym = getattr(p, 'symbol', '???')
                pnl_p = getattr(p, 'unrealized_pnl_pct', 0) * 100
                direction = "up" if pnl_p >= 0 else "down"
                lines.append(f"• **{sym}** is {direction} {abs(pnl_p):.1f}%")
            positions_text = "Here's how our stocks are doing:\n" + "\n".join(lines)

        new_buys = [s for s in (signals or []) if getattr(s, 'action', '') == 'BUY']
        if new_buys:
            signals_text = f"\n\nThe bot found **{len(new_buys)} new stocks** worth buying this week."
        else:
            signals_text = "\n\nNo new stocks met the bar this week — the bot would rather do nothing than make a bad trade."

        desc = (
            f"**☕ Weekly Coffee Briefing**\n\n"
            f"Our portfolio is worth **${total_value:,.0f}** — "
            f"{'up' if total_pnl >= 0 else 'down'} **${abs(total_pnl):,.0f}** "
            f"({'+' if total_pnl >= 0 else ''}{pnl_pct:.1f}%) from where we started.\n\n"
            f"{positions_text}"
            f"{signals_text}"
        )

        await self.send_embed(ch, {
            "title": "☕ This Week in Plain English",
            "description": desc,
            "color": 0x8b5cf6,
        })

    async def send_eli5_daily(self, summary):
        """Simple daily update to #what-the-bot-did."""
        ch = self.channels.get("what_bot_did")
        if not ch:
            return

        day_pnl = getattr(summary, 'day_pnl', 0)
        num_positions = getattr(summary, 'num_positions', 0)
        trades_today = getattr(summary, 'trades_today', []) or []

        if not trades_today:
            trade_text = "No trades today — the bot is holding steady."
        else:
            trade_text = f"{len(trades_today)} trade(s) today."

        desc = (
            f"**End of day update.**\n\n"
            f"Today we {'made' if day_pnl >= 0 else 'lost'} **${abs(day_pnl):,.2f}**. "
            f"We're holding **{num_positions}** stocks. {trade_text}\n\n"
            f"The bot will keep watching overnight and be ready for tomorrow's market."
        )

        await self.send_embed(ch, {
            "title": f"📅 Daily Update — {getattr(summary, 'date', 'Today')}",
            "description": desc,
            "color": 0x22c55e if day_pnl >= 0 else 0xef4444,
        })


# ── Helpers ───────────────────────────────────────────────────────────


def _conviction_badge(conviction: int) -> str:
    if conviction >= 9:
        return f"\U0001f7e2 **{conviction}/10**"
    elif conviction >= 7:
        return f"\U0001f7e1 **{conviction}/10**"
    else:
        return f"\u26aa **{conviction}/10**"


def _split_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks, breaking at newlines."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
