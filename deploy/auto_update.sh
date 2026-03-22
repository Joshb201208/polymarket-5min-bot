#!/bin/bash
cd /root/polymarket-bot || exit 1
git fetch origin master
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)
if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date): New code detected, updating..."
    git reset --hard origin/master
    venv/bin/pip install -r requirements.txt --quiet
    systemctl restart nba-agent
    systemctl restart nba-dashboard
    echo "$(date): Update complete (agent + dashboard restarted)"
fi
