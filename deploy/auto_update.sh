#!/bin/bash
# ============================================================
# Auto-Updater — Pulls latest code from GitHub every 10 min
# ============================================================
# When Computer pushes code updates to GitHub, this script
# picks them up automatically and restarts the bot.
# ============================================================

BOT_DIR="$HOME/polymarket-bot"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

cd "$BOT_DIR" || exit 1

# Store current commit hash
OLD_HASH=$(git rev-parse HEAD 2>/dev/null)

# Pull latest (force-reset to match remote exactly)
# Detect branch name (master or main)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "master")
git fetch origin "$BRANCH" --quiet 2>/dev/null
REMOTE_HASH=$(git rev-parse origin/"$BRANCH" 2>/dev/null)

# Only update if there's actually a change
if [ "$OLD_HASH" = "$REMOTE_HASH" ]; then
    echo "$LOG_PREFIX No updates"
    exit 0
fi

echo "$LOG_PREFIX UPDATE: $OLD_HASH -> $REMOTE_HASH"

# Apply update (preserves .env and logs)
git reset --hard origin/"$BRANCH" --quiet 2>/dev/null

# Show what changed
git log --oneline "$OLD_HASH".."$REMOTE_HASH" 2>/dev/null

# Reinstall requirements if changed
if git diff --name-only "$OLD_HASH" "$REMOTE_HASH" | grep -q "requirements.txt"; then
    echo "$LOG_PREFIX Requirements changed, reinstalling..."
    source "$BOT_DIR/venv/bin/activate"
    pip install --quiet -r requirements.txt
fi

# Restart both services to pick up new code
echo "$LOG_PREFIX Restarting services..."
systemctl restart polymarket-bot telegram-commands 2>/dev/null || true
sleep 2

if systemctl is-active --quiet polymarket-bot; then
    echo "$LOG_PREFIX Bot restarted successfully with new code"
    STATUS="running"
else
    echo "$LOG_PREFIX WARNING: Bot failed to restart!"
    STATUS="FAILED"
fi

# Send Telegram notification
source "$BOT_DIR/venv/bin/activate"
python3 -c "
import os
from dotenv import load_dotenv
load_dotenv('$BOT_DIR/.env')
import httpx
token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
if token and chat_id:
    changes = '''$(git log --oneline ${OLD_HASH}..${REMOTE_HASH} 2>/dev/null | head -5)'''
    msg = f'Bot Updated & Restarted\n\nNew commits:\n{changes}\n\nStatus: $STATUS\nNext cycle starts in ~30s.'
    try:
        httpx.post(f'https://api.telegram.org/bot{token}/sendMessage',
                   json={'chat_id': chat_id, 'text': msg}, timeout=10)
    except:
        pass
" 2>/dev/null

echo "$LOG_PREFIX Update complete"
