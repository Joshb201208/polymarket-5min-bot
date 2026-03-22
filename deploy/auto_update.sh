#!/bin/bash
# ============================================================
# Auto-Updater — Pulls latest code from GitHub every 10 min
# ============================================================
# v2: Only manages polymarket-agents service (no more old bot services)
# ============================================================

BOT_DIR="$HOME/polymarket-bot"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

cd "$BOT_DIR" || exit 1

# Store current commit hash
OLD_HASH=$(git rev-parse HEAD 2>/dev/null)

# Detect which branch exists on the remote
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

if [ -z "$OLD_HASH" ] || [ -z "$REMOTE_HASH" ]; then
    echo "$LOG_PREFIX ERROR: Could not determine hashes (old=$OLD_HASH remote=$REMOTE_HASH)"
    exit 1
fi

# Sync service file if changed
REPO_SERVICE="$BOT_DIR/deploy/agents.service"
SYSTEM_SERVICE="/etc/systemd/system/polymarket-agents.service"
if [ -f "$REPO_SERVICE" ] && ! diff -q "$REPO_SERVICE" "$SYSTEM_SERVICE" >/dev/null 2>&1; then
    echo "$LOG_PREFIX Service file out of sync — updating and reloading..."
    cp "$REPO_SERVICE" "$SYSTEM_SERVICE" 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart polymarket-agents 2>/dev/null || true
    sleep 3
    echo "$LOG_PREFIX Service file synced and restarted"
fi

if [ "$OLD_HASH" = "$REMOTE_HASH" ]; then
    if ! systemctl is-active --quiet polymarket-agents; then
        echo "$LOG_PREFIX No code updates BUT agents NOT running — restarting..."
        systemctl restart polymarket-agents 2>/dev/null || true
        sleep 3
        if systemctl is-active --quiet polymarket-agents; then
            echo "$LOG_PREFIX Agents restarted successfully (was dead)"
        else
            echo "$LOG_PREFIX WARNING: Agents FAILED to restart!"
        fi
        bash "$BOT_DIR/deploy/health_check.sh" 2>/dev/null &
    else
        echo "$LOG_PREFIX No updates, agents running"
    fi
    exit 0
fi

echo "$LOG_PREFIX UPDATE: $OLD_HASH -> $REMOTE_HASH"

# Make sure local branch matches remote
if [ "$LOCAL_BRANCH" != "$BRANCH" ]; then
    echo "$LOG_PREFIX Switching from $LOCAL_BRANCH to $BRANCH"
    git checkout "$BRANCH" --quiet 2>/dev/null
fi

# Apply update
git reset --hard "origin/$BRANCH" --quiet 2>/dev/null

# Show what changed
git log --oneline "$OLD_HASH".."$REMOTE_HASH" 2>/dev/null

# Reinstall requirements if changed
if git diff --name-only "$OLD_HASH" "$REMOTE_HASH" | grep -q "requirements.txt"; then
    echo "$LOG_PREFIX Requirements changed, reinstalling..."
    source "$BOT_DIR/venv/bin/activate"
    pip install --quiet -r requirements.txt
fi

# Sync service files and reload systemd
echo "$LOG_PREFIX Syncing service files..."
cp "$BOT_DIR/deploy/agents.service" /etc/systemd/system/polymarket-agents.service 2>/dev/null || true
systemctl daemon-reload

# Restart agents service
echo "$LOG_PREFIX Restarting agents service..."
systemctl restart polymarket-agents 2>/dev/null || true
sleep 2

if systemctl is-active --quiet polymarket-agents; then
    echo "$LOG_PREFIX Agents restarted successfully with new code"
    STATUS="running"
else
    echo "$LOG_PREFIX WARNING: Agents failed to restart!"
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
    msg = f'Agents Updated & Restarted\n\nNew commits:\n{changes}\n\nStatus: $STATUS'
    try:
        httpx.post(f'https://api.telegram.org/bot{token}/sendMessage',
                   json={'chat_id': chat_id, 'text': msg}, timeout=10)
    except:
        pass
" 2>/dev/null

bash "$BOT_DIR/deploy/health_check.sh" 2>/dev/null &
echo "$LOG_PREFIX Update complete"
