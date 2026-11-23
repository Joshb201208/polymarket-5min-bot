import asyncio
import inspect
import logging
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from stock_agent.alerts import AlertService
from stock_agent.analyst import Analyst
from stock_agent.asset_registry import AssetRegistry, SymbolRenameEvent
from stock_agent.broker import AlpacaClient
from stock_agent.config import Config
from stock_agent.execution import ExecutionEngine
from stock_agent.fundamentals_service import UNIVERSE_CATALOGUE, FundamentalsService
from stock_agent.heartbeat import HeartbeatClient
from stock_agent.health import ProviderHealthRegistry
from stock_agent.http_safety import DataHttpClient
from stock_agent.models import DailySummary, Signal
from stock_agent.portfolio import Portfolio
from stock_agent.ledger import JobStatus, NewIntent
from stock_agent.llm_client import LLMClient
from stock_agent.market_calendar import GateDecision, MarketCalendar, MarketSource, MarketStatus
from stock_agent.news_service import NewsService
from stock_agent.options_book import KvOptionsStore, import_legacy_options
from stock_agent.options_manager import OptionCandidate, OptionsError, OptionsManager
from stock_agent.performance import DEPOSIT_KINDS, close_session
from stock_agent.price_service import PriceService
from stock_agent.research_gate import ResearchGate
from stock_agent.research_focus import focus_symbols
from stock_agent.research_health import ResearchInputHealth
from stock_agent.research_pipeline import JobOutcome, OptionIdea, ResearchPipeline
from stock_agent.research_sources import ResearchSources
from stock_agent.research_store import ResearchStore
from stock_agent.risk_manager import (
    DRAWDOWN_STATE_KEY, DrawdownState, RiskControls, RiskManager, TradeKind, update_drawdown,
)
from stock_agent.runtime_status import RuntimeStatus, notify_systemd
from stock_agent.state_boot import build_ledger, prepare_ledger
from stock_agent.state_store import StateUnavailable
from stock_agent.discord_alerts import DiscordReporter
from stock_agent.telegram_alerts import TelegramReporter
from stock_agent.macro_monitor import MacroMonitor, REGIME_AGGRESSIVE_DEPLOY, REGIME_DIP_OPPORTUNITY, REGIME_CAUTIOUS

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
        self.ledger = service("ledger", lambda: build_ledger(
            self.config.DATA_DIR, broker=self.broker, alert=self._queue_ledger_alert, clock=_now_utc,
        ))
        self.store = self.ledger.store
        self.journal = self.ledger.journal
        self.discord = service("discord", lambda: DiscordReporter(self.config))
        self.telegram = service("telegram", lambda: TelegramReporter(self.config))
        self.alerts = service("alerts", lambda: AlertService(
            self.config, discord=self.discord, telegram=self.telegram, journal=self.journal, clock=_now_utc,
        ))
        if isinstance(self.alerts, AlertService) and hasattr(self.ledger, "set_exit_fill_handler"):
            self.ledger.set_exit_fill_handler(self.alerts.alert_stop_filled)   # REQ-061: stop fills -> #trades
        self.heartbeat = service("heartbeat", lambda: HeartbeatClient(self.config, clock=_now_utc))
        self.http = service("http", lambda: DataHttpClient(self.config))
        self.risk = service("risk", lambda: RiskManager(self.config))
        self.calendar = service("calendar", lambda: MarketCalendar(
            self.broker, self.config.DATA_DIR / "market-calendar.json", namespace=self.config.MODE,
            now=_now_utc,
        ))
        self.health = service("health", lambda: ProviderHealthRegistry(
            self.config, store=self.store, defer_load=True, now=_now_utc,
            on_transition=getattr(self.alerts, "handle_health_transition", None),
        ))
        self.assets = service("assets", lambda: AssetRegistry(
            self.http, config=self.config, store=self.store, report=self._provider_health, now=_now_utc,
            on_rename=self._on_asset_rename, defer_load=True,
        ))
        self.prices = service("prices", lambda: PriceService(
            self.http, config=self.config, store=self.store, calendar=self.calendar,
            assets=self.assets, report=self._provider_health, now=_now_utc,
        ))
        self.price_service = self.prices
        self.health_registry = self.health
        self._universe_symbols: tuple[str, ...] = ()
        self.fundamentals = service("fundamentals", lambda: FundamentalsService(
            self.http, config=self.config, health=self.health, assets=self.assets, prices=self.prices,
            focus_symbols=self._pollable_focus, now=_now_utc, universe_size=self.config.UNIVERSE_SIZE,
        ))
        self.news = service("news", self._build_news_service)
        self.research_store = service("research_store", lambda: ResearchStore(self.store, self.config))
        self.llm = service("llm", lambda: LLMClient(
            self.config, spend=self.research_store, on_failure=self._on_research_failure, now=_now_utc,
            health=self.health,
        ))
        self.analyst = service("analyst", lambda: Analyst(self.llm))
        self.research_sources = service("research_sources", lambda: ResearchSources(
            self.prices, self.news, list_universe=self._list_universe, fetch_company=self.fundamentals.fetch_company,
            cached_company=getattr(self.fundamentals, "cached_company", None),
            fetch_releases=self.fundamentals.fetch_releases, asset_for=self.assets.get_asset_meta, health=self.health,
        ))
        self.research_health = service("research_health", lambda: ResearchInputHealth(
            self.ledger, self.llm, quote_ok=self.research_sources.quote_ok, news_ok=self.research_sources.news_ok,
            research_store=self.research_store,
        ))
        self.research_gate = service("research_gate", lambda: ResearchGate(
            self.ledger, self.research_health, self.config,
        ))
        self.research = service("research", lambda: ResearchPipeline(
            self.config, self.ledger, self.calendar, self.analyst, self.research_store,
            self.research_sources, self.research_gate, clock=_now_utc, focus=self._pollable_focus, alerts=self.alerts,
        ))
        self.capital_lock = asyncio.Lock()
        self._asset_lock = asyncio.Lock()
        self.engine = service("engine", lambda: ExecutionEngine(
            self.broker, self.ledger, self.risk, self.calendar, self.prices, alert=self._alert,
            bars=self._stop_bars, controls=self._risk_controls,
            market_gate=self.market_gate, config=self.config, now=_now_utc,
            capital_lock=self.capital_lock, research_gate=self.research_gate, notifier=self.alerts,
        ))
        self.options_store = service("options_store", lambda: KvOptionsStore(self.store))
        self.options = service("options", lambda: OptionsManager(
            broker=self.broker, store=self.options_store, gate=self.research_gate, risk=self.risk,
            snapshot=self._options_snapshot, calendar=self.calendar, market_gate=self.market_gate,
            spot=self._option_spot, cover_short=getattr(self.engine, "cover_short", None),
            universe=self._option_universe, risk_controls=self._risk_controls, journal=self.journal,
            alerts=self.alerts, config=self.config, now=_now_utc,
            capital_lock=self.capital_lock, defer_load=True,
        ))
        if isinstance(self.engine, ExecutionEngine):
            self.engine.options = self.options
        self.runtime = service("runtime", lambda: RuntimeStatus.for_data_dir(self.config.DATA_DIR))
        self.pause_file: Path = service("pause_file", lambda: self.config.DATA_DIR / "pause-trading")
        self._stop_event = asyncio.Event()
        self._normalised = False
        self._engine_resumed = False
        self._state_ready = False
        self._reconciled = False
        self._needs_rebuild = False
        self._last_news_at: datetime | None = None
        self._last_reconcile_at: datetime | None = None
        self._last_close_report = None
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._order_task: asyncio.Task | None = None
        self._options_task: asyncio.Task | None = None
        self._telemetry_task: asyncio.Task | None = None
        self._last_issue: dict[str, datetime] = {}
        self._pending_ledger_alerts: set[asyncio.Task] = set()
        # Legacy read view temporarily retained for the two reporting callbacks.
        self.portfolio = Portfolio(self.config, persist=False)
        self.ledger.add_snapshot_listener(self.portfolio.sync_from_snapshot)
        self.macro = service("macro", lambda: MacroMonitor(
            self.config, prices=self.prices, store=self.store, defer_load=True,
            alert=self._alert, report=self._provider_health, now=_now_utc,
        ))
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
                routing = {"channel_key": "announcements"} if (
                    key == "normalise-plan" and isinstance(self.alerts, AlertService)
                ) else {}
                result = self.alerts.alert(severity, title, body, key=key, **routing)
                if inspect.isawaitable(result):
                    async with asyncio.timeout(2):
                        await result
            except Exception:
                logger.warning("Could not queue alert: %s", title)
        else:
            logger.log(logging.WARNING if severity != "info" else logging.INFO, "%s: %s", title, body)

    def _queue_ledger_alert(self, severity: str, title: str, body: str, *, key: str | None = None) -> None:
        if isinstance(self.alerts, AlertService):
            self.alerts.alert(severity, title, body, key=key)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Ledger alert outside scheduler loop: %s", title)
            return
        task = loop.create_task(self._alert(severity, title, body, key=key))
        self._pending_ledger_alerts.add(task)
        task.add_done_callback(self._pending_ledger_alerts.discard)

    async def _report_exception(self, where: str, exc: Exception, *, critical: bool = False) -> None:
        if isinstance(self.alerts, AlertService):
            self.alerts.alert_unhandled_exception(where, type(exc).__name__, critical=critical)
        else:
            await self._alert("critical" if critical else "warning", f"{where} failed", type(exc).__name__,
                              key=f"exception:{where}")

    def _entries_enabled(self) -> bool:
        if not self._normalised or not self._reconciled or self._shutdown or self._manually_paused():
            return False
        try:
            return not self._risk_controls().drawdown.paused
        except (ValueError, StateUnavailable, OptionsError, sqlite3.Error):
            return False

    def _risk_controls(self) -> RiskControls:
        commitments = getattr(self.options, "risk_commitments", None)
        return RiskControls(pause_new_buys=self._manually_paused(),
                            drawdown=self._drawdown_state(),
                            option_commitments=commitments() if commitments is not None else ())

    def _drawdown_state(self) -> DrawdownState:
        return DrawdownState.from_record(self.ledger.risk_state_get(DRAWDOWN_STATE_KEY), config=self.config)

    def _manually_paused(self) -> bool:
        if self.pause_file.exists() or getattr(self.config, "STOCK_TRADING_PAUSED", False):
            return True
        if isinstance(self.runtime, RuntimeStatus) and self.runtime.path.with_name("pause-trading").exists():
            return True
        configured = getattr(self.config, "is_trading_paused", None)
        return bool(configured is not None and configured(self.config.DATA_DIR))

    async def _on_research_failure(self, error) -> None:
        await self._alert("warning", "Research provider unavailable", f"{error.provider}: {error.kind}",
                          key=f"research-provider:{error.provider}:{error.kind}")

    def _provider_health(self, provider: str, capability: str, ok: bool, error: str | None) -> None:
        report = getattr(self.health, "report", None)
        if report is not None:
            report(provider, capability, ok, error)

    async def _on_asset_rename(self, event: SymbolRenameEvent) -> None:
        self.ledger.register_symbol_alias(event.old_symbol, event.new_symbol, source="asset_registry",
                                          effective=event.process_date)
        await self._alert(
            "warning", "Broker confirmed a symbol change",
            f"{event.old_symbol} now trades as {event.new_symbol}; a fresh thesis is needed for new entries.",
            key=f"symbol-rename:{event.old_symbol}:{event.new_symbol}:{event.process_date}",
        )

    def _tracked_symbols(self) -> tuple[str, ...]:
        current = self.ledger.last_snapshot()
        held = (position.symbol for position in current.holdings) if current else ()
        catalogue = tuple(s for symbols in UNIVERSE_CATALOGUE.values() for s in symbols)
        return tuple(dict.fromkeys([
            *held, *focus_symbols(now=_now_utc()), *self.config.PRIORITY_SYMBOLS,
            *self._universe_symbols, *catalogue,
        ]))

    def _resolve_symbols(self, symbols) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            resolved for symbol in symbols if (resolved := self.assets.resolve_symbol(symbol))
        ))

    def _pollable_focus(self, *, now: datetime | None = None) -> tuple[str, ...]:
        return self._resolve_symbols(focus_symbols(now=now or _now_utc()))

    def _build_news_service(self) -> NewsService:
        return NewsService(
            self.http, config=self.config, store=self.store, defer_load=True, health=self.health,
            assets=self.assets, enable_sec_filings=bool(getattr(self.config, "SEC_USER_AGENT", "")),
            now=_now_utc,
        )

    async def _refresh_assets(self, *, force: bool = False):
        if isinstance(self.assets, AssetRegistry) and not (
            self.config.ALPACA_API_KEY and self.config.ALPACA_SECRET_KEY
        ):
            return JobOutcome(JobStatus.SKIPPED, detail={"reason": "assets_credentials_missing"})
        async with self._asset_lock:
            symbols = (*self._tracked_symbols(), *(m.symbol for m in self.ledger.all_open_meta()
                                                   if m.asset_class == "us_equity"))
            await self.assets.refresh(tuple(dict.fromkeys(symbols)), force=force)
        if not self.assets.active_symbols():
            return JobOutcome(JobStatus.FAILED, detail={"reason": "active_assets_unavailable"})
        return None

    async def _list_universe(self) -> tuple[str, ...]:
        candidates = await self.fundamentals.list_universe()
        self._universe_symbols = self._resolve_symbols((*self.config.PRIORITY_SYMBOLS, *candidates))[
            :self.config.UNIVERSE_SIZE
        ]
        return self._universe_symbols

    async def _stop_bars(self, symbol: str):
        getter = getattr(self.prices, "get_daily_bars", None)
        if getter is None:
            return ()
        return (await getter([symbol], sessions=60)).get(symbol, ())

    async def _shadow_bars(self, symbol, start, end):
        return (await self.prices.get_daily_bars([symbol], start_date=start, end_date=end)).get(symbol, ())

    async def _options_snapshot(self):
        return await self.ledger.refresh("option entry recheck")

    async def _option_spot(self, symbol: str):
        quote = (await self.prices.get_quotes([symbol], purpose="order_sizing")).quotes.get(symbol)
        return quote if quote is not None and getattr(quote, "symbol", symbol) == symbol else None

    def _option_universe(self) -> tuple[str, ...]:
        current = self.ledger.last_snapshot()
        held = (position.symbol for position in current.holdings) if current else ()
        candidates = self._universe_symbols or self.assets.active_symbols()[:self.config.UNIVERSE_SIZE]
        return self._resolve_symbols((*held, *self._pollable_focus(), *self.config.PRIORITY_SYMBOLS, *candidates))

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
            result = self.ledger.last_reconcile()
            self._reconciled = not self.ledger.trading_block() and bool(result is None or result.get("clean"))
            if self._engine_resumed and self._reconciled:
                self._normalised = self.engine.normalised
            self._last_reconcile_at = _now_utc()
            self._provider_health("alpaca", "broker", True, None)
        except Exception as exc:
            self._reconciled = False
            self._provider_health("alpaca", "broker", False, type(exc).__name__)
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
        boot = prepare_ledger(self.ledger, self.config.DATA_DIR, options_importer=import_legacy_options)
        self._needs_rebuild = boot.needs_rebuild
        if isinstance(self.health, ProviderHealthRegistry):
            self.health.load_state()
        if isinstance(self.assets, AssetRegistry):
            self.assets.load_state()
        if isinstance(self.news, NewsService):
            self.news.load_state()
        if isinstance(self.macro, MacroMonitor):
            self.macro.load_state()
        if isinstance(self.options, OptionsManager):
            self.options.load_state()
        if not boot.health.ok:
            await self._alert("critical", "State needs broker rebuild",
                              "The previous state was quarantined; new entries stay blocked until recovery.")
        self.ledger.mark_submitting_ambiguous()
        await self.calendar.refresh()
        # Resolve aliases for yesterday's book before reconcile can archive an old ticker.
        if not self._shutdown and self.ledger.all_open_meta():
            try:
                async with asyncio.timeout(8):
                    await self._refresh_assets()
            except Exception as exc:
                await self._alert("warning", "Startup asset refresh pending", type(exc).__name__,
                                  key="assets-startup")
        if not self._shutdown and self.engine is not None:
            try:
                # Bind lost HTTP replies to broker order IDs before a reconcile
                # can record one of our own fills as an unexplained import.
                await self._resume_execution()
            except TimeoutError:
                await self._alert("warning", "Order recovery is still pending",
                                  "The scheduler will keep retrying; new entries are blocked.",
                                  key="order-resume")
        if not self._shutdown and self._engine_resumed:
            await self._reconcile_scheduler("startup")
        if not self._shutdown and self._last_reconcile_at is not None and isinstance(self.options, OptionsManager):
            try:
                async with asyncio.timeout(8):
                    await self.options.reconcile(_now_utc())
            except Exception as exc:
                await self._alert("warning", "Option startup reconcile pending", type(exc).__name__,
                                  key="options-startup")
        if self._shutdown:
            return
        self._state_ready = True
        self._publish_runtime()
        startup_at = _now_et()
        self._schedule_job("assets_startup", startup_at.isoformat(), self._refresh_assets, timeout=30)
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
                    self._schedule_notifications()
                except Exception as exc:
                    logger.error("Scheduler cycle failed: %s", type(exc).__name__)
                    await self._report_exception("scheduler tick", exc)
                if not self._shutdown:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=TICK_SECONDS)
                    except TimeoutError:
                        pass
        except Exception as exc:
            await self._report_exception("scheduler startup", exc, critical=True)
            raise
        finally:
            await self._cleanup()

    def _schedule_notifications(self) -> None:
        if self._telemetry_task is not None:
            if not self._telemetry_task.done():
                return
            if not self._telemetry_task.cancelled() and self._telemetry_task.exception() is not None:
                logger.warning("Notification lane exited unexpectedly")
        self._telemetry_task = asyncio.create_task(self._notification_tick(), name="stock-agent:notifications")

    async def _notification_tick(self) -> None:
        if isinstance(self.runtime, RuntimeStatus) and isinstance(self.alerts, AlertService):
            event = self.runtime.deployment_event()
            if event and event["at"] != self.store.kv_get("deploy.last_notified"):
                if self.alerts.alert_deploy_event(
                    event["event"], self.runtime.sha, reason=str(event.get("reason") or ""),
                    rollback=event["event"] == "rollback",
                ):
                    self.store.kv_set("deploy.last_notified", event["at"])
        try:
            async with asyncio.timeout(6):
                note = "broker sync healthy" if self._reconciled else "broker sync needs attention"
                await self.heartbeat.tick(self.alerts, note=note)
        except Exception as exc:
            logger.warning("Heartbeat did not complete: %s", type(exc).__name__)
        try:
            async with asyncio.timeout(8):
                await self.alerts.flush(max_budget_sec=7)
        except Exception as exc:
            logger.warning("Alert delivery deferred: %s", type(exc).__name__)

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
        if not self._engine_resumed and self._last_reconcile_at is None:
            try:
                await self._resume_execution()
            except TimeoutError:
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
                self._schedule_job("options", day_key, self._run_options_job, timeout=180)
            if self.options is not None:
                await self._drive_options_monitor(now_et)

        # Re-read the clock after the protection lane and schedule by actual session.
        now_et = _now_et()
        day_key = now_et.date().isoformat()
        jobs = (
            ("assets", self._should_refresh_assets, self._refresh_session_assets, 90),
            ("macro", self._should_run_macro_check, self._run_macro_check, 120),
            ("earnings", self._should_run_earnings_check, self._run_earnings_reactive, 900),
            ("prescan", self._should_run_daily_prescan, self._run_daily_prescan, 900),
            ("morning_brief", self._should_send_morning_brief, self._send_morning_brief, 90),
            ("drawdown_close", self._should_send_daily_summary, self._close_drawdown, 55),
            ("daily_summary", self._summary_ready, self._send_daily_summary, 120),
            ("shadow_close", self._should_run_shadow_close, self._run_shadow_close, 240),
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
        now = _now_et()
        market = await self.calendar.refresh()
        session = self.calendar.session_on(now.date())
        if (market.source is not MarketSource.LIVE or market.status is not MarketStatus.CLOSED
                or session is None or now < session.closes_at):
            return JobOutcome(JobStatus.FAILED, detail={"reason": "close_clock_unverified"})
        previous = self.calendar.previous_session(session.date)
        if previous is None:
            return JobOutcome(JobStatus.FAILED, detail={"reason": "previous_session_unverified"})
        snapshot = await self.ledger.refresh("drawdown close")
        if (snapshot.quality != "live" or snapshot.equity <= 0 or snapshot.as_of < session.closes_at
                or abs(_now_utc() - snapshot.as_of) > timedelta(seconds=60)
                or self.ledger.trading_block()):
            return JobOutcome(JobStatus.FAILED, detail={"reason": "close_equity_unverified"})
        marks = self.ledger.equity_marks("close", since=session.date)
        mark = next((item for item in marks if item.session_date == session.date), None)
        if mark is None:
            self._last_close_report = await close_session(self.ledger, snapshot, session.date)
        try:
            state = self._drawdown_state()
        except ValueError:
            await self._alert("critical", "Invalid persisted drawdown",
                              "Existing high-water record retained; entries stay blocked.",
                              key="invalid-drawdown")
            return JobOutcome(JobStatus.FAILED, detail={"reason": "drawdown_record_invalid"})
        try:
            day = session.date.isoformat()
            net_flow = Decimal(0)
            if state.last_session is not None and state.last_session < day:
                last_day = date.fromisoformat(state.last_session)
                net_flow = sum(
                    (flow.amount for flow in self.ledger.cash_flows()
                     if flow.kind in DEPOSIT_KINDS and last_day < flow.date <= session.date),
                    Decimal(0),
                )
            updated = update_drawdown(
                state, day, mark.equity if mark else snapshot.equity,
                previous_session=previous.date.isoformat(), net_external_flow=net_flow, config=self.config,
            )
        except (ValueError, InvalidOperation):
            await self._alert("critical", "Drawdown close could not be verified",
                              "External cash flows could not be reconciled to the close. "
                              "The existing high-water record was retained.",
                              key=f"invalid-drawdown-close:{session.date}")
            return JobOutcome(JobStatus.FAILED, detail={"reason": "drawdown_close_invalid"})
        risk_result = await self._evaluate_close_limits(snapshot, session.date, previous.date)
        if updated == state:
            return risk_result
        self.ledger.risk_state_set(DRAWDOWN_STATE_KEY, updated.to_record())
        before = (state.should_alert(self.config), state.is_throttled(self.config), state.paused)
        alert, throttled = updated.should_alert(self.config), updated.is_throttled(self.config)
        if before != (alert, throttled, updated.paused):
            await self._alert(
                "critical" if updated.paused else "warning" if alert else "info",
                "Drawdown controls changed",
                f"Drawdown: {updated.drawdown:.1%}; throttled: {throttled}; paused: {updated.paused}.",
                key=f"drawdown:{session.date}",
            )
        return risk_result

    async def _evaluate_close_limits(self, snapshot, session_date: date, previous_session: date):
        controls = self._risk_controls()
        breaches = self.risk.evaluate_portfolio(snapshot, controls=controls, include_pending=False)
        if any(breach.kind == "invalid_risk_input" for breach in breaches):
            return JobOutcome(JobStatus.FAILED, detail={"reason": "close_risk_input_unverified"})
        key = "a6.close_drift"
        state = self.ledger.risk_state_get(key, {})
        counts = {(kind, scope): n for kind, scope, n in state.get("counts", ())}
        day = session_date.isoformat()
        if state.get("session") != day:
            previous = counts if state.get("session") == previous_session.isoformat() else {}
            counts = self.risk.advance_drift_counters(breaches, previous)
            self.ledger.risk_state_set(key, {"session": day, "counts": [
                [kind, scope, n] for (kind, scope), n in sorted(counts.items())
            ]})
        if breaches:
            await self._alert("warning", "Portfolio at or beyond risk limits", "\n".join(
                f"{b.kind} {b.key}: {b.value} (limit {b.cap})" for b in breaches
            ), key=f"risk-limit-close:{day}")
        plans = self.risk.plan_remediation(snapshot, consecutive_closes=counts, controls=controls)
        if any(plan.kind is TradeKind.SELL for plan in plans):
            following = self.calendar.next_session(session_date)
            if following is None:
                return JobOutcome(JobStatus.FAILED, detail={"reason": "trim_session_unverified"})
            intents = self._trim_intents(
                snapshot, plans, "limit_drift", day, following,
                consecutive_closes=[[kind, scope, n] for (kind, scope), n in sorted(counts.items())],
            )
            return JobOutcome(JobStatus.DONE, intents=intents)
        return None

    def _trim_intents(self, snapshot, plans, source: str, event: str, session, **proof) -> tuple[NewIntent, ...]:
        intents = []
        for plan in plans:
            if plan.kind is not TradeKind.SELL or not (holding := snapshot.holding(plan.symbol)):
                continue
            intents.append(NewIntent(
                symbol=plan.symbol, side="sell", kind="trim", origin=source,
                dedupe_key=f"a6:{source}:{event}:{plan.symbol}",
                position_id=holding.meta.position_id if holding.meta else None,
                reference_price=plan.price,
                session_date=session.date, next_attempt_at=session.opens_at + timedelta(minutes=5),
                expires_at=session.closes_at,
                meta={"rule": "assignment" if source == "option_delivery" else "risk_limit",
                      "qty": str(plan.qty), "reason": plan.reason, **proof},
            ))
        return tuple(intents)

    async def _queue_delivery_trims(self, now: datetime) -> None:
        session = self.calendar.session_on(now.date())
        if (session is None or now < session.opens_at + timedelta(minutes=5)
                or now >= session.closes_at or not (await self.market_gate("exit", rule="assignment")).allowed):
            return
        previous = self.calendar.previous_session(session.date)
        if previous is None:
            return
        deliveries = self.store.query(
            "SELECT activity_id, symbol, ts FROM broker_activities WHERE activity_type='OPTRD'"
            " AND qty>0 AND LOWER(COALESCE(side,'')) NOT LIKE 'sell%'"
            " AND ts>=? ORDER BY ts, activity_id", (previous.date.isoformat(),),
        )
        if not deliveries:
            return
        key = "a6.delivery_processed"
        processed = self.ledger.risk_state_get(key, {})
        retained = {row["activity_id"]: processed[row["activity_id"]] for row in deliveries
                    if row["activity_id"] in processed}
        pending = [row for row in deliveries if row["activity_id"] not in retained]
        if not pending:
            return
        snapshot = await self.ledger.refresh("option delivery risk check")
        if snapshot.quality != "live" or self.ledger.trading_block() or snapshot.equity <= 0:
            return
        controls = self._risk_controls()
        if any(b.kind == "invalid_risk_input" for b in self.risk.evaluate_portfolio(
            snapshot, controls=controls, include_pending=False,
        )):
            return
        for row in pending:
            activity, symbol, ts = row["activity_id"], row["symbol"], row["ts"]
            try:
                delivered_at = datetime.fromisoformat(ts) if "T" in ts else None
                delivered_day = (delivered_at.astimezone(ET).date() if delivered_at and delivered_at.tzinfo
                                 else date.fromisoformat(ts[:10]))
            except ValueError:
                continue
            if delivered_day > session.date or (delivered_at and delivered_at.tzinfo
                                                  and delivered_at >= session.opens_at):
                continue
            if symbol and (holding := snapshot.holding(symbol)) and holding.qty > 0:
                plans = self.risk.plan_remediation(snapshot, broker_action=True, broker_symbol=symbol,
                                                   controls=controls)
                intents = self._trim_intents(snapshot, plans, "option_delivery", session.date.isoformat(), session,
                                            delivery_symbol=symbol)
                if intents:
                    self.ledger.enqueue_intents(intents)
                    await self._alert("warning", f"Option delivery triggered a trim for {symbol}",
                                      "Queued for execution against the reconciled book.",
                                      key=f"option-delivery:{activity}")
            retained[activity] = ts
        if retained != processed:
            self.ledger.risk_state_set(key, retained)

    def _summary_ready(self, now: datetime) -> bool:
        if not self._should_send_daily_summary(now):
            return False
        close = self.ledger.job_status("drawdown_close", now.date().isoformat())
        return bool(close and (close.status is JobStatus.DONE or (
            close.status is JobStatus.FAILED and close.attempts >= MAX_JOB_ATTEMPTS
        )))

    def _should_run_shadow_close(self, now: datetime) -> bool:
        session = self.calendar.session_on(now.date())
        return bool(session and now >= session.closes_at + timedelta(minutes=16))

    def _should_refresh_assets(self, now: datetime) -> bool:
        session = self.calendar.session_on(now.date())
        return bool(session and session.opens_at - timedelta(hours=6) <= now < session.closes_at)

    async def _refresh_session_assets(self):
        return await self._refresh_assets(force=True)

    async def _run_shadow_close(self):
        day = _now_et().date()
        snapshot = self.ledger.last_snapshot()
        if snapshot is None or not any(m.session_date == day for m in self.ledger.equity_marks("close", since=day)):
            return JobOutcome(JobStatus.FAILED, detail={"reason": "trusted_close_pending"})
        report = await close_session(self.ledger, snapshot, day, daily_bars=self._shadow_bars)
        self._last_close_report = report
        missing = sum(w.startswith("shadow stop") for w in report.warnings)
        return JobOutcome(JobStatus.FAILED if missing else JobStatus.DONE,
                          detail={"shadow_rows": report.shadow_rows, "warnings": missing})

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
                if isinstance(self.alerts, AlertService):
                    self.alerts.alert_skipped_cycle(name, "interrupted; replay would risk a duplicate notification")
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
            if status is JobStatus.SKIPPED and isinstance(self.alerts, AlertService):
                self.alerts.alert_skipped_cycle(name, str(detail.get("reason") or "schedule gate declined"))
            elif status is JobStatus.FAILED:
                await self._alert("warning", f"{name} {status.value}", "See the job journal for details.",
                                  key=f"job-result:{name}")
        except Exception as exc:
            self.ledger.finish_job(name, window_key, JobStatus.FAILED,
                                   detail={"error": type(exc).__name__},
                                   eligible_retry_at=_now_utc() + timedelta(minutes=5 * attempt))
            await self._report_exception(name, exc)

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

    async def _run_options_job(self):
        outcomes = await self._run_options_scan()
        if outcomes and not any(getattr(outcome, "final", True) for outcome in outcomes):
            return JobOutcome(JobStatus.FAILED, detail={"reason": "option_scan_temporarily_blocked"})
        return None

    async def _drive_options_monitor(self, now: datetime):
        if self._options_task is not None:
            if not self._options_task.done():
                return
            if not self._options_task.cancelled() and self._options_task.exception() is not None:
                await self._alert("warning", "Option protection did not complete",
                                  type(self._options_task.exception()).__name__, key="options-monitor")
        if "options" not in self._job_tasks and not self._shutdown:
            self._options_task = asyncio.create_task(
                self._run_options_lane(now), name="stock-agent:options-protection",
            )
        else:
            self._options_task = None

    async def _run_options_lane(self, now: datetime):
        await self._check_options_positions()
        if not self._entries_enabled():
            return
        ideas = [OptionIdea.from_payload(row) for row in self.research_store.pending_option_ideas(now)]
        if not ideas:
            return
        if not (await self.market_gate("entry")).allowed:
            return
        candidates = [OptionCandidate(idea.symbol, source="news", story_id=idea.story_id,
                                      direction_hint=idea.direction.value) for idea in ideas]
        await self.options.scan(_now_utc(), candidates=candidates)
        handled = self.options.handled_story_ids(idea.story_id for idea in ideas)
        if handled:
            self.research_store.acknowledge_options(tuple(sorted(handled)))

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
        try:
            async with asyncio.timeout(8):
                await self._queue_delivery_trims(_now_et())
        except Exception as exc:
            await self._alert("warning", "Option delivery risk check did not complete",
                              type(exc).__name__, key="option-delivery-check")
        await self._run_market_monitoring()
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

    # ── Market monitoring ────────────────────────────────────────────

    async def _run_market_monitoring(self):
        """Broker safety and queued orders: engine.monitor_tick only (clean-up,
        resolve accepted orders, cover any short, one broker stop per long,
        queued exits then entries). No software stop sell, no legacy APIs; the
        research hook is dispatched separately by the execution cycle."""
        self._last_monitor_time = _now_et()
        if self.engine is None:
            logger.error("Execution engine not wired; no monitoring")
            return None
        return await self.engine.monitor_tick()

    def _should_check_events(self) -> bool:
        now = _now_et()
        return self._is_market_open(now) and (
            self._last_news_at is None or now.timestamp() - self._last_news_at.timestamp() >= NEWS_SECONDS
        )

    # ── Daily summary ────────────────────────────────────────────────

    async def _send_daily_summary(self):
        """End-of-day summary (plus weekly scorecard on the last exchange session of the week)."""
        try:
            from stock_agent.reporting import ReportBuilder

            now = _now_et()
            self._daily_summary_sent_today = now.strftime("%Y-%m-%d")
            snap = self.ledger.last_snapshot() if self.ledger is not None else None
            if snap is None and self.ledger is not None:
                try:
                    snap = await self.ledger.refresh("daily_summary")
                except Exception as exc:
                    logger.warning("Daily summary refresh fallback failed: %s", exc)

            builder = ReportBuilder(
                self.config,
                ledger=self.ledger,
                price_service=getattr(self, "prices", None),
                health_registry=getattr(self, "health", None),
                options_store=getattr(self, "options_store", None),
                calendar=getattr(self, "calendar", None),
                research_store=getattr(self, "research_store", None),
                clock=_now_utc,
            )
            embeds = await builder.build_daily_summary(
                snapshot=snap,
                close_report=getattr(self, "_last_close_report", None),
                as_of=now,
            )

            if self.alerts is not None:
                self.alerts.alert(
                    "info",
                    f"Daily Summary — {now.strftime('%Y-%m-%d')}",
                    "",
                    key=f"daily_summary:{now.strftime('%Y-%m-%d')}",
                    channel_key="daily_summary",
                    embeds=embeds,
                )
            elif self.discord is not None:
                ch = self.discord.channels.get("daily_summary") or self.discord.channels.get("announcements", "")
                if ch:
                    await self.discord._send_multi_embed(ch, embeds)

            # Emit the weekly paper-test scorecard on Friday (or if Friday is a holiday, Thursday)
            is_week_end = now.weekday() == 4
            if not is_week_end and now.weekday() == 3 and getattr(self, "calendar", None) is not None:
                try:
                    is_week_end = self.calendar.session_on(now.date() + timedelta(days=1)) is None
                except Exception:
                    is_week_end = False
            if is_week_end:
                wk_embeds, _ = await builder.build_weekly_scorecard(snapshot=snap, as_of=now)
                if self.alerts is not None:
                    self.alerts.alert(
                        "info",
                        f"Weekly Scorecard — {now.strftime('%Y-%m-%d')}",
                        "",
                        key=f"weekly_scorecard:{now.strftime('%Y-%W')}",
                        channel_key="daily_summary",
                        embeds=wk_embeds,
                    )
                elif self.discord is not None:
                    ch = self.discord.channels.get("daily_summary") or self.discord.channels.get("announcements", "")
                    if ch:
                        await self.discord._send_multi_embed(ch, wk_embeds)

            if self.alerts is not None:
                await self.alerts.flush()
            logger.info("Daily summary sent (%d embeds)", len(embeds))
        except Exception as e:
            logger.exception("Daily summary failed: %s", e)
            if self.alerts is not None:
                self.alerts.alert_unhandled_exception("daily_summary", e)
                await self.alerts.flush()

    async def _run_options_scan(self):
        """Once-per-session options entry scan via OptionsManager.

        The manager owns everything else: mode, market gate, research gate,
        risk, sizing, the write-ahead book and confirmed orders. Without a
        manager no options orders are placed (the legacy path is not used).
        Returns the manager's ScanOutcomes; exceptions propagate to the caller,
        which records the job status and retries.
        """
        now = _now_utc()
        if self.options is None:
            logger.warning("Options scan skipped: no OptionsManager wired")
            return []
        outcomes = await self.options.scan(now)
        self._options_scan_done_today = now.astimezone(ET).strftime("%Y-%m-%d")
        opened = [o for o in outcomes if getattr(o, "opened", None)]
        logger.info("Options scan: %d candidates, %d opened", len(outcomes), len(opened))
        return outcomes

    async def _check_options_positions(self):
        """Exit manager and repair pass: reconcile with the broker, then apply
        exits (T-2 time exit, targets, thesis break, ex-dividend) and repairs.
        Returns the MonitorOutcome; exceptions propagate to the caller."""
        if self.options is None:
            return None
        return await self.options.monitor(_now_utc())

    async def _check_for_market_event(self):
        """A healthy regime change can alert, but cannot open a trade job."""
        research = getattr(self, "research", None)
        if research is None:
            return
        change = await research.observe_regime()
        if change is not None:
            before, after = change
            await self._alert(
                "warning", "Market regime changed", f"{before} to {after}; no trade triggered",
                key=f"market_regime_{before}_{after}",
            )

    # ── Morning Brief ────────────────────────────────────────────────

    async def _send_morning_brief(self):
        """Honest morning brief posted to #announcements from reconciled state."""
        try:
            from stock_agent.reporting import ReportBuilder

            now = _now_et()
            self._morning_brief_done_today = now.strftime("%Y-%m-%d")
            snap = self.ledger.last_snapshot() if self.ledger is not None else None
            if snap is None and self.ledger is not None:
                try:
                    snap = await self.ledger.refresh("morning_brief")
                except Exception as exc:
                    logger.warning("Morning brief refresh fallback failed: %s", exc)

            macro_embed: dict | None = None
            macro_svc = getattr(self, "macro", None)
            if macro_svc is not None:
                try:
                    macro_snap = macro_svc.get_last_snapshot()
                    if macro_snap is None:
                        macro_snap = await macro_svc.check_conditions()
                    if macro_snap is not None:
                        macro_embed = macro_svc.format_discord_brief(macro_snap)
                        macro_embed["title"] = "\U0001f30d Market Conditions"
                except Exception as exc:
                    logger.warning("Could not fetch macro data for morning brief: %s", exc)

            builder = ReportBuilder(
                self.config,
                ledger=self.ledger,
                price_service=getattr(self, "prices", None),
                health_registry=getattr(self, "health", None),
                options_store=getattr(self, "options_store", None),
                calendar=getattr(self, "calendar", None),
                research_store=getattr(self, "research_store", None),
                clock=_now_utc,
            )
            embeds = await builder.build_morning_brief(
                snapshot=snap,
                as_of=now,
                macro_embed=macro_embed,
            )

            if self.alerts is not None:
                self.alerts.alert(
                    "info",
                    f"Morning Brief — {now.strftime('%Y-%m-%d')}",
                    "",
                    key=f"morning_brief:{now.strftime('%Y-%m-%d')}",
                    channel_key="announcements",
                    embeds=embeds,
                )
                await self.alerts.flush()
            elif self.discord is not None:
                ch = self.discord.channels.get("announcements", "")
                if ch:
                    await self.discord._send_multi_embed(ch, embeds)

            logger.info("Morning brief sent — %d embeds", len(embeds))
        except Exception as e:
            logger.exception("Morning brief failed: %s", e)
            if self.alerts is not None:
                self.alerts.alert_unhandled_exception("morning_brief", e)
                await self.alerts.flush()

    # ── Cleanup ──────────────────────────────────────────────────────

    async def _cleanup(self):
        """Cut off new work, drain order paths, then release network and database resources."""
        self.shutdown()
        notify_systemd("STOPPING=1\nSTATUS=Draining trading tasks")
        passive = [task for name, task in self._job_tasks.items() if name != "options"]
        if self._telemetry_task is not None:
            passive.append(self._telemetry_task)
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
        dynamic = ("telegram", "discord", "macro", "alerts", "prices", "news", "llm", "broker", "http", "heartbeat")
        for name in dynamic:
            resource = getattr(self, name, None)
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if close is not None:
                try:
                    async with asyncio.timeout(1):
                        await close()
                except Exception:
                    logger.warning("Service resource did not close: %s", name)
        self.store.close()
        logger.info("Cleanup complete")
