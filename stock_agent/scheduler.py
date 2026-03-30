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
        self._shutdown = False
        self._last_monitor_time: datetime | None = None
        self._daily_summary_sent_today: str | None = None

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
        # Weekly analysis: Sunday, between 6-8 PM ET (good time before Asia Monday morning)
        if self._should_run_weekly(now_et):
            logger.info("Starting weekly analysis cycle")
            await self._run_weekly_analysis()

        # Market monitoring during open hours
        if self._is_market_open(now_et):
            if self._should_monitor(now_et):
                logger.info("Running market monitoring")
                await self._run_market_monitoring()

        # Daily summary at market close
        if self._should_send_daily_summary(now_et):
            logger.info("Sending daily summary")
            await self._send_daily_summary()

    # ── Schedule checks ──────────────────────────────────────────────

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

    async def _run_weekly_analysis(self):
        """Full weekly research cycle."""
        try:
            await asyncio.gather(
                self.telegram.send_message("\U0001f50d <b>Weekly Analysis Starting</b>"),
                self.discord.send_system_log("INFO", "Weekly analysis cycle starting"),
                return_exceptions=True,
            )

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
            companies: list[CompanyData] = []
            for sym in symbols:
                try:
                    data = await self.data_feed.get_company_fundamentals(sym)
                    if data:
                        companies.append(data)
                except Exception as e:
                    logger.warning("Failed to fetch fundamentals for %s: %s", sym, e)

            if not companies:
                await asyncio.gather(
                    self.telegram.send_message("\u26a0\ufe0f No fundamental data retrieved"),
                    self.discord.send_system_log("WARNING", "No fundamental data retrieved"),
                    return_exceptions=True,
                )
                return

            logger.info("Fetched fundamentals for %d companies", len(companies))

            # 3. GPT-4.1 Nano screening
            ranked = await self.screener.screen_candidates(companies)
            top_symbols = [r["symbol"] for r in ranked[: self.config.DEEP_ANALYSIS_SIZE]]
            logger.info("Top %d for deep analysis: %s", len(top_symbols), top_symbols)

            # Send screener results to Discord
            try:
                await self.discord.send_screener_results(ranked[:25])
            except Exception as e:
                logger.error("Failed to send screener results to Discord: %s", e)

            # 4. Deep analysis with Perplexity Sonar Pro
            signals: list[Signal] = []
            for sym in top_symbols:
                company = next((c for c in companies if c.symbol == sym), None)
                if not company:
                    continue

                try:
                    thesis = await self.analyst.analyze_stock(sym, company)
                    if not thesis:
                        continue

                    self.portfolio.update_thesis(sym, thesis)

                    # Send thesis and analysis to Discord
                    try:
                        await asyncio.gather(
                            self.discord.send_thesis(thesis),
                            self.discord.send_analysis_report(
                                sym, thesis.summary + "\n\n**Bull Case:** " + thesis.bull_case + "\n\n**Bear Case:** " + thesis.bear_case,
                                thesis.sources,
                            ),
                            return_exceptions=True,
                        )
                    except Exception:
                        pass

                    if thesis.direction == "BUY" and thesis.conviction >= self.config.MIN_CONVICTION:
                        pv = self.portfolio.get_portfolio_value()
                        size_pct = (thesis.conviction / 10.0) * self.config.MAX_POSITION_PCT
                        signal = Signal(
                            symbol=sym,
                            action="BUY",
                            conviction=thesis.conviction,
                            thesis=thesis,
                            entry_price=company.price,
                            position_size_pct=size_pct,
                            generated_at=_now_utc(),
                        )
                        signals.append(signal)
                        logger.info("BUY signal: %s conviction=%d", sym, thesis.conviction)
                except Exception as e:
                    logger.error("Analysis failed for %s: %s", sym, e)

            # 5. Re-analyze existing positions
            await self._re_analyze_existing_positions(companies)

            # 6. Execute trades from signals
            executed_signals = await self._execute_signals(signals)

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
            logger.info("Daily summary sent — PV=$%,.0f, Day P&L=$%+,.0f", pv, day_pnl)

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
            sector = signal.thesis.summary[:50]  # fallback
            # Try to get actual sector from thesis/data
            company_sector = "Unknown"
            for p in self.portfolio.state.positions:
                if p.sector and p.sector != "Unknown":
                    pass  # We need it from company data
            # Use the active thesis data
            thesis_data = self.portfolio.state.active_theses.get(sym, {})
            if isinstance(thesis_data, dict):
                company_sector = thesis_data.get("sector", "Unknown")

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
                logger.info("Risk check blocked %s: %s", sym, reason)
                continue

            pv = self.portfolio.get_portfolio_value()
            qty = self.risk_manager.calculate_position_size(pv, price, signal.conviction)
            if qty <= 0:
                logger.info("Position size 0 for %s — skipping", sym)
                continue

            stop_loss = self.risk_manager.get_stop_loss_price(price)
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
                "Executed SELL: %s x%d @ $%.2f — P&L: $%+,.0f (%+.1%%)",
                position.symbol, position.shares, sell_price, pnl, pnl_pct * 100,
            )

        except Exception as e:
            logger.exception("Sell execution failed for %s: %s", position.symbol, e)
            await asyncio.gather(
                self.telegram.send_error_alert(f"Sell failed for {position.symbol}: {e}"),
                self.discord.send_error(str(e), context=f"Sell execution for {position.symbol}"),
                return_exceptions=True,
            )

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

        for resource in [self.data_feed, self.analyst, self.screener, self.executor, self.telegram, self.discord]:
            try:
                await resource.close()
            except Exception:
                pass

        logger.info("Cleanup complete")
