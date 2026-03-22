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

# Detect which branch exists on the remote (master or main)
# Try the local branch first, then fall back
LOCAL_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
git fetch origin --quiet 2>/dev/null

# Figure out which remote branch to track
BRANCH=""
for try_branch in "$LOCAL_BRANCH" "master" "main"; do
    if git rev-parse --verify "origin/$try_branch" >/dev/null 2>&1; then
        BRANCH="$try_branch"
        break
    fi
done

if [ -z "$BRANCH" ]; then
    echo "$LOG_PREFIX ERROR: Could not find remote branch (tried: $LOCAL_BRANCH, master, main)"
    exit 1
fi

REMOTE_HASH=$(git rev-parse "origin/$BRANCH" 2>/dev/null)

# Safety: if we couldn't get either hash, bail out
if [ -z "$OLD_HASH" ] || [ -z "$REMOTE_HASH" ]; then
    echo "$LOG_PREFIX ERROR: Could not determine hashes (old=$OLD_HASH remote=$REMOTE_HASH)"
    exit 1
fi

# Even if no code changes, check if bot service is alive
# If bot is dead, restart it immediately

# Always ensure service files are in sync (fixes switchover to scalp_main.py)
# Compare what systemd has vs what's in the repo
REPO_SERVICE="$BOT_DIR/deploy/polymarket-bot.service"
SYSTEM_SERVICE="/etc/systemd/system/polymarket-bot.service"
if [ -f "$REPO_SERVICE" ] && ! diff -q "$REPO_SERVICE" "$SYSTEM_SERVICE" >/dev/null 2>&1; then
    echo "$LOG_PREFIX Service file out of sync — updating and reloading..."
    cp "$REPO_SERVICE" "$SYSTEM_SERVICE" 2>/dev/null || true
    cp "$BOT_DIR/deploy/telegram-commands.service" /etc/systemd/system/ 2>/dev/null || true
    cp "$BOT_DIR/deploy/agents.service" /etc/systemd/system/polymarket-agents.service 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart polymarket-bot telegram-commands polymarket-agents 2>/dev/null || true
    sleep 3
    echo "$LOG_PREFIX Service file synced and restarted"
fi

if [ "$OLD_HASH" = "$REMOTE_HASH" ]; then
    if ! systemctl is-active --quiet polymarket-bot; then
        echo "$LOG_PREFIX No code updates BUT bot is NOT running — restarting..."
        systemctl restart polymarket-bot telegram-commands polymarket-agents 2>/dev/null || true
        sleep 3
        if systemctl is-active --quiet polymarket-bot; then
            echo "$LOG_PREFIX Bot restarted successfully (was dead)"
            bash "$BOT_DIR/deploy/health_check.sh" 2>/dev/null &
        else
            echo "$LOG_PREFIX WARNING: Bot FAILED to restart!"
            bash "$BOT_DIR/deploy/health_check.sh" 2>/dev/null &
        fi
    else
        echo "$LOG_PREFIX No updates, bot is running"
    fi
    exit 0
fi

echo "$LOG_PREFIX UPDATE: $OLD_HASH -> $REMOTE_HASH"

# Make sure local branch matches remote branch name
if [ "$LOCAL_BRANCH" != "$BRANCH" ]; then
    echo "$LOG_PREFIX Switching from $LOCAL_BRANCH to $BRANCH"
    git checkout "$BRANCH" --quiet 2>/dev/null
fi

# Apply update (preserves .env and logs)
git reset --hard "origin/$BRANCH" --quiet 2>/dev/null

# Show what changed
git log --oneline "$OLD_HASH".."$REMOTE_HASH" 2>/dev/null

# Reinstall requirements if changed
if git diff --name-only "$OLD_HASH" "$REMOTE_HASH" | grep -q "requirements.txt"; then
    echo "$LOG_PREFIX Requirements changed, reinstalling..."
    source "$BOT_DIR/venv/bin/activate"
    pip install --quiet -r requirements.txt
fi

# Always sync service files and reload systemd
# This ensures the service file in /etc/systemd/system/ matches the repo
echo "$LOG_PREFIX Syncing service files..."
cp "$BOT_DIR/deploy/polymarket-bot.service" /etc/systemd/system/ 2>/dev/null || true
cp "$BOT_DIR/deploy/telegram-commands.service" /etc/systemd/system/ 2>/dev/null || true
cp "$BOT_DIR/deploy/agents.service" /etc/systemd/system/polymarket-agents.service 2>/dev/null || true
systemctl daemon-reload

# Restart all services to pick up new code
echo "$LOG_PREFIX Restarting services..."
systemctl restart polymarket-bot telegram-commands polymarket-agents 2>/dev/null || true
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

# Run health check to report status
bash "$BOT_DIR/deploy/health_check.sh" 2>/dev/null &

echo "$LOG_PREFIX Update complete"
