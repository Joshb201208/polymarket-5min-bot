"""
Orchestrator — Runs all agents on schedule with shared executor.

Threads:
1. Agent 1 (Events): every 45 minutes
2. Agent 2 (Soccer): every 20 minutes
3. Agent 3 (NBA): every 20 minutes
4. Early Exit Monitor: every 30 minutes
5. Redemption Check: every 30 minutes
6. Daily Summary: at 16:00 UTC (midnight SGT)
7. Backtest Runner: once per day at 08:00 UTC (4pm SGT)
"""

import logging
import os
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

# Setup logging before imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path(__file__).parent.parent / "logs" / "agents.log",
            mode="a",
        ) if (Path(__file__).parent.parent / "logs").exists() else logging.StreamHandler(),
    ],
)
logger = logging.getLogger("orchestrator")

from agents.common import config
from agents.common import telegram
from agents.common.bankroll import BankrollManager
from agents.common.paper_tracker import PaperTracker
from agents.common.backtester import Backtester
from agents.common.executor import Executor
from agents.agent1_events import main as agent1
from agents.agent2_soccer import main as agent2
from agents.agent3_nba import main as agent3

# Shared state
bankroll = BankrollManager()
paper_tracker = PaperTracker(bankroll)
backtester = Backtester()
executor = Executor()

shutdown = False


def run_agent_loop(name: str, run_fn, interval: int):
    """Run an agent on a fixed interval."""
    while not shutdown:
        try:
            logger.info(f"--- {name} cycle starting ---")
            run_fn(bankroll, executor)
            logger.info(f"--- {name} cycle complete, sleeping {interval}s ---")
        except Exception as e:
            logger.error(f"{name} error: {e}", exc_info=True)
        time.sleep(interval)


def run_early_exit_monitor():
    """Monitor open positions for early exit conditions."""
    while not shutdown:
        try:
            exits = paper_tracker.check_early_exits()
            if exits:
                logger.info(f"Early exit monitor: {len(exits)} positions closed")
            paper_tracker.update_position_prices()
        except Exception as e:
            logger.error(f"Early exit monitor error: {e}", exc_info=True)
        time.sleep(config.EARLY_EXIT_CHECK_INTERVAL)


def run_redemption_check():
    """Check for resolved markets and redeem winnings every 30 minutes."""
    while not shutdown:
        try:
            _check_and_redeem_resolved()
        except Exception as e:
            logger.error(f"Redemption check error: {e}", exc_info=True)
        time.sleep(1800)  # every 30 min


def _check_and_redeem_resolved():
    """Check open positions for resolved markets and redeem."""
    for position in list(bankroll.positions):
        if position["status"] != "OPEN":
            continue
        condition_id = position.get("condition_id", "")
        if not condition_id:
            continue
        try:
            result = executor.redeem(condition_id)
            if result.get("success"):
                logger.info(f"Redemption attempted for {position['question'][:50]}")
        except Exception as e:
            logger.debug(f"Redemption check failed for {condition_id}: {e}")


def run_daily_summary():
    """Send daily summary at 16:00 UTC (midnight SGT)."""
    while not shutdown:
        now = datetime.now(timezone.utc)
        # Target: 16:00 UTC
        if now.hour == 16 and now.minute < 5:
            try:
                status = bankroll.get_status()
                positions = bankroll.positions
                day_pnl = paper_tracker.get_daily_pnl()
                telegram.send_daily_summary(status, positions, day_pnl)
                logger.info("Daily summary sent")
            except Exception as e:
                logger.error(f"Daily summary error: {e}", exc_info=True)
            time.sleep(3600)  # Sleep 1h to avoid double-sending
        else:
            time.sleep(60)  # Check every minute


def run_daily_backtest():
    """Run backtests at 08:00 UTC (4pm SGT)."""
    while not shutdown:
        now = datetime.now(timezone.utc)
        if now.hour == 8 and now.minute < 5:
            try:
                logger.info("Starting daily backtest...")
                report = backtester.run_full_backtest()
                logger.info(f"Backtest complete: {report.get('win_rate', 0):.1%} win rate")
            except Exception as e:
                logger.error(f"Backtest error: {e}", exc_info=True)
            time.sleep(3600)
        else:
            time.sleep(60)


def _maybe_reset_bankroll():
    """Reset bankroll once for v3 migration. Uses a flag file to avoid repeated resets."""
    data_dir = Path(__file__).parent.parent / "data"
    flag_file = data_dir / ".v3_reset_done"
    state_file = data_dir / "bankroll_state.json"

    if not flag_file.exists():
        # Delete existing state to force fresh start
        if state_file.exists():
            os.remove(str(state_file))
            logger.info("Bankroll state reset — starting fresh at $500")

        # Reload bankroll from scratch (will use STARTING_BANKROLL default)
        bankroll.capital = config.STARTING_BANKROLL
        bankroll.positions = []
        bankroll.history = []
        bankroll.total_pnl = 0.0
        bankroll.day_pnl = 0.0
        bankroll.save_state()

        # Create the flag so we don't reset again
        flag_file.touch()
        logger.info("v3 reset complete — flag file created")


def main():
    """Start all agent threads."""
    # Ensure data and logs directories exist
    (Path(__file__).parent.parent / "data").mkdir(exist_ok=True)
    (Path(__file__).parent.parent / "logs").mkdir(exist_ok=True)

    # One-time bankroll reset for v3
    _maybe_reset_bankroll()

    logger.info("=" * 60)
    logger.info("Polymarket AI Betting Agents v3 — Starting")
    logger.info(f"Bankroll: ${bankroll.capital:.2f}")
    logger.info(f"Mode: {executor.mode.upper()}")
    logger.info(f"Scan intervals: Events={config.SCAN_EVENTS}s, Soccer={config.SCAN_SOCCER}s, NBA={config.SCAN_NBA}s")
    logger.info("=" * 60)

    # Sync bankroll from chain on startup if live
    if executor.mode == "live":
        balance = executor.get_balance()
        if balance > 0:
            bankroll.sync_from_chain(balance)
            logger.info(f"Bankroll synced from chain: ${balance:.2f}")

    # Custom startup message
    telegram.send_message(
        "<b>Polymarket AI Agents v3 — Online</b>\n"
        f"Bankroll: ${bankroll.capital:.2f}\n"
        f"Mode: <b>{executor.mode.upper()}</b>\n"
        "Agents: Events, Soccer, NBA\n"
        "Scanning every 20-45 min."
    )

    threads = [
        threading.Thread(
            target=run_agent_loop,
            args=("Agent 1 (Events)", agent1.run_cycle, config.SCAN_EVENTS),
            daemon=True,
            name="agent1-events",
        ),
        threading.Thread(
            target=run_agent_loop,
            args=("Agent 2 (Soccer)", agent2.run_cycle, config.SCAN_SOCCER),
            daemon=True,
            name="agent2-soccer",
        ),
        threading.Thread(
            target=run_agent_loop,
            args=("Agent 3 (NBA)", agent3.run_cycle, config.SCAN_NBA),
            daemon=True,
            name="agent3-nba",
        ),
        threading.Thread(
            target=run_early_exit_monitor,
            daemon=True,
            name="early-exit-monitor",
        ),
        threading.Thread(
            target=run_redemption_check,
            daemon=True,
            name="redemption-check",
        ),
        threading.Thread(
            target=run_daily_summary,
            daemon=True,
            name="daily-summary",
        ),
        threading.Thread(
            target=run_daily_backtest,
            daemon=True,
            name="daily-backtest",
        ),
    ]

    for t in threads:
        t.start()
        logger.info(f"Started thread: {t.name}")
        time.sleep(5)  # Stagger starts to avoid API burst

    logger.info("All agent threads running. Main thread monitoring...")

    # Keep main thread alive and monitor
    while True:
        alive = [t for t in threads if t.is_alive()]
        dead = [t for t in threads if not t.is_alive()]
        if dead:
            logger.warning(f"Dead threads: {[t.name for t in dead]}")
        time.sleep(300)  # Check every 5 min


if __name__ == "__main__":
    main()
