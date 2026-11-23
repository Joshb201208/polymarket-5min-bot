from __future__ import annotations

import asyncio
import copy
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from stock_agent.config import Config
from stock_agent.models import DailySummary, Position, Signal, Thesis, Trade

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
MAX_CONTENT_LENGTH = 2000
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_TOTAL_CHARS = 6000
MAX_EMBED_TITLE = 256
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FIELDS = 25
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FOOTER_TEXT = 2048
MAX_AUTHOR_NAME = 256
MAX_INLINE_RETRY_AFTER = 5.0


def _now_utc() -> datetime:
    sched = sys.modules.get("stock_agent.scheduler")
    if sched is not None and hasattr(sched, "_now_utc"):
        return sched._now_utc()
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SendOutcome:
    ok: bool
    status_code: int | None = None
    retry_after_sec: float | None = None
    permanent_error: bool = False
    auth_error: bool = False
    error: str | None = None
    message_id: str | None = None


def _clamp_non_empty(val: Any, max_len: int) -> str:
    text = str(val if val is not None else "").strip()
    if not text:
        text = "—"
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _clamp_optional(val: Any, max_len: int) -> str:
    text = str(val if val is not None else "")
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def embed_char_count(embed: dict[str, Any]) -> int:
    """Discord counts title + description + field names/values + footer + author toward 6,000."""
    total = len(str(embed.get("title") or "")) + len(str(embed.get("description") or ""))
    footer = embed.get("footer")
    if isinstance(footer, dict):
        total += len(str(footer.get("text") or ""))
    author = embed.get("author")
    if isinstance(author, dict):
        total += len(str(author.get("name") or ""))
    for f in embed.get("fields") or []:
        if isinstance(f, dict):
            total += len(str(f.get("name") or "")) + len(str(f.get("value") or ""))
    return total


def sanitize_and_expand_embed(
    raw_embed: dict[str, Any],
    *,
    mode: str = "PAPER",
    timestamp_iso: str | None = None,
) -> list[dict[str, Any]]:
    """Return 1+ Discord-valid embeds (splitting >25 fields and clamping every string limit)."""
    e = copy.deepcopy(raw_embed)
    ts = timestamp_iso or _now_utc().isoformat()
    if not e.get("timestamp"):
        e["timestamp"] = ts

    footer = e.get("footer")
    if not isinstance(footer, dict) or not footer.get("text"):
        e["footer"] = {"text": f"Stock Agent • {mode} Mode"}
    else:
        e["footer"] = {"text": _clamp_non_empty(footer.get("text"), MAX_FOOTER_TEXT)}

    if "author" in e and isinstance(e["author"], dict) and "name" in e["author"]:
        e["author"] = {**e["author"], "name": _clamp_optional(e["author"].get("name"), MAX_AUTHOR_NAME)}

    title = _clamp_optional(e.get("title", ""), MAX_EMBED_TITLE)
    if title:
        e["title"] = title
    else:
        e.pop("title", None)

    desc = _clamp_optional(e.get("description", ""), MAX_EMBED_DESCRIPTION)
    if desc:
        e["description"] = desc
    else:
        e.pop("description", None)

    raw_fields = e.pop("fields", None) or []
    clean_fields: list[dict[str, Any]] = []
    for f in raw_fields:
        if not isinstance(f, dict):
            continue
        clean_fields.append({
            "name": _clamp_non_empty(f.get("name"), MAX_FIELD_NAME),
            "value": _clamp_non_empty(f.get("value"), MAX_FIELD_VALUE),
            "inline": bool(f.get("inline", False)),
        })

    # Split into chunks of <= MAX_EMBED_FIELDS AND each single embed <= MAX_EMBED_TOTAL_CHARS
    if not clean_fields:
        return [e]

    out: list[dict[str, Any]] = []
    current = copy.deepcopy(e)
    current["fields"] = []

    for f in clean_fields:
        f_chars = len(f["name"]) + len(f["value"])
        if (
            len(current["fields"]) >= MAX_EMBED_FIELDS
            or (current["fields"] and embed_char_count(current) + f_chars > MAX_EMBED_TOTAL_CHARS - 100)
        ):
            out.append(current)
            cont_title = _clamp_optional(f"{title} (cont.)" if title else "Continued", MAX_EMBED_TITLE)
            current = {
                "title": cont_title,
                "color": e.get("color", 0x3B82F6),
                "timestamp": e["timestamp"],
                "footer": copy.deepcopy(e["footer"]),
                "fields": [],
            }
        current["fields"].append(f)

    out.append(current)
    return out


def chunk_embeds_for_discord(
    embeds: list[dict[str, Any]],
    *,
    mode: str = "PAPER",
    timestamp_iso: str | None = None,
) -> list[list[dict[str, Any]]]:
    """Batch embeds so each POST has <= 10 embeds and <= 6,000 combined characters."""
    sanitized: list[dict[str, Any]] = []
    for raw in embeds:
        sanitized.extend(sanitize_and_expand_embed(raw, mode=mode, timestamp_iso=timestamp_iso))

    batches: list[list[dict[str, Any]]] = []
    current_batch: list[dict[str, Any]] = []
    current_chars = 0

    for emb in sanitized:
        chars = embed_char_count(emb)
        if current_batch and (
            len(current_batch) >= MAX_EMBEDS_PER_MESSAGE
            or current_chars + chars > MAX_EMBED_TOTAL_CHARS
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append(emb)
        current_chars += chars

    if current_batch:
        batches.append(current_batch)
    return batches


class DiscordReporter:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.channels = self.config.DISCORD_CHANNELS
        self._client: httpx.AsyncClient | None = None
        self._client_token: str | None = None
        self._disabled_logged = False

    @property
    def token(self) -> str:
        return getattr(self.config, "DISCORD_BOT_TOKEN", "") or os.environ.get("DISCORD_BOT_TOKEN", "")

    @property
    def mode(self) -> str:
        return str(getattr(self.config, "MODE", "") or os.environ.get("STOCK_AGENT_MODE", "PAPER"))

    @property
    def owner_user_id(self) -> str:
        val = getattr(self.config, "DISCORD_OWNER_USER_ID", "") or os.environ.get("DISCORD_OWNER_USER_ID", "")
        return str(val).strip()

    @property
    def legacy_extras_enabled(self) -> bool:
        val = getattr(self.config, "DISCORD_ENABLE_LEGACY_EXTRAS", None)
        if val is not None:
            return bool(val)
        return os.environ.get("DISCORD_ENABLE_LEGACY_EXTRAS", "").strip().lower() in {"1", "true", "yes", "on"}

    def build_critical_mention(self, title: str) -> tuple[str, dict[str, Any]]:
        """Return (content, allowed_mentions) that triggers a phone notification."""
        clean_title = _clamp_optional(title, 300)
        uid = self.owner_user_id
        if uid:
            content = f"\U0001f6a8 <@{uid}> **{clean_title}**"
            allowed = {"parse": [], "users": [uid]}
        else:
            content = f"\U0001f6a8 @here **{clean_title}**"
            allowed = {"parse": ["everyone"]}
        return content[:MAX_CONTENT_LENGTH], allowed

    async def _get_client(self) -> httpx.AsyncClient:
        tok = self.token
        if self._client is None or self._client.is_closed or self._client_token != tok:
            if self._client and not self._client.is_closed:
                await self._client.aclose()
            self._client_token = tok
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "Authorization": f"Bot {tok}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def send_payload_once(
        self,
        channel_id: str,
        *,
        content: str | None = None,
        embeds: list[dict[str, Any]] | None = None,
        allowed_mentions: dict[str, Any] | None = None,
        reply_to_message_id: str | None = None,
    ) -> SendOutcome:
        """Single HTTP attempt with full Discord validation and structured outcome for AlertService."""
        if not self.token:
            if not self._disabled_logged:
                self._disabled_logged = True
                logger.warning("Discord not configured — skipping Discord delivery")
            return SendOutcome(ok=False, permanent_error=True, error="discord_not_configured")

        if not channel_id:
            return SendOutcome(ok=False, permanent_error=True, error="missing_channel_id")

        mentions = allowed_mentions if allowed_mentions is not None else {"parse": []}
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        client = await self._get_client()

        batches = chunk_embeds_for_discord(embeds or [], mode=self.mode) if embeds else [[]]
        last_message_id: str | None = None

        for idx, batch in enumerate(batches):
            payload: dict[str, Any] = {"allowed_mentions": mentions}
            if idx == 0 and content:
                payload["content"] = _clamp_optional(content, MAX_CONTENT_LENGTH)
            if batch:
                payload["embeds"] = batch
            if idx == 0 and reply_to_message_id:
                payload["message_reference"] = {"message_id": str(reply_to_message_id), "fail_if_not_exists": False}

            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 429:
                    retry_after = 1.0
                    try:
                        retry_after = float(resp.json().get("retry_after", 1.0))
                    except Exception:
                        hdr = resp.headers.get("Retry-After")
                        if hdr:
                            try:
                                retry_after = float(hdr)
                            except ValueError:
                                pass
                    return SendOutcome(
                        ok=False,
                        status_code=429,
                        retry_after_sec=max(retry_after, 0.1),
                        error="rate_limited",
                    )
                if resp.status_code in (401, 403):
                    logger.error("Discord auth/permission error (%s): %s", resp.status_code, resp.text[:200])
                    return SendOutcome(
                        ok=False,
                        status_code=resp.status_code,
                        auth_error=True,
                        error=f"http_{resp.status_code}",
                    )
                if 400 <= resp.status_code < 500:
                    logger.error("Discord 4xx payload error (%s): %s", resp.status_code, resp.text[:300])
                    return SendOutcome(
                        ok=False,
                        status_code=resp.status_code,
                        permanent_error=True,
                        error=f"http_{resp.status_code}",
                    )
                resp.raise_for_status()
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("id"):
                        last_message_id = str(data["id"])
                except Exception:
                    pass
            except httpx.HTTPStatusError as e:
                return SendOutcome(
                    ok=False,
                    status_code=e.response.status_code,
                    error=f"http_{e.response.status_code}",
                )
            except Exception as e:
                logger.error("Discord transport error (%s): %s", type(e).__name__, e)
                return SendOutcome(ok=False, error=f"{type(e).__name__}: {e}")

        return SendOutcome(ok=True, status_code=200, message_id=last_message_id)

    async def send_embed(
        self,
        channel_id: str,
        embed: dict[str, Any],
        *,
        content: str | None = None,
        allowed_mentions: dict[str, Any] | None = None,
    ) -> bool:
        """POST an embed to a Discord channel with bounded retry."""
        return await self._send_multi_embed(
            channel_id,
            [embed],
            content=content,
            allowed_mentions=allowed_mentions,
        )

    async def _send_multi_embed(
        self,
        channel_id: str,
        embeds: list[dict[str, Any]],
        *,
        content: str | None = None,
        allowed_mentions: dict[str, Any] | None = None,
    ) -> bool:
        """Send 1+ embeds respecting Discord's 10-embed and 6,000-char caps. Never lies on 429 exhaustion."""
        if not self.token:
            if not self._disabled_logged:
                self._disabled_logged = True
                logger.warning("Discord not configured — skipping embed")
            return False

        batches = chunk_embeds_for_discord(embeds, mode=self.mode)
        if not batches and not content:
            return True

        mentions = allowed_mentions if allowed_mentions is not None else {"parse": []}
        for idx, batch in enumerate(batches or [[]]):
            batch_content = content if idx == 0 else None
            sent = False
            for attempt in range(4):
                outcome = await self.send_payload_once(
                    channel_id,
                    content=batch_content,
                    embeds=batch,
                    allowed_mentions=mentions,
                )
                if outcome.ok:
                    sent = True
                    break
                if outcome.permanent_error or outcome.auth_error:
                    return False
                if outcome.status_code == 429:
                    wait_s = min(outcome.retry_after_sec or 1.0, MAX_INLINE_RETRY_AFTER)
                    if (outcome.retry_after_sec or 0.0) > MAX_INLINE_RETRY_AFTER:
                        logger.warning(
                            "Discord rate limit retry_after=%.1fs exceeds inline cap %.1fs — aborting inline retry",
                            outcome.retry_after_sec,
                            MAX_INLINE_RETRY_AFTER,
                        )
                        return False
                    if attempt < 3:
                        await asyncio.sleep(wait_s)
                        continue
                    return False
                if attempt < 3:
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
                return False
            if not sent:
                return False
        return True

    async def fetch_recent_messages(self, channel_id: str, *, after_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Poll recent channel messages via REST (used for stretch A11 owner commands)."""
        if not self.token or not channel_id:
            return []
        client = await self._get_client()
        params: dict[str, Any] = {"limit": max(1, min(limit, 50))}
        if after_id:
            params["after"] = str(after_id)
        try:
            resp = await client.get(f"{DISCORD_API}/channels/{channel_id}/messages", params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug("Discord message poll failed (%s): %s", type(e).__name__, e)
            return []

    async def fetch_bot_user_id(self) -> str | None:
        """Fetch bot's own user ID via /users/@me once when needed for mention parsing."""
        if not self.token:
            return None
        client = await self._get_client()
        try:
            resp = await client.get(f"{DISCORD_API}/users/@me")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("id"):
                    return str(data["id"])
        except Exception:
            pass
        return None

    # ── ANNOUNCEMENTS ─────────────────────────────────────────────────

    async def send_startup(self, mode: str, cash: float, *, equity: float | None = None) -> bool:
        stop_desc = "ATR 7–12% (broker GTC)"
        min_conv = getattr(self.config, "MIN_CONVICTION", 7)
        fields = [
            {"name": "Mode", "value": f"`{mode}`", "inline": True},
            {"name": "Available Cash", "value": f"${cash:,.0f}", "inline": True},
            {"name": "Max Position", "value": f"{self.config.MAX_POSITION_PCT:.0%}", "inline": True},
            {"name": "Max Exposure", "value": f"{self.config.MAX_TOTAL_EXPOSURE:.0%}", "inline": True},
            {"name": "Stop Protection", "value": stop_desc, "inline": True},
            {"name": "Min Conviction", "value": f"{min_conv}/10", "inline": True},
        ]
        if equity is not None:
            fields.insert(1, {"name": "Account Equity", "value": f"${equity:,.0f}", "inline": True})
        embed = {
            "title": "\U0001f680 Stock Agent Online",
            "color": 0x10B981,
            "fields": fields,
            "footer": {"text": f"Stock Agent • {mode} Mode"},
        }
        return await self.send_embed(self.channels["announcements"], embed)

    async def send_shutdown(self, reason: str) -> bool:
        embed = {
            "title": "\U0001f6d1 Stock Agent Offline",
            "description": reason,
            "color": 0xEF4444,
        }
        return await self.send_embed(self.channels["announcements"], embed)

    # ── TRADE EXECUTIONS ──────────────────────────────────────────────

    async def send_trade_alert(
        self,
        trade: Trade,
        thesis: Thesis | None = None,
        *,
        stop_verified: bool | None = None,
        stop_order_id: str | None = None,
    ) -> bool:
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
            {"name": "Fill Price" if is_buy else "Exit Price", "value": f"${trade.price:.2f}", "inline": True},
            {"name": "Shares", "value": str(trade.shares), "inline": True},
            {"name": "Trade Value", "value": f"${cost:,.0f}", "inline": True},
        ]

        if is_buy:
            if stop_verified is False:
                fields.append({
                    "name": "Stop Protection",
                    "value": "\U0001f6a8 **MISSING AT BROKER — UNPROTECTED**",
                    "inline": True,
                })
            elif thesis and thesis.stop_loss_price:
                pct = (thesis.stop_loss_price - trade.price) / trade.price if trade.price else 0.0
                stop_label = f"${thesis.stop_loss_price:.2f} ({pct:+.1%})"
                if stop_order_id:
                    stop_label += " • Confirmed"
                fields.append({"name": "Stop Protection", "value": stop_label, "inline": True})
            if thesis:
                fields.append({"name": "Conviction", "value": _conviction_badge(thesis.conviction), "inline": True})
                if thesis.catalysts:
                    cat_text = "\n".join(f"\u2022 {c}" for c in thesis.catalysts[:5])
                    fields.append({"name": "Why Now", "value": cat_text[:MAX_FIELD_VALUE], "inline": False})
        else:
            if trade.pnl is not None:
                pnl_emoji = "\u2705" if trade.pnl >= 0 else "\u274c"
                pnl_text = f"{pnl_emoji} ${trade.pnl:+,.2f}"
                if trade.pnl_pct is not None:
                    pnl_text += f" ({trade.pnl_pct:+.1%})"
                fields.append({"name": "Realized P&L", "value": pnl_text, "inline": True})
            if trade.hold_days is not None:
                fields.append({"name": "Held", "value": f"{trade.hold_days} days", "inline": True})

        embed = {
            "title": f"{emoji} {trade.action} — {trade.symbol}",
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": f"Stock Agent • {self.mode} Mode • {trade.symbol}"},
        }

        ok = await self.send_embed(self.channels["trades"], embed)
        if self.legacy_extras_enabled and self.channels.get("trade_history"):
            await self.send_embed(self.channels["trade_history"], embed)
        return ok

    # ── THESIS BOARD ──────────────────────────────────────────────────

    async def send_thesis(self, thesis: Thesis) -> bool:
        color = 0xF59E0B if thesis.conviction >= 8 else 0x3B82F6
        fields = [
            {"name": "Direction", "value": f"**{thesis.direction}**", "inline": True},
            {"name": "Conviction", "value": _conviction_badge(thesis.conviction), "inline": True},
            {"name": "Time Horizon", "value": thesis.time_horizon or "—", "inline": True},
        ]
        if thesis.target_price:
            fields.append({"name": "Target Price", "value": f"${thesis.target_price:.2f}", "inline": True})
        if thesis.stop_loss_price:
            fields.append({"name": "Planned Stop", "value": f"${thesis.stop_loss_price:.2f}", "inline": True})

        fields.append({"name": "Bull Case", "value": (thesis.bull_case or "—")[:MAX_FIELD_VALUE], "inline": False})
        fields.append({"name": "Bear Case", "value": (thesis.bear_case or "—")[:MAX_FIELD_VALUE], "inline": False})

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
            "footer": {"text": f"Stock Agent • {self.mode} Mode • {thesis.symbol}"},
        }
        return await self.send_embed(self.channels["thesis"], embed)

    # ── DAILY P&L (legacy model wrapper; rich BookSnapshot reports live in reporting.py) ──

    async def send_daily_summary(self, summary: DailySummary) -> bool:
        color = 0x22C55E if summary.day_pnl >= 0 else 0xEF4444
        trades = summary.trades_today or []
        buys = [t for t in trades if getattr(t, "action", "") == "BUY"]
        sells = [t for t in trades if getattr(t, "action", "") == "SELL"]
        if sells:
            wins = sum(1 for t in sells if t.pnl is not None and t.pnl > 0)
            trades_label = f"{len(buys)} buy, {len(sells)} sell ({wins}/{len(sells)} profitable)"
        elif buys:
            trades_label = f"{len(buys)} buy, 0 closed sells"
        else:
            trades_label = "None"

        fields = [
            {"name": "Account Equity", "value": f"```${summary.portfolio_value:,.2f}```", "inline": True},
            {"name": "Day Change", "value": f"```{'+' if summary.day_pnl >= 0 else ''}${summary.day_pnl:,.2f} ({summary.day_pnl_pct:+.2%})```", "inline": True},
            {"name": "Since Launch", "value": f"```{'+' if summary.total_pnl >= 0 else ''}${summary.total_pnl:,.2f} ({summary.total_pnl_pct:+.2%})```", "inline": True},
            {"name": "Cash", "value": f"${summary.cash:,.0f}", "inline": True},
            {"name": "Exposure", "value": f"{summary.exposure_pct:.1%}", "inline": True},
            {"name": "Trades Today", "value": trades_label, "inline": True},
        ]

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
                chunk_rows = [header]
                for row in rows[1:]:
                    chunk_rows.append(row)
                    test = "```\n" + "\n".join(chunk_rows) + "\n```"
                    if len(test) > MAX_FIELD_VALUE - 50:
                        fields.append({"name": "\U0001f4cc Positions", "value": "```\n" + "\n".join(chunk_rows[:-1]) + "\n```", "inline": False})
                        chunk_rows = [header, row]
                if len(chunk_rows) > 1:
                    fields.append({"name": "Positions (cont.)", "value": "```\n" + "\n".join(chunk_rows) + "\n```", "inline": False})

        if trades:
            trade_lines = []
            for t in trades:
                pnl_str = f" → ${t.pnl:+,.0f}" if t.pnl is not None else ""
                trade_lines.append(f"\u2022 **{t.action}** {t.symbol} x{t.shares} @ ${t.price:.2f}{pnl_str}")
            fields.append({"name": "Executed Trades", "value": "\n".join(trade_lines)[:MAX_FIELD_VALUE], "inline": False})

        embed = {
            "title": f"\U0001f4ca Daily Summary — {summary.date}",
            "color": color,
            "fields": fields,
            "footer": {"text": f"Stock Agent • {self.mode} Mode • {summary.num_positions} positions"},
        }
        return await self.send_embed(self.channels["daily_pnl"], embed)

    # ── PORTFOLIO OVERVIEW ────────────────────────────────────────────

    async def send_weekly_report(
        self,
        portfolio_value: float,
        cash: float,
        positions: list[Position],
        signals: list[Signal],
        stats: dict,
    ) -> bool:
        embeds: list[dict[str, Any]] = []
        fields = [
            {"name": "Account Equity", "value": f"```${portfolio_value:,.2f}```", "inline": True},
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
            sharpe = stats.get("sharpe_ratio", stats.get("sharpe"))
            if sharpe is not None:
                fields.append({"name": "Sharpe Ratio", "value": f"{sharpe:.2f}", "inline": True})

        embeds.append({
            "title": "\U0001f4cb Weekly Report",
            "color": 0x3B82F6,
            "fields": fields,
        })

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
                        f"Conviction: {conv}/10 | {p.sector or 'Unknown'}"
                    ),
                    "inline": True,
                })
            embeds.append({
                "title": f"\U0001f4bc Positions ({len(positions)})",
                "color": 0x3B82F6,
                "fields": pos_fields,
            })

        if signals:
            sig_fields = []
            for s in signals:
                desc = (s.thesis.summary[:200] if s.thesis and s.thesis.summary else "Queued for next open")
                sig_fields.append({
                    "name": f"\U0001f7e2 {s.action} {s.symbol} — {s.conviction}/10",
                    "value": desc,
                    "inline": False,
                })
            embeds.append({
                "title": f"\U0001f4e1 Queued Signals for Next Open ({len(signals)})",
                "color": 0xF59E0B,
                "fields": sig_fields,
            })

        return await self._send_multi_embed(self.channels["portfolio"], embeds)

    # ── RISK ALERTS ───────────────────────────────────────────────────

    async def send_risk_alert(
        self,
        alert_type: str,
        details: str,
        symbol: str | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        type_config = {
            "STOP_LOSS": ("\U0001f6a8 Stop-Loss Triggered", 0xEF4444),
            "THESIS_BREAK": ("\u26a0\ufe0f Thesis Broken", 0xEF4444),
            "EXPOSURE_WARNING": ("\U0001f4c9 Exposure Warning", 0xF59E0B),
            "SECTOR_LIMIT": ("\U0001f4ca Sector Limit", 0xF59E0B),
            "PDT_WARNING": ("\u26a0\ufe0f PDT Compliance", 0xF59E0B),
        }
        title, color = type_config.get(alert_type, (f"\u26a0\ufe0f {alert_type}", 0xEF4444))
        fields = [{"name": "Details", "value": (details or "—")[:MAX_FIELD_VALUE], "inline": False}]
        if symbol:
            fields.insert(0, {"name": "Symbol", "value": f"**{symbol}**", "inline": True})

        embed = {"title": title, "color": color, "fields": fields}
        content, mentions = self.build_critical_mention(title) if critical else (None, {"parse": []})
        return await self.send_embed(
            self.channels["risk"],
            embed,
            content=content,
            allowed_mentions=mentions,
        )

    # ── MARKET MONITOR ────────────────────────────────────────────────

    async def send_material_event(self, symbol: str, event: str, impact: str, severity: int) -> bool:
        color_map = {"positive": 0x10B981, "negative": 0xEF4444, "neutral": 0x3B82F6}
        color = color_map.get(impact, 0x6B7280)
        sev_bar = "\U0001f7e5" * min(severity, 10) + "\u2b1c" * max(0, 10 - severity)
        embed = {
            "title": f"\U0001f4e2 Material Event — {symbol}",
            "color": color,
            "fields": [
                {"name": "Event", "value": (event or "—")[:MAX_FIELD_VALUE], "inline": False},
                {"name": "Impact", "value": f"**{impact.title()}**", "inline": True},
                {"name": "Severity", "value": f"{severity}/10\n{sev_bar}", "inline": True},
            ],
        }
        return await self.send_embed(self.channels["market_monitor"], embed)

    # ── SCREENER OUTPUT ───────────────────────────────────────────────

    async def send_screener_results(self, ranked_companies: list) -> bool:
        if not ranked_companies:
            return False
        header = f"{'#':<3} {'Symbol':<6} {'Score':>6} {'Name':<20}"
        rows = [header, "-" * 40]
        for i, r in enumerate(ranked_companies[:25], 1):
            sym = r.get("symbol", "?")
            score = r.get("composite", r.get("score", r.get("composite_score", 0.0))) or 0.0
            name = str(r.get("name") or r.get("companyName") or r.get("sector") or "")[:20]
            rows.append(f"{i:<3} {sym:<6} {float(score):>6.1f} {name:<20}")
        table = "```\n" + "\n".join(rows) + "\n```"
        embed = {
            "title": f"\U0001f50d Screener Results — Top {min(len(ranked_companies), 25)}",
            "description": table[:MAX_EMBED_DESCRIPTION],
            "color": 0x3B82F6,
        }
        return await self.send_embed(self.channels["screener"], embed)

    # ── ANALYST ARCHIVE (legacy optional) ─────────────────────────────

    async def send_analysis_report(self, symbol: str, analysis: str, sources: list) -> bool:
        if not self.legacy_extras_enabled:
            return False
        source_text = ""
        if sources:
            source_text = "\n\n**Sources:**\n" + "\n".join(f"\u2022 {s}" for s in sources[:10])
        full_text = analysis + source_text
        chunks = _split_text(full_text, MAX_EMBED_DESCRIPTION)
        embeds = [
            {
                "title": f"\U0001f9e0 Analysis — {symbol}" + (f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""),
                "description": chunk,
                "color": 0x3B82F6,
            }
            for i, chunk in enumerate(chunks)
        ]
        return await self._send_multi_embed(self.channels["analyst"], embeds)

    # ── MACRO CONTEXT ─────────────────────────────────────────────────

    async def send_macro_update(self, title: str, content: str) -> bool:
        embed = {
            "title": f"\U0001f30d {title}",
            "description": (content or "—")[:MAX_EMBED_DESCRIPTION],
            "color": 0x8B5CF6,
        }
        return await self.send_embed(self.channels["macro"], embed)

    # ── SYSTEM LOGS ───────────────────────────────────────────────────

    async def send_system_log(self, level: str, message: str) -> bool:
        lvl = level.upper()
        if lvl == "INFO" and not self.legacy_extras_enabled:
            logger.info("System status: %s", message)
            return False
        color_map = {"INFO": 0x6B7280, "WARNING": 0xF59E0B, "ERROR": 0xEF4444}
        embed = {
            "title": lvl,
            "description": f"```{(message or '—')[:4000]}```",
            "color": color_map.get(lvl, 0x6B7280),
        }
        return await self.send_embed(self.channels["system_logs"], embed)

    async def send_error(self, error: str, context: str = "", *, critical: bool = False) -> bool:
        desc = f"```{(error or 'Unknown error')[:3500]}```"
        if context:
            desc = f"**Context:** {context}\n\n" + desc
        title = f"\u274c Error — {context}" if context else "\u274c Error"
        embed = {
            "title": title[:MAX_EMBED_TITLE],
            "description": desc[:MAX_EMBED_DESCRIPTION],
            "color": 0xEF4444,
        }
        ch = self.channels.get("risk") if critical else self.channels.get("system_logs")
        content, mentions = self.build_critical_mention(title) if critical else (None, {"parse": []})
        return await self.send_embed(ch or self.channels["system_logs"], embed, content=content, allowed_mentions=mentions)

    # ── Legacy ELI5 channels (disabled by default to cut duplicate/stale noise) ──

    async def send_eli5_trade(self, trade, thesis, is_buy: bool = True) -> bool:
        if not self.legacy_extras_enabled:
            return False
        ch = self.channels.get("what_bot_did")
        if not ch:
            return False
        if is_buy:
            stop_price = getattr(thesis, "stop_loss_price", None)
            stop_line = (
                f"A broker stop-loss order is set near ${stop_price:,.2f} to cap downside."
                if stop_price
                else "Protected by a broker GTC stop-loss order (7–12% ATR band)."
            )
            desc = (
                f"**Bought {getattr(trade, 'symbol', 'a stock')}.**\n\n"
                f"**Why:** {getattr(thesis, 'summary', 'No summary available.')}\n\n"
                f"**Size:** {getattr(trade, 'shares', '?')} shares at "
                f"${getattr(trade, 'price', 0):,.2f}.\n\n"
                f"**Protection:** {stop_line}"
            )
            color = 0x22C55E
            title = f"Bought {getattr(trade, 'symbol', '???')}"
        else:
            pnl = getattr(trade, "pnl", 0) or 0
            pnl_word = "profit" if pnl >= 0 else "loss"
            desc = (
                f"**Sold {getattr(trade, 'symbol', 'a stock')}.**\n\n"
                f"**Why:** {getattr(trade, 'reason', 'Rule triggered.')}\n\n"
                f"**Result:** ${abs(pnl):,.2f} {pnl_word}."
            )
            color = 0xEF4444 if pnl < 0 else 0x22C55E
            title = f"Sold {getattr(trade, 'symbol', '???')}"
        return await self.send_embed(ch, {"title": title, "description": desc, "color": color})

    async def send_eli5_weekly(self, portfolio_state, signals) -> bool:
        if not self.legacy_extras_enabled:
            return False
        ch = self.channels.get("weekly_eli5")
        if not ch:
            return False
        positions = getattr(portfolio_state, "positions", []) or []
        cash = getattr(portfolio_state, "cash", 100000)
        starting = getattr(portfolio_state, "starting_capital", 100000)
        total_value = cash + sum(getattr(p, "market_value", 0) for p in positions)
        total_pnl = total_value - starting
        pnl_pct = (total_pnl / starting * 100) if starting else 0
        desc = (
            f"Account value: **${total_value:,.0f}** "
            f"({'+' if total_pnl >= 0 else ''}{pnl_pct:.1f}% vs start). "
            f"Queued ideas for next open: **{len(signals or [])}**."
        )
        return await self.send_embed(ch, {"title": "Weekly Plain-English Recap", "description": desc, "color": 0x8B5CF6})

    async def send_eli5_daily(self, summary) -> bool:
        if not self.legacy_extras_enabled:
            return False
        ch = self.channels.get("what_bot_did")
        if not ch:
            return False
        day_pnl = getattr(summary, "day_pnl", 0)
        desc = (
            f"Today's change: **{'+' if day_pnl >= 0 else '-'}${abs(day_pnl):,.2f}** "
            f"across **{getattr(summary, 'num_positions', 0)}** positions."
        )
        return await self.send_embed(
            ch,
            {
                "title": f"Daily Recap — {getattr(summary, 'date', 'Today')}",
                "description": desc,
                "color": 0x22C55E if day_pnl >= 0 else 0xEF4444,
            },
        )


def _conviction_badge(conviction: int) -> str:
    if conviction >= 9:
        return f"\U0001f7e2 **{conviction}/10**"
    if conviction >= 7:
        return f"\U0001f7e1 **{conviction}/10**"
    return f"\u26aa **{conviction}/10**"


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
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


__all__ = [
    "DiscordReporter",
    "SendOutcome",
    "chunk_embeds_for_discord",
    "embed_char_count",
    "sanitize_and_expand_embed",
    "datetime",
]
