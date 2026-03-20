#!/bin/bash
# ============================================================
# Health Check — Run after auto_update.sh restarts the bot
# Reports service status and recent logs to Telegram
# ============================================================

BOT_DIR="$HOME/polymarket-bot"
cd "$BOT_DIR" || exit 1

# Load env for Telegram credentials
source "$BOT_DIR/venv/bin/activate" 2>/dev/null
export $(grep -v '^#' "$BOT_DIR/.env" | xargs) 2>/dev/null

TOKEN="${TELEGRAM_BOT_TOKEN}"
CHAT="${TELEGRAM_CHAT_ID}"

if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
    echo "No Telegram credentials found"
    exit 1
fi

# Gather diagnostics
BOT_STATUS=$(systemctl is-active polymarket-bot 2>/dev/null || echo "unknown")
CMD_STATUS=$(systemctl is-active telegram-commands 2>/dev/null || echo "unknown")
BOT_LOG_TAIL=$(journalctl -u polymarket-bot --no-pager -n 20 2>/dev/null | tail -15)
UPTIME=$(uptime -p 2>/dev/null || echo "unknown")
GIT_HASH=$(cd "$BOT_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")

# Check if paper_state.json exists and read balance
PAPER_BAL="N/A"
OPEN_ORDERS="N/A"
if [ -f "$BOT_DIR/paper_state.json" ]; then
    PAPER_BAL=$(python3 -c "import json; d=json.load(open('$BOT_DIR/paper_state.json')); print(f\"\${d.get('balance',0):.2f}\")" 2>/dev/null || echo "error")
    OPEN_ORDERS=$(python3 -c "import json; d=json.load(open('$BOT_DIR/paper_state.json')); print(len(d.get('open_orders',{})))" 2>/dev/null || echo "error")
fi

MSG="HEALTH CHECK REPORT

Bot Service: ${BOT_STATUS}
Command Handler: ${CMD_STATUS}
Git Commit: ${GIT_HASH}
Server Uptime: ${UPTIME}
Paper Balance: ${PAPER_BAL}
Open Orders: ${OPEN_ORDERS}

--- Recent Bot Logs ---
${BOT_LOG_TAIL}"

# Send to Telegram
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d "chat_id=${CHAT}" \
    --data-urlencode "text=${MSG}" > /dev/null 2>&1

echo "Health check sent to Telegram"
