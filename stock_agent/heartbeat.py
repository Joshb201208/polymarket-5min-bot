"""Optional outbound dead-man's-switch pinger (for example Healthchecks.io).

Binding rule (DECISIONS B6): when ``HEARTBEAT_PING_URL`` is unset/empty, every
method is a silent no-op and returns ``False``. Nothing requires this setting.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Callable

import httpx

from stock_agent.config import Config
from stock_agent.http_safety import configure_safe_http_logging, scrub_secrets

logger = logging.getLogger(__name__)


def _default_now() -> datetime:
    sched = sys.modules.get("stock_agent.scheduler")
    if sched is not None and hasattr(sched, "_now_utc"):
        return sched._now_utc()
    return datetime.now(timezone.utc)


class HeartbeatClient:
    """Outbound HTTP heartbeat client. Safe 24/7 including weekends."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        ping_url: str | None = None,
        interval_sec: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or Config()
        self._explicit_url = ping_url
        self._explicit_interval = interval_sec
        self._clock = clock or _default_now
        self._last_ping_at: datetime | None = None
        self._last_discord_heartbeat_day: str | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def ping_url(self) -> str:
        if self._explicit_url is not None:
            return self._explicit_url.strip()
        val = (
            getattr(self.config, "HEARTBEAT_PING_URL", "")
            or getattr(self.config, "HEARTBEAT_URL", "")
            or os.environ.get("HEARTBEAT_PING_URL", "")
            or os.environ.get("STOCK_HEARTBEAT_URL", "")
            or os.environ.get("HEARTBEAT_URL", "")
        )
        return str(val).strip()

    @property
    def interval_sec(self) -> int:
        if self._explicit_interval is not None:
            return max(30, int(self._explicit_interval))
        raw = getattr(self.config, "HEARTBEAT_INTERVAL_SEC", None) or os.environ.get("HEARTBEAT_INTERVAL_SEC", "300")
        try:
            return max(30, int(raw))
        except (TypeError, ValueError):
            return 300

    @property
    def enabled(self) -> bool:
        return bool(self.ping_url)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            configure_safe_http_logging()
            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def maybe_ping(self, *, ok: bool = True, note: str = "") -> bool:
        """Send a periodic heartbeat if configured and due; never raises."""
        url = self.ping_url
        if not url:
            return False

        if not ok:
            return await self.send_fail(note or "unhealthy_loop")

        now = self._clock()
        if self._last_ping_at is not None:
            elapsed = (now - self._last_ping_at).total_seconds()
            if elapsed < self.interval_sec:
                return False

        mode = str(getattr(self.config, "MODE", "") or os.environ.get("STOCK_AGENT_MODE", "PAPER"))
        body = f"ok mode={mode} ts={now.isoformat()}"
        if note:
            body = f"{body} {note[:200]}"

        try:
            client = await self._get_client()
            resp = await client.post(url, content=body.encode("utf-8"))
            if 200 <= resp.status_code < 300:
                self._last_ping_at = now
                return True
            logger.warning("Heartbeat ping returned HTTP %s", resp.status_code)
            return False
        except Exception as e:
            safe_err = scrub_secrets(str(e)).replace(url, "[REDACTED_HEARTBEAT_URL]")
            logger.warning("Heartbeat ping failed (%s): %s", type(e).__name__, safe_err)
            return False

    async def send_fail(self, reason: str = "") -> bool:
        """Signal immediate failure (<HEARTBEAT_PING_URL>/fail) if configured."""
        url = self.ping_url
        if not url:
            return False
        fail_url = url.rstrip("/") + "/fail"
        now = self._clock()
        body = f"fail ts={now.isoformat()} {reason[:300]}".strip()
        try:
            client = await self._get_client()
            resp = await client.post(fail_url, content=body.encode("utf-8"))
            return 200 <= resp.status_code < 300
        except Exception as e:
            safe_err = (
                scrub_secrets(str(e))
                .replace(fail_url, "[REDACTED_HEARTBEAT_URL]/fail")
                .replace(url, "[REDACTED_HEARTBEAT_URL]")
            )
            logger.warning("Heartbeat fail-ping failed (%s): %s", type(e).__name__, safe_err)
            return False


    async def tick(self, alerts: object | None = None, *, ok: bool = True, note: str = "") -> bool:
        """Run both the default daily Discord proof-of-life line and optional external HTTP ping."""
        now = self._clock()
        day_str = now.strftime("%Y-%m-%d")
        queued_discord = False
        if ok and alerts is not None and self._last_discord_heartbeat_day != day_str:
            send_fn = getattr(alerts, "send_daily_heartbeat_line", None) or getattr(
                alerts, "send_daily_heartbeat", None
            )
            if callable(send_fn):
                try:
                    if bool(send_fn(date_str=day_str, note=note)):
                        self._last_discord_heartbeat_day = day_str
                        queued_discord = True
                except Exception as e:
                    logger.warning("Daily Discord heartbeat callback failed (%s): %s", type(e).__name__, e)
        ext_ok = await self.maybe_ping(ok=ok, note=note)
        return queued_discord or ext_ok


__all__ = ["HeartbeatClient", "datetime"]
