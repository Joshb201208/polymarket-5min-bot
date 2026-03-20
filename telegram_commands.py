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


def cmd_help() -> str:
    """Return list of available commands."""
    return (
        "AVAILABLE COMMANDS\n"
        "\n"
        "/status  - Bot status and uptime\n"
        "/pnl     - Profit & loss summary\n"
        "/trades  - Recent trade history\n"
        "/balance - Current balance\n"
        "/help    - This message"
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
    "/balance": cmd_balance,
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
