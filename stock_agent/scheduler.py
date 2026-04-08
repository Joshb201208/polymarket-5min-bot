import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
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
    def __init__(self):
        self.config = Config()
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
        """Full weekly research cycle."""
        try:
            await asyncio.gather(
                self.telegram.send_message("\U0001f50d <b>Weekly Analysis Starting</b>"),
                self.discord.send_system_log("INFO", "Weekly analysis cycle starting"),
                return_exceptions=True,
            )

            # 0. Run TipRanks scraper and load data (supplementary — never blocks)
            tipranks_stocks = await self._load_tipranks_data()

            # 1. Screen universe
            symbols = await self.data_feed.screen_universe()
            if not symbols:
                await asyncio.gather(
                    self.telegram.send_message("\u26a0\ufe0f Universe screening returned no symbols"),
                    self.discord.send_system_log("WARNING", "Universe screening returned no symbols"),
                    return_exceptions=True,
                )
                return

            self.portfolio.set_universe(symbols)
            logger.info("Universe: %d symbols", len(symbols))

            # 2. Fetch fundamentals for all candidates
            companies = await self._fetch_fundamentals(symbols)
            if not companies:
                await asyncio.gather(
                    self.telegram.send_message("\u26a0\ufe0f No fundamental data retrieved"),
                    self.discord.send_system_log("WARNING", "No fundamental data retrieved"),
                    return_exceptions=True,
                )
                return

            # 3. GPT-4.1 Nano screening
            ranked = await self.screener.screen_candidates(companies)
            top_symbols = [r["symbol"] for r in ranked[: self.macro.get_effective_analysis_depth()]]
            logger.info("Top %d for deep analysis: %s", len(top_symbols), top_symbols)

            # Send screener results to Discord
            try:
                await self.discord.send_screener_results(ranked[:25])
            except Exception as e:
                logger.error("Failed to send screener results to Discord: %s", e)

            # 4. Deep analysis with Perplexity Sonar Pro
            signals = await self._deep_analyze(top_symbols, companies, tipranks_stocks)

            # 5. Re-analyze existing positions
            await self._re_analyze_existing_positions(companies)

            # 6. Queue signals — execute at next market open (weekly runs Sunday evening, market is closed)
            if self._is_market_open(_now_et()):
                executed_signals = await self._execute_signals(signals)
            else:
                # Store for execution at next market open
                self._pending_signals.extend(signals)
                executed_signals = []
                logger.info(
                    "Market closed — queued %d signals for next open: %s",
                    len(signals), [s.symbol for s in signals]
                )

            # 7. Report
            stats = self.portfolio.calculate_stats()
            await asyncio.gather(
                self.telegram.send_weekly_report(
                    portfolio_value=self.portfolio.get_portfolio_value(),
                    cash=self.portfolio.state.cash,
                    positions=self.portfolio.state.positions,
                    signals=executed_signals,
                    stats=stats,
                ),
                self.discord.send_weekly_report(
                    portfolio_value=self.portfolio.get_portfolio_value(),
                    cash=self.portfolio.state.cash,
                    positions=self.portfolio.state.positions,
                    signals=executed_signals,
                    stats=stats,
                ),
                self.discord.send_eli5_weekly(self.portfolio.state, executed_signals),
                return_exceptions=True,
            )

            self.portfolio.set_last_weekly_analysis(_now_utc())
            logger.info("Weekly analysis complete — %d signals, %d executed", len(signals), len(executed_signals))

        except Exception as e:
            logger.exception("Weekly analysis failed: %s", e)
            await asyncio.gather(
                self.telegram.send_error_alert(f"Weekly analysis failed: {e}"),
                self.discord.send_error(str(e), context="Weekly analysis"),
                return_exceptions=True,
            )

    # ── Mid-week analysis (Wednesday) ────────────────────────────────

    async def _run_midweek_analysis(self):
        """Mid-week re-analysis: re-screen universe and re-analyze positions.
        Similar to weekly but lighter — focuses on refreshing theses and
        looking for new opportunities that emerged Mon-Wed.
        """
        try:
            now = _now_et()
            week_key = now.strftime("%Y-W%W")
            self._midweek_analysis_done_this_week = week_key

            await self.discord.send_system_log("INFO", "Mid-week analysis starting (Wednesday refresh)")

            # 0. Load TipRanks data if available
            tipranks_stocks = await self._load_tipranks_data()

            # 1. Screen universe (same as weekly)
            symbols = await self.data_feed.screen_universe()
            if not symbols:
                await self.discord.send_system_log("WARNING", "Mid-week: universe screening returned no symbols")
                return

            self.portfolio.set_universe(symbols)
            logger.info("Mid-week universe: %d symbols", len(symbols))

            # 2. Fetch fundamentals
            companies = await self._fetch_fundamentals(symbols)
            if not companies:
                return

            # 3. Screen
            ranked = await self.screener.screen_candidates(companies)
            top_symbols = [r["symbol"] for r in ranked[: self.macro.get_effective_analysis_depth()]]

            try:
                await self.discord.send_screener_results(ranked[:25])
            except Exception:
                pass

            # 4. Deep analysis for new opportunities
            signals = await self._deep_analyze(top_symbols, companies, tipranks_stocks)

            # 5. Re-analyze all existing positions with fresh data
            await self._re_analyze_existing_positions(companies)

            # 6. Queue or execute signals
            if self._is_market_open(_now_et()):
                executed = await self._execute_signals(signals)
            else:
                self._pending_signals.extend(signals)
                executed = []
                logger.info(
                    "Mid-week: market closed — queued %d signals for next open: %s",
                    len(signals), [s.symbol for s in signals]
                )

            await self.discord.send_system_log(
                "INFO",
                f"Mid-week analysis complete — {len(signals)} signals, {len(executed)} executed",
            )
            logger.info("Mid-week analysis complete — %d signals, %d executed", len(signals), len(executed))

        except Exception as e:
            logger.exception("Mid-week analysis failed: %s", e)
            await self.discord.send_error(str(e), context="Mid-week analysis")

    # ── Daily pre-market scan ────────────────────────────────────────

    async def _run_daily_prescan(self):
        """Daily pre-market scan: quick check for overnight developments
        and material events on held positions. Lighter than full analysis.
        """
        try:
            now = _now_et()
            self._daily_prescan_done_today = now.strftime("%Y-%m-%d")

            await self.discord.send_system_log("INFO", "Daily pre-market scan starting")

            # 1. Update prices on all positions
            positions = self.portfolio.state.positions
            if positions:
                symbols = [p.symbol for p in positions]
                prices = await self.data_feed.get_batch_quotes(symbols)
                self.portfolio.update_prices(prices)

                # 2. Check for overnight material events on held positions
                for pos in list(positions):
                    try:
                        event_check = await self.analyst.check_for_material_events(
                            pos.symbol, pos.thesis.symbol if pos.thesis else pos.symbol
                        )
                        if event_check.get("material_event"):
                            severity = event_check.get("severity", 0)
                            impact = event_check.get("thesis_impact", "neutral")
                            event_desc = event_check.get("event", "Unknown event")

                            logger.info(
                                "Pre-market event for %s: %s (severity=%d, impact=%s)",
                                pos.symbol, event_desc, severity, impact,
                            )

                            try:
                                await self.discord.send_material_event(
                                    pos.symbol, event_desc, impact, severity,
                                )
                            except Exception:
                                pass

                            # Severe negative → queue for sell at market open
                            if severity >= 7 and impact == "negative":
                                company = await self.data_feed.get_company_fundamentals(pos.symbol)
                                if company:
                                    new_thesis = await self.analyst.re_analyze_position(
                                        pos.symbol, company, pos.thesis
                                    )
                                    if new_thesis and new_thesis.direction == "SELL":
                                        # If market is open, execute immediately; otherwise queue
                                        if self._is_market_open(now):
                                            await self._execute_sell(
                                                pos, reason=f"Pre-market event: {event_desc}"
                                            )
                                        else:
                                            logger.info(
                                                "Queuing sell for %s at market open: %s",
                                                pos.symbol, event_desc,
                                            )
                                    elif new_thesis:
                                        self.portfolio.update_thesis(pos.symbol, new_thesis)
                    except Exception as e:
                        logger.error("Pre-market event check failed for %s: %s", pos.symbol, e)

            # 3. Quick scan for new high-conviction opportunities
            # Only look at a smaller universe for speed
            try:
                symbols = await self.data_feed.screen_universe()
                if symbols:
                    # Quick screen — top 10 only for daily scan
                    companies = await self._fetch_fundamentals(symbols[:30])
                    if companies:
                        ranked = await self.screener.screen_candidates(companies)
                        top_5 = [r["symbol"] for r in ranked[:5]]
                        # Only analyze top 5 and only if conviction would be high
                        for sym in top_5:
                            # Skip if already holding
                            if any(p.symbol == sym for p in self.portfolio.state.positions):
                                continue
                            company = next((c for c in companies if c.symbol == sym), None)
                            if company:
                                try:
                                    thesis = await self.analyst.analyze_stock(sym, company)
                                    if thesis and thesis.direction == "BUY" and thesis.conviction >= 8:
                                        # High conviction from daily scan — queue signal
                                        pv = self.portfolio.get_portfolio_value()
                                        signal = Signal(
                                            symbol=sym,
                                            action="BUY",
                                            conviction=thesis.conviction,
                                            thesis=thesis,
                                            entry_price=company.price,
                                            position_size_pct=self.config.CONVICTION_SIZE_MAP.get(
                                                thesis.conviction, 0.03
                                            ),
                                            generated_at=_now_utc(),
                                        )
                                        self._pending_signals.append(signal)
                                        logger.info(
                                            "Daily scan: queued BUY signal for %s (conviction=%d)",
                                            sym, thesis.conviction,
                                        )
                                except Exception as e:
                                    logger.error("Daily scan analysis failed for %s: %s", sym, e)
            except Exception as e:
                logger.error("Daily scan universe screening failed: %s", e)

            if self._pending_signals:
                await self.discord.send_system_log(
                    "INFO",
                    f"Daily pre-market scan complete — {len(self._pending_signals)} signals queued for market open",
                )
            else:
                await self.discord.send_system_log("INFO", "Daily pre-market scan complete — no new signals")

            logger.info("Daily pre-market scan complete")

        except Exception as e:
            logger.exception("Daily pre-market scan failed: %s", e)
            await self.discord.send_error(str(e), context="Daily pre-market scan")

    # ── Earnings-reactive scanning ───────────────────────────────────

    async def _run_earnings_reactive(self):
        """Check for stocks that just reported earnings (last 1-2 days).
        If a held position reported, re-analyze thesis.
        If a non-held high-quality stock had a blowout quarter, flag it.
        """
        try:
            now = _now_et()
            self._earnings_check_done_today = now.strftime("%Y-%m-%d")

            await self.discord.send_system_log("INFO", "Earnings-reactive scan starting")

            lookback = getattr(self.config, "EARNINGS_LOOKBACK_DAYS", 2)

            # 1. Check held positions for recent earnings
            for pos in list(self.portfolio.state.positions):
                try:
                    earnings = await self.data_feed.get_recent_earnings(
                        pos.symbol, lookback_days=lookback
                    )
                    if not earnings:
                        continue

                    logger.info("Earnings detected for held position %s", pos.symbol)
                    await self.discord.send_system_log(
                        "INFO", f"Earnings detected for {pos.symbol} — re-analyzing thesis"
                    )

                    # Re-analyze with fresh post-earnings data
                    company = await self.data_feed.get_company_fundamentals(pos.symbol)
                    if company:
                        new_thesis = await self.analyst.re_analyze_position(
                            pos.symbol, company, pos.thesis
                        )
                        if new_thesis:
                            self.portfolio.update_thesis(pos.symbol, new_thesis)
                            if new_thesis.direction == "SELL":
                                logger.info("Post-earnings thesis broken for %s — selling", pos.symbol)
                                await self._execute_sell(
                                    pos, reason=f"Post-earnings thesis broken: {new_thesis.summary[:80]}"
                                )
                            else:
                                try:
                                    await self.discord.send_thesis(new_thesis)
                                except Exception:
                                    pass
                except Exception as e:
                    logger.error("Earnings check failed for %s: %s", pos.symbol, e)

            # 2. Check universe for blowout earnings (new opportunities)
            try:
                universe = self.portfolio.state.universe or []
                for sym in universe[:30]:  # Limit to avoid excessive API calls
                    # Skip already held
                    if any(p.symbol == sym for p in self.portfolio.state.positions):
                        continue
                    try:
                        earnings = await self.data_feed.get_recent_earnings(
                            sym, lookback_days=lookback
                        )
                        if not earnings:
                            continue
                        # Only interested in strong beats
                        if not earnings.get("beat", False):
                            continue
                        surprise_pct = earnings.get("surprise_pct", 0)
                        if surprise_pct < 10:  # Only big beats (>10% surprise)
                            continue

                        logger.info(
                            "Blowout earnings for %s: %+.1f%% surprise",
                            sym, surprise_pct,
                        )

                        company = await self.data_feed.get_company_fundamentals(sym)
                        if company:
                            thesis = await self.analyst.analyze_stock(sym, company)
                            if thesis and thesis.direction == "BUY" and thesis.conviction >= 8:
                                signal = Signal(
                                    symbol=sym,
                                    action="BUY",
                                    conviction=thesis.conviction,
                                    thesis=thesis,
                                    entry_price=company.price,
                                    position_size_pct=self.config.CONVICTION_SIZE_MAP.get(
                                        thesis.conviction, 0.03
                                    ),
                                    generated_at=_now_utc(),
                                )
                                self._pending_signals.append(signal)
                                logger.info(
                                    "Earnings-reactive: queued BUY for %s (conviction=%d, surprise=%+.1f%%)",
                                    sym, thesis.conviction, surprise_pct,
                                )
                                await self.discord.send_system_log(
                                    "INFO",
                                    f"Earnings beat for {sym} ({surprise_pct:+.1f}%) — "
                                    f"BUY signal queued (conviction {thesis.conviction})",
                                )
                    except Exception as e:
                        logger.debug("Earnings check for %s failed: %s", sym, e)
            except Exception as e:
                logger.error("Earnings-reactive universe scan failed: %s", e)

            await self.discord.send_system_log(
                "INFO",
                f"Earnings-reactive scan complete — {len(self._pending_signals)} pending signals",
            )
            logger.info("Earnings-reactive scan complete")

        except Exception as e:
            logger.exception("Earnings-reactive scan failed: %s", e)
            await self.discord.send_error(str(e), context="Earnings-reactive scan")

    # ── Shared helpers ───────────────────────────────────────────────

    async def _load_tipranks_data(self):
        """Load TipRanks data if enabled. Returns list or None."""
        if not getattr(self.config, "TIPRANKS_ENABLED", False):
            return None
        try:
            from stock_agent.tipranks_scraper import scrape_tipranks

            await scrape_tipranks(self.config)

            from stock_agent.tipranks_data import TipRanksData

            tr = TipRanksData(self.config)
            tipranks_stocks = tr.load_latest()
            if tipranks_stocks:
                logger.info("Loaded %d stocks from TipRanks", len(tipranks_stocks))
                await self.discord.send_system_log(
                    "INFO", f"TipRanks data loaded: {len(tipranks_stocks)} stocks"
                )
                # Report aligned signals
                aligned = [
                    s
                    for s in tipranks_stocks
                    if s.smart_score >= 9
                    and s.analyst_consensus == "Strong Buy"
                    and s.hedge_fund_signal.lower() == "positive"
                ]
                if aligned:
                    symbols_str = ", ".join(s.symbol for s in aligned[:15])
                    await self.discord.send_system_log(
                        "INFO",
                        f"TipRanks aligned signals: {len(aligned)} stocks "
                        f"with SS9+, Strong Buy, HF Positive: {symbols_str}",
                    )
                return tipranks_stocks
        except Exception as e:
            logger.warning("TipRanks scrape failed (non-critical): %s", e)
        return None

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

    async def _deep_analyze(
        self,
        top_symbols: list[str],
        companies: list[CompanyData],
        tipranks_stocks=None,
    ) -> list[Signal]:
        """Run deep Perplexity Sonar Pro analysis on top symbols. Returns signals."""
        signals: list[Signal] = []
        for sym in top_symbols:
            company = next((c for c in companies if c.symbol == sym), None)
            if not company:
                continue

            try:
                # Build TipRanks context for this symbol if available
                tipranks_context = ""
                if tipranks_stocks:
                    tr_stock = next(
                        (s for s in tipranks_stocks if s.symbol.upper() == sym.upper()),
                        None,
                    )
                    if tr_stock:
                        tipranks_context = (
                            f"TIPRANKS DATA:\n"
                            f"- Smart Score: {tr_stock.smart_score}/10\n"
                            f"- Analyst Consensus: {tr_stock.analyst_consensus}\n"
                            f"- Analyst Price Target Upside: {tr_stock.analyst_target_upside:+.1f}%\n"
                            f"- Hedge Fund Signal: {tr_stock.hedge_fund_signal}\n"
                            f"- Insider Signal: {tr_stock.insider_signal}\n"
                            f"- News Sentiment: {tr_stock.news_sentiment}\n"
                        )

                thesis = await self.analyst.analyze_stock(
                    sym, company, tipranks_context=tipranks_context
                )
                if not thesis:
                    continue

                self.portfolio.update_thesis(sym, thesis)

                # Send thesis and analysis to Discord
                try:
                    await asyncio.gather(
                        self.discord.send_thesis(thesis),
                        self.discord.send_analysis_report(
                            sym,
                            thesis.summary
                            + "\n\n**Bull Case:** "
                            + thesis.bull_case
                            + "\n\n**Bear Case:** "
                            + thesis.bear_case,
                            thesis.sources,
                        ),
                        return_exceptions=True,
                    )
                except Exception:
                    pass

                if thesis.direction == "BUY" and thesis.conviction >= self.macro.get_effective_min_conviction():
                    signal = Signal(
                        symbol=sym,
                        action="BUY",
                        conviction=thesis.conviction,
                        thesis=thesis,
                        entry_price=company.price,
                        position_size_pct=self.config.CONVICTION_SIZE_MAP.get(
                            thesis.conviction, 0.03
                        ),
                        generated_at=_now_utc(),
                    )
                    signals.append(signal)
                    logger.info("BUY signal: %s conviction=%d", sym, thesis.conviction)
            except Exception as e:
                logger.error("Analysis failed for %s: %s", sym, e)

        return signals

    async def _re_analyze_existing_positions(self, companies: list[CompanyData]):
        """Re-analyze all existing positions with fresh data."""
        for pos in list(self.portfolio.state.positions):
            try:
                company = next((c for c in companies if c.symbol == pos.symbol), None)
                if not company:
                    company = await self.data_feed.get_company_fundamentals(pos.symbol)
                if not company:
                    continue

                new_thesis = await self.analyst.re_analyze_position(
                    pos.symbol, company, pos.thesis
                )
                if not new_thesis:
                    continue

                self.portfolio.update_thesis(pos.symbol, new_thesis)

                # If thesis is now SELL, generate exit signal
                if new_thesis.direction == "SELL":
                    logger.info("Thesis broken for %s — selling", pos.symbol)
                    await self._execute_sell(
                        pos, reason=f"Thesis broken: {new_thesis.summary[:100]}"
                    )
            except Exception as e:
                logger.error("Re-analysis failed for %s: %s", pos.symbol, e)

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
                self.portfolio.state, sym, company_sector, price
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

            pv = self.portfolio.get_portfolio_value()
            qty = self.risk_manager.calculate_position_size(pv, price, signal.conviction)
            if qty <= 0:
                logger.info("Position size 0 for %s — skipping", sym)
                continue

            # Volatility-adjusted stop-loss
            beta = getattr(signal.thesis, "beta", None)
            stop_loss = self.risk_manager.get_stop_loss_price(price, beta)
            signal.thesis.stop_loss_price = stop_loss

            # Execute order
            order = await self.executor.place_buy(sym, qty, stop_loss)
            if not order:
                logger.error("Order execution failed for %s", sym)
                continue

            # Record position and trade
            now = _now_utc()
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

            trade = Trade(
                symbol=sym,
                action="BUY",
                shares=qty,
                price=price,
                timestamp=now,
                reason=f"New thesis: {signal.thesis.summary[:100]}",
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

            await asyncio.gather(
                self.telegram.send_trade_alert(trade),
                self.discord.send_trade_alert(trade),
                self.discord.send_eli5_trade(trade, position.thesis, is_buy=False),
                return_exceptions=True,
            )
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
        """Daily options scan: find and execute options opportunities."""
        try:
            now = _now_et()
            self._options_scan_done_today = now.strftime("%Y-%m-%d")

            await self.discord.send_system_log("INFO", "Options scan starting")

            # Refresh options portfolio with latest prices
            portfolio_value = self.portfolio.get_portfolio_value()
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

            # Refresh prices
            portfolio_value = self.portfolio.get_portfolio_value()
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

    # ── Event-Triggered Scanning ────────────────────────────────────────────

    async def _check_for_market_event(self):
        """After each market monitoring cycle, check if a major event occurred.

        Triggers an immediate full analysis cycle (re-scan + re-analyze positions)
        when ANY of these thresholds are crossed since the last check:

        - SPX moved > 1.5% in either direction
        - VIX spiked > 3 points
        - Macro regime changed
        - Oil (crude) moved > 3%

        Cooldown of 30 minutes prevents back-to-back triggers.
        """
        try:
            # Respect cooldown
            now = _now_et()
            if self._event_scan_cooldown:
                elapsed = (now - self._event_scan_cooldown).total_seconds()
                if elapsed < 1800:  # 30-minute cooldown
                    return

            # Run a fresh macro check to get current values
            snap = await self.macro.check_conditions()

            # Compare against last known values
            spx_now = snap.spx_price
            vix_now = snap.vix_level
            regime_now = snap.regime

            events_detected: list[str] = []

            # SPX move > 1.5%
            if self._last_event_spx > 0 and spx_now > 0:
                spx_move_pct = abs(spx_now - self._last_event_spx) / self._last_event_spx * 100
                if spx_move_pct >= 1.5:
                    events_detected.append(
                        f"SPX moved {spx_move_pct:+.1f}% ({self._last_event_spx:,.0f} → {spx_now:,.0f})"
                    )

            # VIX spike > 3 points
            if self._last_event_vix > 0 and vix_now > 0:
                vix_change = vix_now - self._last_event_vix
                if abs(vix_change) >= 3.0:
                    events_detected.append(
                        f"VIX moved {vix_change:+.1f} pts ({self._last_event_vix:.1f} → {vix_now:.1f})"
                    )

            # Regime change
            if self._last_event_regime and regime_now != self._last_event_regime:
                events_detected.append(
                    f"Regime changed: {self._last_event_regime} → {regime_now}"
                )

            # Update last known values
            self._last_event_spx = spx_now
            self._last_event_vix = vix_now
            self._last_event_regime = regime_now

            if not events_detected:
                return

            # Something significant happened — trigger immediate full scan
            self._event_scan_cooldown = now
            event_summary = " | ".join(events_detected)

            logger.info("MARKET EVENT DETECTED: %s — triggering immediate scan", event_summary)

            # Notify Discord
            await self.discord.send_embed(
                self.discord.channels.get("announcements", ""),
                {
                    "title": "\u26a1 Market Event — Immediate Scan Triggered",
                    "description": (
                        f"Significant market movement detected. Running full analysis now.\n\n"
                        f"**Events:**\n" + "\n".join(f"\u2022 {e}" for e in events_detected)
                    ),
                    "color": 0xF59E0B,
                    "fields": [
                        {"name": "Regime", "value": regime_now.replace('_', ' ').title(), "inline": True},
                        {"name": "SPX", "value": f"${spx_now:,.0f}", "inline": True},
                        {"name": "VIX", "value": f"{vix_now:.1f}", "inline": True},
                    ],
                },
            )

            # 1. Re-analyze all existing positions with fresh data
            if self.portfolio.state.positions:
                symbols = [p.symbol for p in self.portfolio.state.positions]
                companies = await self._fetch_fundamentals(symbols)
                await self._re_analyze_existing_positions(companies)

            # 2. Run a fresh universe screen and deep analysis for new opportunities
            symbols = await self.data_feed.screen_universe()
            if symbols:
                self.portfolio.set_universe(symbols)
                companies = await self._fetch_fundamentals(symbols)
                if companies:
                    ranked = await self.screener.screen_candidates(companies)
                    top_symbols = [r["symbol"] for r in ranked[: self.macro.get_effective_analysis_depth()]]
                    signals = await self._deep_analyze(top_symbols, companies)
                    # Execute signals if market is open
                    if self._is_market_open(now):
                        executed = await self._execute_signals(signals)
                    else:
                        self._pending_signals.extend(signals)
                        executed = []

            # 3. Re-run options scan if in market hours
            if self._is_market_open(now):
                self._options_scan_done_today = None  # Reset so it runs again
                await self._run_options_scan()

            logger.info(
                "Event-triggered scan complete — event: %s",
                event_summary,
            )

        except Exception as e:
            logger.error("Event check failed: %s", e)

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
        """One-time backfill: fetch sector for positions marked 'Unknown'."""
        updated = 0
        for pos in self.portfolio.state.positions:
            if pos.sector and pos.sector != "Unknown":
                continue
            try:
                company = await self.data_feed.get_company_fundamentals(pos.symbol)
                if company and company.sector and company.sector != "Unknown":
                    pos.sector = company.sector
                    updated += 1
                    logger.info("Backfilled sector for %s: %s", pos.symbol, company.sector)
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
