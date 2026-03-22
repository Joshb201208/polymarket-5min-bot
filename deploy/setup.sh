#!/bin/bash
# ============================================================
# Polymarket AI Betting Agents v2 — One-Command VPS Setup
# ============================================================
# Run on a fresh Ubuntu 22.04/24.04 DigitalOcean Droplet:
#
#   ssh root@YOUR_IP
#   git clone https://github.com/Joshb201208/polymarket-5min-bot.git ~/polymarket-bot
#   bash ~/polymarket-bot/deploy/setup.sh
#
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

BOT_DIR="$HOME/polymarket-bot"

echo "=========================================="
echo " Polymarket AI Betting Agents v2 — Setup"
echo "=========================================="

# ── 1. System packages ──────────────────────────────────────
echo -e "${GREEN}[1/7] Installing system packages...${NC}"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git cron curl

# ── 2. Python virtual environment ───────────────────────────
echo -e "${GREEN}[2/7] Setting up Python environment...${NC}"
cd "$BOT_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ── 3. Environment file ────────────────────────────────────
echo -e "${GREEN}[3/7] Checking .env file...${NC}"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo -e "${YELLOW}"
    echo "  ============================================"
    echo "  .env file created from template."
    echo "  Default Telegram credentials are set."
    echo "  Edit if you want to change them:"
    echo ""
    echo "    nano $BOT_DIR/.env"
    echo "  ============================================"
    echo -e "${NC}"
else
    echo "  .env file exists"
fi

# ── 4. Create directories ───────────────────────────────────
echo -e "${GREEN}[4/7] Creating directories...${NC}"
mkdir -p "$BOT_DIR/logs"
mkdir -p "$BOT_DIR/data"

# ── 5. Install systemd service ──────────────────────────────
echo -e "${GREEN}[5/7] Installing systemd service...${NC}"

# Stop old services if they exist
systemctl stop polymarket-bot 2>/dev/null || true
systemctl disable polymarket-bot 2>/dev/null || true
systemctl stop telegram-commands 2>/dev/null || true
systemctl disable telegram-commands 2>/dev/null || true
rm -f /etc/systemd/system/polymarket-bot.service 2>/dev/null || true
rm -f /etc/systemd/system/telegram-commands.service 2>/dev/null || true

# Install agents service
sed "s|/root/polymarket-bot|$BOT_DIR|g" "$BOT_DIR/deploy/agents.service" > /etc/systemd/system/polymarket-agents.service
sed -i "s|User=root|User=$(whoami)|g" /etc/systemd/system/polymarket-agents.service

systemctl daemon-reload
systemctl enable polymarket-agents
systemctl restart polymarket-agents

echo "  Systemd service installed and started"

# ── 6. Install cron jobs ────────────────────────────────────
echo -e "${GREEN}[6/7] Installing cron jobs...${NC}"

crontab -l 2>/dev/null | grep -v "polymarket-bot" | crontab - 2>/dev/null || true

CRON_LINES="# Polymarket Agents auto-updater — pulls code from GitHub every 10 min
*/10 * * * * cd $BOT_DIR && bash $BOT_DIR/deploy/auto_update.sh >> $BOT_DIR/logs/updater.log 2>&1 # polymarket-bot
# Health check — reports to Telegram every 6 hours
0 */6 * * * cd $BOT_DIR && $BOT_DIR/venv/bin/python $BOT_DIR/deploy/health_check.py >> $BOT_DIR/logs/health.log 2>&1 # polymarket-bot"

(crontab -l 2>/dev/null; echo "$CRON_LINES") | crontab -

echo "  Crons installed"

# ── 7. Verify ───────────────────────────────────────────────
echo -e "${GREEN}[7/7] Verifying...${NC}"
sleep 3

if systemctl is-active --quiet polymarket-agents; then
    echo -e "  Agents service: ${GREEN}RUNNING${NC}"
else
    echo -e "  Agents service: ${RED}NOT RUNNING${NC}"
    echo "  Check logs: journalctl -u polymarket-agents -n 50"
fi

echo ""
echo "=========================================="
echo -e "${GREEN} Setup complete!${NC}"
echo "=========================================="
echo ""
echo "  Agents:       3 agents running (Events, Soccer, NBA)"
echo "  Auto-updates: Every 10 min from GitHub"
echo "  Health check: Every 6 hours to Telegram"
echo ""
echo "  COMMANDS:"
echo "  View live logs:    journalctl -u polymarket-agents -f"
echo "  View agent logs:   tail -f $BOT_DIR/logs/agents.log"
echo "  Restart agents:    systemctl restart polymarket-agents"
echo "  Stop agents:       systemctl stop polymarket-agents"
echo "  Agent status:      systemctl status polymarket-agents"
echo ""
echo "  The agents will auto-update when code is pushed to GitHub."
echo ""
