"""
health_check.py — Reports bot health to Telegram every 6 hours.

Checks:
  - Bot process running (last cron execution)
  - Current balance and P&L
  - Recent trade count and win rate
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


def get_trading_stats():
    """Read stats from stats.json and trades.csv."""
    bot_dir = Path(__file__).parent.parent
    stats = {}

    # Read stats.json
    stats_file = bot_dir / "stats.json"
    if stats_file.exists():
        try:
            with open(stats_file) as f:
                stats = json.load(f)
        except Exception:
            pass

    # Count recent trades from trades.csv
    trades_file = bot_dir / "trades.csv"
    recent_trades = 0
    if trades_file.exists():
        try:
            with open(trades_file) as f:
                lines = f.readlines()[1:]  # skip header
                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                for line in lines:
                    # Try to parse timestamp from first column
                    try:
                        ts = datetime.fromisoformat(line.split(",")[0].strip())
                        if ts > cutoff:
                            recent_trades += 1
                    except Exception:
                        recent_trades += 1  # count if can't parse
        except Exception:
            pass

    return {
        "balance": stats.get("paper_balance", stats.get("balance", "?")),
        "pnl": stats.get("running_pnl", stats.get("pnl", "?")),
        "total_trades": stats.get("total_trades", "?"),
        "wins": stats.get("wins", "?"),
        "losses": stats.get("losses", "?"),
        "recent_trades_24h": recent_trades,
    }


def get_last_run():
    """Check when bot last ran from cron log."""
    log_file = Path(__file__).parent.parent / "logs" / "cron.log"
    if log_file.exists():
        try:
            # Get last modification time
            mtime = datetime.fromtimestamp(
                log_file.stat().st_mtime, tz=timezone.utc
            )
            age = datetime.now(timezone.utc) - mtime
            return {
                "last_run": mtime.strftime("%Y-%m-%d %H:%M UTC"),
                "minutes_ago": int(age.total_seconds() / 60),
            }
        except Exception:
            pass
    return {"last_run": "unknown", "minutes_ago": -1}


def get_git_info():
    """Get current git commit and last update."""
    try:
        commit = subprocess.check_output(
            ["git", "log", "--oneline", "-1"], text=True, cwd=str(Path(__file__).parent.parent)
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
    trade_stats = get_trading_stats()
    last_run = get_last_run()
    git_info = get_git_info()

    # Determine health status
    status = "OK"
    if last_run["minutes_ago"] > 15:
        status = "WARNING - Bot may be stuck"
    if last_run["minutes_ago"] > 60:
        status = "CRITICAL - Bot not running"
    if last_run["minutes_ago"] < 0:
        status = "UNKNOWN - No log file"

    # Calculate win rate
    wins = trade_stats.get("wins", 0)
    total = trade_stats.get("total_trades", 0)
    if isinstance(wins, int) and isinstance(total, int) and total > 0:
        win_rate = f"{(wins / total) * 100:.1f}%"
    else:
        win_rate = "N/A"

    msg = f"""📊 BOT HEALTH REPORT

Status: {status}
Last Run: {last_run['last_run']} ({last_run['minutes_ago']}m ago)
Code Version: {git_info}

💰 TRADING
Balance: ${trade_stats['balance']}
P&L: ${trade_stats['pnl']}
Total Trades: {trade_stats['total_trades']}
Win Rate: {win_rate} ({wins}W / {trade_stats.get('losses', '?')}L)
Trades (24h): {trade_stats['recent_trades_24h']}

🖥️ SYSTEM
Disk: {sys_stats['disk_used']} used / {sys_stats['disk_avail']} free ({sys_stats['disk_pct']})
RAM: {sys_stats['mem_used_mb']}MB / {sys_stats['mem_total_mb']}MB

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"""

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
