#!/bin/bash
cd /root/polymarket-bot || exit 1

# Clean stale refs to avoid "unable to update local ref" errors
rm -f .git/refs/remotes/origin/master 2>/dev/null
git remote prune origin 2>/dev/null
git fetch --all 2>/dev/null || { echo "$(date): git fetch failed"; exit 1; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master 2>/dev/null || echo "unknown")
if [ "$LOCAL" != "$REMOTE" ] && [ "$REMOTE" != "unknown" ]; then
    echo "$(date): New code detected ($LOCAL -> $REMOTE), updating..."
    git reset --hard origin/master
    venv/bin/pip install -r requirements.txt --quiet
    systemctl restart nba-agent
    systemctl restart nba-dashboard
    echo "$(date): Update complete (agent + dashboard restarted)"
fi
