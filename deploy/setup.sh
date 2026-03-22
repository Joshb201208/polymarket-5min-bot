#!/bin/bash
set -e
echo "=== Setting up NBA Polymarket Agent ==="

# Install system packages
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git nginx

# Clone repo if not already present
cd /root
if [ ! -d "polymarket-bot" ]; then
    git clone https://github.com/Joshb201208/polymarket-5min-bot.git polymarket-bot
    echo "Cloned repository."
else
    cd polymarket-bot && git pull origin master && cd /root
    echo "Updated existing repository."
fi

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

# Copy .env.example to .env if .env doesn't exist
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
fi

# Make auto_update.sh executable
chmod +x deploy/auto_update.sh

# Set up auto-updater cron (runs every 10 min)
(crontab -l 2>/dev/null | grep -v auto_update; echo "*/10 * * * * /root/polymarket-bot/deploy/auto_update.sh >> /var/log/auto_update.log 2>&1") | crontab -
echo "Set up auto-updater cron."

# Set up systemd service for the agent
cp deploy/agents.service /etc/systemd/system/nba-agent.service
systemctl daemon-reload
systemctl enable nba-agent
systemctl restart nba-agent

# Set up dashboard (nginx + FastAPI)
cp dashboard/nginx.conf /etc/nginx/sites-available/dashboard
ln -sf /etc/nginx/sites-available/dashboard /etc/nginx/sites-enabled/dashboard
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
echo "Configured nginx for dashboard."

cp dashboard/dashboard.service /etc/systemd/system/nba-dashboard.service
systemctl daemon-reload
systemctl enable nba-dashboard
systemctl restart nba-dashboard
echo "Started dashboard API service."

echo ""
echo "=== Setup complete! Agent + Dashboard running. ==="
echo "Check agent:     systemctl status nba-agent"
echo "Check dashboard:  systemctl status nba-dashboard"
echo "View agent logs:  journalctl -u nba-agent -f"
echo "Dashboard URL:    http://144.126.192.118/"
