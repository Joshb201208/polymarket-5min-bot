"""Orchestrator — runs all 3 agents on independent schedules using threads.

Agent 1 (Events):  every 2 hours
Agent 2 (Soccer):  every 1 hour
Agent 3 (NBA):     every 1 hour

Startup sends a Telegram alert. Daily at 16:00 UTC (midnight SGT) sends
a performance summary. Each agent runs in its own thread with staggered
start times to avoid API rate limit collisions.
"""

import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from agents.agent1_events.main import run_agent1
from agents.agent2_soccer.main import run_agent2
from agents.agent3_nba.main import run_agent3
from agents.common.config import (
    LOG_FORMAT,
    LOG_DATE_FORMAT,
    SCAN_INTERVAL_EVENTS,
    SCAN_INTERVAL_SOCCER,
    SCAN_INTERVAL_NBA,
)
from agents.common.paper_tracker import PaperTracker
from agents.common.telegram_alerts import send_startup_alert, send_daily_summary

# ── Logging setup ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
)
logger = logging.getLogger("agents.orchestrator")

# ── Shared state ─────────────────────────────────────────────
_shutdown = threading.Event()
_agent_stats: dict[str, dict] = {
    "Agent 1 (Events)": {"alerts_sent": 0, "markets_scanned": 0},
    "Agent 2 (Soccer)": {"alerts_sent": 0, "markets_scanned": 0},
    "Agent 3 (NBA)": {"alerts_sent": 0, "markets_scanned": 0},
}
_stats_lock = threading.Lock()


def _agent_loop(name: str, run_fn, paper_tracker: PaperTracker, interval: int) -> None:
    """Run an agent function repeatedly with the given interval."""
    logger.info("%s thread started (interval=%ds)", name, interval)
    while not _shutdown.is_set():
        try:
            stats = run_fn(paper_tracker)
            with _stats_lock:
                for key in stats:
                    _agent_stats[name][key] = _agent_stats[name].get(key, 0) + stats[key]
        except Exception:
            logger.exception("%s — unhandled error in run loop", name)

        # Sleep in small increments so we can respond to shutdown quickly
        for _ in range(interval):
            if _shutdown.is_set():
                break
            time.sleep(1)

    logger.info("%s thread exiting", name)


def _daily_summary_loop(paper_tracker: PaperTracker) -> None:
    """Send daily summary at 16:00 UTC (midnight SGT). Also check resolutions."""
    logger.info("Daily summary thread started")
    last_summary_day: int | None = None

    while not _shutdown.is_set():
        now = datetime.now(timezone.utc)

        # Check market resolutions every 6 hours
        if now.hour % 6 == 0 and now.minute < 2:
            try:
                resolved = paper_tracker.check_resolutions()
                if resolved:
                    logger.info("Resolved %d paper trades", len(resolved))
            except Exception:
                logger.exception("Error checking resolutions")

        # Daily summary at 16:00 UTC
        if now.hour == 16 and now.minute < 2 and last_summary_day != now.day:
            last_summary_day = now.day
            try:
                with _stats_lock:
                    agents_snapshot = {k: dict(v) for k, v in _agent_stats.items()}
                    # Reset daily counters
                    for v in _agent_stats.values():
                        v["alerts_sent"] = 0
                        v["markets_scanned"] = 0

                paper_stats = paper_tracker.get_stats()
                send_daily_summary({
                    "agents": agents_snapshot,
                    "paper": paper_stats,
                })
                logger.info("Daily summary sent")
            except Exception:
                logger.exception("Error sending daily summary")

        # Check every 60 seconds
        for _ in range(60):
            if _shutdown.is_set():
                break
            time.sleep(1)


def _handle_signal(signum, frame) -> None:
    logger.info("Received signal %d — shutting down", signum)
    _shutdown.set()


def main() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=" * 60)
    logger.info("Polymarket AI Betting Agents — starting up")
    logger.info("=" * 60)

    paper_tracker = PaperTracker()
    send_startup_alert()

    threads = [
        threading.Thread(
            target=_agent_loop,
            args=("Agent 1 (Events)", run_agent1, paper_tracker, SCAN_INTERVAL_EVENTS),
            daemon=True,
            name="agent1-events",
        ),
        threading.Thread(
            target=_agent_loop,
            args=("Agent 2 (Soccer)", run_agent2, paper_tracker, SCAN_INTERVAL_SOCCER),
            daemon=True,
            name="agent2-soccer",
        ),
        threading.Thread(
            target=_agent_loop,
            args=("Agent 3 (NBA)", run_agent3, paper_tracker, SCAN_INTERVAL_NBA),
            daemon=True,
            name="agent3-nba",
        ),
        threading.Thread(
            target=_daily_summary_loop,
            args=(paper_tracker,),
            daemon=True,
            name="daily-summary",
        ),
    ]

    # Stagger start times to avoid API rate limit collisions
    for i, t in enumerate(threads):
        t.start()
        if i < len(threads) - 1:
            time.sleep(30)  # 30-second stagger between agents

    logger.info("All agent threads started")

    # Main thread waits for shutdown signal
    while not _shutdown.is_set():
        time.sleep(1)

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
