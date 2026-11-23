"""Drive the bot's scheduler through simulated time, minute by minute.

    from tests.scenarios import ScenarioRunner

    def test_week(world):
        r = ScenarioRunner(world)
        r.at(et(2026, 11, 23, 9, 45), lambda w, r: w.news(["AAA"], "AAA wins contract"))
        r.at(et(2026, 11, 24, 11, 0), lambda w, r: w.price("AAA", 90.0))     # stop fills
        r.at(et(2026, 11, 25, 10, 2), r.restart)                  # restart
        r.run_week(date(2026, 11, 23))
        assert not r.errors

The runner owns no trading logic. It builds the agent with `build(world)`
(default `world.scheduler()`), awaits `startup(agent)` once per build (default:
`agent.startup()` if the agent has one, else nothing; today's `run()` loop is
not used because it never returns), and awaits `step(agent, now_et)` every
step (default `agent._tick(now_et)`). scheduler_deploy owns those methods; if
they rename them, pass `build`/`startup`/`step` instead of editing this file.

Like today's `run()` loop, an exception from a tick is caught and recorded in
`errors`/`timeline` and the run continues, so a scenario can assert on it.
Everything happens on one event loop per `run_*` call; asyncio.sleep is
simulated by the clock (it advances simulated time, it does not wait).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Awaitable, Callable

from tests.fakes.clock import ET

logger = logging.getLogger(__name__)

Event = Callable[[Any, "ScenarioRunner"], Any]


@dataclass
class TickError:
    at: datetime
    error: BaseException
    trace: str


@dataclass(order=True)
class _Scheduled:
    when: datetime
    seq: int
    label: str = field(compare=False)
    fn: Event = field(compare=False)


@dataclass
class TimelineEntry:
    at: datetime
    kind: str  # "event" | "restart" | "error" | "build" | "day"
    detail: str


async def _default_startup(agent) -> None:
    fn = getattr(agent, "startup", None)
    if fn is not None:
        res = fn()
        if inspect.isawaitable(res):
            await res


async def _default_step(agent, now_et: datetime) -> None:
    await agent._tick(now_et)


async def _default_shutdown(agent) -> None:
    fn = getattr(agent, "_cleanup", None)
    if fn is not None:
        res = fn()
        if inspect.isawaitable(res):
            await res


class ScenarioRunner:
    def __init__(
        self,
        world,
        *,
        build: Callable[[Any], Any] | None = None,
        startup: Callable[[Any], Awaitable[None]] | None = _default_startup,
        step: Callable[[Any, datetime], Awaitable[None]] = _default_step,
        shutdown: Callable[[Any], Awaitable[None]] | None = _default_shutdown,
        step_seconds: int = 60,
        max_steps: int = 50_000,
    ):
        self.world = world
        self.clock = world.clock
        self._build = build or (lambda w: w.scheduler())
        self._startup = startup
        self._step = step
        self._shutdown = shutdown
        self.step_seconds = step_seconds
        self.max_steps = max_steps
        self.agent = None
        self.builds = 0
        self.steps = 0
        self.errors: list[TickError] = []
        self.timeline: list[TimelineEntry] = []
        self._events: list[_Scheduled] = []
        self._seq = 0
        self._restart_requested = False
        self._watchers: list[tuple[Callable[[Any, "ScenarioRunner"], bool], Event, str, bool]] = []

    # ── scripting ────────────────────────────────────────────────────
    def at(self, when: datetime, fn: Event, label: str = "") -> "ScenarioRunner":
        """Run `fn(world, runner)` (sync or async) just before the first step at or after `when`."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=ET)
        self._seq += 1
        self._events.append(_Scheduled(when, self._seq, label or getattr(fn, "__name__", "event"), fn))
        self._events.sort()
        return self

    def restart(self, *_args) -> None:
        """Process restart: drop the agent, build a fresh one from the same DATA_DIR and fakes.

        Usable directly or as an event: `runner.at(t, runner.restart)`.
        Broker state (orders in flight, fills) carries over, as it would in life.
        For "restart mid-order", first arm `world.alpaca.fail("/v2/orders",
        "lost_response", method="POST")` so the order reaches the broker but the
        reply is lost, then restart.
        """
        self._restart_requested = True

    def when(self, predicate: Callable[[Any, "ScenarioRunner"], bool], fn: Event, label: str = "",
             *, once: bool = True) -> "ScenarioRunner":
        """After each step, if `predicate(world, runner)` is true, run `fn(world, runner)`."""
        self._watchers.append((predicate, fn, label or getattr(fn, "__name__", "watch"), once))
        return self

    def restart_after_lost_order(self, *, lookup_down: bool = False) -> "ScenarioRunner":
        """Restart the agent right after the next order whose broker reply is lost.

        Arms a one-shot `lost_response` fault on POST /v2/orders (the order is
        accepted by the fake broker, the client sees a timeout), then rebuilds
        the agent at the end of that step: a crash mid-order. With
        `lookup_down=True` the by-client-order-id lookup also fails (5xx) until
        the restart, so the old process cannot learn the outcome and the new
        one must recover it.
        """
        a = self.world.alpaca
        a.fail("/v2/orders", "lost_response", method="POST", times=1)
        lookup = a.fail("/v2/orders:by_client_order_id", "5xx", method="GET", times=None) if lookup_down else None

        def lost(w, r):
            return any(q.fault == "lost_response" for q in w.net.requests("alpaca", "POST", "/v2/orders"))

        def restart(w, r):
            if lookup is not None and lookup in a.faults:
                a.faults.remove(lookup)
            r.restart()

        return self.when(lost, restart, "restart after lost order reply")

    def note(self, kind: str, detail: str) -> None:
        self.timeline.append(TimelineEntry(self.clock.now_et(), kind, detail))

    # ── running ──────────────────────────────────────────────────────
    def run_until(self, end: datetime) -> None:
        asyncio.run(self._run_until(end if end.tzinfo else end.replace(tzinfo=ET)))

    def run_day(self, day: date, start: time = time(7, 0), end: time = time(16, 30)) -> None:
        """Jump to `start` on `day` (overnight is not ticked) and step until `end`."""
        asyncio.run(self._run_day(day, start, end))

    def run_days(self, first: date, n: int, *, weekends: bool = False,
                 start: time = time(7, 0), end: time = time(16, 30)) -> None:
        async def go():
            d, done = first, 0
            while done < n:
                if weekends or d.weekday() < 5:
                    await self._run_day(d, start, end)
                    done += 1
                d += timedelta(days=1)
        asyncio.run(go())

    def run_week(self, monday: date, **kw) -> None:
        self.run_days(monday, 5, **kw)

    async def _run_day(self, day: date, start: time, end: time) -> None:
        begin = datetime.combine(day, start, tzinfo=ET)
        if self.clock.now_et() < begin:
            self.clock.set(begin)
        self.note("day", day.isoformat())
        await self._run_until(datetime.combine(day, end, tzinfo=ET))

    async def _ensure_agent(self) -> None:
        if self._restart_requested and self.agent is not None:
            if self._shutdown is not None:
                try:
                    await self._shutdown(self.agent)
                except Exception as e:  # noqa: BLE001 - a crash on shutdown is part of a restart
                    self.note("error", f"shutdown: {e!r}")
            self.agent = None
            self.note("restart", "agent rebuilt")
        self._restart_requested = False
        if self.agent is None:
            self.agent = self._build(self.world)
            self.builds += 1
            self.note("build", f"build #{self.builds}")
            if self._startup is not None:
                try:
                    await self._startup(self.agent)
                except Exception as e:  # noqa: BLE001
                    self._record_error(e)

    async def _fire_due_events(self) -> None:
        now = self.clock.now_et()
        while self._events and self._events[0].when <= now:
            ev = self._events.pop(0)
            self.note("event", ev.label)
            res = ev.fn(self.world, self)
            if inspect.isawaitable(res):
                await res

    def _record_error(self, e: BaseException) -> None:
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        self.errors.append(TickError(self.clock.now_et(), e, tb))
        self.note("error", repr(e))
        logger.warning("scenario tick error at %s: %r", self.clock.now_et(), e)

    async def _run_until(self, end: datetime) -> None:
        while self.clock.now_et() < end:
            if self.steps >= self.max_steps:
                raise RuntimeError(f"scenario exceeded max_steps={self.max_steps}")
            await self._fire_due_events()
            await self._ensure_agent()
            tick_at = self.clock.now_et()
            try:
                await self._step(self.agent, tick_at)
            except Exception as e:  # noqa: BLE001 - mirrors the production loop
                self._record_error(e)
            self.steps += 1
            for item in list(self._watchers):
                pred, fn, label, once = item
                if pred(self.world, self):
                    self.note("event", label)
                    res = fn(self.world, self)
                    if inspect.isawaitable(res):
                        await res
                    if once:
                        self._watchers.remove(item)
            # Next step boundary after the tick's start; a tick that slept past it
            # (simulated asyncio.sleep) resumes at the next boundary, like run().
            nxt = tick_at + timedelta(seconds=self.step_seconds)
            while nxt <= self.clock.now_et():
                nxt += timedelta(seconds=self.step_seconds)
            self.clock.set(min(nxt, end) if nxt > end else nxt)

    # ── reporting ────────────────────────────────────────────────────
    def summary(self) -> str:
        lines = [f"steps={self.steps} builds={self.builds} errors={len(self.errors)}"]
        lines += [f"{e.at:%a %m-%d %H:%M} {e.kind:7} {e.detail}" for e in self.timeline if e.kind != "build"]
        return "\n".join(lines)
