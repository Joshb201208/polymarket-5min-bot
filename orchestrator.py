"""Orchestrator — runs both NBA and NHL agents concurrently.

Single entry point that replaces `python -m nba_agent.main` in systemd.
Both agents run in the same process via asyncio, sharing the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

from nba_agent.config import Config as NBAConfig
from nba_agent.main import NBAAgent
from nba_agent.utils import utcnow
from nhl_agent.main import NHLAgent
from shared.bankroll import get_current_bankroll
from shared.telegram import CombinedTelegramReporter

logger = logging.getLogger("orchestrator")

_shutdown = False


def setup_logging(level: str) -> None:
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _combined_reports(nba_agent: NBAAgent, nhl_agent: NHLAgent) -> None:
    """Send combined daily/weekly Telegram summaries for both sports."""
    global _shutdown
    reporter = CombinedTelegramReporter()
    last_daily: datetime | None = None
    last_weekly: datetime | None = None

    while not _shutdown:
        try:
            now = utcnow()

            # Daily summary at 4pm UTC
            if now.hour == 16 and (last_daily is None or (now - last_daily) > timedelta(hours=12)):
                nba_stats = nba_agent.tracker.get_daily_stats()
                nhl_stats = nhl_agent.tracker.get_daily_stats()
                bankroll = get_current_bankroll()
                mode = nba_agent.config.TRADING_MODE

                await reporter.send_combined_daily_summary(
                    bankroll=bankroll,
                    nba_stats=nba_stats,
                    nhl_stats=nhl_stats,
                    mode=mode,
                )
                last_daily = now
                logger.info("Sent combined daily summary")

            # Weekly summary on Monday at 4pm UTC
            if now.weekday() == 0 and now.hour == 16 and (last_weekly is None or (now - last_weekly) > timedelta(days=5)):
                nba_stats = nba_agent.tracker.get_weekly_stats()
                nhl_stats = nhl_agent.tracker.get_weekly_stats()
                bankroll = get_current_bankroll()
                mode = nba_agent.config.TRADING_MODE

                await reporter.send_combined_weekly_summary(
                    bankroll=bankroll,
                    nba_stats=nba_stats,
                    nhl_stats=nhl_stats,
                    mode=mode,
                )
                last_weekly = now
                logger.info("Sent combined weekly summary")

        except Exception as e:
            logger.error("Combined report error: %s", e, exc_info=True)

        # Check every 5 minutes
        for _ in range(300):
            if _shutdown:
                return
            await asyncio.sleep(1)


async def run_orchestrator() -> None:
    """Run both NBA and NHL agents concurrently."""
    global _shutdown
    nba_agent = NBAAgent()
    nhl_agent = NHLAgent()

    # Handle graceful shutdown for both agents
    def handle_signal(sig, frame):
        global _shutdown
        logger.info("Shutdown signal received — stopping both agents")
        _shutdown = True
        nba_agent.shutdown()
        nhl_agent.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger.info("Orchestrator starting — NBA + NHL agents")
    logger.info("NBA mode=%s | NHL mode=%s", nba_agent.config.TRADING_MODE, nhl_agent.config.TRADING_MODE)

    # Run both agent loops + combined reporting concurrently
    await asyncio.gather(
        _run_with_restart(nba_agent, "NBA"),
        _run_with_restart(nhl_agent, "NHL"),
        _combined_reports(nba_agent, nhl_agent),
    )


async def _run_with_restart(agent, name: str) -> None:
    """Run an agent with automatic restart on crash."""
    while True:
        try:
            logger.info("%s agent loop starting", name)
            await agent.run()
            break  # Clean shutdown
        except Exception as e:
            logger.error("%s agent crashed: %s — restarting in 30s", name, e, exc_info=True)
            await asyncio.sleep(30)
            # Check if orchestrator was told to shut down
            if agent._shutdown:
                break


def main() -> None:
    config = NBAConfig()
    setup_logging(config.LOG_LEVEL)

    try:
        asyncio.run(run_orchestrator())
    except KeyboardInterrupt:
        logger.info("Orchestrator stopped by keyboard interrupt")


if __name__ == "__main__":
    main()
