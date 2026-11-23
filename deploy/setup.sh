#!/bin/bash
set -eu
umask 077

# Fresh installations only. The existing droplet uses the cron updater.
if [ -e /root/polymarket-bot ] || [ -e /etc/systemd/system/stock-agent.service ]; then
    echo "Existing installation detected; leaving checkout and service unchanged." >&2
    exit 1
fi
echo "=== Setting up Stock Trading Agent ==="

# Install system packages
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git

# Clone into the standard root checkout
cd /root
git clone https://github.com/Joshb201208/polymarket-5min-bot.git polymarket-bot

cd /root/polymarket-bot

# Create virtual environment and install dependencies
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "Created virtual environment."
fi
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q
echo "Installed Python dependencies."

# Create data directory
mkdir -p data

# Starter configuration, never used on the existing deployment
install -m 0600 .env.example .env

# Set up auto-updater cron (runs every 10 min)
(crontab -l 2>/dev/null | grep -v auto_update; echo "*/10 * * * * /root/polymarket-bot/deploy/auto_update.sh >> /var/log/auto_update.log 2>&1") | crontab -
echo "Set up auto-updater cron."

# Set up systemd service for the stock agent
install -m 0644 deploy/stock-agent.service /etc/systemd/system/stock-agent.service
systemctl daemon-reload

echo ""
echo "=== Fresh checkout installed; service not started. ==="
echo "Configure .env, then enable and start stock-agent explicitly."
echo "Check agent:      systemctl status stock-agent"
echo "View agent logs:  journalctl -u stock-agent -f"
