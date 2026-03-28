"""Orchestrator — runs NBA + Events agents on their own schedules in one process."""

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


class Orchestrator:
    """Runs all agents concurrently on their own scan intervals."""

    def __init__(self) -> None:
        self.nba_agent = NBAAgent()
        self.events_agent = EventsAgent()
        self.digest = CombinedDigest(SharedConfig())
        self._shutdown = False

    async def run(self) -> None:
        """Start all agents as concurrent tasks."""
        logger.info("Orchestrator starting — NBA + Events agents")

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
        """Events agent loop — runs on its own interval."""
        logger.info("Events agent task started (interval=%d min)", self.events_agent.config.SCAN_INTERVAL)

        # Stagger start: wait 2 minutes so NBA agent gets first scan
        for _ in range(120):
            if self._shutdown:
                return
            await asyncio.sleep(1)

        while not self._shutdown:
            try:
                await self.events_agent._tick()
            except Exception as e:
                logger.error("Events agent tick error: %s", e, exc_info=True)

            for _ in range(self.events_agent.config.SCAN_INTERVAL * 60):
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
