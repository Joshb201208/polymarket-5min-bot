#!/bin/bash
set -e
echo "Setting up NBA Polymarket Agent..."
apt-get update
apt-get install -y python3 python3-pip python3-venv git
cd /root
if [ ! -d "polymarket-bot" ]; then
    git clone https://github.com/Joshb201208/polymarket-5min-bot.git polymarket-bot
fi
cd polymarket-bot
pip3 install -r requirements.txt
mkdir -p data
# Copy .env.example to .env if .env doesn't exist
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it to add Polymarket keys for live trading"
fi
# Set up auto-updater cron
(crontab -l 2>/dev/null; echo "*/10 * * * * /root/polymarket-bot/deploy/auto_update.sh >> /var/log/auto_update.log 2>&1") | sort -u | crontab -
# Set up systemd service
cp deploy/agents.service /etc/systemd/system/nba-agent.service
systemctl daemon-reload
systemctl enable nba-agent
systemctl start nba-agent
echo "Setup complete! Agent is running."
