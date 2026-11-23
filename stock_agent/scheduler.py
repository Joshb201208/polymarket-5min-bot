import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from stock_agent.analyst import Analyst
from stock_agent.config import Config
from stock_agent.data_feed import DataFeed
from stock_agent.executor import Executor
from stock_agent.models import (
    CompanyData,
    DailySummary,
    Position,
    Signal,
    Thesis,
    Trade,
)
from stock_agent.portfolio import Portfolio
from stock_agent.risk_manager import RiskManager
from stock_agent.screener import Screener
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

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def _now_et() -> datetime:
    return datetime.now(ET)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class StockAgentScheduler:
    def __init__(self, config: Config | None = None, *, services: SimpleNamespace | None = None):
        self.config = config if config is not None else Config()
        # Integration seam for one instance of each shared service. The legacy paths
        # below stay intact until the corresponding peer-owned service is wired.
        self.broker = getattr(services, "broker", None)
        self.ledger = getattr(services, "ledger", None)
        self.risk = getattr(services, "risk", None)
        self.calendar = getattr(services, "calendar", None)
        self.alerts = getattr(services, "alerts", None)
        self.health = getattr(services, "health", None)
        self.prices = getattr(services, "prices", None)
        self.news = getattr(services, "news", None)
        self.llm = getattr(services, "llm", None)
        self.engine = getattr(services, "engine", None)
        self.options = getattr(services, "options", None)
        self.data_feed = DataFeed(self.config)
        self.analyst = Analyst(self.config)
        self.screener = Screener(self.config)
        self.portfolio = Portfolio(self.config)
        self.risk_manager = RiskManager(self.config)
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
        self._first_run_done = False
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
        logger.info("Shutdown requested")
        self._shutdown = True

    async def run(self):
        """Main entry point — runs forever."""
        logger.info("Stock Agent starting in %s mode", self.config.MODE)
        try:
            await asyncio.gather(
                self.telegram.send_message(
                    f"\U0001f680 <b>Stock Agent started</b> in <code>{self.config.MODE}</code> mode"
                ),
                self.discord.send_startup(self.config.MODE, self.portfolio.state.cash),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("Failed to send startup message: %s", e)

        # Backfill missing sectors on existing positions
        await self._backfill_sectors()

        try:
            while not self._shutdown:
                now = _now_et()
                try:
                    await self._tick(now)
                except Exception as e:
                    logger.exception("Error in main tick: %s", e)
                    try:
                        await asyncio.gather(
                            self.telegram.send_error_alert(str(e)),
                            self.discord.send_error(str(e), context="Main tick"),
                            return_exceptions=True,
                        )
                    except Exception:
                        pass

                await asyncio.sleep(60)
        finally:
            await self._cleanup()

    async def _tick(self, now_et: datetime):
        """Single tick of the main loop."""
        # ── First run: execute any pending signals from pre-market scan ──
        if not self._first_run_done:
            self._first_run_done = True
            if self._pending_signals:
                logger.info("Executing %d pending signals from pre-market scan", len(self._pending_signals))
                await self._execute_signals(self._pending_signals)
                self._pending_signals.clear()

        # ── Macro conditions check: Mon-Fri, 7-8 AM ET (before everything else) ──
        if self._should_run_macro_check(now_et):
            logger.info("Running macro conditions check")
            await self._run_macro_check()

        # ── Weekly analysis: Sunday, between 6-8 PM ET ──
        if self._should_run_weekly(now_et):
            logger.info("Starting weekly analysis cycle")
            await self._run_weekly_analysis()

        # ── Mid-week analysis: Wednesday, between 6-8 PM ET ──
        if self._should_run_midweek(now_et):
            logger.info("Starting mid-week analysis cycle")
            await self._run_midweek_analysis()

        # ── Morning brief: Mon-Fri, 8:15-8:45 AM ET (after macro check, before scan) ──
        if self._should_send_morning_brief(now_et):
            logger.info("Sending morning brief")
            await self._send_morning_brief()

        # ── Daily pre-market scan: Mon-Fri, 8-9 AM ET ──
        if self._should_run_daily_prescan(now_et):
            logger.info("Starting daily pre-market scan")
            await self._run_daily_prescan()

        # ── Options scan: Mon-Fri, 9:00-9:30 AM ET (after prescan, before market open) ──
        if self._should_run_options_scan(now_et):
            logger.info("Starting options scan")
            await self._run_options_scan()

        # ── Earnings-reactive scan: Mon-Fri, check for recent earnings ──
        if self._should_run_earnings_check(now_et):
            logger.info("Starting earnings-reactive scan")
            await self._run_earnings_reactive()

        # ── Market monitoring during open hours ──
        if self._is_market_open(now_et):
            # Execute any pending signals once market opens
            if self._pending_signals:
                logger.info("Market open — executing %d pending signals", len(self._pending_signals))
                executed = await self._execute_signals(self._pending_signals)
                self._pending_signals.clear()

            if self._should_monitor(now_et):
                logger.info("Running market monitoring")
                await self._run_market_monitoring()
                # Check options positions for auto-close triggers
                await self._check_options_positions()
                # Check for major market events that should trigger immediate re-scan
                await self._check_for_market_event()

        # ── Daily summary at market close ──
        if self._should_send_daily_summary(now_et):
            logger.info("Sending daily summary")
            await self._send_daily_summary()

    # ── Schedule checks ──────────────────────────────────────────────

    def _should_run_macro_check(self, now_et: datetime) -> bool:
        """Run macro conditions check Mon-Fri, 6:30-7:30 AM ET (before pre-market scan)."""
        if now_et.weekday() >= 5:
            return False
        today_str = now_et.strftime("%Y-%m-%d")
        if self._macro_check_done_today == today_str:
            return False
        current_time = now_et.time()
        return time(6, 30) <= current_time <= time(7, 30)

    def _should_run_options_scan(self, now_et: datetime) -> bool:
        """Run options scan Mon-Fri, 9:45 AM - 2:00 PM ET.

        Wide window so restarts during market hours don't miss it.
        Runs after market open for live bid/ask quotes on calls.
        """
        if now_et.weekday() >= 5:
            return False
        today_str = now_et.strftime("%Y-%m-%d")
        if self._options_scan_done_today == today_str:
            return False
        current_time = now_et.time()
        return time(9, 45) <= current_time <= time(14, 0)

    def _should_send_morning_brief(self, now_et: datetime) -> bool:
        """Send morning brief Mon-Fri, 8:15-8:45 AM ET (8:15-8:45 PM SGT)."""
        if now_et.weekday() >= 5:
            return False
        today_str = now_et.strftime("%Y-%m-%d")
        if self._morning_brief_done_today == today_str:
            return False
        current_time = now_et.time()
        return time(8, 15) <= current_time <= time(8, 45)

    def _should_run_weekly(self, now_et: datetime) -> bool:
        """Run weekly analysis on Sunday between 6-8 PM ET."""
        if now_et.weekday() != self.config.WEEKLY_ANALYSIS_DAY:
            return False
        if not (18 <= now_et.hour <= 20):
            return False
        # Only once per week
        last = self.portfolio.state.last_weekly_analysis
        if last:
            last_et = last.astimezone(ET) if last.tzinfo else last.replace(tzinfo=timezone.utc).astimezone(ET)
            if (now_et - last_et).days < 5:
                return False
        return True

    def _should_run_midweek(self, now_et: datetime) -> bool:
        """Run mid-week analysis on Wednesday between 6-8 PM ET."""
        if now_et.weekday() != self.config.MIDWEEK_ANALYSIS_DAY:
            return False
        if not (18 <= now_et.hour <= 20):
            return False
        # Only once per week
        week_key = now_et.strftime("%Y-W%W")
        if self._midweek_analysis_done_this_week == week_key:
            return False
        return True

    def _should_run_daily_prescan(self, now_et: datetime) -> bool:
        """Run daily pre-market scan Mon-Fri, 8-9 AM ET."""
        if now_et.weekday() >= 5:  # Weekend
            return False
        today_str = now_et.strftime("%Y-%m-%d")
        if self._daily_prescan_done_today == today_str:
            return False
        # Between 8:00 AM and 9:00 AM ET
        current_time = now_et.time()
        return time(8, 0) <= current_time <= time(9, 0)

    def _should_run_earnings_check(self, now_et: datetime) -> bool:
        """Check for earnings-reactive opportunities Mon-Fri, 7-8 AM ET."""
        if now_et.weekday() >= 5:
            return False
        if not getattr(self.config, "EARNINGS_CHECK_ENABLED", True):
            return False
        today_str = now_et.strftime("%Y-%m-%d")
        if self._earnings_check_done_today == today_str:
            return False
        current_time = now_et.time()
        return time(7, 0) <= current_time <= time(8, 0)

    def _is_market_open(self, now_et: datetime) -> bool:
        """Check if US stock market is currently open."""
        if now_et.weekday() >= 5:  # Saturday or Sunday
            return False
        current_time = now_et.time()
        return MARKET_OPEN <= current_time <= MARKET_CLOSE

    def _should_monitor(self, now_et: datetime) -> bool:
        """Check if enough time has passed since last monitoring run."""
        if not self._last_monitor_time:
            return True
        elapsed = (now_et - self._last_monitor_time).total_seconds()
        return elapsed >= self.config.SCAN_INTERVAL_MINUTES * 60

    def _should_send_daily_summary(self, now_et: datetime) -> bool:
        """Send daily summary once per day after market close."""
        if now_et.weekday() >= 5:
            return False
        today_str = now_et.strftime("%Y-%m-%d")
        if self._daily_summary_sent_today == today_str:
            return False
        # Between 4:05 PM and 4:30 PM ET
        current_time = now_et.time()
        return time(16, 5) <= current_time <= time(16, 30)

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
        """Check positions during market hours."""
        try:
            self._last_monitor_time = _now_et()
            positions = self.portfolio.state.positions
            if not positions:
                logger.debug("No positions to monitor")
                return

            # 1. Update prices
            symbols = [p.symbol for p in positions]
            prices = await self.data_feed.get_batch_quotes(symbols)
            self.portfolio.update_prices(prices)

            # 2. Check stop-losses (belt-and-suspenders with Alpaca bracket orders)
            alerts = self.risk_manager.check_position_health(self.portfolio.state)
            for alert in alerts:
                sym = alert["symbol"]
                pos = self.portfolio.get_position(sym)
                if pos:
                    logger.warning("Stop-loss triggered for %s", sym)
                    await self._execute_sell(pos, reason=alert["reason"])

            # 3. Check for material events (only every other check to save API costs)
            if self._should_check_events():
                for pos in list(self.portfolio.state.positions):
                    try:
                        event_check = await self.analyst.check_for_material_events(
                            pos.symbol, pos.thesis.symbol
                        )
                        if event_check.get("material_event"):
                            severity = event_check.get("severity", 0)
                            impact = event_check.get("thesis_impact", "neutral")
                            event_desc = event_check.get("event", "Unknown event")

                            logger.info(
                                "Material event for %s: %s (severity=%d, impact=%s)",
                                pos.symbol, event_desc, severity, impact,
                            )

                            # Send to Discord market-monitor
                            try:
                                await self.discord.send_material_event(
                                    pos.symbol, event_desc, impact, severity,
                                )
                            except Exception:
                                pass

                            # Severe negative event → full re-analysis
                            if severity >= 7 and impact == "negative":
                                company = await self.data_feed.get_company_fundamentals(pos.symbol)
                                if company:
                                    new_thesis = await self.analyst.re_analyze_position(
                                        pos.symbol, company, pos.thesis
                                    )
                                    if new_thesis and new_thesis.direction == "SELL":
                                        await self._execute_sell(
                                            pos,
                                            reason=f"Material event: {event_desc}",
                                        )
                                    elif new_thesis:
                                        self.portfolio.update_thesis(pos.symbol, new_thesis)
                    except Exception as e:
                        logger.error("Event check failed for %s: %s", pos.symbol, e)

            self.portfolio.set_last_daily_check(_now_utc())
            logger.info("Market monitoring complete — %d positions checked", len(positions))

        except Exception as e:
            logger.exception("Market monitoring failed: %s", e)

    def _should_check_events(self) -> bool:
        """Only check material events every other monitoring cycle to manage API costs."""
        if not self._last_monitor_time:
            return True
        # Check events on even hours only
        return _now_et().hour % 2 == 0

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
        """Execute BUY signals that pass risk checks."""
        executed: list[Signal] = []

        # Check PDT compliance first
        if not self.risk_manager.check_pdt_compliance(self.portfolio.state.trade_history):
            logger.warning("PDT limit reached — skipping new buys")
            await asyncio.gather(
                self.telegram.send_message("\u26a0\ufe0f PDT limit reached — no new trades today"),
                self.discord.send_risk_alert("PDT_WARNING", "PDT day-trade limit reached — no new trades today"),
                return_exceptions=True,
            )
            return executed

        for signal in signals:
            if signal.action != "BUY":
                continue

            sym = signal.symbol
            # Get sector from company fundamentals (FMP profile)
            company_sector = "Unknown"
            try:
                company_data = await self.data_feed.get_company_fundamentals(sym)
                if company_data and company_data.sector:
                    company_sector = company_data.sector
            except Exception:
                pass

            price = signal.entry_price or 0
            if price <= 0:
                quote = await self.data_feed.get_stock_quote(sym)
                if quote:
                    price = quote["price"]
                else:
                    logger.warning("Cannot get price for %s — skipping", sym)
                    continue

            ok, reason = self.risk_manager.can_open_position(
                self.portfolio.state, sym, company_sector, price,
                conviction=signal.conviction,
            )
            if not ok:
                logger.warning("Risk check BLOCKED %s (sector=%s): %s", sym, company_sector, reason)
                try:
                    await self.discord.send_system_log(
                        "WARNING", f"Trade blocked for {sym}: {reason}"
                    )
                except Exception:
                    pass
                continue

            is_add_on = (reason == "ADD_TO_EXISTING")
            pv = self.portfolio.get_portfolio_value()

            if is_add_on:
                # Calculate how many MORE shares to reach effective target weight.
                # Effective target = min(conviction-size, hard cap) — so a single
                # add can never take us above config.MAX_POSITION_PCT, even if
                # conviction target is higher.
                existing = self.portfolio.get_position(sym)
                if not existing:
                    continue
                conviction_target = self.config.CONVICTION_SIZE_MAP.get(
                    signal.conviction, 0.05
                )
                hard_cap = self.config.MAX_POSITION_PCT
                target_pct = min(conviction_target, hard_cap)
                target_value = pv * target_pct
                current_value = existing.current_price * existing.shares
                additional_value = target_value - current_value
                qty = int(additional_value / price)
                if qty <= 0:
                    logger.info("Add-on for %s: already at target weight — skipping", sym)
                    continue
                logger.info(
                    "Adding to %s: %d more shares (%.1f%% → %.1f%% target, cap %.0f%%)",
                    sym, qty, current_value / pv * 100, target_pct * 100, hard_cap * 100,
                )
            else:
                qty = self.risk_manager.calculate_position_size(pv, price, signal.conviction)
                if qty <= 0:
                    logger.info("Position size 0 for %s — skipping", sym)
                    continue

            # Volatility-adjusted stop-loss
            beta = getattr(signal.thesis, "beta", None)
            stop_loss = self.risk_manager.get_stop_loss_price(price, beta)
            if hasattr(signal.thesis, "stop_loss_price"):
                signal.thesis.stop_loss_price = stop_loss

            # Execute order
            order = await self.executor.place_buy(sym, qty, stop_loss)
            if not order:
                logger.error("Order execution failed for %s", sym)
                continue

            # Record position and trade
            now = _now_utc()
            if is_add_on:
                # Update existing position shares
                existing = self.portfolio.get_position(sym)
                if existing:
                    existing.shares += qty
                    existing.market_value = existing.current_price * existing.shares
                    existing.last_updated = now
                    self.portfolio._save()
            else:
                position = Position(
                    symbol=sym,
                    shares=qty,
                    entry_price=price,
                    entry_date=now,
                    current_price=price,
                    market_value=price * qty,
                    stop_loss=stop_loss,
                    thesis=signal.thesis,
                    sector=company_sector,
                    last_updated=now,
                )
                self.portfolio.add_position(position)

            action_type = "Add-on" if is_add_on else "New thesis"
            trade = Trade(
                symbol=sym,
                action="BUY",
                shares=qty,
                price=price,
                timestamp=now,
                reason=f"{action_type}: {signal.thesis.summary[:100]}",
            )
            self.portfolio.log_trade(trade)

            await asyncio.gather(
                self.telegram.send_trade_alert(trade, signal.thesis),
                self.discord.send_trade_alert(trade, signal.thesis),
                self.discord.send_eli5_trade(trade, signal.thesis, is_buy=True),
                return_exceptions=True,
            )
            executed.append(signal)
            logger.info("Executed BUY: %s x%d @ $%.2f", sym, qty, price)

        return executed

    async def _execute_sell(self, position: Position, reason: str):
        """Sell an entire position."""
        try:
            # Get current price
            quote = await self.data_feed.get_stock_quote(position.symbol)
            sell_price = quote["price"] if quote else position.current_price

            order = await self.executor.place_sell(position.symbol, position.shares)
            if not order:
                logger.error("Sell order failed for %s", position.symbol)
                return

            pnl = (sell_price - position.entry_price) * position.shares
            pnl_pct = (sell_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0
            hold_days = (_now_utc() - position.entry_date).days

            trade = Trade(
                symbol=position.symbol,
                action="SELL",
                shares=position.shares,
                price=sell_price,
                timestamp=_now_utc(),
                reason=reason,
                pnl=pnl,
                pnl_pct=pnl_pct,
                hold_days=hold_days,
            )

            # Remove position first (log_trade also removes, but be explicit)
            self.portfolio.remove_position(position.symbol)
            self.portfolio.log_trade(trade)

            tr_clause = format_tipranks_telegram_clause(
                self._tipranks_lookup.get(position.symbol.upper())
            )
            alert_tasks = [
                self.telegram.send_trade_alert(trade),
                self.discord.send_trade_alert(trade),
                self.discord.send_eli5_trade(trade, position.thesis, is_buy=False),
            ]
            if tr_clause:
                alert_tasks.append(self.telegram.send_message(tr_clause.lstrip(" |").strip()))
            await asyncio.gather(*alert_tasks, return_exceptions=True)
            logger.info(
                "Executed SELL: %s x%d @ $%.2f — P&L: $%+,.0f (%.1f%%)",
                position.symbol, position.shares, sell_price, pnl, pnl_pct * 100,
            )

        except Exception as e:
            logger.exception("Sell execution failed for %s: %s", position.symbol, e)
            await asyncio.gather(
                self.telegram.send_error_alert(f"Sell failed for {position.symbol}: {e}"),
                self.discord.send_error(str(e), context=f"Sell execution for {position.symbol}"),
                return_exceptions=True,
            )

    # ── Options Trading ─────────────────────────────────────────────

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
        """Graceful cleanup of all resources."""
        logger.info("Cleaning up resources...")
        try:
            await asyncio.gather(
                self.telegram.send_message("\U0001f6d1 <b>Stock Agent shutting down</b>"),
                self.discord.send_shutdown("Graceful shutdown"),
                return_exceptions=True,
            )
        except Exception:
            pass

        for resource in [self.data_feed, self.analyst, self.screener, self.executor, self.telegram, self.discord, self.macro, self.options_data, self.options_executor]:
            try:
                await resource.close()
            except Exception:
                pass

        logger.info("Cleanup complete")
