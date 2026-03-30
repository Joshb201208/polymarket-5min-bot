#!/bin/bash
set -e

echo "=== Deploying Stock Agent ==="
cd /root/polymarket-bot

# Pull latest
git pull origin master

# Install deps
source venv/bin/activate
pip install -r requirements.txt

# Create data directory
mkdir -p /root/polymarket-bot/data/stock_agent

# Install systemd service
cp deploy/stock-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable stock-agent
systemctl restart stock-agent

# Restart dashboard to pick up new endpoints
systemctl restart dashboard

echo "=== Stock Agent deployed! ==="
echo "Check status: systemctl status stock-agent"
echo "View logs: journalctl -u stock-agent -f"
