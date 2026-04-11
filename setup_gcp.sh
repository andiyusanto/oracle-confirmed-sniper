#!/bin/bash
# ============================================================
# Oracle-Confirmed Sniper — GCP Deployment Script
# ============================================================
# Run this ONCE after creating your GCP instance.
#
# Instance creation (run from your local machine):
#   gcloud compute instances create polymarket-bot \
#   --zone=europe-southwest1-a \
#   --machine-type=e2-small \
#   --network-tier=PREMIUM \
#   --image-family=ubuntu-2404-lts-amd64 \
#   --image-project=ubuntu-os-cloud \
#   --boot-disk-size=20GB \
#   --boot-disk-type=pd-balanced
#
# NOTE: Do NOT use europe-west2 (London) — UK is geoblocked by Polymarket.
#       europe-west1 (Belgium) gives ~5ms latency to Polymarket's London CLOB.
#
# SSH into the instance:
#   gcloud compute ssh polymarket-bot --zone=europe-west1-b
#
# Then run this script:
#   bash setup_gcp.sh
# ============================================================

set -e

echo "============================================"
echo "  Polymarket Bot — GCP Setup"
echo "============================================"

# ── 1. System packages ────────────────────────────────────────
echo "[1/6] Installing system packages..."
sudo apt update -qq
sudo apt install -y -qq python3.12 python3.12-venv python3-pip git unzip screen tmux

# ── 2. Clone or upload the bot ────────────────────────────────
echo "[2/6] Setting up bot directory..."
mkdir -p ~/polymarket-bot
cd ~/polymarket-bot

# If you're uploading the zip:
# gcloud compute scp oracle-confirmed-sniper-fixed.zip polymarket-bot:~/polymarket-bot/ --zone=europe-west1-b
# unzip -o oracle-confirmed-sniper-fixed.zip

# If you're cloning from git:
# git clone https://github.com/andiyusanto/oracle-confirmed-sniper.git .
# Then copy the fixed files over

echo "  → Place your bot files in ~/polymarket-bot/"

# ── 3. Python virtual environment ─────────────────────────────
echo "[3/6] Creating Python virtual environment..."
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install httpx -q

# ── 4. Environment variables ──────────────────────────────────
echo "[4/6] Setting up .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "  ⚠️  IMPORTANT: Edit .env with your credentials:"
    echo "     nano ~/polymarket-bot/.env"
    echo ""
    echo "  Required fields:"
    echo "    POLY_PRIVATE_KEY=0x..."
    echo "    POLY_API_KEY=..."
    echo "    POLY_API_SECRET=..."
    echo "    POLY_API_PASSPHRASE=..."
    echo "    POLY_FUNDER_ADDRESS=..."
    echo "    TELEGRAM_BOT_TOKEN=..."
    echo "    TELEGRAM_CHAT_ID=..."
    echo ""
else
    echo "  → .env already exists, skipping"
fi

# ── 5. Systemd service (auto-restart on crash/reboot) ─────────
echo "[5/6] Creating systemd service..."
sudo tee /etc/systemd/system/polymarket-bot.service > /dev/null << 'EOF'
[Unit]
Description=Polymarket Oracle Sniper Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=REPLACE_USER
WorkingDirectory=/home/REPLACE_USER/polymarket-bot
ExecStart=/home/REPLACE_USER/polymarket-bot/venv/bin/python bot.py --live --confirm-live --accept-risk
Restart=always
RestartSec=10

# Logging
StandardOutput=append:/home/REPLACE_USER/polymarket-bot/bot-stdout.log
StandardError=append:/home/REPLACE_USER/polymarket-bot/bot-stderr.log

# Environment
EnvironmentFile=/home/REPLACE_USER/polymarket-bot/.env

# Safety limits
MemoryMax=512M
CPUQuota=80%

[Install]
WantedBy=multi-user.target
EOF

# Replace REPLACE_USER with actual username
CURRENT_USER=$(whoami)
sudo sed -i "s/REPLACE_USER/${CURRENT_USER}/g" /etc/systemd/system/polymarket-bot.service

sudo systemctl daemon-reload

echo "  → Service created (not started yet)"

# ── 6. Helper scripts ─────────────────────────────────────────
echo "[6/6] Creating helper scripts..."

# Start bot
cat > ~/start-bot.sh << 'SCRIPT'
#!/bin/bash
sudo systemctl start polymarket-bot
sudo systemctl enable polymarket-bot
echo "Bot started. Check status: sudo systemctl status polymarket-bot"
echo "View logs: tail -f ~/polymarket-bot/hybrid.log"
SCRIPT
chmod +x ~/start-bot.sh

# Stop bot
cat > ~/stop-bot.sh << 'SCRIPT'
#!/bin/bash
sudo systemctl stop polymarket-bot
echo "Bot stopped."
SCRIPT
chmod +x ~/stop-bot.sh

# View logs
cat > ~/logs.sh << 'SCRIPT'
#!/bin/bash
tail -f ~/polymarket-bot/hybrid.log
SCRIPT
chmod +x ~/logs.sh

# Run analysis
cat > ~/analyze.sh << 'SCRIPT'
#!/bin/bash
cd ~/polymarket-bot
source venv/bin/activate
python analyze.py "$@"
SCRIPT
chmod +x ~/analyze.sh

# Paper mode (for testing)
cat > ~/paper.sh << 'SCRIPT'
#!/bin/bash
cd ~/polymarket-bot
source venv/bin/activate
python bot.py --portfolio ${1:-1000}
SCRIPT
chmod +x ~/paper.sh

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Upload your bot files:"
echo "     gcloud compute scp oracle-confirmed-sniper-fixed.zip \\"
echo "       polymarket-bot:~/polymarket-bot/ --zone=europe-west2-b"
echo ""
echo "  2. SSH in and unzip:"
echo "     cd ~/polymarket-bot && unzip -o oracle-confirmed-sniper-fixed.zip"
echo ""
echo "  3. Edit your credentials:"
echo "     nano ~/polymarket-bot/.env"
echo ""
echo "  4. Test with paper mode first:"
echo "     ~/paper.sh"
echo ""
echo "  5. When ready, start live:"
echo "     ~/start-bot.sh"
echo ""
echo "  Useful commands:"
echo "     ~/logs.sh           — tail the bot log"
echo "     ~/analyze.sh        — run trade analysis"
echo "     ~/analyze.sh --days 7  — last 7 days"
echo "     ~/stop-bot.sh       — stop the bot"
echo "     sudo systemctl status polymarket-bot  — check status"
echo ""
