"""Orchestrator — runs NBA + Events + Intelligence agents on their own schedules.

Wires all advanced systems: lifecycle, regime, calibrator, dedup, live quality,
smart executor, and Telegram command handler.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

from nba_agent.config import Config as NBAConfig
from nba_agent.main import NBAAgent, setup_logging
from events_agent.main import EventsAgent
from shared.telegram_digest import CombinedDigest
from shared.config import SharedConfig
from nba_agent.utils import utcnow, load_json, atomic_json_write

logger = logging.getLogger("orchestrator")


def _try_init_intelligence():
    """Try to initialize IntelligenceManager; return None if modules not ready."""
    try:
        from intelligence.manager import IntelligenceManager
        return IntelligenceManager()
    except Exception as e:
        logger.warning("Intelligence modules not available yet: %s", e)
        return None


def _try_init_lifecycle():
    """Try to initialize EventLifecycle."""
    try:
        from intelligence.lifecycle import EventLifecycle
        return EventLifecycle()
    except Exception as e:
        logger.warning("Lifecycle module not available yet: %s", e)
        return None


def _try_init_regime():
    """Try to initialize RegimeDetector."""
    try:
        from intelligence.regime import RegimeDetector
        return RegimeDetector()
    except Exception as e:
        logger.warning("Regime module not available yet: %s", e)
        return None


def _try_init_calibrator():
    """Try to initialize SignalCalibrator."""
    try:
        from intelligence.calibrator import SignalCalibrator
        return SignalCalibrator()
    except Exception as e:
        logger.warning("Calibrator module not available yet: %s", e)
        return None


def _try_init_dedup():
    """Try to initialize SignalDeduplicator."""
    try:
        from intelligence.dedup import SignalDeduplicator
        return SignalDeduplicator()
    except Exception as e:
        logger.warning("Dedup module not available yet: %s", e)
        return None


def _try_init_live_quality():
    """Try to initialize LiveQualityScorer."""
    try:
        from intelligence.live_quality import LiveQualityScorer
        return LiveQualityScorer()
    except Exception as e:
        logger.warning("Live quality module not available yet: %s", e)
        return None


def _try_init_smart_executor(config):
    """Try to initialize SmartExecutor."""
    try:
        from events_agent.smart_executor import SmartExecutor
        return SmartExecutor(config)
    except Exception as e:
        logger.warning("Smart executor not available yet: %s", e)
        return None


def _try_init_telegram_commands(config):
    """Try to initialize EventsTelegramCommands."""
    try:
        from events_agent.telegram_commands import EventsTelegramCommands
        return EventsTelegramCommands(config)
    except Exception as e:
        logger.warning("Telegram commands not available yet: %s", e)
        return None


class Orchestrator:
    """Runs all agents concurrently on their own scan intervals."""

    def __init__(self) -> None:
        self.nba_agent = NBAAgent()
        self.events_agent = EventsAgent()
        self.digest = CombinedDigest(SharedConfig())
        self.intelligence = _try_init_intelligence()
        self._shutdown = False

        # Advanced systems
        self.lifecycle = _try_init_lifecycle()
        self.regime_detector = _try_init_regime()
        self.calibrator = _try_init_calibrator()
        self.dedup = _try_init_dedup()
        self.live_quality = _try_init_live_quality()
        self.smart_executor = _try_init_smart_executor(self.events_agent.config)
        self.telegram_commands = _try_init_telegram_commands(self.events_agent.config)

        # Wire subsystem references into telegram commands
        if self.telegram_commands:
            self.telegram_commands.portfolio = self.events_agent.portfolio
            self.telegram_commands.scanner = self.events_agent.scanner
            self.telegram_commands.executor = self.events_agent.executor
            self.telegram_commands.smart_executor = self.smart_executor
            self.telegram_commands.intelligence = self.intelligence
            self.telegram_commands.lifecycle = self.lifecycle
            self.telegram_commands.regime_detector = self.regime_detector
            self.telegram_commands.calibrator = self.calibrator
            self.telegram_commands.live_quality = self.live_quality
            self.telegram_commands.dedup = self.dedup

        # Calibration tracking
        self._last_calibration: datetime | None = None

    async def run(self) -> None:
        """Start all agents as concurrent tasks."""
        logger.info("Orchestrator starting — NBA + Events agents")

        # Start persistent intelligence connections
        if self.intelligence:
            try:
                await self.intelligence.start_persistent()
                logger.info("Intelligence persistent connections started")
            except Exception as e:
                logger.error("Failed to start intelligence persistent connections: %s", e)

        # Run agents concurrently
        tasks = [
            asyncio.create_task(self._run_nba()),
            asyncio.create_task(self._run_events()),
            asyncio.create_task(self._run_digest()),
            asyncio.create_task(self._run_telegram_poller()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Orchestrator tasks cancelled")

    async def _run_nba(self) -> None:
        """NBA agent loop — runs on its own interval."""
        logger.info("NBA agent task started (interval=%d min)", self.nba_agent.config.SCAN_INTERVAL)
        while not self._shutdown:
            try:
                await self.nba_agent._tick()
            except Exception as e:
                logger.error("NBA agent tick error: %s", e, exc_info=True)

            for _ in range(self.nba_agent.config.SCAN_INTERVAL * 60):
                if self._shutdown:
                    break
                await asyncio.sleep(1)

    async def _run_events(self) -> None:
        """Events agent loop — runs on its own interval.

        Full pipeline: lifecycle → regime → intelligence → dedup → decay →
        quality → composite → edge check → smart_executor.
        """
        logger.info("Events agent task started (interval=%d min)", self.events_agent.config.SCAN_INTERVAL)

        # Stagger start: wait 2 minutes so NBA agent gets first scan
        for _ in range(120):
            if self._shutdown:
                return
            await asyncio.sleep(1)

        while not self._shutdown:
            try:
                # Check if events agent is paused
                if self.telegram_commands and self.telegram_commands.is_paused:
                    logger.info("Events agent paused — skipping scan")
                else:
                    # Run events tick
                    await self.events_agent._tick()

                    # Check and execute pending tranches
                    if self.smart_executor:
                        try:
                            trades = await self.smart_executor.execute_pending_tranches(
                                scanner=self.events_agent.scanner,
                            )
                            for trade in trades:
                                self.events_agent.portfolio.log_trade(trade)
                            if trades:
                                logger.info("Executed %d pending tranches", len(trades))
                            self.smart_executor.cleanup_old_tranches()
                        except Exception as e:
                            logger.error("Pending tranche error: %s", e)

                    # Run intelligence scan cycle if available
                    if self.intelligence:
                        try:
                            await self._run_intelligence_cycle()
                        except Exception as e:
                            logger.error("Intelligence cycle error: %s", e)

                    # Run calibrator daily
                    await self._maybe_run_calibration()

            except Exception as e:
                logger.error("Events agent tick error: %s", e, exc_info=True)

            for _ in range(self.events_agent.config.SCAN_INTERVAL * 60):
                if self._shutdown:
                    break
                await asyncio.sleep(1)

    async def _run_intelligence_cycle(self) -> None:
        """Run intelligence scan with dedup, decay, and quality adjustments."""
        if not self.intelligence:
            return

        try:
            # Get active markets from events scanner
            active_markets = []
            if hasattr(self.events_agent, "scanner") and self.events_agent.scanner:
                try:
                    active_markets = await self.events_agent.scanner.scan()
                except Exception:
                    pass

            # Get open positions
            open_positions = []
            if hasattr(self.events_agent, "portfolio") and self.events_agent.portfolio:
                try:
                    open_positions = self.events_agent.portfolio.get_open_positions()
                except Exception:
                    pass

            report = await self.intelligence.run_scan_cycle(
                active_markets=active_markets,
                open_positions=open_positions,
            )

            # Apply dedup + decay to signals if dedup module is available
            raw_signal_count = len(report.signals)
            if self.dedup and report.signals:
                try:
                    deduped = self.dedup.deduplicate(report.signals)
                    decayed = self.dedup.apply_decay(deduped)
                    decay_dropped = len(deduped) - len(decayed)
                    report.signals[:] = decayed

                    # Save dedup stats for dashboard
                    data_dir = self.events_agent.config.DATA_DIR
                    atomic_json_write(data_dir / "dedup_stats.json", {
                        "total_raw": raw_signal_count,
                        "total_deduped": len(deduped),
                        "decay_dropped": decay_dropped,
                        "clusters": [],  # Filled by dedup module if available
                        "timestamp": utcnow().isoformat(),
                    })
                except Exception as e:
                    logger.error("Dedup/decay error: %s", e)

            # Run lifecycle + regime assessments and save for dashboard
            data_dir = self.events_agent.config.DATA_DIR
            if self.lifecycle and active_markets:
                try:
                    assessments = {}
                    for market in active_markets[:50]:
                        try:
                            assessment = self.lifecycle.classify(market)
                            key = market.id
                            if hasattr(assessment, "__dict__"):
                                assessments[key] = {
                                    "stage": getattr(assessment, "stage", "unknown"),
                                    "days_remaining": getattr(assessment, "days_remaining", 0),
                                    "min_edge": getattr(assessment, "min_edge", 0.05),
                                    "max_bet_pct": getattr(assessment, "max_bet_pct", 0.02),
                                    "hold_strategy": getattr(assessment, "hold_strategy", "hold"),
                                    "take_profit": getattr(assessment, "take_profit", 0.30),
                                    "stop_loss": getattr(assessment, "stop_loss", 0.25),
                                    "market_question": market.question[:80],
                                }
                            elif isinstance(assessment, dict):
                                assessment["market_question"] = market.question[:80]
                                assessments[key] = assessment
                        except Exception:
                            pass
                    atomic_json_write(data_dir / "lifecycle_assessments.json", {
                        "assessments": assessments,
                        "timestamp": utcnow().isoformat(),
                    })
                except Exception as e:
                    logger.error("Lifecycle assessment error: %s", e)

            if self.regime_detector and active_markets:
                try:
                    assessments = {}
                    for market in active_markets[:50]:
                        try:
                            assessment = self.regime_detector.detect(
                                market_id=market.id,
                                price_history=[],
                                volume_history=[],
                            )
                            key = market.id
                            if hasattr(assessment, "__dict__"):
                                assessments[key] = {
                                    "regime": getattr(assessment, "regime", "stale"),
                                    "volatility": getattr(assessment, "volatility", 0),
                                    "trend_strength": getattr(assessment, "trend_strength", 0),
                                    "volume_ratio": getattr(assessment, "volume_ratio", 0),
                                    "edge_multiplier": getattr(assessment, "edge_multiplier", 1.0),
                                    "size_multiplier": getattr(assessment, "size_multiplier", 1.0),
                                    "recommendation": getattr(assessment, "recommendation", "trade"),
                                    "market_question": market.question[:80],
                                }
                            elif isinstance(assessment, dict):
                                assessment["market_question"] = market.question[:80]
                                assessments[key] = assessment
                        except Exception:
                            pass
                    atomic_json_write(data_dir / "regime_assessments.json", {
                        "assessments": assessments,
                        "timestamp": utcnow().isoformat(),
                    })
                except Exception as e:
                    logger.error("Regime assessment error: %s", e)

            logger.info(
                "Intelligence cycle complete: %d signals, %d scores",
                len(report.signals),
                len(report.scores),
            )
        except Exception as e:
            logger.error("Intelligence scan cycle failed: %s", e)

    async def _maybe_run_calibration(self) -> None:
        """Run calibrator if 24h have passed since last run."""
        if not self.calibrator:
            return

        now = utcnow()
        if self._last_calibration and (now - self._last_calibration) < timedelta(hours=24):
            return

        try:
            new_weights = self.calibrator.calibrate()
            if new_weights:
                self._last_calibration = now
                logger.info("Calibration complete: %s", new_weights)
        except Exception as e:
            logger.error("Calibration error: %s", e)

    async def _run_telegram_poller(self) -> None:
        """Poll for Telegram commands every 5 seconds."""
        if not self.telegram_commands:
            return

        logger.info("Telegram command poller started")
        while not self._shutdown:
            try:
                await self.telegram_commands.poll_and_handle()
            except Exception as e:
                logger.debug("Telegram poll error: %s", e)

            for _ in range(5):
                if self._shutdown:
                    break
                await asyncio.sleep(1)

    async def _run_digest(self) -> None:
        """Combined digest loop — checks every 10 minutes if it's time to send."""
        while not self._shutdown:
            try:
                if self.digest.should_send():
                    await self.digest.send_combined_digest()
            except Exception as e:
                logger.error("Digest error: %s", e, exc_info=True)

            # Check every 10 minutes
            for _ in range(600):
                if self._shutdown:
                    break
                await asyncio.sleep(1)

    def shutdown(self) -> None:
        """Signal graceful shutdown to all agents."""
        logger.info("Orchestrator shutdown requested")
        self._shutdown = True
        self.nba_agent.shutdown()
        self.events_agent.shutdown()


def main() -> None:
    """Entry point for `python orchestrator.py`."""
    config = NBAConfig()
    setup_logging(config.LOG_LEVEL)

    orch = Orchestrator()

    def handle_signal(sig, frame):
        orch.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger.info(
        "Starting orchestrator — NBA (every %d min) + Events (every %d min)",
        orch.nba_agent.config.SCAN_INTERVAL,
        orch.events_agent.config.SCAN_INTERVAL,
    )

    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        orch.shutdown()
        logger.info("Orchestrator stopped by keyboard interrupt")


if __name__ == "__main__":
    main()
