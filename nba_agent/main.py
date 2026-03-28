"""Entry point — async scheduler orchestration."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

from nba_agent.config import Config
from nba_agent.balldontlie import BDLClient
from nba_agent.bankroll_manager import BankrollManager
from nba_agent.calibrator import Calibrator
from nba_agent.edge_calculator import EdgeCalculator
from nba_agent.injury_scanner import InjuryScanner
from nba_agent.models import MarketType
from nba_agent.nba_research import NBAResearch
from nba_agent.odds_api import OddsAPI
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
        self.odds_api = OddsAPI(self.config)
        self.bdl = BDLClient(self.config)
        self.edge_calc = EdgeCalculator(
            self.config, self.research, self.injury_scanner,
            self.odds_api, self.bdl,
        )
        self.calibrator = Calibrator(self.config)
        self.bankroll = BankrollManager(self.config)
        self.engine = TradingEngine(self.config)
        self.tracker = PerformanceTracker(self.config)
        self.telegram = TelegramBot(self.config)

        # Log data sources on startup
        sources = ["ESPN (always)"]
        if self.odds_api.is_configured:
            sources.append("The Odds API (Vegas lines)")
        if self.bdl.is_configured:
            sources.append("BallDontLie (advanced stats + injuries)")
        logger.info("Data sources: %s", ", ".join(sources))

        # Log calibrator status
        cal = self.calibrator
        if cal.is_active:
            logger.info("Calibrator ACTIVE: %d bets resolved, adjustments applied", cal.total_resolved)
        else:
            remaining = max(0, 200 - cal.total_resolved)
            logger.info("Calibrator observing: %d/%d bets resolved (%d until active)",
                        cal.total_resolved, 200, remaining)

        self._shutdown = False
        self._last_daily: datetime | None = None
        self._last_weekly: datetime | None = None

    async def run(self) -> None:
        """Main loop — schedule all recurring tasks."""
        logger.info("NBA Agent starting — mode=%s bankroll=$%.2f", self.config.TRADING_MODE, self.bankroll.current_bankroll)

        # Startup message logged only, not sent to Telegram
        logger.info("Agent ready — mode=%s bankroll=$%.2f sources=%d markets",
                    self.config.TRADING_MODE, self.bankroll.current_bankroll, 0)

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

        # Write last scan time for system health dashboard
        import json as _json
        status_path = self.config.DATA_DIR / "system_status.json"
        try:
            status = _json.loads(status_path.read_text()) if status_path.exists() else {}
        except Exception:
            status = {}
        status["nba_last_scan"] = now.isoformat()
        try:
            status_path.write_text(_json.dumps(status, default=str))
        except Exception:
            pass

        # 1. Snapshot game-time prices for drift tracking
        await self._snapshot_gametime_prices()

        # 2. Check for early exits on existing positions
        await self._check_exits()

        # 3. Check for resolved positions (auto-collects winnings)
        await self._check_resolutions()

        # 4. Scan for new opportunities
        await self._scan_and_trade()

        # 5. Daily summary at 4pm UTC (midnight SGT)
        if now.hour == 16 and (self._last_daily is None or (now - self._last_daily) > timedelta(hours=12)):
            await self._send_daily_summary()
            self._last_daily = now

        # 6. Weekly summary on Monday at 4pm UTC
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
                # Skip if we already have a position in this market or game
                if self.tracker.has_existing_position(market.id, market.slug):
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

                    # Trade alert logged only (daily report handles Telegram)
                    logger.info("BET PLACED: %s | $%.2f @ %.2f¢ | edge=%.1f%% | conf=%s",
                                position.market_question, bet_size,
                                position.entry_price * 100, edge_result.edge * 100,
                                edge_result.confidence.value)

                    # Refresh open positions
                    open_positions = self.tracker.get_open_positions()

            except Exception as e:
                logger.error("Error processing market %s: %s", market.id, e, exc_info=True)

    async def _snapshot_gametime_prices(self) -> None:
        """Record market price at game time for drift tracking.

        For each open position where game_start_time has passed but
        price_at_gametime hasn't been recorded yet, snapshot the current
        market price. This lets us analyze how much the price drifted
        between our entry and the actual game start.
        """
        now = utcnow()
        open_positions = self.tracker.get_open_positions()

        for pos in open_positions:
            if pos.price_at_gametime is not None:
                continue  # Already recorded
            if not pos.game_start_time:
                continue

            try:
                game_str = pos.game_start_time.replace("Z", "+00:00")
                game_dt = datetime.fromisoformat(game_str)
                if game_dt.tzinfo is None:
                    game_dt = game_dt.replace(tzinfo=now.tzinfo)

                # Record if game has started (within 30 min window)
                if now >= game_dt and (now - game_dt).total_seconds() < 1800:
                    price = await self.scanner.get_market_price(pos.token_id)
                    if price is not None:
                        pos.price_at_gametime = round(price, 4)
                        self.tracker.save_position(pos)
                        drift = (pos.price_at_gametime - pos.entry_price) * 100
                        logger.info(
                            "PRICE DRIFT: %s | entry=%.1f¢ gametime=%.1f¢ drift=%+.1f¢",
                            pos.market_question, pos.entry_price * 100,
                            pos.price_at_gametime * 100, drift,
                        )
            except Exception as e:
                logger.debug("Gametime snapshot error for %s: %s", pos.id, e)

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

                    # Exit alert logged only
                    logger.info("EXIT: %s | P&L=$%.2f | reason=%s",
                                position.market_question, position.pnl or 0, reason)

            except Exception as e:
                logger.error("Error checking exit for %s: %s", position.id, e)

    async def _check_resolutions(self) -> None:
        """Check for markets that have ended and auto-resolve positions."""
        resolved = self.tracker.check_resolved_positions()
        for pos in resolved:
            try:
                # Fetch current price to determine outcome
                current_price = await self.scanner.get_market_price(pos.token_id)
                if current_price is None:
                    # If we can't get price, try to infer from end date passing
                    # Default: assume loss (conservative) — will be corrected on next check
                    logger.info("Cannot get price for resolved position %s — will retry", pos.id)
                    continue

                # Only resolve when the outcome is final:
                # price >= 0.99 = definitively won (market settled)
                # price <= 0.01 = definitively lost (market settled)
                # Anything in between = game still in progress or not yet settled
                if current_price >= 0.99:
                    payout = pos.shares * 1.0
                    pnl = payout - pos.cost
                    result = "WIN"
                elif current_price <= 0.01:
                    payout = 0.0
                    pnl = -pos.cost
                    result = "LOSS"
                else:
                    # Not yet settled — wait for final resolution
                    continue

                # Close the position
                pos.status = "closed"
                pos.exit_price = current_price
                pos.exit_time = utcnow().isoformat()
                pos.pnl = round(pnl, 2)
                pos.exit_reason = f"Market resolved: {result}"
                self.tracker.save_position(pos)

                # Update bankroll — add back cost + P&L
                self.bankroll.update_bankroll(pos.cost + pnl)

                logger.info("RESOLVED: %s | %s | P&L=$%.2f | Bankroll=$%.2f",
                            pos.market_question, result, pnl, self.bankroll.current_bankroll)

                # Record in calibrator for self-learning
                try:
                    self.calibrator.record_result(
                        won=(result == "WIN"),
                        edge=pos.edge_at_entry or 0.05,
                        confidence=pos.confidence or "LOW",
                        market_type=pos.market_slug.split("-")[0] if pos.market_slug else "unknown",
                        side="home" if "home" in (pos.side or "").lower() else "away",
                        pnl=pnl,
                    )
                except Exception as cal_err:
                    logger.warning("Calibrator record failed: %s", cal_err)

            except Exception as e:
                logger.error("Error resolving position %s: %s", pos.id, e)

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
