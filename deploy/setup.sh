#!/bin/bash
# ============================================================
# Polymarket Bot — One-Command VPS Setup
# ============================================================
# Run on a fresh Ubuntu 22.04/24.04 DigitalOcean Droplet:
#
#   ssh root@YOUR_IP
#   git clone https://github.com/YOUR_USER/polymarket-5min-bot.git ~/polymarket-bot
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
echo " Polymarket Bot — VPS Setup"
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
    echo "  You MUST edit it before the bot will work:"
    echo ""
    echo "    nano $BOT_DIR/.env"
    echo ""
    echo "  Fill in at minimum:"
    echo "    - TELEGRAM_BOT_TOKEN"
    echo "    - TELEGRAM_CHAT_ID (already set to 8451693416)"
    echo ""
    echo "  For live trading also fill in:"
    echo "    - API_KEY, API_SECRET, API_PASSPHRASE"
    echo "    - PRIVATE_KEY, FUNDER_ADDRESS"
    echo "  ============================================"
    echo -e "${NC}"
    echo -e "${RED}  Run 'bash deploy/setup.sh' again after editing .env${NC}"
    exit 0
else
    echo "  .env file exists ✓"
fi

# ── 4. Create logs directory ───────────────────────────────
echo -e "${GREEN}[4/7] Creating logs directory...${NC}"
mkdir -p "$BOT_DIR/logs"

# ── 5. Install systemd service (bot runs continuously) ─────
echo -e "${GREEN}[5/7] Installing systemd service...${NC}"

# Update paths in service file based on actual user
sed "s|/root/polymarket-bot|$BOT_DIR|g" "$BOT_DIR/deploy/polymarket-bot.service" > /etc/systemd/system/polymarket-bot.service
sed -i "s|User=root|User=$(whoami)|g" /etc/systemd/system/polymarket-bot.service

# Telegram command handler service
sed "s|/root/polymarket-bot|$BOT_DIR|g" "$BOT_DIR/deploy/telegram-commands.service" > /etc/systemd/system/telegram-commands.service
sed -i "s|User=root|User=$(whoami)|g" /etc/systemd/system/telegram-commands.service

# AI Betting Agents service
sed "s|/root/polymarket-bot|$BOT_DIR|g" "$BOT_DIR/deploy/agents.service" > /etc/systemd/system/polymarket-agents.service
sed -i "s|User=root|User=$(whoami)|g" /etc/systemd/system/polymarket-agents.service

systemctl daemon-reload
systemctl enable polymarket-bot telegram-commands polymarket-agents
systemctl restart polymarket-bot telegram-commands polymarket-agents

echo "  Systemd services installed and started ✓"

# ── 6. Install cron jobs (auto-update + health check) ──────
echo -e "${GREEN}[6/7] Installing cron jobs...${NC}"

# Remove existing polymarket crons
crontab -l 2>/dev/null | grep -v "polymarket-bot" | crontab - 2>/dev/null || true

# Add auto-updater (every 10 min) and health check (every 6 hours)
CRON_LINES="# Polymarket Bot auto-updater — pulls code from GitHub every 10 min
*/10 * * * * cd $BOT_DIR && bash $BOT_DIR/deploy/auto_update.sh >> $BOT_DIR/logs/updater.log 2>&1 # polymarket-bot
# Health check — reports to Telegram every 6 hours
0 */6 * * * cd $BOT_DIR && $BOT_DIR/venv/bin/python $BOT_DIR/deploy/health_check.py >> $BOT_DIR/logs/health.log 2>&1 # polymarket-bot"

(crontab -l 2>/dev/null; echo "$CRON_LINES") | crontab -

echo "  Crons installed ✓"

# ── 7. Verify everything is running ────────────────────────
echo -e "${GREEN}[7/7] Verifying...${NC}"
sleep 3

if systemctl is-active --quiet polymarket-bot; then
    echo -e "  Bot service: ${GREEN}RUNNING ✓${NC}"
else
    echo -e "  Bot service: ${RED}NOT RUNNING ✗${NC}"
    echo "  Check logs: journalctl -u polymarket-bot -n 50"
fi

echo ""
echo "=========================================="
echo -e "${GREEN} Setup complete!${NC}"
echo "=========================================="
echo ""
echo "  🤖 Bot:          Running continuously (every 30s cycle)"
echo "  🔄 Auto-updates: Every 10 min from GitHub"
echo "  📊 Health check: Every 6 hours to Telegram"
echo ""
echo "  COMMANDS:"
echo "  View live logs:    journalctl -u polymarket-bot -f"
echo "  View bot logs:     tail -f $BOT_DIR/logs/bot.log"
echo "  Restart bot:       systemctl restart polymarket-bot"
echo "  Stop bot:          systemctl stop polymarket-bot"
echo "  Bot status:        systemctl status polymarket-bot"
echo "  View updater log:  tail -f $BOT_DIR/logs/updater.log"
echo ""
echo "  The bot will auto-update when code is pushed to GitHub."
echo "  You don't need to touch anything — it's fully autonomous."
echo ""
