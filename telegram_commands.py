"""
telegram_commands.py — Telegram command handler for the Polymarket bot.

Runs as a separate process alongside the main bot. Listens for commands
from the user and responds with bot status, P&L, trade history, etc.

Supported commands:
  /status   — Bot status, uptime, current balance
  /pnl      — Profit & loss summary
  /trades   — Recent trade history
  /balance  — Current balance
  /help     — List all commands

Run with:
    python telegram_commands.py
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
import httpx

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/telegram_cmd.log", mode="a"),
    ],
)
logger = logging.getLogger("telegram_commands")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = 2  # seconds between checking for new messages
STATS_FILE = Path("stats.json")
PAPER_STATE_FILE = Path("paper_state.json")
SCALP_STATE_FILE = Path("scalp_paper_state.json")
TRADES_FILE = Path("trades.csv")
BOT_LOG = Path("logs/bot.log")

if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set in .env")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
_last_update_id = 0


def get_updates(offset: int = 0) -> list:
    """Fetch new messages via long polling."""
    try:
        resp = httpx.get(
            f"{API_BASE}/getUpdates",
            params={"offset": offset, "timeout": 10},
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("result", [])
    except Exception as e:
        logger.warning("Failed to get updates: %s", e)
    return []


def send_message(text: str) -> None:
    """Send a message to the configured chat."""
    try:
        resp = httpx.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Telegram send failed: %s", resp.text)
    except Exception as e:
        logger.warning("Failed to send message: %s", e)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_status() -> str:
    """Return bot status and uptime."""
    stats = _load_stats()
    
    # Check if bot is running by looking at log file age
    bot_status = "UNKNOWN"
    last_activity = "?"
    if BOT_LOG.exists():
        mtime = datetime.fromtimestamp(BOT_LOG.stat().st_mtime, tz=timezone.utc)
        age_min = (datetime.now(timezone.utc) - mtime).total_seconds() / 60
        if age_min < 2:
            bot_status = "RUNNING"
        elif age_min < 10:
            bot_status = "SLOW"
        else:
            bot_status = "POSSIBLY DOWN"
        last_activity = f"{age_min:.0f}m ago"
    
    # Paper balance from paper_state.json (most accurate source)
    balance = stats.get("paper_balance", stats.get("last_balance", stats.get("balance", 500)))
    total_trades = stats.get("total_trades", 0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    open_orders = stats.get("open_orders", 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    pnl = balance - 500.0
    
    # Scalp bot info
    scalp = _load_scalp_state()
    scalp_section = ""
    if scalp:
        scalp_balance = float(scalp.get("balance", 0))
        scalp_positions = len(scalp.get("positions", {}))
        scalp_stats = scalp.get("stats", {})
        scalp_trades = scalp_stats.get("total_trades", 0)
        scalp_wins = scalp_stats.get("wins", 0)
        scalp_pnl = scalp_stats.get("total_pnl", 0)
        scalp_section = (
            f"\n--- SCALP BOT ---\n"
            f"Balance: ${scalp_balance:.2f} (P&L: ${scalp_pnl:+.2f})\n"
            f"Open Positions: {scalp_positions}\n"
            f"Scalp Trades: {scalp_trades} ({scalp_wins}W)\n"
        )

    return (
        f"BOT STATUS\n"
        f"\n"
        f"Status: {bot_status}\n"
        f"Last Activity: {last_activity}\n"
        f"Mode: Paper Trading\n"
        f"Balance: ${balance:.2f} (P&L: ${pnl:+.2f})\n"
        f"Open Positions: {open_orders}\n"
        f"Resolved Trades: {total_trades}\n"
        f"Record: {wins}W / {losses}L ({win_rate:.0f}%)\n"
        f"{scalp_section}"
        f"\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )


def cmd_pnl() -> str:
    """Return P&L summary."""
    stats = _load_stats()
    
    balance = stats.get("paper_balance", stats.get("last_balance", stats.get("balance", 500)))
    starting = 500.0  # initial paper balance
    pnl = balance - starting
    pnl_pct = (pnl / starting) * 100
    
    total = stats.get("total_trades", 0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    win_rate = (wins / total * 100) if total > 0 else 0
    open_orders = stats.get("open_orders", 0)
    
    # Calculate average win/loss from trades
    avg_win, avg_loss = _calc_avg_win_loss()
    
    # Profit factor
    profit_factor = stats.get("profit_factor", 0)
    pf_str = f"{profit_factor:.2f}" if profit_factor else "N/A"
    
    # Scalp P&L
    scalp = _load_scalp_state()
    scalp_section = ""
    if scalp:
        scalp_stats = scalp.get("stats", {})
        scalp_pnl = scalp_stats.get("total_pnl", 0)
        scalp_total = scalp_stats.get("total_trades", 0)
        scalp_wins = scalp_stats.get("wins", 0)
        scalp_losses = scalp_stats.get("losses", 0)
        scalp_wr = (scalp_wins / scalp_total * 100) if scalp_total > 0 else 0
        scalp_bal = float(scalp.get("balance", 0))
        scalp_section = (
            f"\n--- SCALP P&L ---\n"
            f"Balance: ${scalp_bal:.2f}\n"
            f"P&L: ${scalp_pnl:+.2f}\n"
            f"Trades: {scalp_total} ({scalp_wins}W/{scalp_losses}L, {scalp_wr:.0f}%)\n"
        )

    return (
        f"P&L REPORT\n"
        f"\n"
        f"Balance: ${balance:.2f}\n"
        f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
        f"Open Positions: {open_orders}\n"
        f"\n"
        f"Total Trades: {total}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"Profit Factor: {pf_str}\n"
        f"\n"
        f"Avg Win: ${avg_win:+.2f}\n"
        f"Avg Loss: ${avg_loss:+.2f}"
        f"{scalp_section}"
    )


def cmd_trades() -> str:
    """Return last 10 trades."""
    if not TRADES_FILE.exists():
        return "No trades recorded yet."
    
    try:
        with open(TRADES_FILE) as f:
            lines = f.readlines()
        
        if len(lines) <= 1:
            return "No trades recorded yet."
        
        # Get last 10 trades (skip header)
        recent = lines[-10:]
        header = lines[0].strip().split(",")
        
        msg = "RECENT TRADES (last 10)\n\n"
        
        for line in recent:
            parts = line.strip().split(",")
            if len(parts) < 5:
                continue
            
            # Try to extract key fields
            try:
                ts = parts[0][:16]  # truncate timestamp
                asset = parts[3] if len(parts) > 3 else "?"
                direction = parts[5] if len(parts) > 5 else "?"
                size = parts[6] if len(parts) > 6 else "?"
                pnl = parts[10] if len(parts) > 10 else "?"
                won = parts[-1] if parts[-1] in ("True", "False") else "?"
                
                result = "W" if won == "True" else "L" if won == "False" else "?"
                msg += f"{ts} | {asset} {direction} | ${size} | PnL: ${pnl} | {result}\n"
            except (IndexError, ValueError):
                msg += f"{line.strip()[:80]}\n"
        
        return msg
    except Exception as e:
        return f"Error reading trades: {e}"


def cmd_balance() -> str:
    """Return current balance."""
    stats = _load_stats()
    balance = stats.get("paper_balance", stats.get("last_balance", stats.get("balance", 500)))
    pnl = balance - 500.0
    open_orders = stats.get("open_orders", 0)
    return f"Balance: ${balance:.2f} (P&L: ${pnl:+.2f})\nOpen Positions: {open_orders}"


def cmd_logs() -> str:
    """Return last 30 lines of bot log."""
    if not BOT_LOG.exists():
        return "No bot log found."
    try:
        with open(BOT_LOG, "r") as f:
            lines = f.readlines()
        recent = lines[-30:] if len(lines) > 30 else lines
        text = "RECENT BOT LOGS (last 30 lines)\n\n"
        for line in recent:
            text += line.rstrip()[:120] + "\n"  # truncate long lines
        # Telegram has a 4096 char limit
        if len(text) > 4000:
            text = text[:3990] + "\n...(truncated)"
        return text
    except Exception as e:
        return f"Error reading logs: {e}"


def cmd_restart() -> str:
    """Restart the bot service via systemctl."""
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "restart", "polymarket-bot"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return "Bot service restarted successfully. It should be back online in ~20s."
        else:
            return f"Restart failed (exit code {result.returncode}):\n{result.stderr[:500]}"
    except Exception as e:
        return f"Restart error: {e}"


def cmd_health() -> str:
    """Quick health check — is the bot actually running and trading?"""
    import subprocess
    
    # Check systemd status
    try:
        svc = subprocess.run(
            ["systemctl", "is-active", "polymarket-bot"],
            capture_output=True, text=True, timeout=5,
        )
        bot_svc = svc.stdout.strip()
    except Exception:
        bot_svc = "unknown"
    
    # Check git commit
    try:
        git = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd="/root/polymarket-bot",
        )
        git_hash = git.stdout.strip() if git.returncode == 0 else "unknown"
    except Exception:
        git_hash = "unknown"
    
    # Paper state
    stats = _load_stats()
    balance = stats.get("paper_balance", stats.get("balance", 500))
    open_orders = stats.get("open_orders", 0)
    
    # Log freshness
    log_age = "unknown"
    if BOT_LOG.exists():
        age_s = (datetime.now(timezone.utc) - datetime.fromtimestamp(BOT_LOG.stat().st_mtime, tz=timezone.utc)).total_seconds()
        log_age = f"{age_s:.0f}s ago"
    
    return (
        f"HEALTH CHECK\n\n"
        f"Bot Service: {bot_svc}\n"
        f"Git Commit: {git_hash}\n"
        f"Last Log Write: {log_age}\n"
        f"Balance: ${balance:.2f}\n"
        f"Open Positions: {open_orders}\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )


def cmd_reset() -> str:
    """Reset paper trading state to fresh $500 balance."""
    import subprocess
    try:
        # Stop the bot first so it doesn't overwrite our reset
        subprocess.run(["systemctl", "stop", "polymarket-bot"], timeout=10)
        time.sleep(2)

        # Write fresh paper state
        fresh_state = {
            "balance": 500.0,
            "order_counter": 0,
            "open_orders": {},
            "resolved_count": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "updated": datetime.now(tz=timezone.utc).isoformat(),
        }
        state_path = Path("/root/polymarket-bot/paper_state.json")
        with open(state_path, "w") as f:
            json.dump(fresh_state, f, indent=2)

        # Clear trades.csv (keep header if it exists)
        trades_path = Path("/root/polymarket-bot/trades.csv")
        if trades_path.exists():
            with open(trades_path, "r") as f:
                header = f.readline()
            with open(trades_path, "w") as f:
                if header.strip():
                    f.write(header)

        # Restart the bot
        subprocess.run(["systemctl", "start", "polymarket-bot"], timeout=10)

        return (
            "PAPER TRADING RESET\n\n"
            "Balance: $500.00\n"
            "Trades: cleared\n"
            "Open positions: cleared\n\n"
            "Bot restarted with fresh state."
        )
    except Exception as e:
        # Try to restart bot even if something failed
        try:
            subprocess.run(["systemctl", "start", "polymarket-bot"], timeout=10)
        except Exception:
            pass
        return f"Reset error: {e}"


def cmd_export() -> str:
    """Export full paper state + all trades for analysis."""
    msg_parts = []

    # --- Paper state ---
    if PAPER_STATE_FILE.exists():
        try:
            with open(PAPER_STATE_FILE) as f:
                paper = json.load(f)
            balance = float(paper.get("balance", 500))
            open_count = len(paper.get("open_orders", {}))
            resolved = int(paper.get("resolved_count", 0))
            wins = int(paper.get("wins", 0))
            losses = int(paper.get("losses", 0))
            total = wins + losses
            pnl = balance - 500.0
            wr = (wins / total * 100) if total > 0 else 0
            msg_parts.append(
                f"PAPER STATE\n"
                f"Balance: ${balance:.2f} (PnL: ${pnl:+.2f})\n"
                f"Resolved: {total} ({wins}W/{losses}L, {wr:.0f}%)\n"
                f"Open: {open_count}\n"
                f"Updated: {paper.get('updated', '?')}"
            )
        except Exception as e:
            msg_parts.append(f"Paper state error: {e}")
    else:
        msg_parts.append("No paper_state.json found")

    # --- All trades ---
    if TRADES_FILE.exists():
        try:
            with open(TRADES_FILE) as f:
                lines = f.readlines()
            if len(lines) > 1:
                msg_parts.append(f"\nALL TRADES ({len(lines)-1} total)\n")
                # Send header + all trade lines
                # Format: timestamp, slug, asset, direction, size, entry, exit, pnl, won
                header = lines[0].strip()
                msg_parts.append(header[:120])
                for line in lines[1:]:
                    parts = line.strip().split(",")
                    if len(parts) >= 14:
                        try:
                            ts = parts[0][:16]
                            asset = parts[3]
                            direction = parts[5]
                            size = parts[6]
                            entry = parts[7]
                            exit_p = parts[8]
                            gross_pnl = parts[9]
                            net_pnl = parts[10]
                            bal = parts[11]
                            edge = parts[12]
                            won = parts[-1].strip()
                            result = "W" if won == "True" else "L"
                            msg_parts.append(
                                f"{ts}|{asset}|{direction}|${size}|E:{entry}|X:{exit_p}|PnL:${net_pnl}|Bal:${bal}|{result}"
                            )
                        except (IndexError, ValueError):
                            msg_parts.append(line.strip()[:120])
                    else:
                        msg_parts.append(line.strip()[:120])
            else:
                msg_parts.append("No trades yet")
        except Exception as e:
            msg_parts.append(f"Trades file error: {e}")
    else:
        msg_parts.append("No trades.csv found")

    # --- Scalp state ---
    scalp = _load_scalp_state()
    if scalp:
        scalp_bal = float(scalp.get("balance", 0))
        scalp_stats = scalp.get("stats", {})
        scalp_positions = len(scalp.get("positions", {}))
        scalp_trades = scalp_stats.get("total_trades", 0)
        scalp_pnl = scalp_stats.get("total_pnl", 0)
        scalp_wins = scalp_stats.get("wins", 0)
        scalp_losses = scalp_stats.get("losses", 0)
        msg_parts.append(
            f"\nSCALP STATE\n"
            f"Balance: ${scalp_bal:.2f} (PnL: ${scalp_pnl:+.2f})\n"
            f"Open: {scalp_positions}\n"
            f"Trades: {scalp_trades} ({scalp_wins}W/{scalp_losses}L)\n"
            f"Updated: {scalp.get('updated', '?')}"
        )

        # Recent scalp trades
        closed = scalp.get("closed_trades", [])
        if closed:
            recent = closed[-10:]
            msg_parts.append(f"\nRECENT SCALP TRADES (last {len(recent)})")
            for t in recent:
                asset = t.get("asset", "?")
                direction = t.get("direction", "?")
                entry = float(t.get("entry_price", 0))
                exit_p = float(t.get("exit_price", 0))
                net = float(t.get("net_pnl", 0))
                hold = float(t.get("hold_time_secs", 0))
                reason = t.get("exit_reason", "?")
                result = "W" if net > 0 else "L"
                msg_parts.append(
                    f"{asset}|{direction}|E:{entry:.2f}|X:{exit_p:.2f}|PnL:${net:+.2f}|{hold:.0f}s|{reason}|{result}"
                )

    full_msg = "\n".join(msg_parts)
    # Telegram 4096 char limit — split if needed
    if len(full_msg) > 4000:
        # Send first chunk
        send_message(full_msg[:4000] + "\n...(cont)")
        remaining = full_msg[4000:]
        while remaining:
            chunk = remaining[:4000]
            remaining = remaining[4000:]
            send_message(chunk if not remaining else chunk + "\n...(cont)")
        return ""  # already sent
    return full_msg


def cmd_positions() -> str:
    """Show open scalp positions."""
    scalp = _load_scalp_state()
    if not scalp:
        return "No scalp state found. Scalp bot may not be running."

    positions = scalp.get("positions", {})
    if not positions:
        return "SCALP POSITIONS\n\nNo open positions."

    now = time.time()
    msg = f"SCALP POSITIONS ({len(positions)} open)\n\n"

    for pid, p in positions.items():
        entry_price = float(p.get("entry_price", 0))
        size_usd = float(p.get("size_usd", 0))
        entry_time = float(p.get("entry_time", 0))
        hold_secs = now - entry_time if entry_time > 0 else 0
        asset = p.get("asset", "?")
        direction = p.get("direction", "?")
        shares = float(p.get("shares", 0))

        msg += (
            f"{asset} {direction}\n"
            f"  Entry: {entry_price:.3f} | Size: ${size_usd:.2f}\n"
            f"  Shares: {shares:.2f} | Hold: {hold_secs:.0f}s\n"
            f"  ID: {pid[:20]}\n\n"
        )

    balance = float(scalp.get("balance", 0))
    msg += f"Balance: ${balance:.2f}"
    return msg


def cmd_help() -> str:
    """Return list of available commands."""
    return (
        "AVAILABLE COMMANDS\n"
        "\n"
        "/status    - Bot status and uptime\n"
        "/pnl       - Profit & loss summary\n"
        "/trades    - Recent trade history\n"
        "/positions - Open scalp positions\n"
        "/balance   - Current balance\n"
        "/logs      - Last 30 lines of bot log\n"
        "/health    - Quick health diagnostic\n"
        "/restart   - Restart bot service\n"
        "/reset     - Reset paper balance to $500\n"
        "/export    - Export all data for analysis\n"
        "/help      - This message"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_stats() -> dict:
    """Load stats from stats.json and paper_state.json, merging them."""
    result = {}

    # Load monitor stats (nested under "stats" key)
    if STATS_FILE.exists():
        try:
            with open(STATS_FILE) as f:
                raw = json.load(f)
            # Stats are nested: {"updated": ..., "stats": {"total_trades": ...}}
            inner = raw.get("stats", raw)
            result.update(inner)
        except Exception:
            pass

    # Load paper trader state for live balance + open orders
    if PAPER_STATE_FILE.exists():
        try:
            with open(PAPER_STATE_FILE) as f:
                paper = json.load(f)
            result["paper_balance"] = float(paper.get("balance", 500))
            result["open_orders"] = len(paper.get("open_orders", {}))
            result["resolved_count"] = int(paper.get("resolved_count", 0))
        except Exception:
            pass

    return result


def _load_scalp_state() -> dict:
    """Load scalp paper trader state."""
    if not SCALP_STATE_FILE.exists():
        return {}
    try:
        with open(SCALP_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _calc_avg_win_loss() -> tuple:
    """Calculate average win and loss from trades.csv."""
    if not TRADES_FILE.exists():
        return (0.0, 0.0)
    
    try:
        with open(TRADES_FILE) as f:
            lines = f.readlines()[1:]  # skip header
        
        wins = []
        losses = []
        for line in lines:
            parts = line.strip().split(",")
            try:
                pnl = float(parts[10])  # net_pnl column
                won = parts[-1].strip()
                if won == "True":
                    wins.append(pnl)
                elif won == "False":
                    losses.append(pnl)
            except (IndexError, ValueError):
                continue
        
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        return (avg_win, avg_loss)
    except Exception:
        return (0.0, 0.0)


# ---------------------------------------------------------------------------
# Command router
# ---------------------------------------------------------------------------

COMMANDS = {
    "/status": cmd_status,
    "/pnl": cmd_pnl,
    "/trades": cmd_trades,
    "/positions": cmd_positions,
    "/balance": cmd_balance,
    "/logs": cmd_logs,
    "/restart": cmd_restart,
    "/health": cmd_health,
    "/reset": cmd_reset,
    "/export": cmd_export,
    "/help": cmd_help,
    "/start": cmd_help,  # default Telegram /start command
}


def handle_message(text: str, from_chat_id: str) -> None:
    """Route a message to the appropriate command handler."""
    # Only respond to our configured chat
    if str(from_chat_id) != str(CHAT_ID):
        return
    
    cmd = text.strip().lower().split()[0] if text.strip() else ""
    
    if cmd in COMMANDS:
        response = COMMANDS[cmd]()
        send_message(response)
    elif cmd.startswith("/"):
        send_message(f"Unknown command: {cmd}\n\nType /help for available commands.")


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def main():
    """Run the Telegram command listener."""
    global _last_update_id
    
    logger.info("Telegram command handler started")
    logger.info("Listening for commands from chat %s", CHAT_ID)
    
    # Get initial offset (skip old messages)
    updates = get_updates(0)
    if updates:
        _last_update_id = updates[-1]["update_id"] + 1
        logger.info("Skipped %d old messages", len(updates))
    
    while True:
        try:
            updates = get_updates(offset=_last_update_id)
            
            for update in updates:
                _last_update_id = update["update_id"] + 1
                
                message = update.get("message", {})
                text = message.get("text", "")
                chat_id = message.get("chat", {}).get("id", "")
                
                if text:
                    logger.info("Received: %s from %s", text, chat_id)
                    handle_message(text, chat_id)
            
            time.sleep(POLL_INTERVAL)
        
        except KeyboardInterrupt:
            logger.info("Command handler stopped")
            break
        except Exception as e:
            logger.error("Error in polling loop: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
