"""Entry point — async scheduler orchestration."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

from nba_agent.config import Config
from nba_agent.bankroll_manager import BankrollManager
from nba_agent.edge_calculator import EdgeCalculator
from nba_agent.injury_scanner import InjuryScanner
from nba_agent.models import MarketType
from nba_agent.nba_research import NBAResearch
from nba_agent.performance_tracker import PerformanceTracker
from nba_agent.polymarket_scanner import PolymarketScanner
from nba_agent.telegram_alerts import TelegramBot
from nba_agent.trading_engine import TradingEngine
from nba_agent.utils import utcnow

logger = logging.getLogger("nba_agent")


class NBAAgent:
    """Main agent orchestrator — runs scan cycles, exit checks, and summaries."""

    def __init__(self) -> None:
        self.config = Config()
        self.config.ensure_data_dir()

        self.scanner = PolymarketScanner(self.config)
        self.research = NBAResearch(self.config)
        self.injury_scanner = InjuryScanner()
        self.edge_calc = EdgeCalculator(self.config, self.research, self.injury_scanner)
        self.bankroll = BankrollManager(self.config)
        self.engine = TradingEngine(self.config)
        self.tracker = PerformanceTracker(self.config)
        self.telegram = TelegramBot(self.config)

        self._shutdown = False
        self._last_daily: datetime | None = None
        self._last_weekly: datetime | None = None

    async def run(self) -> None:
        """Main loop — schedule all recurring tasks."""
        logger.info("NBA Agent starting — mode=%s bankroll=$%.2f", self.config.TRADING_MODE, self.bankroll.current_bankroll)

        await self.telegram.send_startup_message(
            self.config.TRADING_MODE,
            self.bankroll.current_bankroll,
        )

        # Run the main loop
        while not self._shutdown:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Main loop error: %s", e, exc_info=True)

            # Wait for next scan interval
            for _ in range(self.config.SCAN_INTERVAL * 60):
                if self._shutdown:
                    break
                await asyncio.sleep(1)

    async def _tick(self) -> None:
        """Single cycle: scan, evaluate, trade, check exits, send summaries."""
        now = utcnow()

        # 1. Check for early exits on existing positions
        await self._check_exits()

        # 2. Check for resolved positions
        self._check_resolutions()

        # 3. Scan for new opportunities
        await self._scan_and_trade()

        # 4. Daily summary at 4pm UTC (midnight SGT)
        if now.hour == 16 and (self._last_daily is None or (now - self._last_daily) > timedelta(hours=12)):
            await self._send_daily_summary()
            self._last_daily = now

        # 5. Weekly summary on Monday at 4pm UTC
        if now.weekday() == 0 and now.hour == 16 and (self._last_weekly is None or (now - self._last_weekly) > timedelta(days=5)):
            await self._send_weekly_summary()
            self._last_weekly = now

    async def _scan_and_trade(self) -> None:
        """Scan markets, evaluate edges, and execute trades."""
        if self.bankroll.is_paused:
            logger.info("Trading paused (stop-loss) — skipping scan")
            return

        # Scan for NBA markets
        markets = await self.scanner.scan()
        logger.info("Evaluating %d markets", len(markets))

        open_positions = self.tracker.get_open_positions()

        for market in markets:
            try:
                # Skip if we already have a position in this market
                if self.tracker.has_existing_position(market.id):
                    continue

                # Evaluate edge
                edge_result = await self.edge_calc.evaluate(market)
                if edge_result is None or not edge_result.has_edge:
                    continue

                logger.info(
                    "EDGE FOUND: %s | edge=%.1f%% conf=%s fair=%.2f market=%.2f",
                    market.question,
                    edge_result.edge * 100,
                    edge_result.confidence.value,
                    edge_result.our_fair_price,
                    edge_result.market_price,
                )

                # Calculate bet size
                bet_size = self.bankroll.calculate_bet_size(edge_result)
                if bet_size <= 0:
                    logger.debug("Bet size too small for %s", market.question)
                    continue

                # Check exposure limits
                if not self.bankroll.check_game_exposure(market.slug, open_positions, bet_size):
                    logger.info("Game exposure limit reached for %s", market.slug)
                    continue
                if not self.bankroll.check_total_exposure(open_positions, bet_size):
                    logger.info("Total exposure limit reached")
                    break

                # Execute trade
                position, trade = self.engine.execute_buy(edge_result, bet_size)
                if position and trade:
                    # Save position and trade
                    self.tracker.save_position(position)
                    self.tracker.log_trade(trade)

                    # Update bankroll
                    self.bankroll.current_bankroll -= bet_size
                    self.bankroll.save_state()

                    # Send alert
                    await self.telegram.send_trade_alert(
                        position, edge_result, self.bankroll.current_bankroll
                    )

                    # Refresh open positions
                    open_positions = self.tracker.get_open_positions()

            except Exception as e:
                logger.error("Error processing market %s: %s", market.id, e, exc_info=True)

    async def _check_exits(self) -> None:
        """Check all open positions for early exit conditions."""
        open_positions = self.tracker.get_open_positions()

        for position in open_positions:
            try:
                # Get current price
                current_price = await self.scanner.get_market_price(position.token_id)
                if current_price is None:
                    continue

                # Check if we should exit
                should_exit, reason = self.bankroll.should_exit_early(position, current_price)
                if not should_exit:
                    continue

                logger.info("EARLY EXIT: %s | reason=%s", position.market_question, reason)

                # Execute sell
                trade = self.engine.execute_sell(position, current_price, reason)
                if trade:
                    # Save updated position and trade
                    self.tracker.save_position(position)
                    self.tracker.log_trade(trade)

                    # Update bankroll
                    pnl = position.pnl or 0
                    self.bankroll.update_bankroll(position.cost + pnl)

                    # Send alert
                    await self.telegram.send_exit_alert(
                        position, trade, self.bankroll.current_bankroll
                    )

                    # Send stop-loss alert if triggered
                    if self.bankroll.is_paused:
                        await self.telegram.send_stop_loss_alert(
                            self.bankroll.current_bankroll,
                            self.bankroll.peak_bankroll,
                        )

            except Exception as e:
                logger.error("Error checking exit for %s: %s", position.id, e)

    def _check_resolutions(self) -> None:
        """Check for markets that have ended and resolve positions."""
        resolved = self.tracker.check_resolved_positions()
        for pos in resolved:
            # In paper mode, we just log it — we can't easily determine the outcome without
            # re-querying the market. For now, log that it needs manual review.
            logger.info("Position %s market has ended — needs resolution: %s", pos.id, pos.market_question)

    async def _send_daily_summary(self) -> None:
        """Generate and send daily P&L summary."""
        try:
            stats = self.tracker.get_daily_stats()
            mode = self.config.TRADING_MODE

            await self.telegram.send_daily_summary(
                date_str=stats["date_str"],
                open_positions=stats["open_positions"],
                trades_today=stats["trades_today"],
                buys_today=stats["buys_today"],
                sells_today=stats["sells_today"],
                daily_pnl=stats["daily_pnl"],
                bankroll=self.bankroll.current_bankroll,
                best_trade=stats["best_trade"],
                worst_trade=stats["worst_trade"],
                win_rate=stats["win_rate"],
                avg_edge=stats["avg_edge"],
                mode=mode,
            )
        except Exception as e:
            logger.error("Failed to send daily summary: %s", e)

    async def _send_weekly_summary(self) -> None:
        """Generate and send weekly summary."""
        try:
            stats = self.tracker.get_weekly_stats()
            mode = self.config.TRADING_MODE

            await self.telegram.send_weekly_summary(
                week_str=stats["week_str"],
                total_bets=stats["total_bets"],
                wins=stats["wins"],
                total_pnl=stats["total_pnl"],
                roi=stats["roi"],
                bankroll=self.bankroll.current_bankroll,
                biggest_win=stats["biggest_win"],
                biggest_loss=stats["biggest_loss"],
                expected_wr=stats["expected_wr"],
                actual_wr=stats["actual_wr"],
                mode=mode,
            )
        except Exception as e:
            logger.error("Failed to send weekly summary: %s", e)

    def shutdown(self) -> None:
        """Signal graceful shutdown."""
        logger.info("Shutdown requested")
        self._shutdown = True
        self.bankroll.save_state()


def setup_logging(level: str) -> None:
    """Configure structured logging."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Reduce noise from third-party libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    """Entry point for `python -m nba_agent.main`."""
    config = Config()
    setup_logging(config.LOG_LEVEL)

    agent = NBAAgent()

    # Handle graceful shutdown
    def handle_signal(sig, frame):
        agent.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        agent.shutdown()
        logger.info("Agent stopped by keyboard interrupt")


if __name__ == "__main__":
    main()
