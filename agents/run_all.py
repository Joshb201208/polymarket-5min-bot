"""
Orchestrator — runs all 3 Polymarket AI agents on independent schedules.

Uses threading with staggered start times to avoid API rate limits.
Each agent runs in its own thread on its own interval:
  - Agent 1 (Events):  every 2 hours
  - Agent 2 (Soccer):  every 1 hour
  - Agent 3 (NBA):     every 1 hour

Daily summary sent at 16:00 UTC (midnight SGT).
"""

import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path so `agents.*` imports work
# when invoked as `python -m agents.run_all` from the repo root.
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.agent1_events.main import run_agent1, get_daily_stats as stats1, reset_daily_stats as reset1
from agents.agent2_soccer.main import run_agent2, get_daily_stats as stats2, reset_daily_stats as reset2
from agents.agent3_nba.main import run_agent3, get_daily_stats as stats3, reset_daily_stats as reset3
from agents.common.config import (
    LOG_LEVEL,
    SCAN_INTERVAL_EVENTS,
    SCAN_INTERVAL_NBA,
    SCAN_INTERVAL_SOCCER,
)
from agents.common.paper_tracker import PaperTracker
from agents.common.telegram_alerts import send_daily_summary, send_startup_alert

# ── Logging setup ─────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agents.orchestrator")

# Shared paper tracker (thread-safe via GIL for dict/list ops)
paper_tracker = PaperTracker()

# Shutdown event
_shutdown = threading.Event()


def _agent_loop(name: str, run_fn, interval: int, stagger: int) -> None:
    """Run an agent function in a loop with the given interval."""
    # Stagger start to avoid simultaneous API calls
    logger.info("%s: starting in %ds (stagger)", name, stagger)
    if _shutdown.wait(timeout=stagger):
        return

    while not _shutdown.is_set():
        try:
            run_fn(paper_tracker)
        except Exception as exc:
            logger.error("%s crashed: %s\n%s", name, exc, traceback.format_exc())

        # Check for resolved paper trades after each cycle
        try:
            resolved = paper_tracker.check_resolutions()
            if resolved:
                from agents.common.telegram_alerts import send_market_resolved
                for trade in resolved:
                    send_market_resolved(
                        agent_name=trade["agent_name"],
                        market_title=trade["market_question"],
                        direction=trade["direction"],
                        entry_price=trade["entry_price"],
                        outcome=trade["outcome"],
                        pnl=trade["pnl"],
                    )
        except Exception as exc:
            logger.error("Resolution check failed: %s", exc)

        logger.info("%s: sleeping %ds until next cycle", name, interval)
        if _shutdown.wait(timeout=interval):
            break


def _daily_summary_loop() -> None:
    """Send a daily performance summary at 16:00 UTC (midnight SGT)."""
    while not _shutdown.is_set():
        now = datetime.now(timezone.utc)
        # Calculate seconds until next 16:00 UTC
        target_hour = 16
        if now.hour < target_hour:
            next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        else:
            # Tomorrow — use timedelta to handle month/year rollovers safely
            from datetime import timedelta
            tomorrow = now + timedelta(days=1)
            next_run = tomorrow.replace(hour=target_hour, minute=0, second=0, microsecond=0)

        wait_seconds = (next_run - now).total_seconds()
        if wait_seconds < 0:
            wait_seconds = 3600  # fallback: 1 hour
        wait_seconds = min(wait_seconds, 86400)  # cap at 24h

        logger.info("Daily summary scheduled in %.0f seconds", wait_seconds)
        if _shutdown.wait(timeout=wait_seconds):
            break

        try:
            agent_stats = [stats1(), stats2(), stats3()]
            paper_stats = paper_tracker.get_stats()
            active = paper_tracker.get_active_count()
            send_daily_summary(agent_stats, paper_stats, active)
            logger.info("Daily summary sent")

            # Reset daily counters
            reset1()
            reset2()
            reset3()
        except Exception as exc:
            logger.error("Daily summary failed: %s", exc)


def main() -> None:
    """Start all agent threads and the daily summary scheduler."""
    logger.info("=" * 60)
    logger.info("Polymarket AI Betting Agents — Starting")
    logger.info("=" * 60)

    # Ensure data directory exists
    Path("data").mkdir(exist_ok=True)

    # Send startup notification
    send_startup_alert()

    # Launch agent threads with staggered starts
    threads = [
        threading.Thread(
            target=_agent_loop,
            args=("Agent 1 (Events)", run_agent1, SCAN_INTERVAL_EVENTS, 0),
            daemon=True,
            name="agent1-events",
        ),
        threading.Thread(
            target=_agent_loop,
            args=("Agent 2 (Soccer)", run_agent2, SCAN_INTERVAL_SOCCER, 30),
            daemon=True,
            name="agent2-soccer",
        ),
        threading.Thread(
            target=_agent_loop,
            args=("Agent 3 (NBA)", run_agent3, SCAN_INTERVAL_NBA, 60),
            daemon=True,
            name="agent3-nba",
        ),
        threading.Thread(
            target=_daily_summary_loop,
            daemon=True,
            name="daily-summary",
        ),
    ]

    for t in threads:
        t.start()
        logger.info("Started thread: %s", t.name)

    # Keep main thread alive, respond to keyboard interrupt
    try:
        while True:
            time.sleep(60)
            # Log heartbeat
            alive = [t.name for t in threads if t.is_alive()]
            logger.debug("Heartbeat — alive threads: %s", alive)
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
        _shutdown.set()
        # Give threads time to finish current cycle
        for t in threads:
            t.join(timeout=10)
        logger.info("All agents stopped. Goodbye.")


if __name__ == "__main__":
    main()
