#!/bin/bash
# deploy.sh — First-time setup script for Oracle Cloud Oracle Linux 9 VM
# Run as root or with sudo

set -e

echo "═══════════════════════════════════════════"
echo "  Reputation Bot v3.1 — OCI Deployment"
echo "═══════════════════════════════════════════"

echo ""
echo "📦 Creating Swap Space (to prevent out-of-memory on 1GB/500MB instances)..."
if [ ! -f /swapfile2 ]; then
    dd if=/dev/zero of=/swapfile2 bs=1M count=1024
    chmod 600 /swapfile2
    mkswap /swapfile2
    swapon /swapfile2
    echo "/swapfile2 swap swap defaults 0 0" >> /etc/fstab
    echo "Swap added."
else
    echo "Swap already exists."
fi

# 1. System updates
echo ""
echo "📦 Updating system packages..."
dnf upgrade -y

# 2. Install Python + deps
echo ""
echo "🐍 Installing Python 3.11..."
dnf install -y python3.11 python3.11-pip sqlite

# 3. Create bot user (if not exists)
if ! id "botuser" &>/dev/null; then
    echo ""
    echo "👤 Creating bot user..."
    useradd -m -s /bin/bash botuser
fi

# 4. Set up bot directory
echo ""
echo "📂 Setting up /home/botuser/bot..."
mkdir -p /home/botuser/bot
cp -r ./*.py ./handlers ./requirements.txt /home/botuser/bot/
chown -R botuser:botuser /home/botuser/bot

# 5. Create venv + install deps (as botuser)
echo ""
echo "📦 Installing Python dependencies..."
sudo -u botuser bash -c '
    cd /home/botuser/bot
    python3.11 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
'

# 6. Prompt for .env
if [ ! -f /home/botuser/bot/.env ]; then
    echo ""
    echo "⚠️  You need to create /home/botuser/bot/.env"
    echo "   Copy from .env.example and fill in your credentials:"
    echo ""
    cp .env.example /home/botuser/bot/.env
    chown botuser:botuser /home/botuser/bot/.env
    chmod 600 /home/botuser/bot/.env
fi

# 7. Install systemd service
echo ""
echo "⚙️  Installing systemd service..."
cp repbot.service /etc/systemd/system/repbot.service
systemctl daemon-reload
systemctl enable repbot

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ Deployment ready!"
echo ""
echo "  Next steps:"
echo "    1. Edit .env:    sudo nano /home/botuser/bot/.env"
echo "    2. Start bot:    sudo systemctl start repbot"
echo "    3. View logs:    sudo journalctl -u repbot -f"
echo "═══════════════════════════════════════════"
