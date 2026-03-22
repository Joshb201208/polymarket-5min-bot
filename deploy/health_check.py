"""
health_check.py — Reports agent health to Telegram every 6 hours.

Checks:
  - Agent process running
  - Current bankroll and P&L
  - Open positions
  - Disk space and memory
  - Last update timestamp
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import httpx


def get_system_stats():
    """Get basic system resource usage."""
    try:
        disk = subprocess.check_output(
            ["df", "-h", "/"], text=True
        ).strip().split("\n")[-1].split()
        disk_used = disk[2]
        disk_avail = disk[3]
        disk_pct = disk[4]
    except Exception:
        disk_used = disk_avail = disk_pct = "?"

    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        mem_total = int(lines[0].split()[1]) // 1024
        mem_avail = int(lines[2].split()[1]) // 1024
        mem_used = mem_total - mem_avail
    except Exception:
        mem_total = mem_used = "?"

    return {
        "disk_used": disk_used,
        "disk_avail": disk_avail,
        "disk_pct": disk_pct,
        "mem_used_mb": mem_used,
        "mem_total_mb": mem_total,
    }


def get_bankroll_stats():
    """Read bankroll state from data/bankroll_state.json."""
    bot_dir = Path(__file__).parent.parent
    state_file = bot_dir / "data" / "bankroll_state.json"
    stats = {}

    if state_file.exists():
        try:
            with open(state_file) as f:
                stats = json.load(f)
        except Exception:
            pass

    return {
        "capital": stats.get("capital", "?"),
        "total_pnl": stats.get("total_pnl", 0),
        "day_pnl": stats.get("day_pnl", 0),
        "open_positions": len(stats.get("positions", [])),
        "total_trades": len(stats.get("history", [])),
        "updated_at": stats.get("updated_at", "unknown"),
    }


def get_service_status():
    """Check if agents service is running."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "polymarket-agents"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def get_git_info():
    """Get current git commit and last update."""
    try:
        commit = subprocess.check_output(
            ["git", "log", "--oneline", "-1"], text=True,
            cwd=str(Path(__file__).parent.parent)
        ).strip()
        return commit
    except Exception:
        return "unknown"


def send_health_report():
    """Compile and send health report to Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return

    sys_stats = get_system_stats()
    bankroll = get_bankroll_stats()
    service_status = get_service_status()
    git_info = get_git_info()

    # Determine health status
    status = "OK" if service_status == "active" else f"WARNING - {service_status}"

    # Win rate
    total = bankroll.get("total_trades", 0)
    capital = bankroll.get("capital", "?")
    total_pnl = bankroll.get("total_pnl", 0)

    msg = f"""AGENT HEALTH REPORT

Status: {status}
Service: polymarket-agents ({service_status})
Code: {git_info}

BANKROLL
Capital: ${capital}
Total P&L: ${total_pnl:+.2f}
Day P&L: ${bankroll.get('day_pnl', 0):+.2f}
Open Positions: {bankroll.get('open_positions', 0)}
Total Trades: {total}

SYSTEM
Disk: {sys_stats['disk_used']} used / {sys_stats['disk_avail']} free ({sys_stats['disk_pct']})
RAM: {sys_stats['mem_used_mb']}MB / {sys_stats['mem_total_mb']}MB

{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"""

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        print(f"Sent health report: {resp.status_code}")
    except Exception as e:
        print(f"Failed to send health report: {e}")


if __name__ == "__main__":
    send_health_report()
