#!/bin/bash
# deploy.sh — First-time setup for a fresh OCI Ubuntu 22.04 instance
# Run as: sudo bash deploy.sh
# After running, edit /home/botuser/repbot/.env then: sudo systemctl start repbot

set -e

REPO_URL="https://github.com/dfaktzl/repbot.git"
BOT_DIR="/home/botuser/repbot"
SERVICE_NAME="repbot"

echo "═══════════════════════════════════════════════"
echo "  Reputation Bot v3.2 — OCI Deploy Script"
echo "  Server: $(hostname -I | awk '{print $1}')"
echo "═══════════════════════════════════════════════"

# ── Swap (prevents OOM on 1GB instances) ─────────────────────────────────────
if [ ! -f /swapfile2 ]; then
    echo "📦 Adding 1GB swap..."
    dd if=/dev/zero of=/swapfile2 bs=1M count=1024
    chmod 600 /swapfile2
    mkswap /swapfile2
    swapon /swapfile2
    echo "/swapfile2 swap swap defaults 0 0" >> /etc/fstab
else
    echo "✅ Swap already exists."
fi

# ── System packages ───────────────────────────────────────────────────────────
echo "📦 Updating system packages..."
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git sqlite3

# ── Bot user ──────────────────────────────────────────────────────────────────
if ! id "botuser" &>/dev/null; then
    echo "👤 Creating botuser..."
    useradd -m -s /bin/bash botuser
fi

# ── Clone / update repo ───────────────────────────────────────────────────────
if [ -d "$BOT_DIR/.git" ]; then
    echo "🔄 Repo already exists — pulling latest..."
    sudo -u botuser git -C "$BOT_DIR" pull
else
    echo "📥 Cloning repo..."
    sudo -u botuser git clone "$REPO_URL" "$BOT_DIR"
fi

# ── Python venv + deps ────────────────────────────────────────────────────────
echo "🐍 Installing Python dependencies..."
sudo -u botuser bash -c "
    cd $BOT_DIR
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
"

# ── .env file ─────────────────────────────────────────────────────────────────
if [ ! -f "$BOT_DIR/.env" ]; then
    echo ""
    echo "⚠️  Creating .env from template..."
    cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
    chown botuser:botuser "$BOT_DIR/.env"
    chmod 600 "$BOT_DIR/.env"
    echo ""
    echo "  ┌─────────────────────────────────────────────────┐"
    echo "  │  ACTION REQUIRED: fill in your credentials      │"
    echo "  │  Run:  sudo nano $BOT_DIR/.env                  │"
    echo "  └─────────────────────────────────────────────────┘"
fi

# ── Systemd service ───────────────────────────────────────────────────────────
echo "⚙️  Installing systemd service..."
# Patch service file to use correct directory
sed "s|/home/botuser/bot|$BOT_DIR|g" "$BOT_DIR/repbot.service" \
    > /etc/systemd/system/${SERVICE_NAME}.service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "═══════════════════════════════════════════════"
echo "  ✅ Setup complete!"
echo ""
echo "  NEXT STEPS:"
echo "  1. Fill in credentials:  sudo nano $BOT_DIR/.env"
echo "  2. Start the bot:        sudo systemctl start $SERVICE_NAME"
echo "  3. Watch live logs:      sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  FUTURE UPDATES (just these two commands):"
echo "  cd $BOT_DIR && sudo -u botuser git pull"
echo "  sudo systemctl restart $SERVICE_NAME"
echo "═══════════════════════════════════════════════"
