"""Entry point — async scheduler orchestration for the NHL agent."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

from nhl_agent.config import NHLConfig
from nhl_agent.calibrator import NHLCalibrator
from nhl_agent.edge_calculator import NHLEdgeCalculator
from nhl_agent.nhl_research import NHLResearch
from nhl_agent.odds_client import NHLOddsClient
from nhl_agent.performance_tracker import NHLPerformanceTracker
from nhl_agent.polymarket_scanner import NHLPolymarketScanner
from nhl_agent.trading_engine import NHLTradingEngine, NHLBankrollManager
from nba_agent.utils import utcnow
from shared.config import SharedConfig
from shared import line_tracker, whale_detector, odds_snapshots

logger = logging.getLogger("nhl_agent")


class NHLAgent:
    """Main NHL agent orchestrator."""

    def __init__(self) -> None:
        self.config = NHLConfig()
        self.config.ensure_data_dir()

        self.scanner = NHLPolymarketScanner(self.config)
        self.research = NHLResearch(self.config)
        self.odds_client = NHLOddsClient(self.config)
        self.edge_calc = NHLEdgeCalculator(self.config, self.research, self.odds_client)
        self.calibrator = NHLCalibrator(self.config)
        self.bankroll = NHLBankrollManager(self.config)
        self.engine = NHLTradingEngine(self.config)
        self.tracker = NHLPerformanceTracker(self.config)

        sources = ["NHL API", "MoneyPuck"]
        if self.odds_client.is_configured:
            sources.append("The Odds API (Vegas lines)")
        logger.info("NHL data sources: %s", ", ".join(sources))

        cal = self.calibrator
        if cal.is_active:
            logger.info("NHL Calibrator ACTIVE: %d bets resolved", cal.total_resolved)
        else:
            remaining = max(0, 200 - cal.total_resolved)
            logger.info("NHL Calibrator observing: %d/200 resolved (%d until active)",
                        cal.total_resolved, remaining)

        self._shutdown = False
        self._last_daily: datetime | None = None
        self._last_weekly: datetime | None = None

    async def run(self) -> None:
        """Main loop — scan every ~12 minutes."""
        self.bankroll.reload()
        logger.info("NHL Agent starting — mode=%s bankroll=$%.2f",
                     self.config.TRADING_MODE, self.bankroll.current_bankroll)

        while not self._shutdown:
            try:
                await self._tick()
            except Exception as e:
                logger.error("NHL main loop error: %s", e, exc_info=True)

            for _ in range(self.config.NHL_SCAN_INTERVAL * 60):
                if self._shutdown:
                    break
                await asyncio.sleep(1)

    async def _tick(self) -> None:
        """Single cycle: scan, evaluate, trade, check resolutions."""
        now = utcnow()

        # 1. Record line movement snapshots for all open positions
        await self._record_line_snapshots()

        # 2. Check whale / sharp money movements
        await self._check_whale_movements()

        # 3. Snapshot game-time prices
        await self._snapshot_gametime_prices()

        # 4. Auto-hedge / stop-loss early exits
        await self._check_early_exits()

        # 5. Check for resolved positions
        await self._check_resolutions()

        # 6. Scan for new opportunities (also records odds snapshots)
        await self._scan_and_trade()

        # 7. Prune old tracking data
        line_tracker.prune_old_data()
        whale_detector.prune_old_data()
        odds_snapshots.prune_old_data()

        # 8. Daily summary at 4pm UTC
        if now.hour == 16 and (self._last_daily is None or (now - self._last_daily) > timedelta(hours=12)):
            self._last_daily = now

        # 9. Weekly summary on Monday
        if now.weekday() == 0 and now.hour == 16 and (self._last_weekly is None or (now - self._last_weekly) > timedelta(days=5)):
            self._last_weekly = now

    async def _scan_and_trade(self) -> None:
        """Scan NHL markets, evaluate edges, execute trades."""
        self.bankroll.reload()

        if self.bankroll.is_paused:
            logger.info("NHL trading paused (stop-loss)")
            return

        markets = await self.scanner.scan()
        logger.info("NHL evaluating %d markets", len(markets))

        open_positions = self.tracker.get_open_positions()

        for market in markets:
            try:
                if self.tracker.has_existing_position(market.id, market.slug):
                    continue

                edge_result = await self.edge_calc.evaluate(market)

                # Record odds snapshot for every evaluated market
                if edge_result is not None:
                    try:
                        odds_snapshots.record_from_evaluation(
                            sport="nhl",
                            market_question=market.question,
                            market_slug=market.slug,
                            game_date=market.end_date[:10] if market.end_date else "",
                            outcome_prices=market.outcome_prices,
                            our_fair_price=edge_result.our_fair_price,
                            side_index=edge_result.side_index,
                        )
                    except Exception as e:
                        logger.debug("Odds snapshot error: %s", e)

                if edge_result is None or not edge_result.has_edge:
                    continue

                logger.info(
                    "NHL EDGE: %s | edge=%.1f%% conf=%s fair=%.2f market=%.2f",
                    market.question,
                    edge_result.edge * 100,
                    edge_result.confidence.value,
                    edge_result.our_fair_price,
                    edge_result.market_price,
                )

                bet_size = self.bankroll.calculate_bet_size(edge_result)
                if bet_size <= 0:
                    continue

                if not self.bankroll.check_game_exposure(market.slug, open_positions, bet_size):
                    logger.info("NHL game exposure limit for %s", market.slug)
                    continue

                position, trade = self.engine.execute_buy(edge_result, bet_size)
                if position and trade:
                    self.tracker.save_position(position)
                    self.tracker.log_trade(trade)

                    self.bankroll.current_bankroll -= bet_size
                    self.bankroll.save_state()

                    logger.info("NHL BET: %s | $%.2f @ %.2f¢ | edge=%.1f%% | conf=%s",
                                position.market_question, bet_size,
                                position.entry_price * 100, edge_result.edge * 100,
                                edge_result.confidence.value)

                    open_positions = self.tracker.get_open_positions()

            except Exception as e:
                logger.error("NHL error processing %s: %s", market.id, e, exc_info=True)

    async def _record_line_snapshots(self) -> None:
        """Record current market price for all open positions (line tracking)."""
        open_positions = self.tracker.get_open_positions()
        config = SharedConfig()

        for pos in open_positions:
            try:
                current_price = await self.scanner.get_market_price(pos.token_id)
                if current_price is None:
                    continue

                alert = line_tracker.record_snapshot(
                    position_id=pos.id,
                    token_id=pos.token_id,
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    entry_time=pos.entry_time,
                    game_start_time=pos.game_start_time,
                    sport="nhl",
                    market_question=pos.market_question,
                )

                if alert:
                    await line_tracker.send_line_alert(alert, config)
            except Exception as e:
                logger.debug("Line snapshot error for %s: %s", pos.id, e)

    async def _check_whale_movements(self) -> None:
        """Check all scanned markets for sharp price movements."""
        try:
            markets = await self.scanner.scan()
            open_positions = self.tracker.get_open_positions()
            position_token_ids = {pos.token_id for pos in open_positions}

            market_dicts = []
            for m in markets:
                market_dicts.append({
                    "id": m.id,
                    "question": m.question,
                    "outcome_prices": m.outcome_prices,
                    "clob_token_ids": m.clob_token_ids,
                })

            movements = whale_detector.check_movements(
                markets=market_dicts,
                open_position_token_ids=position_token_ids,
                sport="nhl",
            )

            config = SharedConfig()
            for movement in movements:
                await whale_detector.send_whale_alert(movement, config)
        except Exception as e:
            logger.debug("NHL whale detector error: %s", e)

    async def _check_early_exits(self) -> None:
        """Check open positions for auto-hedge (+30%) and stop-loss (-50%) exits."""
        open_positions = self.tracker.get_open_positions()
        config = SharedConfig()

        for pos in open_positions:
            try:
                current_price = await self.scanner.get_market_price(pos.token_id)
                if current_price is None:
                    continue

                # Calculate unrealized P&L
                current_value = pos.shares * current_price
                unrealized_pnl = current_value - pos.cost
                unrealized_pct = unrealized_pnl / pos.cost if pos.cost > 0 else 0

                # Only exit if game hasn't started yet
                if not self._is_pre_game(pos):
                    continue

                reason = None

                if unrealized_pct >= config.AUTO_HEDGE_PCT:
                    reason = f"AUTO_HEDGE: +{unrealized_pct * 100:.0f}%"
                elif unrealized_pct <= config.STOP_LOSS_PCT:
                    reason = f"STOP_LOSS: {unrealized_pct * 100:.0f}%"

                if reason is None:
                    continue

                trade = self.engine.execute_sell(pos, current_price, reason)
                if trade:
                    self.tracker.save_position(pos)
                    self.tracker.log_trade(trade)
                    self.bankroll.update_bankroll(pos.cost + (pos.pnl or 0))
                    line_tracker.mark_position_closed(pos.id)

                    logger.info(
                        "NHL %s: %s | sold @ %.2f¢ | P&L=$%.2f (%+.0f%%)",
                        "AUTO-HEDGE" if "AUTO_HEDGE" in reason else "STOP-LOSS",
                        pos.market_question, current_price * 100,
                        pos.pnl or 0, unrealized_pct * 100,
                    )

            except Exception as e:
                logger.error("NHL early exit check error for %s: %s", pos.id, e)

    def _is_pre_game(self, pos) -> bool:
        """Check if the game hasn't started yet."""
        if not pos.game_start_time:
            return True
        try:
            now = utcnow()
            game_str = pos.game_start_time.replace("Z", "+00:00")
            game_dt = datetime.fromisoformat(game_str)
            if game_dt.tzinfo is None:
                game_dt = game_dt.replace(tzinfo=now.tzinfo)
            return now < game_dt
        except (ValueError, TypeError):
            return True

    async def _snapshot_gametime_prices(self) -> None:
        """Record market price at game time for drift tracking."""
        now = utcnow()
        open_positions = self.tracker.get_open_positions()

        for pos in open_positions:
            if pos.price_at_gametime is not None:
                continue
            if not pos.game_start_time:
                continue
            try:
                game_str = pos.game_start_time.replace("Z", "+00:00")
                game_dt = datetime.fromisoformat(game_str)
                if game_dt.tzinfo is None:
                    game_dt = game_dt.replace(tzinfo=now.tzinfo)
                if now >= game_dt and (now - game_dt).total_seconds() < 1800:
                    price = await self.scanner.get_market_price(pos.token_id)
                    if price is not None:
                        pos.price_at_gametime = round(price, 4)
                        self.tracker.save_position(pos)
            except Exception:
                pass

    async def _check_resolutions(self) -> None:
        """Check for settled NHL markets and auto-resolve positions."""
        resolved = self.tracker.check_resolved_positions()
        for pos in resolved:
            try:
                current_price = await self.scanner.get_market_price(pos.token_id)
                if current_price is None:
                    continue

                if current_price >= 0.99:
                    payout = pos.shares * 1.0
                    pnl = payout - pos.cost
                    result = "WIN"
                elif current_price <= 0.01:
                    payout = 0.0
                    pnl = -pos.cost
                    result = "LOSS"
                else:
                    continue  # Not yet settled

                pos.status = "closed"
                pos.exit_price = current_price
                pos.exit_time = utcnow().isoformat()
                pos.pnl = round(pnl, 2)
                pos.exit_reason = f"Market resolved: {result}"
                self.tracker.save_position(pos)

                self.bankroll.update_bankroll(pos.cost + pnl)

                logger.info("NHL RESOLVED: %s | %s | P&L=$%.2f",
                            pos.market_question, result, pnl)

                try:
                    self.calibrator.record_result(
                        won=(result == "WIN"),
                        edge=pos.edge_at_entry or 0.05,
                        confidence=pos.confidence or "LOW",
                        side="home" if "home" in (pos.side or "").lower() else "away",
                        pnl=pnl,
                    )
                except Exception as cal_err:
                    logger.warning("NHL calibrator record failed: %s", cal_err)

            except Exception as e:
                logger.error("NHL error resolving %s: %s", pos.id, e)

    def shutdown(self) -> None:
        logger.info("NHL Agent shutdown requested")
        self._shutdown = True
        self.bankroll.save_state()


def setup_logging(level: str) -> None:
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    """Entry point for `python -m nhl_agent.main`."""
    config = NHLConfig()
    setup_logging(config.LOG_LEVEL)

    agent = NHLAgent()

    def handle_signal(sig, frame):
        agent.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        agent.shutdown()
        logger.info("NHL Agent stopped by keyboard interrupt")


if __name__ == "__main__":
    main()
