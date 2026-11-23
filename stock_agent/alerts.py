"""Central operator alert service with severity tiers, deduplication and persistent outbox.

Public contract (DECISIONS C):
    from stock_agent.alerts import AlertService, Severity, alert
    alert(severity, title, body, key=None)

Design guarantees:
- ``alert(...)`` / ``service.emit(...)`` is synchronous and performs zero network I/O,
  so raising an alert inside trading, reconcile or risk code can never stall an order.
- Queued items and deduplication state persist on disk (``DATA_DIR/alert_outbox.json``),
  so alerts raised while Discord is unreachable are delivered once connectivity returns,
  and restart loops do not re-flood the channel.
- Every critical alert triggers a mobile notification via ``<@DISCORD_OWNER_USER_ID>``
  when set, or ``@here`` by default (DECISIONS B6). Nothing requires a new setting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from stock_agent.config import Config
from stock_agent.discord_alerts import DiscordReporter
from stock_agent.telegram_alerts import TelegramReporter, _escape_html

logger = logging.getLogger(__name__)

_SECRET_URL_RE = re.compile(
    r"((?:apikey|api_key|token|secret)=)[^&\s'\"]+|(/bot\d+:[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


def redact_secrets_in_text(text: str) -> str:
    """Mask URL query keys and Telegram bot paths before logging or displaying errors."""
    if not text:
        return ""

    def _repl(m: re.Match[str]) -> str:
        if m.group(1):
            return f"{m.group(1)}[REDACTED]"
        return "/bot[REDACTED]"

    return _SECRET_URL_RE.sub(_repl, str(text))


def _default_now() -> datetime:
    sched = sys.modules.get("stock_agent.scheduler")
    if sched is not None and hasattr(sched, "_now_utc"):
        return sched._now_utc()
    return datetime.now(timezone.utc)


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


def coerce_severity(value: Severity | str | Any) -> Severity:
    if isinstance(value, Severity):
        return value
    raw = str(getattr(value, "value", value)).strip().lower()
    if raw in ("critical", "crit", "error", "fatal", "high"):
        return Severity.CRITICAL
    if raw in ("warning", "warn", "medium"):
        return Severity.WARNING
    return Severity.INFO


class AlertCategory(StrEnum):
    GENERAL = "general"
    ORDER_REJECTED = "order_rejected"
    STOP_MISSING = "stop_missing"
    STOP_FILLED = "stop_filled"
    RECONCILE_DIFF = "reconcile_diff"
    INPUT_HEALTH = "input_health"
    SKIPPED_CYCLE = "skipped_cycle"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    TRADE_FILL = "trade_fill"
    NEWS_REACTION = "news_reaction"
    OPTION_STRUCTURE = "option_structure"
    NORMALISE = "normalise"
    RISK_BLOCK = "risk_block"
    LIFECYCLE = "lifecycle"
    REPORT = "report"


_SEVERITY_RANK = {
    Severity.CRITICAL: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}

_SEVERITY_COLOR = {
    Severity.CRITICAL: 0xEF4444,
    Severity.WARNING: 0xF59E0B,
    Severity.INFO: 0x3B82F6,
}


@dataclass
class AlertEvent:
    event_id: str
    created_at: datetime
    severity: Severity
    title: str
    body: str
    key: str | None = None
    category: AlertCategory = AlertCategory.GENERAL
    channel_key: str = "risk"
    fields: list[tuple[str, str, bool]] = field(default_factory=list)
    embeds: list[dict[str, Any]] = field(default_factory=list)
    resolved: bool = False
    suppressed_before_this: int = 0
    mirror_telegram: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "created_at": self.created_at.isoformat(),
            "severity": self.severity.value,
            "title": self.title,
            "body": self.body,
            "key": self.key,
            "category": self.category.value,
            "channel_key": self.channel_key,
            "fields": [list(f) for f in self.fields],
            "embeds": self.embeds,
            "resolved": self.resolved,
            "suppressed_before_this": self.suppressed_before_this,
            "mirror_telegram": self.mirror_telegram,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlertEvent:
        return cls(
            event_id=str(data["event_id"]),
            created_at=_parse_dt(data.get("created_at")) or _default_now(),
            severity=coerce_severity(data.get("severity", "info")),
            title=str(data.get("title", "")),
            body=str(data.get("body", "")),
            key=data.get("key"),
            category=AlertCategory(data.get("category", "general"))
            if data.get("category") in AlertCategory._value2member_map_
            else AlertCategory.GENERAL,
            channel_key=str(data.get("channel_key", "risk")),
            fields=[(str(f[0]), str(f[1]), bool(f[2])) for f in (data.get("fields") or []) if len(f) >= 3],
            embeds=list(data.get("embeds") or []),
            resolved=bool(data.get("resolved", False)),
            suppressed_before_this=int(data.get("suppressed_before_this", 0)),
            mirror_telegram=data.get("mirror_telegram"),
        )


@dataclass
class OutboxItem:
    event: AlertEvent
    attempts: int = 0
    next_attempt_at: datetime = field(default_factory=_default_now)
    last_error: str | None = None
    discord_done: bool = False
    telegram_done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at.isoformat(),
            "last_error": self.last_error,
            "discord_done": self.discord_done,
            "telegram_done": self.telegram_done,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutboxItem:
        ev = AlertEvent.from_dict(data["event"])
        return cls(
            event=ev,
            attempts=int(data.get("attempts", 0)),
            next_attempt_at=_parse_dt(data.get("next_attempt_at")) or ev.created_at,
            last_error=data.get("last_error"),
            discord_done=bool(data.get("discord_done", False)),
            telegram_done=bool(data.get("telegram_done", False)),
        )


@dataclass
class DedupState:
    first_seen_at: datetime
    last_sent_at: datetime
    severity: Severity
    suppressed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_sent_at": self.last_sent_at.isoformat(),
            "severity": self.severity.value,
            "suppressed_count": self.suppressed_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DedupState:
        now = _default_now()
        return cls(
            first_seen_at=_parse_dt(data.get("first_seen_at")) or now,
            last_sent_at=_parse_dt(data.get("last_sent_at")) or now,
            severity=coerce_severity(data.get("severity", "warning")),
            suppressed_count=int(data.get("suppressed_count", 0)),
        )


class AlertService:
    """Non-blocking alert bus with deduplication, persistent outbox and plain-English formatters."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        discord: DiscordReporter | None = None,
        telegram: TelegramReporter | None = None,
        journal: Any | None = None,
        outbox_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or Config()
        self.discord = discord or DiscordReporter(self.config)
        self.telegram = telegram or TelegramReporter(self.config)
        self.journal = journal
        self._clock = clock or _default_now
        data_dir = Path(getattr(self.config, "DATA_DIR", None) or os.environ.get("STOCK_DATA_DIR", "data/stock_agent"))
        self.outbox_path = Path(outbox_path) if outbox_path is not None else (data_dir / "alert_outbox.json")

        self.outbox: list[OutboxItem] = []
        self.active_keys: dict[str, DedupState] = {}
        self._discord_halted_until: datetime | None = None
        self._load()

    def __call__(
        self,
        severity: Severity | str,
        title: str,
        body: str,
        key: str | None = None,
        **kwargs: Any,
    ) -> bool:
        return self.alert(severity, title, body, key=key, **kwargs)

    @property
    def critical_remind_sec(self) -> int:
        return int(getattr(self.config, "ALERT_CRITICAL_REMIND_SEC", 3600))

    @property
    def warning_cooldown_sec(self) -> int:
        return int(getattr(self.config, "ALERT_WARNING_COOLDOWN_SEC", 14400))

    @property
    def info_cooldown_sec(self) -> int:
        return int(getattr(self.config, "ALERT_INFO_COOLDOWN_SEC", 3600))

    @property
    def max_outbox_items(self) -> int:
        return int(getattr(self.config, "ALERT_OUTBOX_MAX_ITEMS", 200))

    def _cooldown_for(self, severity: Severity) -> int:
        if severity == Severity.CRITICAL:
            return self.critical_remind_sec
        if severity == Severity.WARNING:
            return self.warning_cooldown_sec
        return self.info_cooldown_sec

    def _load(self) -> None:
        if not self.outbox_path.exists():
            return
        try:
            raw = json.loads(self.outbox_path.read_text(encoding="utf-8"))
            self.outbox = [OutboxItem.from_dict(x) for x in raw.get("outbox", [])]
            self.active_keys = {
                str(k): DedupState.from_dict(v)
                for k, v in (raw.get("active_keys") or {}).items()
            }
        except Exception as e:
            logger.error("Could not read alert outbox %s (%s): %s — starting fresh queue", self.outbox_path, type(e).__name__, e)
            self.outbox = []
            self.active_keys = {}

    def _save(self) -> None:
        try:
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "outbox": [item.to_dict() for item in self.outbox],
                "active_keys": {k: v.to_dict() for k, v in self.active_keys.items()},
            }
            fd, tmp_name = tempfile.mkstemp(
                prefix=".alert_outbox_",
                suffix=".tmp",
                dir=str(self.outbox_path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                    fh.flush()
                os.replace(tmp_name, self.outbox_path)
            except Exception:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise
        except Exception as e:
            logger.error("Could not persist alert outbox (%s): %s", type(e).__name__, e)

    def _evict_if_full(self) -> None:
        cap = self.max_outbox_items
        while len(self.outbox) >= cap and self.outbox:
            # Evict oldest INFO first, then oldest WARNING, never CRITICAL unless all are CRITICAL
            idx_to_drop: int | None = None
            for target_sev in (Severity.INFO, Severity.WARNING, Severity.CRITICAL):
                for i, item in enumerate(self.outbox):
                    if item.event.severity == target_sev:
                        idx_to_drop = i
                        break
                if idx_to_drop is not None:
                    break
            if idx_to_drop is None:
                break
            dropped = self.outbox.pop(idx_to_drop)
            logger.warning(
                "Alert outbox at cap (%d) — dropped oldest %s alert: %s",
                cap,
                dropped.event.severity.value,
                dropped.event.title,
            )

    def _record_journal(self, event: AlertEvent, *, suppressed: bool = False) -> None:
        if self.journal is None or not hasattr(self.journal, "append"):
            return
        try:
            self.journal.append(
                "alert",
                event_id=event.event_id,
                severity=event.severity.value,
                category=event.category.value,
                key=event.key,
                title=event.title,
                body=event.body,
                resolved=event.resolved,
                suppressed=suppressed,
            )
        except Exception:
            pass

    def alert(
        self,
        severity: Severity | str,
        title: str,
        body: str,
        key: str | None = None,
        dedupe_window_sec: float | None = None,
        *,
        dedupe_window: float | None = None,
        category: AlertCategory = AlertCategory.GENERAL,
        channel_key: str | None = None,
        fields: Sequence[tuple[str, str, bool]] | None = None,
        embeds: list[dict[str, Any]] | None = None,
        resolved: bool = False,
        mirror_telegram: bool | None = None,
    ) -> bool:
        """Queue an alert without network I/O. Returns True if queued, False if deduplicated."""
        if dedupe_window_sec is None and dedupe_window is not None:
            dedupe_window_sec = dedupe_window
        sev = coerce_severity(severity)
        clean_title = redact_secrets_in_text(title).strip() or "Stock Agent Notice"
        clean_body = redact_secrets_in_text(body).strip()
        dedup_key = key.strip() if isinstance(key, str) and key.strip() else None

        default_channel = "risk" if sev in (Severity.CRITICAL, Severity.WARNING) else "announcements"
        target_channel = channel_key or default_channel
        now = self._clock()
        suppressed_count = 0

        if dedup_key:
            state = self.active_keys.get(dedup_key)
            if resolved:
                if state is None:
                    return False
                suppressed_count = state.suppressed_count
                self.active_keys.pop(dedup_key, None)
                sev = Severity.INFO
                clean_title = f"Resolved: {clean_title}"
            else:
                if state is not None:
                    window = float(dedupe_window_sec) if dedupe_window_sec is not None else float(self._cooldown_for(sev))
                    elapsed = (now - state.last_sent_at).total_seconds()
                    # Escalating from WARNING/INFO to CRITICAL bypasses warning cooldown
                    escalated = _SEVERITY_RANK[sev] < _SEVERITY_RANK[state.severity]
                    if not escalated and elapsed < window:
                        state.suppressed_count += 1
                        self._save()
                        return False
                    suppressed_count = state.suppressed_count
                    state.last_sent_at = now
                    state.severity = sev
                    state.suppressed_count = 0
                else:
                    self.active_keys[dedup_key] = DedupState(
                        first_seen_at=now,
                        last_sent_at=now,
                        severity=sev,
                        suppressed_count=0,
                    )

        clean_fields = [
            (redact_secrets_in_text(name), redact_secrets_in_text(val), bool(inl))
            for name, val, inl in (fields or [])
        ]

        event = AlertEvent(
            event_id=f"alrt-{uuid.uuid4().hex[:16]}",
            created_at=now,
            severity=sev,
            title=clean_title,
            body=clean_body,
            key=dedup_key,
            category=category,
            channel_key=target_channel,
            fields=clean_fields,
            embeds=list(embeds or []),
            resolved=resolved,
            suppressed_before_this=suppressed_count,
            mirror_telegram=mirror_telegram,
        )
        self._evict_if_full()
        self.outbox.append(OutboxItem(event=event, next_attempt_at=now))
        self._save()
        self._record_journal(event)
        return True

    def emit(self, event: AlertEvent) -> bool:
        """Enqueue a pre-built AlertEvent."""
        return self.alert(
            event.severity,
            event.title,
            event.body,
            key=event.key,
            category=event.category,
            channel_key=event.channel_key,
            fields=event.fields,
            embeds=event.embeds,
            resolved=event.resolved,
            mirror_telegram=event.mirror_telegram,
        )

    def _build_discord_embeds(self, event: AlertEvent, now: datetime) -> list[dict[str, Any]]:
        if event.embeds:
            return event.embeds

        color = 0x10B981 if event.resolved else _SEVERITY_COLOR[event.severity]
        badge = {
            Severity.CRITICAL: "\U0001f6a8 Action Needed",
            Severity.WARNING: "\u26a0\ufe0f Heads Up",
            Severity.INFO: "\u2705 Resolved" if event.resolved else "\u2139\ufe0f Update",
        }[event.severity]

        desc_parts = []
        if event.body:
            desc_parts.append(event.body)
        if event.suppressed_before_this > 0:
            desc_parts.append(
                f"Note: {event.suppressed_before_this} identical notices were grouped since the previous alert."
            )
        delay_sec = (now - event.created_at).total_seconds()
        if delay_sec > 120:
            desc_parts.append(
                f"Delayed delivery: originally raised at {event.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')} "
                "while Discord was unreachable."
            )

        embed_fields = [
            {"name": name, "value": val, "inline": inl}
            for name, val, inl in event.fields
        ]
        embed: dict[str, Any] = {
            "title": f"{badge} — {event.title}",
            "description": "\n\n".join(desc_parts),
            "color": color,
            "timestamp": event.created_at.isoformat(),
            "fields": embed_fields,
        }
        return [embed]

    def _build_telegram_html(self, event: AlertEvent) -> str:
        prefix = {
            Severity.CRITICAL: "\U0001f6a8 <b>CRITICAL</b>",
            Severity.WARNING: "\u26a0\ufe0f <b>WARNING</b>",
            Severity.INFO: "\u2139\ufe0f <b>UPDATE</b>",
        }[event.severity]
        lines = [f"{prefix}: <b>{_escape_html(event.title)}</b>"]
        if event.body:
            lines.append(_escape_html(event.body))
        for name, val, _ in event.fields:
            lines.append(f"• <b>{_escape_html(name)}:</b> {_escape_html(val)}")
        return "\n".join(lines)

    async def flush(self, *, max_items: int = 25, max_budget_sec: float = 8.0) -> int:
        """Drain due outbox items without blocking the main loop for long."""
        if not self.outbox:
            return 0

        start_ts = self._clock()
        delivered_count = 0
        changed = False

        # Sort indices: CRITICAL first, then WARNING, then INFO, preserving FIFO order
        due_indices = [
            idx
            for idx, item in enumerate(self.outbox)
            if item.next_attempt_at <= self._clock()
        ]
        due_indices.sort(key=lambda idx: (_SEVERITY_RANK[self.outbox[idx].event.severity], idx))

        completed_ids: set[str] = set()

        for idx in due_indices[:max_items]:
            now = self._clock()
            if (now - start_ts).total_seconds() >= max_budget_sec:
                break

            item = self.outbox[idx]
            ev = item.event

            want_telegram = bool(
                self.telegram.enabled
                and (
                    ev.mirror_telegram is True
                    or (ev.mirror_telegram is None and ev.severity == Severity.CRITICAL)
                )
            )
            if not want_telegram:
                item.telegram_done = True

            # 1. Discord delivery
            if not item.discord_done:
                if not self.discord.token:
                    # Discord not configured: mark done if Telegram is configured, else drop cleanly
                    item.discord_done = True
                    changed = True
                elif self._discord_halted_until and now < self._discord_halted_until:
                    item.next_attempt_at = self._discord_halted_until
                    changed = True
                    if want_telegram and not item.telegram_done:
                        item.telegram_done = await self.telegram.send_message(self._build_telegram_html(ev))
                    continue
                else:
                    ch_id = self.discord.channels.get(ev.channel_key) or self.discord.channels.get("risk", "")
                    is_crit = ev.severity == Severity.CRITICAL and not ev.resolved
                    content, mentions = (
                        self.discord.build_critical_mention(ev.title)
                        if is_crit
                        else (None, {"parse": []})
                    )
                    embeds = self._build_discord_embeds(ev, now)
                    outcome = await self.discord.send_payload_once(
                        ch_id,
                        content=content,
                        embeds=embeds,
                        allowed_mentions=mentions,
                    )

                    if outcome.ok:
                        item.discord_done = True
                        changed = True
                    elif outcome.status_code == 429:
                        wait_s = outcome.retry_after_sec or 1.0
                        remaining_budget = max_budget_sec - (self._clock() - start_ts).total_seconds()
                        if wait_s <= 2.0 and wait_s < remaining_budget:
                            await asyncio.sleep(wait_s)
                            outcome2 = await self.discord.send_payload_once(
                                ch_id,
                                content=content,
                                embeds=embeds,
                                allowed_mentions=mentions,
                            )
                            if outcome2.ok:
                                item.discord_done = True
                                changed = True
                            else:
                                item.attempts += 1
                                item.last_error = outcome2.error
                                item.next_attempt_at = self._clock() + timedelta(
                                    seconds=min(outcome2.retry_after_sec or 10.0, 300.0)
                                )
                                changed = True
                                continue
                        else:
                            item.attempts += 1
                            item.last_error = "rate_limited"
                            item.next_attempt_at = self._clock() + timedelta(seconds=min(wait_s, 300.0))
                            changed = True
                            continue
                    elif outcome.auth_error:
                        # Protect against Discord 10k/10min invalid-request IP ban
                        self._discord_halted_until = self._clock() + timedelta(seconds=600)
                        item.attempts += 1
                        item.last_error = outcome.error
                        item.next_attempt_at = self._discord_halted_until
                        changed = True
                        if self.telegram.enabled and not item.telegram_done:
                            item.telegram_done = await self.telegram.send_message(self._build_telegram_html(ev))
                        continue
                    elif outcome.permanent_error:
                        logger.error("Dropping malformed Discord alert %s: %s", ev.title, outcome.error)
                        item.discord_done = True
                        item.last_error = outcome.error
                        changed = True
                        if self.telegram.enabled and not item.telegram_done:
                            item.telegram_done = await self.telegram.send_message(self._build_telegram_html(ev))
                    else:
                        item.attempts += 1
                        backoff = min(15 * (2 ** max(0, item.attempts - 1)), 900)
                        item.last_error = outcome.error
                        item.next_attempt_at = self._clock() + timedelta(seconds=backoff)
                        changed = True
                        if ev.severity == Severity.CRITICAL and self.telegram.enabled and not item.telegram_done:
                            item.telegram_done = await self.telegram.send_message(self._build_telegram_html(ev))
                        continue

            # 2. Optional Telegram mirror
            if want_telegram and not item.telegram_done:
                ok_tg = await self.telegram.send_message(self._build_telegram_html(ev))
                item.telegram_done = ok_tg or item.attempts >= 2
                changed = True

            if item.discord_done and item.telegram_done:
                completed_ids.add(ev.event_id)
                delivered_count += 1
                changed = True

        if completed_ids:
            self.outbox = [it for it in self.outbox if it.event.event_id not in completed_ids]

        if changed:
            self._save()
        return delivered_count

    async def alert_and_flush(
        self,
        severity: Severity | str,
        title: str,
        body: str,
        key: str | None = None,
        **kwargs: Any,
    ) -> bool:
        queued = self.alert(severity, title, body, key=key, **kwargs)
        if queued:
            await self.flush(max_items=5, max_budget_sec=4.0)
        return queued

    # ── Mandatory operator alert rules (plain English for the owner) ──

    def alert_order_rejected(
        self,
        symbol: str,
        side: str,
        qty: Any,
        reason: str,
        *,
        order_type: str = "stock",
    ) -> bool:
        sym = str(symbol).upper()
        side_word = str(side).lower()
        body = (
            f"The broker turned down our {order_type} {side_word} order for {qty} {sym}. "
            f"Reason given: {redact_secrets_in_text(reason)}."
        )
        return self.alert(
            Severity.CRITICAL,
            f"Order rejected for {sym}",
            body,
            key=f"order_rejected:{sym}:{side_word}:{order_type}",
            category=AlertCategory.ORDER_REJECTED,
            channel_key="risk",
        )

    def alert_missing_stop(
        self,
        symbol: str,
        qty: Any,
        reason: str,
        *,
        auto_repaired: bool = False,
        stop_price: Any = None,
    ) -> bool:
        sym = str(symbol).upper()
        if auto_repaired:
            price_note = f" at ${float(stop_price):,.2f}" if stop_price is not None else ""
            body = (
                f"{sym} ({qty} shares) did not have an active stop-loss order at the broker ({reason}). "
                f"The bot placed a fresh protective stop order{price_note}."
            )
            return self.alert(
                Severity.WARNING,
                f"Replaced missing stop on {sym}",
                body,
                key=f"stop_repaired:{sym}",
                category=AlertCategory.STOP_MISSING,
                channel_key="risk",
            )
        body = (
            f"{sym} ({qty} shares) is currently open WITHOUT a working stop-loss order at the broker. "
            f"Why: {redact_secrets_in_text(reason)}. Please check the account."
        )
        return self.alert(
            Severity.CRITICAL,
            f"Unprotected position: {sym} has no broker stop",
            body,
            key=f"stop_missing:{sym}",
            category=AlertCategory.STOP_MISSING,
            channel_key="risk",
        )

    def alert_stop_filled(
        self,
        symbol: str,
        qty: Any,
        fill_price: float,
        *,
        realized_pnl: float | None = None,
        hold_days: int | None = None,
        order_id: str | None = None,
    ) -> bool:
        sym = str(symbol).upper()
        pnl_sentence = ""
        if realized_pnl is not None:
            word = "gain" if realized_pnl >= 0 else "loss"
            pnl_sentence = f" Realized {word}: ${abs(realized_pnl):,.2f}."
        held_sentence = f" Held for {hold_days} days." if hold_days is not None else ""
        body = (
            f"The broker's protective stop-loss sold {qty} shares of {sym} at ${fill_price:,.2f}."
            f"{pnl_sentence}{held_sentence}"
        )
        key = f"stop_filled:{sym}:{order_id}" if order_id else None
        return self.alert(
            Severity.WARNING,
            f"Stop-loss triggered on {sym}",
            body,
            key=key,
            category=AlertCategory.STOP_FILLED,
            channel_key="trades",
            mirror_telegram=True,
        )

    def alert_reconcile_diffs(self, diffs: Iterable[Any]) -> int:
        """Raise plain-English alerts for every non-info reconcile difference."""
        diff_list = list(diffs or [])
        queued = 0
        for d in diff_list:
            sev = coerce_severity(getattr(d, "severity", "warning"))
            if sev == Severity.INFO:
                continue
            kind = str(getattr(getattr(d, "kind", ""), "value", getattr(d, "kind", "diff")))
            sym = getattr(d, "symbol", None) or "ACCOUNT"
            expl = getattr(d, "explanation", "") or "No matching broker activity found."
            action = getattr(d, "action", "")
            ledger_val = getattr(d, "ledger", None)
            broker_val = getattr(d, "broker", None)

            if kind == "short_stock":
                title = f"Unexpected short position in {sym}"
                body = (
                    f"The broker shows a short stock position in {sym} ({broker_val} shares), "
                    f"which is against our long-only stock rule. {expl}"
                )
                sev = Severity.CRITICAL
            else:
                title = f"Account sync difference on {sym} ({kind.replace('_', ' ')})"
                body = (
                    f"Our local record had `{ledger_val}` while the broker reports `{broker_val}`. "
                    f"{expl}" + (f" Action taken: {action}." if action else "")
                )

            if self.alert(
                sev,
                title,
                body,
                key=f"reconcile:{kind}:{sym}",
                category=AlertCategory.RECONCILE_DIFF,
                channel_key="risk",
            ):
                queued += 1
        return queued

    def alert_input_health(
        self,
        provider: str,
        capability: str,
        state: str,
        *,
        reason: str = "",
        required_for_trading: bool = False,
    ) -> bool:
        st = str(state).strip().lower()
        key = f"input:{provider}:{capability}"
        if st == "healthy":
            return self.alert(
                Severity.INFO,
                f"{provider} ({capability}) is working again",
                f"Connection to {provider} {capability} recovered.",
                key=key,
                category=AlertCategory.INPUT_HEALTH,
                channel_key="risk",
                resolved=True,
            )
        if st == "disabled":
            return False

        sev = Severity.CRITICAL if (required_for_trading and st == "unhealthy") else Severity.WARNING
        impact = (
            "New entries that depend on this data are paused until it recovers; existing stops and exits still work."
            if required_for_trading
            else "The bot is continuing with backup sources and noting the gap in your daily brief."
        )
        why = f" Last error: {redact_secrets_in_text(reason)}." if reason else ""
        return self.alert(
            sev,
            f"Data source issue: {provider} ({capability}) is {st}",
            f"{provider} {capability} is currently {st}.{why} {impact}",
            key=key,
            category=AlertCategory.INPUT_HEALTH,
            channel_key="risk",
        )

    def handle_health_transition(self, status: Any, old_state: Any = None) -> bool:
        """Listener compatible with ``ProviderHealthRegistry.register_listener``."""
        provider = str(getattr(status, "provider", "unknown"))
        capability = str(getattr(status, "capability", "api"))
        raw_state = getattr(status, "state", "degraded")
        state_val = str(getattr(raw_state, "value", raw_state))
        reason = str(getattr(status, "last_error_summary", "") or "")
        required = bool(getattr(status, "required_for_trading", False))
        return self.alert_input_health(
            provider,
            capability,
            state_val,
            reason=reason,
            required_for_trading=required,
        )

    def alert_skipped_cycle(self, job_name: str, reason: str, *, session_date: str | None = None) -> bool:
        day = session_date or self._clock().strftime("%Y-%m-%d")
        clean_job = job_name.replace("_", " ")
        return self.alert(
            Severity.WARNING,
            f"Skipped scheduled step: {clean_job}",
            f"The bot skipped `{clean_job}` because: {redact_secrets_in_text(reason)}.",
            key=f"skipped:{job_name}:{day}",
            category=AlertCategory.SKIPPED_CYCLE,
            channel_key="risk",
        )

    def alert_unhandled_exception(
        self,
        where: str,
        exc: BaseException | str,
        *,
        critical: bool = True,
    ) -> bool:
        exc_type = type(exc).__name__ if isinstance(exc, BaseException) else "Error"
        msg = redact_secrets_in_text(str(exc)) or exc_type
        sev = Severity.CRITICAL if critical else Severity.WARNING
        return self.alert(
            sev,
            f"Unexpected error in {where}",
            f"`{exc_type}` occurred during {where}: {msg[:500]}",
            key=f"exc:{where}:{exc_type}",
            category=AlertCategory.UNHANDLED_EXCEPTION,
            channel_key="risk",
        )

    def alert_deploy_event(
        self,
        event: str,
        commit_sha: str,
        *,
        reason: str = "",
        rollback: bool = False,
    ) -> bool:
        """Alert on in-place deploy or automatic rollback (DECISIONS B5)."""
        short_sha = str(commit_sha)[:12]
        if rollback:
            body = (
                f"An update failed its startup health check and was rolled back automatically to `{short_sha}`. "
                f"{redact_secrets_in_text(reason)}".strip()
            )
            return self.alert(
                Severity.CRITICAL,
                f"Update rolled back to {short_sha}",
                body,
                key=f"deploy:rollback:{short_sha}",
                category=AlertCategory.LIFECYCLE,
                channel_key="risk",
            )
        body = f"Bot updated cleanly to `{short_sha}` ({event}). {redact_secrets_in_text(reason)}".strip()
        return self.alert(
            Severity.INFO,
            f"Updated to {short_sha}",
            body,
            key=f"deploy:ok:{short_sha}",
            category=AlertCategory.LIFECYCLE,
            channel_key="announcements",
        )

    def send_daily_heartbeat_line(
        self,
        *,
        date_str: str | None = None,
        mode: str | None = None,
        note: str = "",
    ) -> bool:
        """Queue one quiet daily proof-of-life line to Discord (#system-logs) by default, even on weekends."""
        now = self._clock()
        day = date_str or now.strftime("%Y-%m-%d")
        run_mode = mode or str(getattr(self.config, "MODE", "PAPER"))
        extra = f" • {note}" if note else ""
        embed = {
            "title": f"\U0001f49a Daily Heartbeat — {day}",
            "description": f"Stock Agent is online and running normally in `{run_mode}` mode ({now.strftime('%H:%M UTC')}{extra}).",
            "color": 0x10B981,
        }
        return self.alert(
            Severity.INFO,
            f"Daily Heartbeat — {day}",
            f"Online in {run_mode} mode.",
            key=f"heartbeat:daily:{day}",
            dedupe_window_sec=72000,
            category=AlertCategory.LIFECYCLE,
            channel_key="system_logs",
            embeds=[embed],
            mirror_telegram=False,
        )

    # ── Intraday news reactions, options structures & first-run normalise ──

    def send_news_reaction(
        self,
        *,
        symbol: str,
        headline: str,
        source: str,
        published_at: datetime | str | None,
        decision: str,
        reason: str,
        conviction: int | None = None,
        action_summary: str | None = None,
        story_id: str | None = None,
    ) -> bool:
        """Plain-English message whenever the bot evaluates a material live news item (DECISIONS A5)."""
        sym = str(symbol).upper()
        dec = str(decision).strip().upper()
        acted = dec not in ("SKIP", "SKIPPED", "DECLINED", "HOLD", "NONE", "NO_ACTION", "REFUSED")

        if isinstance(published_at, datetime):
            time_str = published_at.strftime("%Y-%m-%d %H:%M UTC")
        elif published_at:
            time_str = str(published_at)
        else:
            time_str = "just now"

        outcome_line = action_summary or (
            f"Acted ({dec})" if acted else f"Declined to trade ({dec.lower().replace('_', ' ')})"
        )
        conv_text = f"{conviction}/10" if conviction is not None else "—"
        color = 0x22C55E if acted else 0x6B7280
        title_verb = f"News Trade — {sym}: {dec}" if acted else f"News Checked — {sym}: Passed"

        embed = {
            "title": f"\U0001f4f0 {title_verb}",
            "description": (
                f"**Story:** {headline}\n"
                f"**What we decided:** {outcome_line}\n"
                f"**Why:** {reason}"
            )[:4096],
            "color": color,
            "fields": [
                {"name": "Source", "value": source or "Market News", "inline": True},
                {"name": "Published", "value": time_str, "inline": True},
                {"name": "Conviction", "value": conv_text, "inline": True},
            ],
        }
        ch = "trades" if acted else "market_monitor"
        dedup = f"news_rx:{story_id}" if story_id else None
        return self.alert(
            Severity.INFO,
            title_verb,
            reason,
            key=dedup,
            category=AlertCategory.NEWS_REACTION,
            channel_key=ch,
            embeds=[embed],
            mirror_telegram=acted,
        )

    def send_option_structure_opened(
        self,
        *,
        underlying: str,
        strategy: str,
        legs_summary: str,
        debit_total: float,
        max_loss: float,
        max_profit: float | None = None,
        expiry: str | None = None,
        conviction: int | None = None,
        reason: str = "",
        origin: str = "SCHEDULED",
        structure_id: str | None = None,
    ) -> bool:
        """Plain-English message when an options trade opens, highlighting exact max loss."""
        sym = str(underlying).upper()
        strat_label = strategy.replace("_", " ").title()
        origin_note = "live news reaction" if str(origin).upper() == "NEWS" else "scheduled research"
        upside_str = f"${max_profit:,.0f}" if max_profit is not None else "Uncapped"

        desc = (
            f"Opened a **{strat_label}** on **{sym}** (from {origin_note}).\n\n"
            f"**Why:** {reason or 'High-conviction directional setup.'}\n"
            f"**Worst-case loss if completely wrong:** **${max_loss:,.0f}** "
            "(risk is strictly capped to the cash paid)."
        )
        fields = [
            {"name": "Contracts", "value": legs_summary, "inline": False},
            {"name": "Cash Paid (Debit)", "value": f"${debit_total:,.0f}", "inline": True},
            {"name": "Max Possible Loss", "value": f"${max_loss:,.0f}", "inline": True},
            {"name": "Target Max Profit", "value": upside_str, "inline": True},
        ]
        if expiry:
            fields.append({"name": "Expiration", "value": str(expiry), "inline": True})
        if conviction is not None:
            fields.append({"name": "Conviction", "value": f"{conviction}/10", "inline": True})

        embed = {
            "title": f"\U0001f3af Options Opened — {sym} ({strat_label})",
            "description": desc,
            "color": 0x3B82F6,
            "fields": fields,
        }
        return self.alert(
            Severity.INFO,
            f"Options opened on {sym}",
            desc,
            key=f"opt_open:{structure_id}" if structure_id else None,
            category=AlertCategory.OPTION_STRUCTURE,
            channel_key="trades",
            embeds=[embed],
            mirror_telegram=True,
        )

    def send_option_structure_closed(
        self,
        *,
        underlying: str,
        strategy: str,
        legs_summary: str,
        realized_pnl: float,
        exit_reason: str,
        realized_pnl_pct: float | None = None,
        held_days: int | None = None,
        structure_id: str | None = None,
    ) -> bool:
        """Plain-English message when an options structure closes, with clear exit reason."""
        sym = str(underlying).upper()
        strat_label = strategy.replace("_", " ").title()
        won = realized_pnl >= 0
        word = "profit" if won else "loss"
        pct_note = f" ({realized_pnl_pct:+.1%})" if realized_pnl_pct is not None else ""

        desc = (
            f"Closed our **{strat_label}** on **{sym}** for a **${abs(realized_pnl):,.2f} {word}**{pct_note}.\n\n"
            f"**Why we closed it:** {exit_reason}"
        )
        fields = [
            {"name": "Contracts", "value": legs_summary, "inline": False},
            {"name": "Result", "value": f"{'✅' if won else '❌'} ${realized_pnl:+,.2f}{pct_note}", "inline": True},
            {"name": "Exit Rule", "value": exit_reason, "inline": True},
        ]
        if held_days is not None:
            fields.append({"name": "Days Held", "value": str(held_days), "inline": True})

        embed = {
            "title": f"{'📈' if won else '📉'} Options Closed — {sym} ({strat_label})",
            "description": desc,
            "color": 0x22C55E if won else 0xEF4444,
            "fields": fields,
        }
        return self.alert(
            Severity.INFO,
            f"Options closed on {sym}",
            desc,
            key=f"opt_close:{structure_id}" if structure_id else None,
            category=AlertCategory.OPTION_STRUCTURE,
            channel_key="trades",
            embeds=[embed],
            mirror_telegram=True,
        )

    def send_normalise_plan(self, steps: Sequence[str], *, session_note: str = "") -> bool:
        """Post the one-time account clean-up plan before executing it (DECISIONS B3)."""
        bullet_list = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) or "No clean-up actions needed."
        when_line = session_note or "These steps will run automatically five minutes after the next regular market open."
        desc = (
            "Before opening any new trades on the upgraded bot, here is the one-time clean-up plan "
            "to bring the broker account in line with all safety rules:\n\n"
            f"{bullet_list}\n\n"
            f"**When:** {when_line}"
        )
        embed = {
            "title": "\U0001f9f9 Account Clean-Up Plan (First Run)",
            "description": desc[:4096],
            "color": 0xF59E0B,
        }
        return self.alert(
            Severity.INFO,
            "Account clean-up plan",
            desc,
            key="normalise:plan:v1",
            category=AlertCategory.NORMALISE,
            channel_key="announcements",
            embeds=[embed],
            mirror_telegram=True,
        )

    def send_normalise_result(self, results: Sequence[str], *, all_clean: bool = True) -> bool:
        """Post the outcome of the one-time account clean-up (DECISIONS B3)."""
        bullet_list = "\n".join(f"• {r}" for r in results) or "• Account verified clean."
        status_line = (
            "All clean-up steps finished and the account matches our books. Normal trading is now unlocked."
            if all_clean
            else "Clean-up stopped partway through so nothing unsafe happens. New entries remain on hold — see details below."
        )
        embed = {
            "title": (
                "\u2705 Account Clean-Up Complete"
                if all_clean
                else "\U0001f6a8 Account Clean-Up Needs Attention"
            ),
            "description": f"{status_line}\n\n{bullet_list}"[:4096],
            "color": 0x10B981 if all_clean else 0xEF4444,
        }
        sev = Severity.INFO if all_clean else Severity.CRITICAL
        return self.alert(
            sev,
            "Account clean-up result",
            status_line,
            key=None if not all_clean else "normalise:result:v1",
            category=AlertCategory.NORMALISE,
            channel_key="announcements" if all_clean else "risk",
            embeds=[embed],
            mirror_telegram=True,
        )


_DEFAULT_SERVICE: AlertService | None = None


def set_default_alert_service(service: AlertService | None) -> None:
    global _DEFAULT_SERVICE
    _DEFAULT_SERVICE = service


def get_default_alert_service() -> AlertService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = AlertService()
    return _DEFAULT_SERVICE


def alert(
    severity: Severity | str,
    title: str,
    body: str,
    key: str | None = None,
    **kwargs: Any,
) -> bool:
    """Module-level alert entry point matching ``alert(severity, title, body, key=None)``."""
    return get_default_alert_service().alert(severity, title, body, key=key, **kwargs)


__all__ = [
    "AlertCategory",
    "AlertEvent",
    "AlertService",
    "DedupState",
    "OutboxItem",
    "Severity",
    "alert",
    "coerce_severity",
    "get_default_alert_service",
    "redact_secrets_in_text",
    "set_default_alert_service",
    "datetime",
]
