"""Orchestrator — runs NBA + Events + Intelligence agents on their own schedules."""

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
from nba_agent.utils import utcnow

logger = logging.getLogger("orchestrator")


def _try_init_intelligence():
    """Try to initialize IntelligenceManager; return None if modules not ready."""
    try:
        from intelligence.manager import IntelligenceManager
        return IntelligenceManager()
    except Exception as e:
        logger.warning("Intelligence modules not available yet: %s", e)
        return None


class Orchestrator:
    """Runs all agents concurrently on their own scan intervals."""

    def __init__(self) -> None:
        self.nba_agent = NBAAgent()
        self.events_agent = EventsAgent()
        self.digest = CombinedDigest(SharedConfig())
        self.intelligence = _try_init_intelligence()
        self._shutdown = False

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

        When intelligence modules are available, runs intelligence scan
        in parallel with the events tick and saves the report for dashboard.
        """
        logger.info("Events agent task started (interval=%d min)", self.events_agent.config.SCAN_INTERVAL)

        # Stagger start: wait 2 minutes so NBA agent gets first scan
        for _ in range(120):
            if self._shutdown:
                return
            await asyncio.sleep(1)

        while not self._shutdown:
            try:
                # Run events tick (intelligence integration happens via
                # the analyzer when it calls analyze_with_intelligence)
                await self.events_agent._tick()

                # Run intelligence scan cycle if available
                if self.intelligence:
                    try:
                        await self._run_intelligence_cycle()
                    except Exception as e:
                        logger.error("Intelligence cycle error: %s", e)
            except Exception as e:
                logger.error("Events agent tick error: %s", e, exc_info=True)

            for _ in range(self.events_agent.config.SCAN_INTERVAL * 60):
                if self._shutdown:
                    break
                await asyncio.sleep(1)

    async def _run_intelligence_cycle(self) -> None:
        """Run intelligence scan and save report for dashboard consumption."""
        if not self.intelligence:
            return

        try:
            # Get active markets from events scanner if available
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
                    open_positions = self.events_agent.portfolio.get_positions()
                except Exception:
                    pass

            report = await self.intelligence.run_scan_cycle(
                active_markets=active_markets,
                open_positions=open_positions,
            )

            logger.info(
                "Intelligence cycle complete: %d signals, %d scores",
                len(report.signals),
                len(report.scores),
            )
        except Exception as e:
            logger.error("Intelligence scan cycle failed: %s", e)

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
