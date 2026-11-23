import asyncio
import logging
import sqlite3
from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from stock_agent.analyst import Analyst
from stock_agent.broker import AlpacaClient
from stock_agent.config import Config
from stock_agent.data_feed import DataFeed
from stock_agent.executor import Executor
from stock_agent.execution import ExecutionEngine
from stock_agent.models import (
    CompanyData,
    DailySummary,
    Position,
    Signal,
    Thesis,
    Trade,
)
from stock_agent.portfolio import Portfolio
from stock_agent.ledger import JobStatus
from stock_agent.llm_client import LLMClient
from stock_agent.market_calendar import GateDecision, MarketCalendar, MarketSource, MarketStatus
from stock_agent.research_gate import ResearchGate
from stock_agent.research_store import ResearchStore
from stock_agent.risk_manager import DRAWDOWN_STATE_KEY, DrawdownState, RiskControls, RiskManager, update_drawdown
from stock_agent.runtime_status import RuntimeStatus, notify_systemd
from stock_agent.screener import Screener
from stock_agent.state_boot import build_ledger, prepare_ledger
from stock_agent.state_store import StateUnavailable
from stock_agent.discord_alerts import DiscordReporter
from stock_agent.telegram_alerts import TelegramReporter
from stock_agent.macro_monitor import MacroMonitor, REGIME_AGGRESSIVE_DEPLOY, REGIME_DIP_OPPORTUNITY, REGIME_CAUTIOUS
from stock_agent.options_data import OptionsDataFeed
from stock_agent.options_engine import OptionsEngine
from stock_agent.options_executor import OptionsExecutor
from stock_agent.options_portfolio import OptionsPortfolio
from stock_agent.options_models import OptionSide, OptionOrderType
from stock_agent.tipranks_client import (
    build_tipranks_lookup,
    adjust_conviction_with_tipranks,
    format_tipranks_context,
    format_tipranks_telegram_clause,
)
from stock_agent.news_feed import build_news_lookup, format_news_context
from stock_agent.sentiment import (
    build_sentiment_lookup,
    format_sentiment_context,
    adjust_conviction_with_sentiment,
)
from stock_agent.analyst_trends import (
    build_analyst_lookup,
    format_analyst_context,
    adjust_conviction_with_analysts,
)
from stock_agent.research_notes import load_notes, format_research_notes_context

logger = logging.getLogger(__name__)

try:
    ET = ZoneInfo("US/Eastern")
except KeyError:
    ET = ZoneInfo("America/New_York")

try:
    SGT = ZoneInfo("Asia/Singapore")
except KeyError:
    SGT = ZoneInfo("Singapore")

TICK_SECONDS = 30
NEWS_SECONDS = 180
MAX_JOB_ATTEMPTS = 3
EXECUTION_DRAIN_SECONDS = 70
SHUTDOWN_DRAIN_SECONDS = 75


def _now_et() -> datetime:
    return datetime.now(ET)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class StockAgentScheduler:
    def __init__(self, config: Config | None = None, *, services: SimpleNamespace | None = None):
        self.config = config if config is not None else Config()
        def service(name, factory):
            supplied = getattr(services, name, None)
            return supplied if supplied is not None else factory()

        self.broker = service("broker", lambda: AlpacaClient(config=self.config))
        self.alerts = service("alerts", lambda: None)
        self.ledger = service("ledger", lambda: build_ledger(
            self.config.DATA_DIR, broker=self.broker, alert=self._queue_ledger_alert, clock=_now_utc,
        ))
        self.store = self.ledger.store
        self.journal = self.ledger.journal
        self.risk = service("risk", lambda: RiskManager(self.config))
        self.calendar = service("calendar", lambda: MarketCalendar(
            self.broker, self.config.DATA_DIR / "market-calendar.json", namespace=self.config.MODE,
            now=_now_utc,
        ))
        self.health = service("health", lambda: None)
        self.prices = service("prices", lambda: None)
        self.news = service("news", lambda: None)
        self.research_store = service("research_store", lambda: ResearchStore(self.store, self.config))
        self.llm = service("llm", lambda: LLMClient(
            self.config, spend=self.research_store, on_failure=self._on_research_failure, now=_now_utc,
        ))
        self.research_gate = service("research_gate", lambda: ResearchGate(self.ledger, self.health, self.config))
        self.research = service("research", lambda: None)
        self.engine = service("engine", lambda: ExecutionEngine(
            self.broker, self.ledger, self.risk, self.calendar, self.prices, alert=self._alert,
            market_gate=self.market_gate, config=self.config, now=_now_utc,
        ))
        self.options = service("options", lambda: None)
        self.runtime = service("runtime", lambda: RuntimeStatus(self.config.DATA_DIR / "runtime-health.json"))
        self.pause_file: Path = service("pause_file", lambda: self.config.DATA_DIR / "pause-trading")
        self._stop_event = asyncio.Event()
        self._normalised = False
        self._engine_resumed = False
        self._state_ready = False
        self._reconciled = False
        self._needs_rebuild = False
        self._last_news_at: datetime | None = None
        self._last_reconcile_at: datetime | None = None
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._order_task: asyncio.Task | None = None
        self._options_task: asyncio.Task | None = None
        self._last_issue: dict[str, datetime] = {}
        self._pending_ledger_alerts: set[asyncio.Task] = set()
        # Legacy read interfaces temporarily remain for report/research callbacks.
        self.data_feed = DataFeed(self.config)
        self.analyst = Analyst(self.llm)
        self.screener = Screener(self.config)
        self.portfolio = Portfolio(self.config)
        self.risk_manager = self.risk
        self.executor = Executor(self.config)
        self.telegram = TelegramReporter(self.config)
        self.discord = DiscordReporter(self.config)
        self.macro = MacroMonitor(self.config)
        self.options_data = OptionsDataFeed(self.config)
        self.options_engine = OptionsEngine(self.config, self.macro, self.options_data)
        self.options_executor = OptionsExecutor(self.config)
        self.options_portfolio = OptionsPortfolio(self.config, self.options_data)
        self._shutdown = False
        self._last_monitor_time: datetime | None = None
        self._daily_summary_sent_today: str | None = None
        self._daily_prescan_done_today: str | None = None
        self._midweek_analysis_done_this_week: str | None = None
        self._earnings_check_done_today: str | None = None
        self._pending_signals: list[Signal] = []
        self._macro_check_done_today: str | None = None
        self._morning_brief_done_today: str | None = None
        self._options_scan_done_today: str | None = None
        # Event-triggered scanning state
        self._last_event_spx: float = 0.0
        self._last_event_vix: float = 0.0
        self._last_event_regime: str = ""
        self._event_scan_cooldown: datetime | None = None  # Don’t fire twice in 30 min

    def shutdown(self):
        if not self._shutdown:
            logger.info("Shutdown requested")
            self._shutdown = True
            self._stop_event.set()

    async def _alert(self, severity: str, title: str, body: str, *, key: str | None = None):
        now = _now_utc()
        if key and key in self._last_issue and now - self._last_issue[key] < timedelta(minutes=30):
            return
        if key:
            self._last_issue[key] = now
        if self.alerts is not None:
            try:
                async with asyncio.timeout(2):
                    await self.alerts.alert(severity, title, body, key=key)
            except Exception:
                logger.warning("Could not queue alert: %s", title)
        else:
            logger.log(logging.WARNING if severity != "info" else logging.INFO, "%s: %s", title, body)

    def _queue_ledger_alert(self, severity: str, title: str, body: str, *, key: str | None = None) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Ledger alert outside scheduler loop: %s", title)
            return
        task = loop.create_task(self._alert(severity, title, body, key=key))
        self._pending_ledger_alerts.add(task)
        task.add_done_callback(self._pending_ledger_alerts.discard)

    def _entries_enabled(self) -> bool:
        if not self._normalised or not self._reconciled or self._shutdown or self._manually_paused():
            return False
        try:
            return not self._risk_controls().drawdown.paused
        except (ValueError, StateUnavailable, sqlite3.Error):
            return False

    def _risk_controls(self) -> RiskControls:
        return RiskControls(pause_new_buys=self._manually_paused(),
                            drawdown=DrawdownState.from_record(self.ledger.risk_state_get(DRAWDOWN_STATE_KEY)))

    def _manually_paused(self) -> bool:
        if self.pause_file.exists() or getattr(self.config, "STOCK_TRADING_PAUSED", False):
            return True
        configured = getattr(self.config, "is_trading_paused", None)
        return bool(configured is not None and configured(self.config.DATA_DIR))

    async def _on_research_failure(self, error) -> None:
        await self._alert("warning", "Research provider unavailable", f"{error.provider}: {error.kind}",
                          key=f"research-provider:{error.provider}:{error.kind}")

    async def market_gate(self, purpose: str, *, rule: str | None = None) -> GateDecision:
        if purpose != "protection" and self._shutdown:
            return GateDecision(False, "shutdown in progress")
        if purpose == "entry" and not self._entries_enabled():
            return GateDecision(False, "entry safety conditions are not satisfied")
        decision = await self.calendar.market_gate(purpose, rule=rule)
        # A signal or pause can arrive while awaiting the fresh market clock.
        if purpose == "entry" and not self._entries_enabled():
            return GateDecision(False, "entry safety conditions are not satisfied")
        if purpose != "protection" and self._shutdown:
            return GateDecision(False, "shutdown in progress")
        return decision

    async def _reconcile_scheduler(self, reason: str) -> bool:
        try:
            async with asyncio.timeout(30):
                await self.ledger.refresh(reason, rebuild=self._needs_rebuild or bool(self.ledger.trading_block()))
            self._needs_rebuild = False
            self._reconciled = not self.ledger.trading_block()
            self._last_reconcile_at = _now_utc()
        except Exception as exc:
            self._reconciled = False
            await self._alert("critical", "Broker reconciliation unavailable",
                              f"{reason}: {type(exc).__name__}. New entries are blocked.",
                              key="reconcile-unavailable")
        return self._reconciled

    def _publish_runtime(self):
        self.runtime.publish(self.calendar.state, ready=self._state_ready, reconciled=self._reconciled,
                             entries_enabled=self._entries_enabled())

    async def startup(self) -> None:
        """Prepare the single-writer ledger and resume broker lifecycle before any orders."""
        if self._state_ready or self._shutdown:
            return
        mode = self.config.MODE.upper()
        logger.info("Stock Agent starting in %s mode", mode)
        expected = {
            "PAPER": "https://paper-api.alpaca.markets",
            "LIVE": "https://api.alpaca.markets",
        }.get(mode)
        if expected is None or self.config.ALPACA_BASE_URL.rstrip("/") != expected:
            raise ValueError("trading mode does not match broker host")
        boot = prepare_ledger(self.ledger, self.config.DATA_DIR)
        self._needs_rebuild = boot.needs_rebuild
        if not boot.health.ok:
            await self._alert("critical", "State needs broker rebuild",
                              "The previous state was quarantined; new entries stay blocked until recovery.")
        self.ledger.mark_submitting_ambiguous()
        await self.calendar.refresh()
        if not self._shutdown:
            await self._reconcile_scheduler("startup")
        if self._shutdown:
            return
        if self._last_reconcile_at is not None and self.engine is not None:
            try:
                await self._resume_execution()
            except TimeoutError:
                await self._alert("warning", "Order recovery is still pending",
                                  "The scheduler will keep retrying; new entries are blocked.",
                                  key="order-resume")
        if self._shutdown:
            return
        self._state_ready = True
        self._publish_runtime()
        notify_systemd("READY=1\nSTATUS=Scheduler running; entry gates checked separately")
        await self._alert("info", "Stock agent started",
                          f"{mode}; broker reconciled: {self._reconciled}; entries enabled: {self._entries_enabled()}")

    async def run(self):
        """Validate and recover before scheduling money work; exit promptly on SIGTERM."""
        try:
            await self.startup()
            while not self._shutdown:
                try:
                    await self._tick_until_stopped()
                    if self._shutdown:
                        break
                    self._publish_runtime()
                    notify_systemd("WATCHDOG=1")
                except Exception as exc:
                    logger.error("Scheduler cycle failed: %s", type(exc).__name__)
                    await self._alert("warning", "Scheduler cycle failed", type(exc).__name__, key="scheduler-tick")
                if not self._shutdown:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=TICK_SECONDS)
                    except TimeoutError:
                        pass
        finally:
            await self._cleanup()

    async def _tick_until_stopped(self) -> None:
        tick = asyncio.create_task(self._tick(_now_et()))
        stopping = asyncio.create_task(self._stop_event.wait())
        try:
            done, _ = await asyncio.wait({tick, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if tick in done:
                tick.result()
        finally:
            # This coordinator only refreshes reads and launches owned, detached work.
            # The execution lanes are not cancelled here.
            for pending in (tick, stopping):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(tick, stopping, return_exceptions=True)

    async def _tick(self, now_et: datetime):
        """Run protection and maintenance first; research never holds this critical path."""
        if self._shutdown:
            return
        await self.calendar.refresh()
        if self._shutdown:
            return
        if not self._reconciled or self._last_reconcile_at is None or (
            _now_utc() - self._last_reconcile_at >= timedelta(seconds=90)
        ):
            await self._reconcile_scheduler("scheduler tick")
        await self._collect_finished_jobs()
        if self._shutdown:
            return
        now_et = _now_et()
        day_key = now_et.date().isoformat()
        await self._drive_order_lane(now_et)
        if self._is_market_open(now_et):
            if self._shutdown:
                return
            if self.research is not None and self._should_check_events():
                self._last_news_at = now_et
                slot = str(int(now_et.timestamp()) // NEWS_SECONDS)
                self._schedule_job("live_news", f"{day_key}:{slot}", self._run_news_slot, timeout=150)
                self._schedule_job("market_regime", f"{day_key}:{slot}", self._check_for_market_event, timeout=90)
            if self._should_run_options_scan(now_et):
                self._schedule_job("options", day_key, self._run_options_scan, timeout=None, replay=False)
            if self.options is not None:
                await self._drive_options_monitor()

        # Re-read the clock after the protection lane and schedule by actual session.
        now_et = _now_et()
        day_key = now_et.date().isoformat()
        jobs = (
            ("macro", self._should_run_macro_check, self._run_macro_check, 120),
            ("earnings", self._should_run_earnings_check, self._run_earnings_reactive, 900),
            ("prescan", self._should_run_daily_prescan, self._run_daily_prescan, 900),
            ("morning_brief", self._should_send_morning_brief, self._send_morning_brief, 90),
            ("daily_summary", self._should_send_daily_summary, self._send_daily_summary, 120),
            ("drawdown_close", self._should_send_daily_summary, self._close_drawdown, 45),
            ("weekly", self._should_run_weekly, self._run_weekly_analysis, 1500),
            ("midweek", self._should_run_midweek, self._run_midweek_analysis, 1500),
        )
        for name, due, callback, timeout in jobs:
            if name in {"earnings", "prescan", "weekly", "midweek"} and self.research is None:
                continue
            if not self._shutdown and due(now_et):
                key = self._research_week_key(now_et, name) if name in {"weekly", "midweek"} else day_key
                self._schedule_job(name, key, callback, timeout=timeout, replay=name not in {
                    "morning_brief", "daily_summary",
                })

    async def _run_news_slot(self):
        return await self._run_held_news_check(self.ledger.snapshot())

    async def _close_drawdown(self):
        from stock_agent.research_pipeline import JobOutcome

        now = _now_et()
        market = await self.calendar.refresh()
        session = self.calendar.session_on(now.date())
        if (market.source is not MarketSource.LIVE or market.status is not MarketStatus.CLOSED
                or session is None or now < session.closes_at):
            return JobOutcome(JobStatus.FAILED, detail={"reason": "close_clock_unverified"})
        previous = self.calendar.previous_session(session.date)
        if previous is None:
            return JobOutcome(JobStatus.FAILED, detail={"reason": "previous_session_unverified"})
        try:
            state = DrawdownState.from_record(self.ledger.risk_state_get(DRAWDOWN_STATE_KEY))
        except ValueError:
            await self._alert("critical", "Invalid persisted drawdown",
                              "Existing high-water record retained; entries stay blocked.",
                              key="invalid-drawdown")
            return JobOutcome(JobStatus.FAILED, detail={"reason": "drawdown_record_invalid"})
        snapshot = await self.ledger.refresh("drawdown close")
        if (snapshot.quality != "live" or snapshot.equity <= 0 or snapshot.as_of < session.closes_at
                or abs(_now_utc() - snapshot.as_of) > timedelta(seconds=60)
                or self.ledger.trading_block()):
            return JobOutcome(JobStatus.FAILED, detail={"reason": "close_equity_unverified"})
        updated = update_drawdown(state, session.date.isoformat(), snapshot.equity,
                                  previous_session=previous.date.isoformat())
        if updated == state:
            return None
        self.ledger.risk_state_set(DRAWDOWN_STATE_KEY, updated.to_record())
        if (state.alert, state.throttled, state.paused) != (updated.alert, updated.throttled, updated.paused):
            await self._alert(
                "critical" if updated.paused else "warning" if updated.alert else "info",
                "Drawdown controls changed",
                f"Drawdown: {updated.drawdown:.1%}; throttled: {updated.throttled}; paused: {updated.paused}.",
                key=f"drawdown:{session.date}",
            )

    def _schedule_job(self, name, window_key, callback, *, timeout, replay=True):
        if self._shutdown or name in self._job_tasks:
            return
        row = self.ledger.job_status(name, window_key)
        if row is not None:
            if row.status in {JobStatus.DONE, JobStatus.SKIPPED} or row.attempts >= MAX_JOB_ATTEMPTS:
                return
            if row.status is JobStatus.STARTED and not replay:
                # An external notification/order may have succeeded before the crash.
                self.ledger.finish_job(name, window_key, JobStatus.SKIPPED, detail={"reason": "interrupted"})
                return
            if row.eligible_retry_at and row.eligible_retry_at > _now_utc():
                return
        attempt = self.ledger.start_job(name, window_key).attempts
        self._job_tasks[name] = asyncio.create_task(
            self._run_scheduled_job(name, window_key, callback, timeout, attempt),
            name=f"stock-agent:{name}",
        )

    async def _run_scheduled_job(self, name, window_key, callback, timeout, attempt):
        try:
            async with asyncio.timeout(timeout):
                outcome = await callback()
            status = JobStatus(getattr(outcome, "status", JobStatus.DONE))
            intents = getattr(outcome, "intents", ())
            detail = getattr(outcome, "detail", {})
            if not isinstance(detail, Mapping):
                detail = {"reason": str(detail)[:160]}
            retry = _now_utc() + timedelta(minutes=5 * attempt) if status is JobStatus.FAILED else None
            self.ledger.finish_job(name, window_key, status, intents=intents, detail=detail, eligible_retry_at=retry)
            if status in {JobStatus.FAILED, JobStatus.SKIPPED}:
                await self._alert("warning", f"{name} {status.value}", "See the job journal for details.",
                                  key=f"job-result:{name}")
        except Exception as exc:
            self.ledger.finish_job(name, window_key, JobStatus.FAILED,
                                   detail={"error": type(exc).__name__},
                                   eligible_retry_at=_now_utc() + timedelta(minutes=5 * attempt))
            await self._alert("warning", f"{name} did not complete", type(exc).__name__, key=f"job:{name}")

    async def _collect_finished_jobs(self):
        for name, task in tuple(self._job_tasks.items()):
            if task.done():
                del self._job_tasks[name]
                if not task.cancelled() and task.exception() is not None:
                    await self._alert("critical", f"{name} could not persist its outcome",
                                      type(task.exception()).__name__, key=f"job-write:{name}")

    async def _drive_order_lane(self, now: datetime):
        if self._order_task is not None:
            if not self._order_task.done():
                return
            if not self._order_task.cancelled() and self._order_task.exception() is not None:
                await self._alert("warning", "Order lane did not complete",
                                  type(self._order_task.exception()).__name__, key="order-lane")
            self._order_task = None
        if (self.engine is not None and self._last_reconcile_at is not None
                and not self._shutdown and self._should_monitor(now)):
            self._last_monitor_time = now
            self._order_task = asyncio.create_task(self._run_execution_cycle(), name="stock-agent:protection")

    async def _drive_options_monitor(self):
        if self._options_task is not None:
            if not self._options_task.done():
                return
            if not self._options_task.cancelled() and self._options_task.exception() is not None:
                await self._alert("warning", "Option protection did not complete",
                                  type(self._options_task.exception()).__name__, key="options-monitor")
        if "options" not in self._job_tasks and not self._shutdown:
            self._options_task = asyncio.create_task(
                self._check_options_positions(), name="stock-agent:options-protection",
            )

    async def _run_execution_cycle(self):
        if not self._engine_resumed:
            try:
                await self._resume_execution()
            except TimeoutError:
                await self._alert("warning", "Order recovery is still pending",
                                  "Existing protection remains in place; new entries are blocked.",
                                  key="order-resume")
                return
        if self._shutdown:
            return
        await self.engine.monitor_tick()
        if self.engine.normalised and not self._normalised:
            self._normalised = await self._reconcile_scheduler("normalise completed")

    async def _resume_execution(self):
        async with asyncio.timeout(30):
            await self.engine.resume()
        self._engine_resumed = True
        if self._reconciled:
            self._normalised = self.engine.normalised

    # ── Schedule checks ──────────────────────────────────────────────

    def _should_run_macro_check(self, now_et: datetime) -> bool:
        return self._in_preopen(now_et, start_minutes=180, end_minutes=120)

    def _should_run_options_scan(self, now_et: datetime) -> bool:
        if self.options is None or not self._entries_enabled() or not self._is_market_open(now_et):
            return False
        session = self.calendar.session_on(now_et.date())
        if session is None or (self._options_task is not None and not self._options_task.done()):
            return False
        until = min(datetime.combine(now_et.date(), time(15, 0), ET), session.closes_at - timedelta(minutes=10))
        return session.opens_at + timedelta(minutes=15) <= now_et < until

    def _should_send_morning_brief(self, now_et: datetime) -> bool:
        session = self.calendar.session_on(now_et.date())
        return bool(session and session.opens_at - timedelta(minutes=75) <= now_et <
                    session.opens_at + timedelta(hours=1))

    def _should_run_weekly(self, now_et: datetime) -> bool:
        return self._research_window(now_et, "weekly")

    def _should_run_midweek(self, now_et: datetime) -> bool:
        return self._research_window(now_et, "midweek")

    def _should_run_daily_prescan(self, now_et: datetime) -> bool:
        return self._in_preopen(now_et, start_minutes=90, end_minutes=30)

    def _should_run_earnings_check(self, now_et: datetime) -> bool:
        return self.config.EARNINGS_CHECK_ENABLED and self._in_preopen(now_et, start_minutes=150, end_minutes=90)

    def _is_market_open(self, now_et: datetime) -> bool:
        return self.calendar.is_open(now_et)

    def _should_monitor(self, now_et: datetime) -> bool:
        if self._last_monitor_time is None:
            return True
        elapsed = now_et.timestamp() - self._last_monitor_time.timestamp()
        return elapsed >= TICK_SECONDS or elapsed < 0

    def _should_send_daily_summary(self, now_et: datetime) -> bool:
        session = self.calendar.session_on(now_et.date())
        return bool(session and now_et >= session.closes_at + timedelta(minutes=5))

    def _in_preopen(self, now_et: datetime, *, start_minutes: int, end_minutes: int) -> bool:
        session = self.calendar.session_on(now_et.date())
        return bool(session and session.opens_at - timedelta(minutes=start_minutes) <= now_et <
                    session.opens_at - timedelta(minutes=end_minutes))

    def _research_anchor(self, now_et: datetime, job: str):
        weekday = self.config.WEEKLY_ANALYSIS_DAY if job == "weekly" else self.config.MIDWEEK_ANALYSIS_DAY
        return now_et.date() - timedelta(days=(now_et.weekday() - weekday) % 7)

    def _research_week_key(self, now_et: datetime, job: str) -> str:
        return self._research_anchor(now_et, job).isoformat()

    def _research_window(self, now_et: datetime, job: str) -> bool:
        anchor = self._research_anchor(now_et, job)
        if now_et.date() == anchor:
            return 18 <= now_et.hour < 21
        next_session = self.calendar.next_session(anchor)
        return bool(next_session and next_session.date == now_et.date() and
                    self._in_preopen(now_et, start_minutes=180, end_minutes=30))

    # ── Weekly analysis ──────────────────────────────────────────────

    async def _run_macro_check(self):
        """Run macro conditions check and post brief to Discord."""
        try:
            now = _now_et()
            self._macro_check_done_today = now.strftime("%Y-%m-%d")

            snapshot = await self.macro.check_conditions()

            # Post macro brief to Discord
            embed = self.macro.format_discord_brief(snapshot)
            await self.discord.send_embed(self.discord.channels.get("macro", ""), embed)

            # If regime changed, also post to announcements
            if snapshot.regime_changed:
                regime_name = snapshot.regime.replace('_', ' ').title()
                prev_name = snapshot.previous_regime.replace('_', ' ').title()
                await self.discord.send_embed(
                    self.discord.channels.get("announcements", ""),
                    {
                        "title": "\u26a1 Market Regime Change",
                        "description": (
                            f"Regime shifted from **{prev_name}** to **{regime_name}**\n\n"
                            f"{snapshot.regime_description}"
                        ),
                        "color": 0xF59E0B,
                        "fields": [{"name": "Signals", "value": "\n".join(f"\u2022 {s}" for s in snapshot.signals[:8]) or "None", "inline": False}],
                    },
                )

            logger.info(
                "Macro check complete: regime=%s, score=%d, signals=%d",
                snapshot.regime, snapshot.regime_score, len(snapshot.signals),
            )

        except Exception as e:
            logger.exception("Macro check failed: %s", e)

    async def _run_weekly_analysis(self):
        return await self._run_research("weekly")

    async def _run_midweek_analysis(self):
        return await self._run_research("midweek")

    async def _run_daily_prescan(self):
        return await self._run_research("prescan")

    async def _run_earnings_reactive(self):
        return await self._run_research("earnings")

    async def _run_held_news_check(self, snapshot):
        return await self._run_research("news", snapshot)

    async def _run_research(self, kind, snapshot=None):
        research = getattr(self, "research", None)
        if research is None:
            from stock_agent.ledger import JobStatus
            from stock_agent.research_pipeline import JobOutcome

            return JobOutcome(JobStatus.SKIPPED, detail={"reason": "research_not_configured"})
        return await research.run(kind, snapshot)

    # ── Shared helpers ───────────────────────────────────────────────

    async def _sync_portfolio_from_alpaca(self) -> None:
        """Pull live positions + account from Alpaca and overwrite local state.

        This makes Alpaca the source of truth for share counts, avg entry
        prices, and cash (including margin balances). Runs at the top of
        every weekly / mid-week cycle so risk checks see real weights.
        """
        try:
            account = await self.executor.get_account()
            positions = await self.executor.get_positions()
            if account is None and not positions:
                logger.warning("Alpaca sync: no account + no positions returned — skipping")
                return
            self.portfolio.sync_from_alpaca(positions or [], account)
        except Exception as exc:
            logger.exception("Alpaca sync failed — continuing with local state: %s", exc)

    async def _load_tipranks_data(self) -> dict:
        """Fetch TipRanks Smart Score data via API screener."""
        if not self.config.TIPRANKS_ENABLED:
            return {}
        try:
            lookup = build_tipranks_lookup(
                min_score=self.config.TIPRANKS_MIN_SMART_SCORE,
                limit=self.config.TIPRANKS_UNIVERSE_LIMIT,
            )
            logger.info("TipRanks lookup built: %d symbols", len(lookup))
            return lookup
        except Exception as exc:
            logger.warning("TipRanks lookup failed: %s \u2014 continuing without", exc)
            return {}

    async def _fetch_fundamentals(self, symbols: list[str]) -> list[CompanyData]:
        """Fetch fundamentals for a list of symbols."""
        companies: list[CompanyData] = []
        for sym in symbols:
            try:
                data = await self.data_feed.get_company_fundamentals(sym)
                if data:
                    companies.append(data)
            except Exception as e:
                logger.warning("Failed to fetch fundamentals for %s: %s", sym, e)
        logger.info("Fetched fundamentals for %d companies", len(companies))
        return companies

    # ── Market monitoring ────────────────────────────────────────────

    async def _run_market_monitoring(self):
        """Broker safety and queued orders, then the held-position news hook.

        Order of work: engine.monitor_tick (clean-up, cover any short, one
        broker stop per long, queued exits then entries), then the research
        hook, whose intents are persisted and dispatched in the same cycle.
        There is no software stop sell: the broker stop is the only one.
        """
        self._last_monitor_time = _now_et()
        if self.engine is None:
            logger.error("Execution engine not wired; no monitoring")
            return None
        report = await self.engine.monitor_tick()
        hook = getattr(self, "_run_held_news_check", None)
        if hook is None or self.ledger is None:
            return report
        try:
            try:
                snapshot = self.ledger.snapshot()
            except Exception:
                snapshot = await self.ledger.refresh("held news check")
            outcome = await hook(snapshot)
            intents = list(getattr(outcome, "intents", outcome) or [])
            if intents:
                self.ledger.enqueue_intents(intents, job="held_news")
                report.executed.extend(await self.engine.execute_due())
        except Exception as e:
            logger.exception("Held news check failed: %s", e)
            report.errors.append(f"held news: {e}")
        return report

    def _should_check_events(self) -> bool:
        now = _now_et()
        return self._is_market_open(now) and (
            self._last_news_at is None or now.timestamp() - self._last_news_at.timestamp() >= NEWS_SECONDS
        )

    # ── Daily summary ────────────────────────────────────────────────

    async def _send_daily_summary(self):
        """End-of-day summary."""
        try:
            now = _now_et()
            self._daily_summary_sent_today = now.strftime("%Y-%m-%d")

            pv = self.portfolio.get_portfolio_value()
            exposure = self.portfolio.get_exposure()

            # Calculate day P&L (compare to previous summary or starting capital)
            prev_value = self.portfolio.state.starting_capital
            if self.portfolio.state.daily_summaries:
                prev_value = self.portfolio.state.daily_summaries[-1].portfolio_value

            day_pnl = pv - prev_value
            day_pnl_pct = day_pnl / prev_value if prev_value > 0 else 0
            total_pnl = pv - self.portfolio.state.starting_capital
            total_pnl_pct = total_pnl / self.portfolio.state.starting_capital if self.portfolio.state.starting_capital > 0 else 0

            # Trades executed today
            today_str = now.strftime("%Y-%m-%d")
            trades_today = [
                t for t in self.portfolio.state.trade_history
                if t.timestamp.strftime("%Y-%m-%d") == today_str
            ]

            summary = DailySummary(
                date=now.strftime("%b %d, %Y (%a)"),
                portfolio_value=pv,
                cash=self.portfolio.state.cash,
                total_exposure=sum(p.current_price * p.shares for p in self.portfolio.state.positions),
                exposure_pct=exposure,
                num_positions=len(self.portfolio.state.positions),
                day_pnl=day_pnl,
                day_pnl_pct=day_pnl_pct,
                total_pnl=total_pnl,
                total_pnl_pct=total_pnl_pct,
                trades_today=trades_today,
                positions=list(self.portfolio.state.positions),
                signals=[],
            )

            self.portfolio.add_daily_summary(summary)
            await asyncio.gather(
                self.telegram.send_daily_summary(summary),
                self.discord.send_daily_summary(summary),
                self.discord.send_eli5_daily(summary),
                return_exceptions=True,
            )
            logger.info("Daily summary sent — PV=$%.0f, Day P&L=$%+.0f", pv, day_pnl)

        except Exception as e:
            logger.exception("Daily summary failed: %s", e)
            await asyncio.gather(
                self.telegram.send_error_alert(f"Daily summary failed: {e}"),
                self.discord.send_error(str(e), context="Daily summary"),
                return_exceptions=True,
            )

    # ── Trade execution ──────────────────────────────────────────────

    async def _execute_signals(self, signals: list[Signal]) -> list[Signal]:
        """Queue BUY signals as order intents and let the execution engine run them.

        Sizing, risk limits, market hours and the protective stop are all
        decided by the engine at execution time from fresh broker state.
        Returns the signals whose buy filled.
        """
        if self.engine is None or self.ledger is None:
            logger.error("Execution engine not wired; %d signals not executed", len(signals))
            return []
        from decimal import Decimal

        from stock_agent.ledger import NewIntent

        session = _now_et().date()
        buys = [s for s in signals if s.action == "BUY"]
        missing = [s.symbol.upper() for s in buys if not s.entry_price]
        marks: dict = {}
        if missing and self.prices is not None:
            try:
                marks = (await self.prices.get_quotes(missing, purpose="order_sizing")).quotes
            except Exception as e:
                logger.warning("Quotes for signal reference prices failed: %s", e)
        by_symbol: dict[str, Signal] = {}
        items: list[NewIntent] = []
        for signal in buys:
            sym = signal.symbol.upper()
            by_symbol[sym] = signal
            ref = signal.entry_price or getattr(marks.get(sym), "price", None)
            items.append(NewIntent(
                symbol=sym, side="buy", dedupe_key=f"signal:{sym}:{session.isoformat()}",
                reference_price=None if not ref else Decimal(str(ref)),
                origin="scheduled", sleeve="CORE_EQUITY", session_date=session,
                meta={"conviction": signal.conviction, "effective_conviction": signal.conviction,
                      "raw_conviction": signal.conviction, "signal_at": signal.generated_at.isoformat()},
            ))
        if not items:
            return []
        ids = set(self.ledger.enqueue_intents(items, job="signals"))
        executed: list[Signal] = []
        for res in await self.engine.execute_due():
            if res.intent_id in ids and res.filled_qty > 0 and res.symbol in by_symbol:
                executed.append(by_symbol[res.symbol])
        return executed

    async def _execute_sell(self, position: Position, reason: str):
        """Queue an exit for a held position and run it through the engine.

        Never sells directly: the engine cancels and confirms the broker stop
        first and sells at most the free shares the broker reports.
        """
        return await self._submit_exit(position.symbol, reason=reason)

    async def _submit_exit(self, symbol: str, *, reason: str, rule: str = "model", qty: float | None = None):
        if self.engine is None or self.ledger is None:
            logger.error("Execution engine not wired; exit for %s not sent", symbol)
            return None
        from stock_agent.ledger import NewIntent

        meta = self.ledger.open_position_meta(symbol)
        session = _now_et().date()
        m: dict = {"rule": rule, "reason": reason}
        if qty is not None:
            m["qty"] = qty
        ids = self.ledger.enqueue_intents([NewIntent(
            symbol=symbol, side="sell", dedupe_key=f"exit:{symbol}:{rule}:{session.isoformat()}",
            position_id=getattr(meta, "position_id", None), origin="scheduled", session_date=session, meta=m,
        )], job="exit")
        intent = self.ledger.get_intent(ids[0])
        if intent is None or intent.terminal:
            return None
        return await self.engine.execute_intent(intent)

    async def _run_options_scan(self):
        """Daily options scan: find and execute options opportunities."""
        try:
            now = _now_et()
            self._options_scan_done_today = now.strftime("%Y-%m-%d")

            await self.discord.send_system_log("INFO", "Options scan starting")

            # Reconcile options portfolio against Alpaca FIRST — mark expired/
            # closed legs so we don't double-count them in the "skip underlyings
            # with existing positions" block below.
            portfolio_value = self.portfolio.get_portfolio_value()
            try:
                live_opts = await self.options_executor.get_option_positions()
                opt_acts = await self.options_executor.get_option_activities()
                sync_result = await self.options_portfolio.sync_from_alpaca(
                    alpaca_option_positions=live_opts,
                    alpaca_option_activities=opt_acts,
                    portfolio_value=portfolio_value,
                )
                if sync_result["closed"]:
                    logger.info(
                        "Options reconcile: %d positions closed, $%.0f realized P&L captured",
                        sync_result["closed"], sync_result["realized_pnl"],
                    )
            except Exception as e:
                logger.warning("Options Alpaca reconcile failed (non-fatal): %s", e)

            # Refresh remaining open options with latest prices
            await self.options_portfolio.refresh_positions(portfolio_value)

            # Build inputs for the options engine
            stock_positions = {
                p.symbol: p.shares for p in self.portfolio.state.positions
            }

            # Build screener candidates: (symbol, conviction, price, target_price)
            screener_candidates: list[tuple[str, int, float, float | None]] = []

            # Track underlyings already in options portfolio to prevent duplicates
            import re as _re
            existing_options_underlyings: set[str] = set()
            for op in self.options_portfolio.state.positions:
                m = _re.match(r'([A-Z]+)', op.contract_symbol)
                if m:
                    existing_options_underlyings.add(m.group(1))
            if existing_options_underlyings:
                logger.info(
                    "Skipping options scan for underlyings with existing positions: %s",
                    existing_options_underlyings,
                )

            universe = self.portfolio.state.universe or []
            for sym in universe[:30]:
                if sym in existing_options_underlyings:
                    continue  # Already have an options position on this underlying
                try:
                    company = await self.data_feed.get_company_fundamentals(sym)
                    if not company or company.price <= 0:
                        continue
                    # Get conviction from active theses if available
                    thesis = self.portfolio.state.active_theses.get(sym)
                    conviction = 6  # default
                    target_price = None
                    if isinstance(thesis, dict):
                        conviction = thesis.get("conviction", 6)
                        target_price = thesis.get("target_price")
                    screener_candidates.append((sym, conviction, company.price, target_price))
                except Exception:
                    continue

            current_exposure = self.options_portfolio.total_exposure_pct

            # Filter stock_positions to exclude underlyings with existing options
            filtered_stock_positions = {
                sym: shares for sym, shares in stock_positions.items()
                if sym not in existing_options_underlyings
            }

            # Run the engine
            signals = await self.options_engine.scan_opportunities(
                portfolio_value=portfolio_value,
                open_options_count=self.options_portfolio.open_position_count,
                current_options_exposure=current_exposure,
                stock_positions=filtered_stock_positions,
                screener_candidates=screener_candidates,
            )

            # Execute each signal
            signals_executed = 0
            for signal in signals:
                try:
                    if signal.leg2_contract_symbol:
                        # Spread order
                        order = await self.options_executor.place_spread_order(
                            long_symbol=signal.contract_symbol,
                            short_symbol=signal.leg2_contract_symbol,
                            qty=signal.qty,
                            order_type=OptionOrderType.MARKET,
                        )
                    else:
                        # Single-leg order — resolve side to proper enum
                        side_str = signal.side.value if hasattr(signal.side, 'value') else str(signal.side)
                        side_enum = OptionSide.SELL if side_str == "sell" else OptionSide.BUY
                        order = await self.options_executor.place_option_order(
                            contract_symbol=signal.contract_symbol,
                            qty=signal.qty,
                            side=side_enum,
                            order_type=OptionOrderType.MARKET,
                            limit_price=None,
                        )

                    if order:
                        # Use max_debit as fill price proxy; fall back to 0.0
                        fill_price = getattr(signal, 'max_debit_per_contract', None) or \
                                     getattr(signal, 'min_credit_per_contract', None) or 0.0
                        regime = self.macro.get_current_regime()
                        pos = self.options_portfolio.open_position(
                            signal=signal,
                            filled_price=fill_price,
                            portfolio_value=portfolio_value,
                            regime=regime,
                        )
                        signals_executed += 1
                        logger.info(
                            "Options: executed %s on %s — %s",
                            signal.strategy.value, signal.underlying_symbol, signal.contract_symbol,
                        )
                        try:
                            embed = self.options_portfolio.format_position_alert(pos, "OPENED")
                            await self.discord.send_embed(
                                self.discord.channels.get("trades", ""), embed
                            )
                        except Exception:
                            pass
                except Exception as e:
                    logger.error("Options execution failed for %s: %s", signal.contract_symbol, e)

            await self.discord.send_system_log(
                "INFO",
                f"Options scan complete — {signals_executed} trades executed, "
                f"{self.options_portfolio.open_position_count} open options positions",
            )
            logger.info("Options scan complete — %d executed", signals_executed)

        except Exception as e:
            logger.exception("Options scan failed: %s", e)
            await self.discord.send_error(str(e), context="Options scan")

    async def _check_options_positions(self):
        """Monitor options positions and send expiry alerts.

        Note: Auto-close via order submission is disabled because Alpaca paper
        trading rejects SELL orders on long options as 'uncovered'. Positions
        will be managed to expiry or closed manually. Only expiry warnings
        (3 DTE) are sent as alerts.
        """
        try:
            if not self.options_portfolio.state.positions:
                return

            # Reconcile with Alpaca first (catch expirations/assignments)
            portfolio_value = self.portfolio.get_portfolio_value()
            try:
                live_opts = await self.options_executor.get_option_positions()
                opt_acts = await self.options_executor.get_option_activities()
                await self.options_portfolio.sync_from_alpaca(
                    alpaca_option_positions=live_opts,
                    alpaca_option_activities=opt_acts,
                    portfolio_value=portfolio_value,
                )
            except Exception as e:
                logger.warning("Intraday options reconcile failed: %s", e)

            # Refresh prices on remaining open positions
            await self.options_portfolio.refresh_positions(portfolio_value)

            # Expiry alerts only (3 DTE warning)
            expiry_alerts = self.options_portfolio.get_expiry_alerts()
            for pos in expiry_alerts:
                try:
                    await self.discord.send_embed(
                        self.discord.channels.get("risk", ""),
                        {
                            "title": "\u26a0\ufe0f Options Expiry Alert",
                            "description": (
                                f"**{pos.contract_symbol}** expires in **{pos.dte} days**\n"
                                f"Strategy: {pos.strategy.value}\n"
                                f"Unrealized P&L tracked internally only — "
                                f"positions will run to expiry.\n"
                                f"Strike: ${getattr(pos, 'strike', '?')}  "
                                f"Expiry: {getattr(pos, 'expiration_date', '?')}"
                            ),
                            "color": 0xF59E0B,
                        },
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error("Options position check failed: %s", e)

    async def _check_for_market_event(self):
        """A healthy regime change can alert, but cannot open a trade job."""
        research = getattr(self, "research", None)
        if research is None:
            return
        change = await research.observe_regime()
        if change is not None:
            before, after = change
            await self.alerts.alert(
                "WARN", "Market regime changed", f"{before} to {after}; no trade triggered",
                key=f"market_regime_{before}_{after}",
            )

    # ── Morning Brief ────────────────────────────────────────────────

    async def _send_morning_brief(self):
        """Comprehensive morning brief posted to #announcements.

        Includes: portfolio status, market regime, yesterday's activity,
        today's outlook, key levels, and what the bot plans to do.
        """
        try:
            now = _now_et()
            self._morning_brief_done_today = now.strftime("%Y-%m-%d")
            today_str = now.strftime("%A, %B %d, %Y")
            sgt_str = datetime.now(SGT).strftime("%I:%M %p SGT")

            embeds: list[dict] = []

            # ── 1. PORTFOLIO STATUS ──────────────────────────────
            # Sync from Alpaca for accurate data
            alpaca_account = await self.executor.get_account()
            alpaca_positions = await self.executor.get_positions()

            if alpaca_account:
                equity = float(alpaca_account.get("equity", 0))
                cash = float(alpaca_account.get("cash", 0))
                last_equity = float(alpaca_account.get("last_equity", 0))
                day_pnl = equity - last_equity
                day_pnl_pct = (day_pnl / last_equity * 100) if last_equity > 0 else 0
                starting = self.portfolio.state.starting_capital
                total_pnl = equity - starting
                total_pnl_pct = (total_pnl / starting * 100) if starting > 0 else 0
                invested = equity - cash
                exposure_pct = (invested / equity * 100) if equity > 0 else 0

                # Also sync internal portfolio
                if alpaca_positions:
                    self.portfolio.sync_from_alpaca(alpaca_positions, alpaca_account)

                pnl_emoji = "\U0001f4c8" if total_pnl >= 0 else "\U0001f4c9"
                day_emoji = "\u2705" if day_pnl >= 0 else "\u274c"

                portfolio_fields = [
                    {"name": "Portfolio Value", "value": f"**${equity:,.2f}**", "inline": True},
                    {"name": "Cash Available", "value": f"${cash:,.0f}", "inline": True},
                    {"name": "Exposure", "value": f"{exposure_pct:.1f}% (${invested:,.0f})", "inline": True},
                    {"name": f"{day_emoji} Yesterday P&L", "value": f"${day_pnl:+,.2f} ({day_pnl_pct:+.2f}%)", "inline": True},
                    {"name": f"{pnl_emoji} Total P&L", "value": f"${total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)", "inline": True},
                    {"name": "Positions", "value": f"{len(alpaca_positions)} / {self.config.MAX_POSITIONS} slots", "inline": True},
                ]

                # Position table
                if alpaca_positions:
                    # Sort by unrealized P&L descending
                    sorted_pos = sorted(
                        alpaca_positions,
                        key=lambda p: float(p.get("unrealized_pl", 0)),
                        reverse=True,
                    )
                    pos_lines = []
                    for p in sorted_pos:
                        sym = p.get("symbol", "?")
                        upnl = float(p.get("unrealized_pl", 0))
                        upnl_pct = float(p.get("unrealized_plpc", 0)) * 100
                        cur = float(p.get("current_price", 0))
                        mv = float(p.get("market_value", 0))
                        emoji = "\u2705" if upnl >= 0 else "\u274c"
                        pos_lines.append(
                            f"{emoji} **{sym}** ${cur:,.2f} | ${upnl:+,.0f} ({upnl_pct:+.1f}%) | ${mv:,.0f}"
                        )
                    pos_text = "\n".join(pos_lines)
                    if len(pos_text) <= 1024:
                        portfolio_fields.append(
                            {"name": "\U0001f4bc Open Positions", "value": pos_text, "inline": False}
                        )
                    else:
                        # Split into two fields
                        mid = len(pos_lines) // 2
                        portfolio_fields.append(
                            {"name": "\U0001f4bc Positions (1/2)", "value": "\n".join(pos_lines[:mid]), "inline": False}
                        )
                        portfolio_fields.append(
                            {"name": "\U0001f4bc Positions (2/2)", "value": "\n".join(pos_lines[mid:]), "inline": False}
                        )

                embeds.append({
                    "title": f"\u2615 Morning Brief \u2014 {today_str}",
                    "description": f"Good {'morning' if now.hour < 12 else 'evening'}. Here's your daily update. ({sgt_str})",
                    "color": 0x3B82F6,
                    "fields": portfolio_fields,
                })
            else:
                embeds.append({
                    "title": f"\u2615 Morning Brief \u2014 {today_str}",
                    "description": "Could not fetch Alpaca account data.",
                    "color": 0xEF4444,
                })

            # ── 2. MARKET REGIME ─────────────────────────────────
            macro_snap = self.macro.get_last_snapshot()
            if macro_snap:
                regime_embed = self.macro.format_discord_brief(macro_snap)
                regime_embed["title"] = "\U0001f30d Market Conditions"
                embeds.append(regime_embed)
            else:
                # Run a fresh check if none exists
                try:
                    macro_snap = await self.macro.check_conditions()
                    regime_embed = self.macro.format_discord_brief(macro_snap)
                    regime_embed["title"] = "\U0001f30d Market Conditions"
                    embeds.append(regime_embed)
                except Exception as e:
                    logger.warning("Could not fetch macro data for morning brief: %s", e)

            # ── 3. YESTERDAY RECAP ───────────────────────────────
            yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            yesterday_trades = [
                t for t in self.portfolio.state.trade_history
                if t.timestamp.strftime("%Y-%m-%d") == yesterday_str
            ]

            recap_lines = []
            if yesterday_trades:
                for t in yesterday_trades:
                    pnl_str = ""
                    if t.pnl is not None:
                        pnl_str = f" \u2192 ${t.pnl:+,.0f}"
                    recap_lines.append(f"\u2022 **{t.action}** {t.symbol} x{t.shares} @ ${t.price:.2f}{pnl_str}")
            else:
                recap_lines.append("No trades executed yesterday. The bot held steady.")

            # What scans ran
            scan_info = []
            day_of_week = now.weekday()
            yesterday_dow = (now - timedelta(days=1)).weekday()
            if yesterday_dow == 6:  # Sunday
                scan_info.append("\u2022 Weekly full analysis ran (Sunday cycle)")
            if yesterday_dow == 2:  # Wednesday
                scan_info.append("\u2022 Mid-week analysis ran (Wednesday refresh)")
            if yesterday_dow < 5:  # Weekday
                scan_info.append("\u2022 Daily pre-market scan completed")
                scan_info.append("\u2022 Market monitoring ran every 30 min during market hours")

            recap_text = "**Trades:**\n" + "\n".join(recap_lines)
            if scan_info:
                recap_text += "\n\n**Bot Activity:**\n" + "\n".join(scan_info)

            embeds.append({
                "title": "\U0001f4cb Yesterday's Recap",
                "description": recap_text,
                "color": 0x6B7280,
            })

            # ── 4. TODAY'S OUTLOOK ───────────────────────────────
            outlook_lines = []

            # What's scheduled today
            if day_of_week < 5:  # Weekday
                outlook_lines.append("\u2022 **6:30 AM ET** \u2014 Macro conditions check")
                outlook_lines.append("\u2022 **7:00 AM ET** \u2014 Earnings-reactive scan")
                outlook_lines.append("\u2022 **8:00 AM ET** \u2014 Daily pre-market scan")
                outlook_lines.append("\u2022 **9:30 AM - 4:00 PM ET** \u2014 Market monitoring (every 30 min)")
                outlook_lines.append("\u2022 **4:05 PM ET** \u2014 Daily summary")
            if day_of_week == 2:  # Wednesday
                outlook_lines.append("\u2022 **6:00 PM ET** \u2014 \U0001f50d Mid-week full analysis (Wednesday refresh)")
            if day_of_week == 6:  # Sunday
                outlook_lines.append("\u2022 **6:00 PM ET** \u2014 \U0001f50d Weekly full analysis")

            # Regime-driven behavior
            regime = self.macro.get_current_regime()
            min_conv = self.macro.get_effective_min_conviction()
            scan_depth = self.macro.get_effective_universe_size()
            analysis_depth = self.macro.get_effective_analysis_depth()

            outlook_lines.append("")
            outlook_lines.append(f"**Active Regime:** {regime.replace('_', ' ').title()}")
            outlook_lines.append(f"**Conviction Threshold:** {min_conv}/10")
            outlook_lines.append(f"**Scan Depth:** {scan_depth} stocks \u2192 analyze top {analysis_depth}")

            # Available slots and firepower
            open_slots = self.config.MAX_POSITIONS - len(self.portfolio.state.positions)
            if open_slots > 0 and alpaca_account:
                outlook_lines.append(f"**Open Slots:** {open_slots} positions available")
                outlook_lines.append(f"**Buying Power:** ${cash:,.0f} ready to deploy")
            elif open_slots <= 0:
                outlook_lines.append("**Slots:** Fully invested \u2014 no new positions unless one is sold")

            # Pending signals
            if self._pending_signals:
                outlook_lines.append(f"\n**\U0001f4e1 {len(self._pending_signals)} signal(s) queued** for market open")
                for sig in self._pending_signals[:5]:
                    outlook_lines.append(f"  \u2022 {sig.action} {sig.symbol} (conviction {sig.conviction})")

            embeds.append({
                "title": "\U0001f3af Today's Plan",
                "description": "\n".join(outlook_lines),
                "color": 0x8B5CF6,
            })

            # ── 5. KEY LEVELS ────────────────────────────────────
            from stock_agent.macro_monitor import (
                SPX_SUPPORT_LOW, SPX_SUPPORT_HIGH,
                SPX_RESISTANCE_1, SPX_RESISTANCE_2, SPX_RESISTANCE_CONFIRM,
                QQQ_RESISTANCE_1, QQQ_RESISTANCE_2,
            )

            spx_price = macro_snap.spx_price if macro_snap else 0
            qqq_price = macro_snap.qqq_price if macro_snap else 0

            levels_text = (
                f"**S&P 500** (current: ${spx_price:,.0f})\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"\U0001f534 Support: {SPX_SUPPORT_LOW:,} - {SPX_SUPPORT_HIGH:,} (buy zone)\n"
                f"\U0001f7e1 Resistance 1: {SPX_RESISTANCE_1:,}\n"
                f"\U0001f7e1 Resistance 2: {SPX_RESISTANCE_2:,}\n"
                f"\U0001f7e2 Trend Confirmed: {SPX_RESISTANCE_CONFIRM:,}\n"
                f"\n"
                f"**QQQ** (current: ${qqq_price:,.2f})\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"\U0001f7e1 Resistance 1: {QQQ_RESISTANCE_1}\n"
                f"\U0001f7e1 Resistance 2: {QQQ_RESISTANCE_2}\n"
            )

            embeds.append({
                "title": "\U0001f4ca Key Levels to Watch",
                "description": levels_text,
                "color": 0xF59E0B,
            })

            # ── POST TO DISCORD ──────────────────────────────────
            # Post to announcements channel as multi-embed message
            ch = self.discord.channels.get("announcements", "")
            if ch:
                # Discord allows max 10 embeds — we should have 5
                await self.discord._send_multi_embed(ch, embeds)

            logger.info("Morning brief sent — %d embeds", len(embeds))

        except Exception as e:
            logger.exception("Morning brief failed: %s", e)

    # ── Sector backfill ──────────────────────────────────────────────

    async def _backfill_sectors(self):
        """Backfill sectors on positions marked 'Unknown'.

        Uses Config.SECTOR_MAP first (fast, deterministic) then falls back to
        FMP for symbols we haven't hardcoded.
        """
        sector_map = getattr(self.config, "SECTOR_MAP", {}) or {}
        updated = 0
        for pos in self.portfolio.state.positions:
            if pos.sector and pos.sector != "Unknown":
                continue
            # 1. Hardcoded map (fast path)
            mapped = sector_map.get(pos.symbol)
            if mapped:
                pos.sector = mapped
                updated += 1
                logger.info("Backfilled sector for %s from SECTOR_MAP: %s", pos.symbol, mapped)
                continue
            # 2. FMP lookup for unknowns
            try:
                company = await self.data_feed.get_company_fundamentals(pos.symbol)
                if company and company.sector and company.sector != "Unknown":
                    pos.sector = company.sector
                    updated += 1
                    logger.info("Backfilled sector for %s from FMP: %s", pos.symbol, company.sector)
            except Exception as e:
                logger.warning("Sector backfill failed for %s: %s", pos.symbol, e)
        if updated:
            self.portfolio._save()
            logger.info("Backfilled sectors for %d positions", updated)

    # ── Cleanup ──────────────────────────────────────────────────────

    async def _cleanup(self):
        """Cut off new work, drain order paths, then release network and database resources."""
        self.shutdown()
        notify_systemd("STOPPING=1\nSTATUS=Draining trading tasks")
        passive = [task for name, task in self._job_tasks.items() if name != "options"]
        for task in passive:
            task.cancel()
        active = [task for task in (self._order_task, self._options_task, self._job_tasks.get("options"))
                  if task is not None]
        if self.engine is not None:
            active.append(asyncio.create_task(self.engine.drain(EXECUTION_DRAIN_SECONDS)))
        all_tasks = set(passive + active)
        if all_tasks:
            done, pending = await asyncio.wait(all_tasks, timeout=SHUTDOWN_DRAIN_SECONDS)
            if pending:
                await self._alert("critical", "Shutdown drain deadline exceeded",
                                  "Unresolved broker intents will be recovered by client order ID on startup.",
                                  key="shutdown-drain")
                for task in pending:
                    task.cancel()
                finished, _ = await asyncio.wait(pending, timeout=2)
                done.update(finished)
            for task in done:
                if not task.cancelled():
                    task.exception()
        self._state_ready = False
        self._publish_runtime()
        await self._alert("info", "Stock agent stopped", "In-flight order recovery remains durable.")
        if self._pending_ledger_alerts:
            _, pending_alerts = await asyncio.wait(self._pending_ledger_alerts, timeout=2)
            for task in pending_alerts:
                task.cancel()

        seen = set()
        dynamic = ("data_feed", "screener", "executor", "telegram", "discord", "macro",
                   "options_data", "options_executor", "alerts", "prices", "news", "llm", "broker")
        for name in dynamic:
            resource = getattr(self, name, None)
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "close", None)
            if close is not None:
                try:
                    async with asyncio.timeout(1):
                        await close()
                except Exception:
                    logger.warning("Service resource did not close: %s", name)
        self.store.close()
        logger.info("Cleanup complete")
