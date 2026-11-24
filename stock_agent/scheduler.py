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

from stock_agent import close_trust, market_move
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
from stock_agent.macro_monitor import MacroMonitor

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
MARKET_MOVE_SLOT_SECONDS = 30 * 60
MARKET_MOVE_STATE_KEY = "scheduler.market_move"
MAX_JOB_ATTEMPTS = 3
OPTIONS_RESCAN_STATE_KEY = "options.rescan_requests"
OPTIONS_RESCAN_QUEUED = "queued"
OPTIONS_RESCAN_ALREADY_QUEUED = "already_queued"
OPTIONS_RESCAN_OUTSIDE_WINDOW = "outside_options_window"
OPTIONS_RESCAN_NO_MANAGER = "no_options_manager"
EXECUTION_DRAIN_SECONDS = 70
SHUTDOWN_DRAIN_SECONDS = 75

DRAWDOWN_CLOSE_CAUSES = {
    "close_clock_unverified": "The broker clock did not confirm the scheduled session had closed",
    "close_equity_unverified": "The broker account equity could not be verified",
    "close_snapshot_nonpositive": "The broker account equity was not positive",
    "close_snapshot_preclose": "The account snapshot was taken before the scheduled close",
    "close_snapshot_stale": "The account snapshot was not fresh enough",
    "close_trading_block": "A ledger trading block was still in force",
    "close_unpriced_stock": "One or more stock prices could not be verified",
    "close_option_marks_crossed": "The broker reported inconsistent prices for the legs of an option spread",
    "close_option_underlying_unpriced": "A reliable underlying price was unavailable for an unpriced option",
    "close_option_mark_inconsistent": "The broker reported a value for an option whose price could not be verified",
    "close_option_near_money": "A missing short-option price was too close to its strike to value safely",
    "close_option_pairing_ambiguous": "An unpriced option leg could not be paired with a single spread partner",
    "close_option_credit_vertical": "An unpriced or crossed leg belongs to a credit spread, which cannot be bounded safely",
    "close_option_mark_missing": "The broker did not report a market value needed to value an option position",
    "close_option_unparseable": "An option position symbol could not be parsed",
    "close_option_adjusted_contract": "An unpriced short option may have a non-standard contract multiplier",
    "close_haircut_nonpositive": "Account equity was not positive after the conservative option adjustment",
    "close_snapshot_quality_unknown": "The account snapshot had an unrecognised quality label",
    "close_snapshot_indicative": "The snapshot was indicative without a verified option-only cause",
    "close_mark_not_recorded": "The account equity mark did not persist",
    "previous_session_unverified": "The previous trading session could not be confirmed",
    "drawdown_record_invalid": "The saved drawdown record could not be read or validated",
    "drawdown_close_invalid": "The close or its external cash flows could not be used to update drawdown",
    "close_risk_input_unverified": "A separate closing risk check did not have usable inputs",
}

DRAWDOWN_CLOSE_ERRORS = {
    "TimeoutError": "The closing check timed out",
    "ReconcileError": "The broker account could not be reconciled",
    "StateUnavailable": "The required local risk state could not be read",
    "OperationalError": "The local database was unavailable during the closing check",
}


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
        self.market_move_vix = getattr(services, "market_move_vix", None)
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
            bars=self._stop_bars, controls=self._risk_controls, margin_controls=self._margin_controls,
            market_gate=self.market_gate, config=self.config, now=_now_utc,
            capital_lock=self.capital_lock, research_gate=self.research_gate,
            research_refresh=getattr(self.research_sources, "refresh_entry_inputs", None), notifier=self.alerts,
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
        self._missed_close_notices: set[str] = set()
        self._pending_ledger_alerts: set[asyncio.Task] = set()
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
        self._macro_check_done_today: str | None = None
        self._morning_brief_done_today: str | None = None
        self._options_scan_done_today: str | None = None
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
                    isinstance(self.alerts, AlertService) and key
                    and (key == "normalise-plan" or key.startswith("market-move-rescan:"))
                ) else {}
                if isinstance(self.alerts, AlertService) and key and key.startswith("trusted-close-missed:"):
                    routing["dedupe_window_sec"] = 48 * 3600
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
        return not self._entry_safety_locks()

    def _entry_safety_locks(self) -> dict[str, tuple[str, str]]:
        if self._shutdown:
            return {"shutdown": ("service shutdown", "the service restarts and reaches readiness")}
        locks = {}
        if not self._normalised:
            locks["first_run_normalise"] = (
                "first-run account cleanup incomplete",
                "the bot finishes corrective orders, verifies broker no-shorting and reconciles cleanly",
            )
        if not self._reconciled:
            locks["broker_reconcile"] = (
                "broker reconciliation not clean",
                "a fresh read of the account, positions, orders and activities resolves the discrepancies",
            )
        if self._manually_paused():
            locks["operator_pause"] = (
                "operator entry pause",
                "every active pause-trading file is removed and any configured pause is turned off",
            )
        try:
            if block := self.ledger.trading_block():
                locks["ledger_trading_block"] = (
                    f"ledger trading block: {block}",
                    "a clean broker reconcile or rebuild clears the ledger block",
                )
            drawdown = self._drawdown_state()
            if drawdown.paused:
                locks["drawdown_pause"] = (
                    "rolling drawdown halt",
                    self._drawdown_release_condition(drawdown),
                )
        except (ValueError, StateUnavailable, OptionsError, sqlite3.Error):
            locks["risk_state_unavailable"] = (
                "required ledger or risk state unavailable",
                "the risk state is readable and valid and the broker reconciles cleanly",
            )
        return locks

    def get_entry_locks(self) -> list[dict[str, str]]:
        locks = self._entry_safety_locks()
        try:
            status = getattr(self.health, "status", None)
            news = status("news", "poll") if status is not None else None
            if news is not None and news.state in {"unhealthy", "plan_blocked", "disabled"}:
                locks["news_health"] = (
                    "required news coverage unavailable",
                    "a successful fresh news poll covers the candidate",
                )
            check_llm = getattr(self.llm, "is_healthy", None)
            if check_llm is not None and not check_llm():
                locks["llm_health"] = (
                    "no usable configured model route",
                    "at least one valid model route recovers or its cooldown ends",
                )
            snapshot = self.ledger.last_snapshot()
            flags = getattr(snapshot, "account_flags", {})
            if any(flags.get(flag) is True for flag in ("account_blocked", "trading_blocked", "trade_suspended_by_user")):
                locks["broker_account_block"] = (
                    "broker account has a trading restriction",
                    "the broker removes the restriction and a fresh account reconcile confirms it",
                )
            account_status = flags.get("status")
            if account_status and str(account_status).lower() != "active":
                locks["broker_account_inactive"] = (
                    "broker account is not active",
                    "the broker reports ACTIVE on a fresh account reconcile",
                )
            if snapshot is not None and abs(_now_utc() - snapshot.as_of) <= timedelta(seconds=120):
                margin = self.risk.margin_status(snapshot)
                target = Decimal(str(self.config.MARGIN_MIN_EXCESS_PCT))
                if margin is not None and margin.margin_call:
                    locks["margin_call"] = (
                        "broker reports a margin call",
                        "a fresh broker reconcile confirms the margin call is cleared",
                    )
                if margin is not None and margin.excess_pct < target:
                    locks["margin_buffer"] = (
                        f"margin excess {margin.excess_pct:.1%} is below the entry minimum {target:.0%}",
                        f"a fresh broker book and projected order both meet the {target:.0%} margin buffer",
                    )
                unpriced = [holding.symbol for holding in snapshot.holdings
                            if holding.qty and (not holding.price_ok or holding.price is None or holding.price <= 0)]
                if unpriced:
                    locks["unpriced_stock"] = (
                        f"stock price missing or invalid for {', '.join(unpriced)}",
                        "a fresh broker reconcile supplies a valid mark or confirms the position is closed",
                    )
        except (ValueError, StateUnavailable, OptionsError, sqlite3.Error):
            locks.setdefault("risk_state_unavailable", (
                "entry health could not be verified", "health state is readable and required inputs are healthy",
            ))
        return [{"code": code, "lock": label, "clears_when": clears} for code, (label, clears) in locks.items()]

    async def _alert_entry_locks(self) -> None:
        locks = [lock for lock in self.get_entry_locks() if lock["code"] != "shutdown"]
        if not locks:
            return
        notify = getattr(self.alerts, "alert_entries_locked", None)
        if notify is not None:
            try:
                notify(locks, session_date=_now_et().date())
                return
            except Exception:
                logger.warning("Could not queue structured entry-lock alert")
        for lock in locks:
            await self._alert("warning", f"Entries locked: {lock['lock']}",
                              f"Clears when {lock['clears_when']}.", key=f"entry-lock:{lock['code']}")

    def _risk_controls(self) -> RiskControls:
        commitments = getattr(self.options, "risk_commitments", None)
        return RiskControls(pause_new_buys=self._manually_paused(),
                            drawdown=self._drawdown_state(),
                            option_commitments=commitments() if commitments is not None else ())

    def _margin_controls(self, snapshot) -> RiskControls:
        convictions = {}
        for holding in snapshot.holdings:
            if holding.qty <= 0:
                continue
            thesis = self.ledger.current_thesis(
                holding.symbol, max_age=timedelta(hours=self.config.RESEARCH_THESIS_MAX_AGE_H),
            )
            if (thesis is None or not thesis.actionable) and holding.meta is not None:
                thesis = self.ledger.last_filled_thesis(holding.meta.position_id)
            if thesis is not None and thesis.actionable:
                grade = thesis.payload.get("effective_conviction")
                if type(grade) is int and 1 <= grade <= 10:
                    convictions[holding.symbol] = grade
        commitments = getattr(self.options, "risk_commitments", None)
        return RiskControls(
            option_commitments=commitments() if commitments is not None else (),
            holding_convictions=convictions,
        )

    async def _evaluate_margin_guard(self, snapshot, now: datetime, *, at_close: bool = False) -> None:
        try:
            await self._queue_margin_trim(snapshot, now, at_close=at_close)
        except Exception as exc:
            # Margin-state failure must not prevent stops or first-run repairs.
            await self._report_exception("Margin guard", exc, critical=True)

    async def _queue_margin_trim(self, snapshot, now: datetime, *, at_close: bool) -> None:
        # Reconciliation can mark the book indicative solely due to an
        # unrelated instrument mark. The guard uses broker account figures;
        # the planner independently excludes unpriced stock from its sells.
        if snapshot is None or abs(_now_utc() - snapshot.as_of) > timedelta(seconds=120):
            return
        status = self.risk.margin_status(snapshot)
        if status is None:
            return
        key = "a13.margin_trim_episode"
        record = self.ledger.risk_state_get(key, {})
        episode = record.get("opened_at")
        target = Decimal(str(self.config.MARGIN_MIN_EXCESS_PCT))
        if status.excess_pct >= target and not status.margin_call:
            if episode:
                self.ledger.risk_state_set(key, {})
            return
        if not episode and not status.needs_trim:
            return
        if not episode:
            episode = snapshot.as_of.isoformat()
            self.ledger.risk_state_set(key, {"opened_at": episode})

        session = self.calendar.next_session(now.date()) if at_close else self.calendar.session_on(now.date())
        queued = False
        self.ledger.expire_intents(_now_utc())
        existing = self.ledger.list_intents(origin="margin_buffer")
        latest_execution = max(
            (row.updated_at or row.created_at for row in existing if row.broker_order_id),
            default=None,
        )
        if (session is not None and not self.ledger.trading_block()
                and (latest_execution is None or snapshot.as_of > latest_execution)
                and not any(not row.terminal for row in existing)):
            plans = self.risk.plan_margin_trim(snapshot, controls=self._margin_controls(snapshot), force=True)
            for plan in plans:
                if plan.kind is not TradeKind.SELL or not (holding := snapshot.holding(plan.symbol)):
                    continue
                # One live leg at a time. With unchanged holdings, avoid
                # hammering a failed order during the same five-minute bucket.
                slot = int(max(now, session.opens_at).timestamp()) // 300
                event = f"{episode}:{session.date}:{holding.qty}:{slot}"
                intents = self._trim_intents(
                    snapshot, (plan,), "margin_buffer", event, session, eligible_at=None if at_close else now,
                    margin_episode=episode,
                )
                if not intents or any(row.dedupe_key == intents[0].dedupe_key for row in existing):
                    break
                self.ledger.enqueue_intents(intents, job="margin_guard")
                if not at_close:
                    self._last_monitor_time = None
                queued = True
                break
        minimum = f"{target:.0%}"
        context = "Broker reports a margin call. " if status.margin_call else ""
        when = "the next regular open plus five minutes" if at_close else "the current session"
        action = (f"A trim is queued for {when}, subject to revalidation." if queued else
                  "The guard will re-evaluate and queue eligible shares; any pending trim is left in place.")
        await self._alert(
            "critical", "Margin buffer breached",
            f"{context}Margin excess is {status.excess_pct:.1%} of equity. {action} "
            f"The trim episode clears at or above the required {minimum} buffer once any margin call clears.",
            key=f"margin-buffer:{episode}",
        )

    def _drawdown_state(self) -> DrawdownState:
        return DrawdownState.from_record(self.ledger.risk_state_get(DRAWDOWN_STATE_KEY), config=self.config)

    def _drawdown_release_condition(self, state: DrawdownState) -> str:
        remaining = state.pause_sessions_remaining(self.config)
        label = "close is" if remaining == 1 else "closes are"
        pct = self.config.DRAWDOWN_THROTTLE_PCT
        recovery = self.config.DRAWDOWN_RECOVERY_SESSIONS
        return (
            f"{remaining} more trusted session {label} recorded, or rolling drawdown stays below "
            f"{pct:.0%} for {recovery} consecutive trusted closes; a fresh breach at expiry can start a new pause"
        )

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
        if purpose == "entry" and (locks := self._entry_safety_locks()):
            return GateDecision(False, "; ".join(locks))
        decision = await self.calendar.market_gate(purpose, rule=rule)
        # A signal or pause can arrive while awaiting the fresh market clock.
        if purpose == "entry" and (locks := self._entry_safety_locks()):
            return GateDecision(False, "; ".join(locks))
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
        await self._alert_entry_locks()

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
        snapshot = self.ledger.last_snapshot()
        if self._is_market_open(now_et):
            await self._evaluate_margin_guard(snapshot, now_et)
        else:
            # Check the first fresh post-close broker read, without waiting for
            # report cut-off. The later close job rechecks from its own book.
            market = self.calendar.state
            session = self.calendar.session_on(now_et.date())
            if (market.source is MarketSource.LIVE and market.status is MarketStatus.CLOSED
                    and session is not None and session.closes_at <= now_et < session.closes_at + timedelta(minutes=5)
                    and snapshot is not None and snapshot.as_of >= session.closes_at):
                await self._evaluate_margin_guard(snapshot, now_et, at_close=True)
        await self._drive_order_lane(now_et)
        try:
            self._schedule_entry_handoff(now_et)
            self._schedule_research_catchup(now_et)
        except (ValueError, StateUnavailable, sqlite3.Error) as exc:
            await self._report_exception("Research catch-up", exc)
        if self._is_market_open(now_et):
            if self._shutdown:
                return
            self._schedule_market_move_sample(now_et)
            if self.research is not None and self._should_check_events():
                self._last_news_at = now_et
                slot = str(int(now_et.timestamp()) // NEWS_SECONDS)
                self._schedule_job("live_news", f"{day_key}:{slot}", self._run_news_slot, timeout=150)
                if "catchup" not in self._job_tasks:
                    self._schedule_job("market_regime", f"{day_key}:{slot}", self._check_for_market_event, timeout=90)
            if self._should_run_options_scan(now_et):
                self._schedule_job("options", day_key, self._run_options_job, timeout=180)
            self._drive_options_rescans(now_et)
            if self.options is not None:
                await self._drive_options_monitor(now_et)

        # Re-read the clock after the protection lane and schedule by actual session.
        now_et = _now_et()
        day_key = now_et.date().isoformat()
        if self._state_ready:
            self._flush_blocked_buy_overflow(now_et)
        if self._state_ready and (self._is_market_open(now_et) or self._in_preopen(
            now_et, start_minutes=180, end_minutes=0,
        )):
            await self._alert_entry_locks()
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
            if name in {"weekly", "prescan", "midweek"} and "catchup" in self._job_tasks:
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
            return JobOutcome(JobStatus.FAILED, detail={"reason": close_trust.CLOSE_CLOCK_UNVERIFIED})
        snapshot = await self.ledger.refresh("drawdown close")
        if snapshot.equity <= 0:
            return JobOutcome(JobStatus.FAILED, detail={"reason": close_trust.CLOSE_SNAPSHOT_NONPOSITIVE})
        if snapshot.as_of < session.closes_at:
            return JobOutcome(JobStatus.FAILED, detail={"reason": close_trust.CLOSE_SNAPSHOT_PRECLOSE})
        if abs(_now_utc() - snapshot.as_of) > timedelta(seconds=60):
            return JobOutcome(JobStatus.FAILED, detail={"reason": close_trust.CLOSE_SNAPSHOT_STALE})
        await self._evaluate_margin_guard(snapshot, now, at_close=True)
        if self.ledger.trading_block():
            return JobOutcome(JobStatus.FAILED, detail={"reason": close_trust.CLOSE_TRADING_BLOCK})
        marks = self.ledger.equity_marks("close", since=session.date)
        mark = next((item for item in marks if item.session_date == session.date), None)
        # On replay, report the persisted conservative fields of the recorded mark, never a refreshed value.
        trust_detail: dict = close_trust.persisted_detail(mark.extra) if mark is not None else {}
        if mark is None:
            # Close path only: a copy at conservative equity; raw snapshot quality is unchanged elsewhere.
            trust = close_trust.assess_close(snapshot, await self._underlying_session_closes(snapshot, session.date))
            if not trust.trusted:
                return JobOutcome(JobStatus.FAILED, detail=trust.detail())
            # A fully live close keeps today's outcome and mark; only a conservative close carries detail.
            trust_detail = trust.detail() if trust.conservative else {}
        previous = self.calendar.previous_session(session.date)
        if previous is None:
            return JobOutcome(JobStatus.FAILED, detail={"reason": "previous_session_unverified"})
        if mark is None:
            self._last_close_report = await close_session(
                self.ledger, trust.close_snapshot(snapshot), session.date, mark_extra=trust_detail or None)
            mark = next((item for item in self.ledger.equity_marks("close", since=session.date)
                         if item.session_date == session.date), None)
            if mark is None:
                return JobOutcome(JobStatus.FAILED, detail={**trust_detail, "reason": "close_mark_not_recorded"})
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
                state, day, mark.equity,
                previous_session=previous.date.isoformat(), net_external_flow=net_flow, config=self.config,
            )
        except (ValueError, InvalidOperation):
            await self._alert("critical", "Drawdown close could not be verified",
                              "External cash flows could not be reconciled to the close. "
                              "The existing high-water record was retained.",
                              key=f"invalid-drawdown-close:{session.date}")
            return JobOutcome(JobStatus.FAILED, detail={"reason": "drawdown_close_invalid"})
        # Commit the drawdown record before the ancillary close-limit check, so a failure there
        # cannot keep the brakes stale (drawdown state does not affect trims or portfolio checks).
        if updated != state:
            self.ledger.risk_state_set(DRAWDOWN_STATE_KEY, updated.to_record())
            before = (state.should_alert(self.config), state.is_throttled(self.config), state.paused)
            alert, throttled = updated.should_alert(self.config), updated.is_throttled(self.config)
            new_episode = updated.paused and updated.pause_start_session != state.pause_start_session
            if before != (alert, throttled, updated.paused) or new_episode:
                message = (
                    f"Rolling decision drawdown: {updated.decision_drawdown:.1%}; "
                    f"all-time drawdown: {updated.drawdown:.1%}. "
                )
                if new_episode:
                    message += "A new drawdown pause started. "
                elif state.paused and not updated.paused:
                    expired = updated.pause_sessions_governed >= self.config.DRAWDOWN_PAUSE_MAX_SESSIONS
                    why = "its session cap was reached" if expired else "the trusted-close recovery streak was met"
                    message += f"The drawdown pause lifted because {why}; other entry checks still apply. "
                if updated.paused:
                    message += f"The pause can lift when {self._drawdown_release_condition(updated)}. "
                if throttled:
                    message += "Throttled: half-size stock entries when allowed, no new borrowing or options."
                else:
                    message += "Drawdown throttle inactive."
                await self._alert(
                    "critical" if updated.paused else "warning" if alert or throttled else "info",
                    "Drawdown controls changed",
                    message,
                    key=f"drawdown:{session.date}",
                )
        risk_result = await self._evaluate_close_limits(snapshot, session.date, previous.date)
        if trust_detail:
            if risk_result is None:
                risk_result = JobOutcome(JobStatus.DONE, detail=dict(trust_detail))
            else:
                risk_result = JobOutcome(risk_result.status, risk_result.intents,
                                         {**trust_detail, **dict(risk_result.detail or {})},
                                         risk_result.option_candidates)
        return risk_result

    async def _underlying_session_closes(self, snapshot, session_date: date) -> dict[str, Decimal]:
        """Official session closes for unpriced short legs' underlyings that are not held."""
        needed = close_trust.short_leg_underlyings(snapshot)
        getter = getattr(self.prices, "get_daily_bars", None)
        if not needed or getter is None:
            return {}
        try:
            bars = await getter(list(needed), start_date=session_date, end_date=session_date)
        except Exception:
            logger.warning("Session closes for option underlyings unavailable", exc_info=True)
            return {}
        return {symbol: bar.close for symbol, rows in bars.items() for bar in rows
                if bar.session_date == session_date}

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

    def _trim_intents(
        self, snapshot, plans, source: str, event: str, session, *, eligible_at=None, **proof,
    ) -> tuple[NewIntent, ...]:
        intents = []
        for plan in plans:
            if plan.kind is not TradeKind.SELL or not (holding := snapshot.holding(plan.symbol)):
                continue
            intents.append(NewIntent(
                symbol=plan.symbol, side="sell", kind="trim", origin=source,
                dedupe_key=f"{'a13' if source == 'margin_buffer' else 'a6'}:{source}:{event}:{plan.symbol}",
                position_id=holding.meta.position_id if holding.meta else None,
                reference_price=plan.price,
                session_date=session.date, next_attempt_at=eligible_at or session.opens_at + timedelta(minutes=5),
                expires_at=session.closes_at,
                meta={"rule": {"option_delivery": "assignment", "margin_buffer": "margin_buffer"}.get(
                    source, "risk_limit"),
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
            if (name == "drawdown_close" and row.status is JobStatus.FAILED
                    and row.attempts >= MAX_JOB_ATTEMPTS and row.detail.get("missed_close_streak")
                    and window_key not in self._missed_close_notices):
                # The terminal outcome is durable before alert dispatch. If a process
                # stopped between them, recover the notice, not the close or any orders.
                self._job_tasks[name] = asyncio.create_task(self._warn_missed_close(window_key, row.detail))
                return
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

    def _terminal_close_failure(self, window_key, detail):
        day = date.fromisoformat(window_key)
        try:
            last_applied = self._drawdown_state().last_session
        except (ValueError, StateUnavailable, sqlite3.Error):
            last_applied = None
        if last_applied == window_key:
            # The close-limit check can fail even after the drawdown record advances.
            return detail
        try:
            mark_present = any(m.session_date == day for m in self.ledger.equity_marks("close", since=day))
        except (ValueError, StateUnavailable, sqlite3.Error):
            mark_present = None
        streak = 1
        prior = self.calendar.previous_session(day)
        lower_bound = prior is None
        while prior is not None and prior.date < day:
            key = prior.date.isoformat()
            if last_applied is not None and key <= last_applied:
                break
            if last_applied is None:
                # Without a verifiable baseline, count only observed terminal
                # misses. A missing job row does not establish an earlier miss.
                try:
                    row = self.ledger.job_status("drawdown_close", key)
                except (ValueError, StateUnavailable, sqlite3.Error):
                    lower_bound = True
                    break
                if row is None or row.status is not JobStatus.FAILED or row.attempts < MAX_JOB_ATTEMPTS:
                    lower_bound = True
                    break
                saved_streak = row.detail.get("missed_close_streak")
                if type(saved_streak) is int and saved_streak > 0:
                    streak += saved_streak
                    lower_bound = bool(row.detail.get("missed_close_streak_lower_bound"))
                    break
                if row.detail.get("reason") == "close_risk_input_unverified":
                    lower_bound = True
                    break
            # When the state is readable and has a date, every later trading
            # session missed the update, including days the process did not run.
            streak += 1
            day = prior.date
            prior = self.calendar.previous_session(day)
            lower_bound = prior is None
        return {**detail, "missed_close_streak": streak, "missed_close_streak_lower_bound": lower_bound,
                "close_mark_present": mark_present}

    async def _warn_missed_close(self, window_key, detail):
        if window_key in self._missed_close_notices:
            return
        self._missed_close_notices.add(window_key)
        if detail.get("close_mark_present") is True:
            headline = "An equity mark exists, but the fall-from-peak brakes did not update."
            title = "Drawdown did not update from the close"
        elif detail.get("close_mark_present") is False:
            headline = "No trusted close was recorded today, so the fall-from-peak brakes did not update."
            title = "Trusted close not recorded"
        else:
            headline = "The bot could not confirm a trusted close or an update to the fall-from-peak brakes."
            title = "Trusted close not verified"
        cause = DRAWDOWN_CLOSE_CAUSES.get(detail.get("reason"))
        if cause is None:
            cause = DRAWDOWN_CLOSE_ERRORS.get(detail.get("error"), "The closing check could not complete")
        values = detail.get("symbols") or detail.get("unpriced_option_legs")
        names = list(dict.fromkeys(
            value for value in values if isinstance(value, str) and value.isascii()
            and 0 < len(value) <= 32 and value.replace("-", "").replace(".", "").isalnum()
        )) if isinstance(values, (list, tuple)) else []
        if names:
            cause += f" ({', '.join(names[:10])}{', plus others' if len(names) > 10 else ''})"
        count = str(detail["missed_close_streak"])
        if detail.get("missed_close_streak_lower_bound"):
            count = f"at least {count} (earlier sessions could not be verified)"
        await self._alert(
            "critical", title,
            f"{window_key}: {headline} Cause: {cause}. "
            f"Consecutive trading days with a missed drawdown update: {count}.",
            key=f"trusted-close-missed:{window_key}",
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
            if name == "drawdown_close" and status is JobStatus.FAILED and attempt >= MAX_JOB_ATTEMPTS:
                detail = self._terminal_close_failure(window_key, detail)
            retry = _now_utc() + timedelta(minutes=5 * attempt) if status is JobStatus.FAILED else None
            self.ledger.finish_job(name, window_key, status, intents=intents, detail=detail, eligible_retry_at=retry)
            if status is JobStatus.SKIPPED and isinstance(self.alerts, AlertService):
                self.alerts.alert_skipped_cycle(name, str(detail.get("reason") or "schedule gate declined"))
            elif status is JobStatus.FAILED:
                if name == "drawdown_close" and detail.get("missed_close_streak"):
                    await self._warn_missed_close(window_key, detail)
                else:
                    await self._alert("warning", f"{name} {status.value}", "See the job journal for details.",
                                      key=f"job-result:{name}")
        except Exception as exc:
            detail = {"error": type(exc).__name__}
            if name == "drawdown_close" and attempt >= MAX_JOB_ATTEMPTS:
                detail = self._terminal_close_failure(window_key, detail)
            self.ledger.finish_job(name, window_key, JobStatus.FAILED,
                                   detail=detail,
                                   eligible_retry_at=_now_utc() + timedelta(minutes=5 * attempt))
            if name == "drawdown_close" and detail.get("missed_close_streak"):
                await self._warn_missed_close(window_key, detail)
            else:
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

    # ── Market-move options re-scan hook (V1 restore, Part 4) ───────────

    def request_options_rescan(self, rescan_key: str, now_et: datetime | None = None) -> str:
        """Ask for one extra options entry scan this session (market-move rescan).

        Returns "queued", "already_queued", "outside_options_window" or "no_options_manager".
        Only persists the request, so a restart keeps it and the caller never waits on the
        options manager. The scan itself runs later as the ordinary "options" job in its own
        window (see _drive_options_rescans), with the daily scan's 180 s timeout: one options
        scan at a time, the options protection lane skipped while it runs, drained (not
        cancelled) on shutdown, never in the stock stop/order lane. Every entry rule of the
        daily scan still applies inside OptionsManager.scan: one structure per underlying,
        open-structure and max-loss budgets, cool-downs, research gate, risk check, options
        buying power, market gate. Requests never carry over to another session.
        """
        key = str(rescan_key).strip()
        if not key:
            raise ValueError("rescan_key required")
        if self.options is None:
            return OPTIONS_RESCAN_NO_MANAGER
        now_et = (now_et or _now_et()).astimezone(ET)
        if not self._options_window_open(now_et):
            return OPTIONS_RESCAN_OUTSIDE_WINDOW  # refused at both ends, never carried over (lead ruling)
        day = now_et.date().isoformat()
        state = self.ledger.risk_state_get(OPTIONS_RESCAN_STATE_KEY, {}) or {}
        keys = list(state.get("keys", ())) if state.get("session") == day else []
        if key in keys:
            return OPTIONS_RESCAN_ALREADY_QUEUED
        keys.append(key)
        self.ledger.risk_state_set(OPTIONS_RESCAN_STATE_KEY, {"session": day, "keys": keys})
        return OPTIONS_RESCAN_QUEUED

    @staticmethod
    def options_rescan_window(day: str, rescan_key: str) -> str:
        return f"{day}:rescan:{rescan_key}"

    def _options_window(self, now_et: datetime) -> tuple[datetime, datetime] | None:
        """Today's options-entry window: open + 15 min until min(15:00 ET, close - 10 min)."""
        session = self.calendar.session_on(now_et.date())
        if session is None:
            return None
        until = min(datetime.combine(now_et.date(), time(15, 0), ET), session.closes_at - timedelta(minutes=10))
        return session.opens_at + timedelta(minutes=15), until

    def _options_window_open(self, now_et: datetime) -> bool:
        window = self._options_window(now_et)
        return bool(window and self._is_market_open(now_et) and window[0] <= now_et < window[1])

    def _drive_options_rescans(self, now_et: datetime) -> None:
        """Start the next pending options re-scan when the daily scan rules allow a scan now;
        once the window has closed, journal every request that never ran as SKIPPED."""
        if self.options is None or self._shutdown:
            return
        day = now_et.date().isoformat()
        state = self.ledger.risk_state_get(OPTIONS_RESCAN_STATE_KEY, {}) or {}
        if state.get("session") != day:
            return
        pending = []
        for key in state.get("keys", ()):
            window = self.options_rescan_window(day, key)
            row = self.ledger.job_status("options", window)
            if row is not None and (row.status in {JobStatus.DONE, JobStatus.SKIPPED}
                                    or row.attempts >= MAX_JOB_ATTEMPTS):
                continue
            pending.append((window, row))
        if not pending:
            return
        window_bounds = self._options_window(now_et)
        if window_bounds is not None and now_et < window_bounds[0]:
            return  # requests are only accepted inside the window; nothing can be pending yet
        if window_bounds is None or now_et >= window_bounds[1]:
            if "options" in self._job_tasks:
                return  # never mark a re-scan while an options job is still running
            for window, row in pending:  # the window has closed for today
                if row is None:
                    self.ledger.start_job("options", window)
                self.ledger.finish_job("options", window, JobStatus.SKIPPED,
                                       detail={"reason": OPTIONS_RESCAN_OUTSIDE_WINDOW})
                logger.info("Options re-scan %s skipped: %s", window, OPTIONS_RESCAN_OUTSIDE_WINDOW)
            return
        if "options" in self._job_tasks or not self._should_run_options_scan(now_et):
            return
        window, row = pending[0]
        if row is not None and row.eligible_retry_at and row.eligible_retry_at > _now_utc():
            return  # keep order: a retrying re-scan is not overtaken by a later one
        self._schedule_job("options", window, self._run_options_job, timeout=180)

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
        candidates = [OptionCandidate(idea.symbol, source=getattr(idea, "source", "news"), story_id=idea.story_id,
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
        if (self.options is None or "catchup" in self._job_tasks
                or not self._entries_enabled() or not self._is_market_open(now_et)):
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

    def _schedule_entry_handoff(self, now: datetime) -> None:
        due = getattr(self.research, "entry_handoff_due", None)
        if self._reconciled and due is not None and due(now):
            slot = int(now.timestamp()) // 30
            self._schedule_job("entry_handoff", f"{now.date()}:{slot}",
                               self.research.run_entry_handoff, timeout=65)

    def _schedule_research_catchup(self, now: datetime) -> None:
        fresh = getattr(self.research, "has_fresh_thesis_set", None)
        if (fresh is None or not self._reconciled
                or any(name in self._job_tasks for name in ("catchup", "weekly", "midweek", "prescan"))):
            return
        in_session = self._is_market_open(now)
        if not in_session and not self._in_preopen(now, start_minutes=180, end_minutes=0):
            return
        session = self.calendar.session_on(now.date())
        if session is None or (in_session and now >= session.closes_at - timedelta(minutes=10)):
            return
        if fresh(_now_utc()):
            return
        phase = "regular" if in_session else "preopen"
        self._schedule_job("catchup", f"{session.date}:{phase}",
                           lambda: self._run_research_catchup(phase), timeout=1500)

    async def _run_research_catchup(self, phase: str):
        if phase == "regular":
            # Regime observation can only raise the floor and never places orders.
            try:
                async with asyncio.timeout(15):
                    await self._check_for_market_event()
            except Exception as exc:
                await self._report_exception("Catch-up regime observation", exc)
        return await self.research.run("weekly", work_scope=f"catchup-{phase}")

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

    def _flush_blocked_buy_overflow(self, now_et: datetime) -> None:
        """Emit session blocked-buy overflow once after close and handoff completion (or on rollover)."""
        alerts_obj = getattr(self, "alerts", None)
        flush_fn = getattr(alerts_obj, "flush_blocked_buy_overflow", None)
        if not callable(flush_fn):
            return

        today = now_et.date()
        cal = getattr(self, "calendar", None)
        session = cal.session_on(today) if cal is not None else None
        if session is not None and now_et >= session.closes_at:
            closed_cutoff = today.isoformat()
        else:
            closed_cutoff = (today - timedelta(days=1)).isoformat()

        store_obj = getattr(self, "store", None)
        last_swept = getattr(self, "_blocked_buy_swept_through", None)
        if last_swept is None:
            kv_get = getattr(store_obj, "kv_get", None)
            if callable(kv_get):
                try:
                    raw_swept = kv_get("alerts.blocked_buy_swept_through")
                except Exception as exc:
                    logger.debug("Could not read blocked-buy sweep marker (%s)", type(exc).__name__)
                    return
            else:
                raw_swept = ""
            last_swept = str(raw_swept or "")[:10]
            self._blocked_buy_swept_through = last_swept

        blocked_map = getattr(alerts_obj, "blocked_buys", None) or {}
        needs_overflow_flush = any(
            int(st.get("overflow_count", 0) or 0) > 0 and not bool(st.get("overflow_emitted", False))
            for day_k, st in blocked_map.items()
            if isinstance(st, dict) and str(day_k)[:10] <= closed_cutoff
        )
        needs_ledger_sweep = bool(closed_cutoff > last_swept)
        if not needs_ledger_sweep and not needs_overflow_flush:
            return

        # Defer both current-close and prior-rollover flushes until any running or due entry_handoff finishes.
        job_tasks = getattr(self, "_job_tasks", None) or {}
        if "entry_handoff" in job_tasks:
            return
        research_obj = getattr(self, "research", None)
        due_fn = getattr(research_obj, "entry_handoff_due", None)
        if callable(due_fn):
            try:
                if due_fn(_now_utc()):
                    return
            except Exception:
                return

        if needs_ledger_sweep:
            if not StockAgentScheduler._sweep_closed_expired_buys(
                self,
                today=today,
                last_swept=last_swept,
                closed_cutoff=closed_cutoff,
            ):
                return
            self._blocked_buy_swept_through = closed_cutoff
            kv_set = getattr(store_obj, "kv_set", None)
            if callable(kv_set):
                try:
                    kv_set("alerts.blocked_buy_swept_through", closed_cutoff)
                except Exception as exc:
                    logger.debug("Could not persist blocked-buy sweep marker (%s)", type(exc).__name__)

        try:
            next_after_closed = (date.fromisoformat(closed_cutoff) + timedelta(days=1)).isoformat()
            flush_fn(before_session=next_after_closed)
        except Exception as exc:
            logger.debug("Session blocked-buy overflow flush skipped (%s)", type(exc).__name__)

    def _sweep_closed_expired_buys(self, *, today: date, last_swept: str, closed_cutoff: str) -> bool:
        alerts_obj = getattr(self, "alerts", None)
        alert_blocked_fn = getattr(alerts_obj, "alert_blocked_buy", None)
        ledger_obj = getattr(self, "ledger", None)
        if not callable(alert_blocked_fn) or ledger_obj is None:
            return True

        retention_floor = (today - timedelta(days=14)).isoformat()
        if last_swept and len(last_swept) == 10:
            try:
                after_last = (date.fromisoformat(last_swept) + timedelta(days=1)).isoformat()
                min_day = max(retention_floor, after_last)
            except ValueError:
                min_day = retention_floor
        else:
            min_day = retention_floor
        if min_day > closed_cutoff:
            return True

        try:
            expire_fn = getattr(ledger_obj, "expire_intents", None)
            if callable(expire_fn):
                expire_fn(_now_utc())

            ledger_store = getattr(ledger_obj, "store", None)
            query_fn = getattr(ledger_store, "query", None)
            if callable(query_fn):
                rows = query_fn(
                    "SELECT symbol, session_date, last_error FROM order_intents "
                    "WHERE status='expired' AND side='buy' AND kind != 'cover' "
                    "AND COALESCE(job, '') != 'open_handoff' "
                    "AND session_date >= ? AND session_date <= ? "
                    "ORDER BY created_at",
                    (min_day, closed_cutoff),
                )
                for r in rows:
                    alert_blocked_fn(
                        str(r["symbol"]),
                        "intent_expired",
                        str(r["last_error"] or ""),
                        session_date=str(r["session_date"])[:10],
                    )
                return True

            list_fn = getattr(ledger_obj, "list_intents", None)
            if callable(list_fn):
                for it in list_fn(["expired"]):
                    side = str(getattr(it.side, "value", it.side) or "").lower()
                    if side != "buy" or getattr(it, "job", None) == "open_handoff":
                        continue
                    it_day = str(it.session_date)[:10] if getattr(it, "session_date", None) else ""
                    if min_day <= it_day <= closed_cutoff:
                        alert_blocked_fn(
                            it.symbol,
                            "intent_expired",
                            str(getattr(it, "last_error", "") or ""),
                            session_date=it_day,
                        )
            return True
        except Exception as exc:
            logger.debug("Expired buy alert sweep skipped (%s)", type(exc).__name__)
            return False

    async def _send_daily_summary(self):
        """End-of-day summary (plus weekly scorecard on the last exchange session of the week)."""
        try:
            from stock_agent.reporting import ReportBuilder, _safe_report_reason

            now = _now_et()
            day_key = now.strftime("%Y-%m-%d")
            self._daily_summary_sent_today = day_key
            snap = self.ledger.last_snapshot() if self.ledger is not None else None
            snap_stale = (
                snap is None
                or snap.as_of.astimezone(ET).date() != now.date()
                or (now - snap.as_of.astimezone(ET)) > timedelta(minutes=15)
            )
            if snap_stale and self.ledger is not None and hasattr(self.ledger, "refresh"):
                try:
                    snap = await self.ledger.refresh("daily_summary")
                except Exception as exc:
                    logger.warning("Daily summary refresh fallback failed (%s)", type(exc).__name__)

            raw_cr = getattr(self, "_last_close_report", None)
            close_report = raw_cr if getattr(raw_cr, "session_date", None) == now.date() else None
            entry_locks = None
            lock_fn = getattr(self, "get_entry_locks", None)
            if callable(lock_fn):
                try:
                    entry_locks = lock_fn()
                except Exception as exc:
                    logger.warning("Could not read scheduler entry locks (%s)", type(exc).__name__)

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

            daily_status = "skipped"
            try:
                embeds = await builder.build_daily_summary(
                    snapshot=snap,
                    close_report=close_report,
                    as_of=now,
                    entry_locks=entry_locks,
                )
                if self.alerts is not None:
                    enq = self.alerts.alert(
                        "info",
                        f"Daily Summary — {day_key}",
                        "",
                        key=f"daily_summary:{day_key}",
                        channel_key="daily_pnl",
                        embeds=embeds,
                    )
                    daily_status = "enqueued" if enq else "suppressed"
                elif self.discord is not None:
                    ch = self.discord.channels.get("daily_pnl") or self.discord.channels.get("announcements", "")
                    if ch:
                        ok = await self.discord._send_multi_embed(ch, embeds)
                        daily_status = "delivered" if ok else "delivery_failed"
            except Exception as e:
                daily_status = f"degraded:{type(e).__name__}"
                reason = _safe_report_reason(e, self.config)
                logger.error("Daily summary failed (%s)", type(e).__name__)
                fallback_embeds = [
                    {
                        "title": f"📊 Daily Summary — {day_key}",
                        "description": f"report unavailable: {reason}",
                        "color": 0xEF4444,
                    }
                ]
                try:
                    if self.alerts is not None:
                        self.alerts.alert(
                            "warning",
                            f"Daily Summary — {day_key}",
                            f"report unavailable: {reason}",
                            key=f"daily_summary_unavailable:{day_key}",
                            channel_key="daily_pnl",
                            embeds=fallback_embeds,
                        )
                    elif self.discord is not None:
                        ch = self.discord.channels.get("daily_pnl") or self.discord.channels.get("announcements", "")
                        if ch:
                            await self.discord._send_multi_embed(ch, fallback_embeds)
                except Exception as alert_err:
                    logger.warning("Daily summary fallback alert failed (%s)", type(alert_err).__name__)

            # Emit the weekly paper-test scorecard on Friday (or if Friday is a holiday, Thursday)
            is_week_end = now.weekday() == 4
            cal = getattr(self, "calendar", None)
            if not is_week_end and now.weekday() == 3 and cal is not None:
                try:
                    is_week_end = cal.session_on(now.date() + timedelta(days=1)) is None
                except Exception:
                    is_week_end = False

            if is_week_end:
                try:
                    wk_embeds, _ = await builder.build_weekly_scorecard(
                        snapshot=snap,
                        as_of=now,
                        entry_locks=entry_locks,
                    )
                    if wk_embeds and cal is not None and hasattr(cal, "session_on"):
                        try:
                            sess = cal.session_on(now.date())
                            if sess is not None and now < sess.closes_at.astimezone(ET) + timedelta(minutes=16):
                                wk_embeds[0] = dict(wk_embeds[0])
                                wk_embeds[0]["description"] = (
                                    str(wk_embeds[0].get("description") or "")
                                    + " Note: generated before post-close shadow replay (+16m) window completed."
                                ).strip()
                        except Exception:
                            pass
                    if self.alerts is not None:
                        self.alerts.alert(
                            "info",
                            f"Weekly Scorecard — {day_key}",
                            "",
                            key=f"weekly_scorecard:{now.strftime('%Y-%W')}",
                            channel_key="daily_pnl",
                            embeds=wk_embeds,
                        )
                    elif self.discord is not None:
                        ch = self.discord.channels.get("daily_pnl") or self.discord.channels.get("announcements", "")
                        if ch:
                            await self.discord._send_multi_embed(ch, wk_embeds)
                except Exception as wk_err:
                    reason = _safe_report_reason(wk_err, self.config)
                    logger.error("Weekly scorecard failed (%s)", type(wk_err).__name__)
                    wk_fallback = [
                        {
                            "title": f"📈 Weekly Scorecard — {day_key}",
                            "description": f"report unavailable: {reason}",
                            "color": 0xEF4444,
                        }
                    ]
                    try:
                        if self.alerts is not None:
                            self.alerts.alert(
                                "warning",
                                f"Weekly Scorecard — {day_key}",
                                f"report unavailable: {reason}",
                                key=f"weekly_scorecard_unavailable:{now.strftime('%Y-%W')}",
                                channel_key="daily_pnl",
                                embeds=wk_fallback,
                            )
                        elif self.discord is not None:
                            ch = self.discord.channels.get("daily_pnl") or self.discord.channels.get("announcements", "")
                            if ch:
                                await self.discord._send_multi_embed(ch, wk_fallback)
                    except Exception as alert_err:
                        logger.warning("Weekly scorecard fallback alert failed (%s)", type(alert_err).__name__)

            if self.alerts is not None:
                try:
                    self._flush_blocked_buy_overflow(now)
                    sent = await self.alerts.flush()
                    logger.info(
                        "Daily summary outcome=%s (outbox flushed=%s)",
                        daily_status,
                        sent,
                    )
                except Exception as flush_err:
                    logger.warning("Daily summary outbox flush failed (%s)", type(flush_err).__name__)
            else:
                logger.info("Daily summary outcome=%s", daily_status)
        except Exception as outer_err:
            logger.error("Daily summary outer guard caught %s", type(outer_err).__name__)

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

    def _schedule_market_move_sample(self, now: datetime) -> None:
        if self.research is None:
            return
        session = self.calendar.session_on(now.date())
        if session is None or not session.opens_at <= now < session.closes_at:
            return
        slot, offset = divmod(int((now - session.opens_at).total_seconds()), MARKET_MOVE_SLOT_SECONDS)
        # Allow the opening print and normal scheduler jitter, but don't use
        # a mid-slot restart as if it were a regularly spaced observation.
        if not 30 <= offset < 300:
            return
        started_at = session.opens_at + timedelta(seconds=slot * MARKET_MOVE_SLOT_SECONDS)
        latest = self.store.kv_get(MARKET_MOVE_STATE_KEY) or {}
        eligible_at = market_move.cooldown_until(latest) if latest.get("session") == str(session.date) else None
        deadline = started_at + timedelta(minutes=3)
        if eligible_at is not None and eligible_at > started_at:
            if now < eligible_at:
                # The prior claim may have landed later within its 30-minute
                # window. Wait, then read fresh inputs at this slot rather
                # than consuming a crossing while still in the cooldown.
                return
            deadline = max(deadline, eligible_at + timedelta(seconds=2 * TICK_SECONDS))
        if now >= deadline:
            return
        self._schedule_job("market_move_sample", f"{session.date}:{slot}",
                           lambda: self._run_market_move_sample(session, slot), timeout=25, replay=False)

    async def _market_move_input(self, label, callback):
        try:
            async with asyncio.timeout(10):
                return await callback()
        except Exception as exc:
            logger.info("Market-move %s unavailable (%s)", label, type(exc).__name__)
            return None

    async def _run_market_move_sample(self, session, slot):
        previous = self.calendar.previous_session(session.date)

        async def spy_close():
            if previous is None:
                return None
            rows = await self.prices.get_daily_bars(["SPY"], start_date=previous.date, end_date=previous.date)
            return next((row.close for row in rows.get("SPY", ()) if row.session_date == previous.date), None)

        async def vix():
            if self.market_move_vix is None:
                return None
            # Inject only a real intraday cash VIX feed. Its verified close must
            # be for previous.date; FRED daily data and VIXY are not substitutes.
            return await self.market_move_vix(session, previous)

        close, vix_data, current_macro = await asyncio.gather(
            self._market_move_input("prior SPY close", spy_close),
            self._market_move_input("VIX", vix),
            self._market_move_input("macro", self.macro.check_conditions),
        )
        quotes = await self._market_move_input(
            "SPY", lambda: self.prices.get_quotes(["SPY"], purpose="brief", max_age_s=90),
        )
        now = _now_utc()
        if self._shutdown or not self._is_market_open(now) or not session.opens_at <= now < session.closes_at:
            return JobOutcome(JobStatus.SKIPPED, detail={"reason": "regular_session_unavailable"})
        readings, closes = {}, {"spy": close}
        quote = quotes.quotes.get("SPY") if quotes is not None else None
        if quote is not None and quote.is_market_hours:
            reading = market_move.valid_reading(quote.price, quote.as_of, now=now, opens_at=session.opens_at)
            if reading is not None:
                readings["spy"] = reading
        if isinstance(vix_data, tuple) and len(vix_data) == 2 and isinstance(vix_data[0], market_move.Reading):
            vix_reading, vix_close = vix_data
            reading = market_move.valid_reading(vix_reading.value, vix_reading.as_of, now=now,
                                                opens_at=session.opens_at)
            if reading is not None:
                readings["vix"] = reading
                if previous is not None:
                    closes["vix"] = vix_close

        regime = None
        if current_macro is not None and not current_macro.degraded:
            try:
                stamp = datetime.fromisoformat(current_macro.timestamp)
            except (ValueError, TypeError):
                stamp = None
            if (stamp is not None and stamp.utcoffset() is not None
                    and session.opens_at <= stamp <= now and now - stamp <= market_move.MAX_QUOTE_AGE):
                regime = current_macro.regime
        day = str(session.date)
        name, window = "market_move_rescan", f"{day}:{slot}"
        attempt = None
        with self.store.transaction():
            state = self.store.kv_get(MARKET_MOVE_STATE_KEY) or {}
            if state.get("session") != day:
                anchor = None
                if previous is not None and state.get("session") == str(previous.date):
                    prior_seconds = int((previous.closes_at - previous.opens_at).total_seconds())
                    prior_final_slot = (prior_seconds - 1) // MARKET_MOVE_SLOT_SECONDS
                    if state.get("slot") == prior_final_slot:
                        anchor = state.get("regime")
                state = {"regime_anchor": anchor}
            state, hits = market_move.observe(
                state, slot=slot, now=now, readings=readings, closes=closes, regime=regime,
                can_claim=name not in self._job_tasks and self.ledger.job_status(name, window) is None,
            )
            self.store.kv_set(MARKET_MOVE_STATE_KEY, {"session": day, **state})
            if hits:
                attempt = self.ledger.start_job(name, window).attempts
        if attempt is not None:
            # Claim and cap are atomic with the job row. Intentional at-most-once
            # semantics: an interrupted or failed rescan consumes its claim.
            self._job_tasks[name] = asyncio.create_task(
                self._run_scheduled_job(name, window, lambda: self._run_market_move_rescan(window, hits), 1500, attempt),
                name=f"stock-agent:{name}",
            )
        return JobOutcome(JobStatus.DONE, detail={"inputs": sorted(readings), "triggered": bool(hits)})

    async def _run_market_move_rescan(self, window, hits):
        if not self._is_market_open(_now_et()):
            return JobOutcome(JobStatus.SKIPPED, detail={"reason": "regular_session_unavailable", "triggers": hits})
        why = "; ".join(hit["text"] for hit in hits)
        await self._alert("info", "Market-move rescan",
                          f"{why}. Reviewing held positions and looking for new ideas. "
                          "The normal options scan will be requested; all research and risk limits still apply.",
                          key=f"market-move-rescan:{window}")
        result = await self.research.run("market_move", work_scope=f"market_move:{window}")
        request_options = getattr(self, "request_options_rescan", None)
        options_status = "not configured"
        if request_options is not None:
            try:
                options_status = request_options(window) or "requested"
            except Exception as exc:
                # Do not lose valid stock intents because the separate options
                # handoff was unavailable. This call only persists a request.
                options_status = "request unavailable"
                logger.warning("Market-move options request failed (%s)", type(exc).__name__)
        detail = {**result.detail, "triggers": hits, "options_rescan": options_status}
        if options_status == "outside_options_window":
            # The normal research run has already journalled its stock stages.
            # Append just this additional skip so it cannot duplicate stock counts.
            outside = {"outside_options_window": 1}
            self.journal.append("research.entry_funnel", job="market_move_options",
                                session_date=window.split(":", 1)[0], rejected_by_reason=outside)
            funnel = dict(detail.get("entry_funnel") or {})
            rejected = {**(funnel.get("rejected_by_reason") or {}), **outside}
            detail["entry_funnel"] = {**funnel, "rejected_by_reason": rejected}
        return JobOutcome(result.status, result.intents, detail, result.option_candidates)

    # ── Morning Brief ────────────────────────────────────────────────

    async def _send_morning_brief(self):
        """Honest morning brief posted to #announcements from reconciled state."""
        try:
            from stock_agent.reporting import ReportBuilder, _safe_report_reason

            now = _now_et()
            day_key = now.strftime("%Y-%m-%d")
            self._morning_brief_done_today = day_key
            snap = self.ledger.last_snapshot() if self.ledger is not None else None
            snap_stale = (
                snap is None
                or (now - snap.as_of.astimezone(ET)) > timedelta(minutes=15)
            )
            if snap_stale and self.ledger is not None and hasattr(self.ledger, "refresh"):
                try:
                    snap = await self.ledger.refresh("morning_brief")
                except Exception as exc:
                    logger.warning("Morning brief refresh fallback failed (%s)", type(exc).__name__)

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
                    logger.warning("Could not fetch macro data for morning brief (%s)", type(exc).__name__)

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

            entry_locks = None
            lock_fn = getattr(self, "get_entry_locks", None)
            if callable(lock_fn):
                try:
                    entry_locks = lock_fn()
                except Exception as exc:
                    logger.warning("Could not read scheduler entry locks (%s)", type(exc).__name__)

            brief_status = "skipped"
            try:
                embeds = await builder.build_morning_brief(
                    snapshot=snap,
                    as_of=now,
                    macro_embed=macro_embed,
                    entry_locks=entry_locks,
                )
                if self.alerts is not None:
                    enq = self.alerts.alert(
                        "info",
                        f"Morning Brief — {day_key}",
                        "",
                        key=f"morning_brief:{day_key}",
                        channel_key="announcements",
                        embeds=embeds,
                    )
                    brief_status = "enqueued" if enq else "suppressed"
                elif self.discord is not None:
                    ch = self.discord.channels.get("announcements", "")
                    if ch:
                        ok = await self.discord._send_multi_embed(ch, embeds)
                        brief_status = "delivered" if ok else "delivery_failed"
            except Exception as e:
                brief_status = f"degraded:{type(e).__name__}"
                reason = _safe_report_reason(e, self.config)
                logger.error("Morning brief failed (%s)", type(e).__name__)
                fallback_embeds = [
                    {
                        "title": f"☕ Morning Brief — {day_key}",
                        "description": f"report unavailable: {reason}",
                        "color": 0xEF4444,
                    }
                ]
                try:
                    if self.alerts is not None:
                        self.alerts.alert(
                            "warning",
                            f"Morning Brief — {day_key}",
                            f"report unavailable: {reason}",
                            key=f"morning_brief_unavailable:{day_key}",
                            channel_key="announcements",
                            embeds=fallback_embeds,
                        )
                    elif self.discord is not None:
                        ch = self.discord.channels.get("announcements", "")
                        if ch:
                            await self.discord._send_multi_embed(ch, fallback_embeds)
                except Exception as alert_err:
                    logger.warning("Morning brief fallback alert failed (%s)", type(alert_err).__name__)

            if self.alerts is not None:
                try:
                    sent = await self.alerts.flush()
                    logger.info(
                        "Morning brief outcome=%s (outbox flushed=%s)",
                        brief_status,
                        sent,
                    )
                except Exception as flush_err:
                    logger.warning("Morning brief outbox flush failed (%s)", type(flush_err).__name__)
            else:
                logger.info("Morning brief outcome=%s", brief_status)
        except Exception as outer_err:
            logger.error("Morning brief outer guard caught %s", type(outer_err).__name__)

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
