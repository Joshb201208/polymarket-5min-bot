"""Broker-backed regular-session schedules and last-mile order authorization."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LOGGER = logging.getLogger(__name__)


class ClockReading(Protocol):
    @property
    def timestamp(self) -> datetime: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def next_open(self) -> datetime: ...
    @property
    def next_close(self) -> datetime: ...


class CalendarDay(Protocol):
    @property
    def date(self) -> date: ...
    @property
    def open(self) -> str | time: ...
    @property
    def close(self) -> str | time: ...


class CalendarClient(Protocol):
    def get_clock(self) -> Awaitable[ClockReading]: ...
    def get_calendar(self, start: date, end: date) -> Awaitable[Sequence[CalendarDay]]: ...


class MarketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class MarketSource(StrEnum):
    LIVE = "live"
    CACHE = "cache"
    NONE = "none"


@dataclass(frozen=True)
class Session:
    date: date
    opens_at: datetime
    closes_at: datetime


@dataclass(frozen=True)
class MarketState:
    status: MarketStatus
    source: MarketSource
    session: Session | None = None
    observed_at: datetime | None = None
    next_open: datetime | None = None
    reason: str = ""

    @property
    def authorizes_orders(self) -> bool:
        return self.status is MarketStatus.OPEN and self.source is MarketSource.LIVE


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str
    next_open: datetime | None = None


class MarketCalendar:
    def __init__(
        self,
        broker: CalendarClient,
        cache_path: Path,
        *,
        namespace: str = "paper",
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        timeout: float = 4.0,
    ) -> None:
        self.broker = broker
        self.cache_path = cache_path
        self.namespace = namespace
        self.now = now
        self.timeout = timeout
        self.state = MarketState(MarketStatus.UNKNOWN, MarketSource.NONE)
        self._sessions: dict[date, Session] = {}
        self._coverage: tuple[date, date] | None = None
        self._calendar_at: datetime | None = None
        self._calendar_attempt_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._load_cache()

    @staticmethod
    def _session(day: date, opens: time, closes: time) -> Session:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError("calendar date has the wrong type")
        if opens.tzinfo or closes.tzinfo or not opens < closes:
            raise ValueError("invalid calendar hours")
        return Session(day, datetime.combine(day, opens, ET), datetime.combine(day, closes, ET))

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text())
            if raw["namespace"] != self.namespace or raw["version"] != 1:
                return
            stamp = datetime.fromisoformat(raw["fetched_at"])
            start, end = date.fromisoformat(raw["start"]), date.fromisoformat(raw["end"])
            if stamp.tzinfo is None or not timedelta() <= self.now() - stamp <= timedelta(days=1):
                return
            rows = {}
            for day, opens, closes in raw["days"]:
                session = self._session(
                    date.fromisoformat(day), time.fromisoformat(opens), time.fromisoformat(closes)
                )
                if not start <= session.date <= end or session.date in rows:
                    raise ValueError("bad cache date")
                rows[session.date] = session
            if start > end or not rows:
                return
            self._sessions, self._coverage, self._calendar_at = rows, (start, end), stamp
        except (OSError, ValueError, TypeError, KeyError):
            return

    def _save_cache(self) -> None:
        assert self._coverage is not None and self._calendar_at is not None
        start, end = self._coverage
        raw = {
            "version": 1,
            "namespace": self.namespace,
            "fetched_at": self._calendar_at.isoformat(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": [
                (s.date.isoformat(), s.opens_at.strftime("%H:%M"), s.closes_at.strftime("%H:%M"))
                for s in sorted(self._sessions.values(), key=lambda s: s.date)
            ],
        }
        temporary = self.cache_path.with_suffix(".tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(raw))
            temporary.replace(self.cache_path)
        except OSError:
            LOGGER.warning("Could not write market calendar cache")

    def _covered(self, day: date) -> bool:
        return (
            self._coverage is not None
            and self._coverage[0] <= day <= self._coverage[1]
            and self._calendar_at is not None
            and timedelta() <= self.now() - self._calendar_at <= timedelta(days=1)
        )

    def session_on(self, day: date) -> Session | None:
        return self._sessions.get(day) if self._covered(day) else None

    def next_session(self, day: date) -> Session | None:
        for candidate in sorted(self._sessions):
            if candidate > day and self._covered(candidate):
                return self._sessions[candidate]
        return None

    def previous_session(self, day: date) -> Session | None:
        for candidate in sorted(self._sessions, reverse=True):
            if candidate < day and self._covered(candidate):
                return self._sessions[candidate]
        return None

    def trading_days_between(self, start: date, end: date) -> int | None:
        """Count sessions after ``start`` through ``end`` in the covered interval."""
        if end < start or not self._covered(start) or not self._covered(end):
            return None
        return sum(start < day <= end for day in self._sessions)

    def is_open(self, now: datetime) -> bool:
        """An inexpensive scheduling check; broker submissions must call market_gate."""
        live = self.state
        if not live.authorizes_orders or live.session is None or live.observed_at is None:
            return False
        return (
            timedelta(seconds=-30) <= now - live.observed_at <= timedelta(seconds=60)
            and live.session.opens_at <= now < live.session.closes_at
        )

    async def _refresh_calendar(self, now: datetime, today: date) -> None:
        start, end = today - timedelta(days=7), today + timedelta(days=120)
        if (self._covered(today) and self._covered(end) and self._calendar_at
                and self._calendar_at.astimezone(ET).date() == today):
            return
        if self._calendar_attempt_at and timedelta() <= now - self._calendar_attempt_at < timedelta(minutes=5):
            return
        self._calendar_attempt_at = now
        try:
            async with asyncio.timeout(self.timeout):
                days = await self.broker.get_calendar(start, end)
            sessions: dict[date, Session] = {}
            for day in days:
                opening = time.fromisoformat(day.open) if isinstance(day.open, str) else day.open
                closing = time.fromisoformat(day.close) if isinstance(day.close, str) else day.close
                session = self._session(day.date, opening, closing)
                if not start <= session.date <= end or session.date in sessions:
                    raise ValueError("bad calendar date")
                sessions[session.date] = session
            if not sessions:
                raise ValueError("empty calendar range")
        except Exception:
            # Provider classifications are consumed by its caller; never log raw replies here.
            LOGGER.warning("Could not refresh broker market calendar")
            return
        self._sessions, self._coverage, self._calendar_at = sessions, (start, end), self.now()
        self._save_cache()

    async def refresh(self) -> MarketState:
        """Always acquire a current broker clock. A calendar cache never authorizes alone."""
        async with self._lock:
            now = self.now().astimezone(ET)
            await self._refresh_calendar(now, now.date())
            try:
                async with asyncio.timeout(self.timeout):
                    clock = await self.broker.get_clock()
                now = self.now().astimezone(ET)
                session = self.session_on(now.date())
                subsequent = self.next_session(now.date())
                scheduled_open = bool(session and session.opens_at <= now < session.closes_at)
                scheduled_next_open = session.opens_at if session and now < session.opens_at else (
                    subsequent.opens_at if subsequent else None
                )
                if (
                    any(x.tzinfo is None or x.utcoffset() is None for x in (
                        clock.timestamp, clock.next_open, clock.next_close,
                    ))
                    or abs(now - clock.timestamp) > timedelta(seconds=30)
                    or type(clock.is_open) is not bool
                    or clock.next_open <= clock.timestamp
                    or clock.next_close <= clock.timestamp
                    or not self._covered(now.date())
                    or clock.is_open != scheduled_open
                    or clock.next_open != scheduled_next_open
                    or (clock.is_open and session and clock.next_close != session.closes_at)
                ):
                    raise ValueError("broker clock and calendar disagree")
                self.state = MarketState(
                    MarketStatus.OPEN if clock.is_open else MarketStatus.CLOSED,
                    MarketSource.LIVE,
                    session,
                    clock.timestamp,
                    clock.next_open,
                )
            except Exception:
                now = self.now().astimezone(ET)
                session = self.session_on(now.date())
                next_session = self.next_session(now.date())
                scheduled_open = bool(session and session.opens_at <= now < session.closes_at)
                self.state = MarketState(
                    (MarketStatus.OPEN if scheduled_open else MarketStatus.CLOSED)
                    if self._covered(now.date()) else MarketStatus.UNKNOWN,
                    MarketSource.CACHE if self._covered(now.date()) else MarketSource.NONE,
                    session,
                    next_open=session.opens_at if session and now < session.opens_at else (
                        next_session.opens_at if next_session else None
                    ),
                    reason="live broker session unconfirmed",
                )
            return self.state

    async def market_gate(self, purpose: str, *, rule: str | None = None) -> GateDecision:
        if purpose == "protection":
            return GateDecision(True, "broker protective maintenance")
        if purpose not in {"entry", "exit"}:
            return GateDecision(False, "unknown order purpose")
        market = await self.refresh()
        if not market.authorizes_orders or market.session is None:
            return GateDecision(False, market.reason or "regular session is closed", market.next_open)
        now = self.now().astimezone(ET)
        session = market.session
        if not session.opens_at <= now < session.closes_at:
            return GateDecision(False, "regular session boundary crossed", market.next_open)
        if now < session.opens_at + timedelta(minutes=5) and (
            purpose == "entry" or rule not in {"hard_stop", "cover_short", "margin_buffer"}
        ):
            return GateDecision(False, "opening buffer", session.opens_at + timedelta(minutes=5))
        if purpose == "entry" and now >= session.closes_at - timedelta(minutes=10):
            return GateDecision(False, "entry closing buffer", market.next_open)
        return GateDecision(True, "live regular session", market.next_open)
