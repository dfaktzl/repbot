# Vouch Bot v3.2

Telegram reputation tracking bot — 45,000+ legacy vouches, persistent SQLite database, full admin panel.

## Features
- Explicit vouching (`+vouch`, `-vouch`, `+1`, etc.)
- Optional sentiment-based auto-detection (togglable via admin panel)
- Legacy vouch import (2020–2023)
- Full admin panel via `/panel` in DMs
- Editable user-facing messages (no code changes needed)
- GateKeeper Bot cross-reference on `/check`

## Quick Start

```bash
cp .env.example .env       # fill in BOT_TOKEN, ADMIN_IDS, LOG_CHANNEL
pip install -r requirements.txt
python main.py
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token |
| `ADMIN_IDS` | ✅ | Comma-separated Telegram user IDs |
| `LOG_CHANNEL` | ✅ | Log channel username or ID |
| `GATEKEEPER_DB_PATH` | ❌ | Absolute path to gatekeeper.db for cross-reference |

## Deploy on OCI (Ubuntu)

```bash
git clone https://github.com/YOUR_USERNAME/repbot.git
cd repbot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp /path/to/your.env .env
sudo cp repbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now repbot
```

### Update from OCI
```bash
cd /path/to/repbot
git pull
sudo systemctl restart repbot
```

## Admin Commands

| Command | Description |
|---|---|
| `/panel` | Full admin panel (DM only) |
| `/scammer @user reason` | Flag a scammer |
| `/unflag <id>` | Remove flag |
| `/dangerous <id> reason` | Mark as dangerous/roller |
| `/forcevouch <from> <to> <+1/-1> <comment>` | Manual vouch |
| `/deletevouch <id>` | Delete a vouch |
| `/flagged` | List all flagged users |
| `/dbstats` | Database statistics |
| `/broadcast <message>` | Broadcast to all users |

## Admin Panel Features (`/panel` in DMs)

- **📋 Vouch Queue** — Review pending vouches with approve/reject/delete
- **✏️ Edit Messages** — Change any user-facing message without touching code
- **⚙️ Settings** — Toggle sentiment vouching on/off (persistent)
- **👥 User Tools** — View flagged users list
- **📊 DB Stats** — Live database statistics

## File Structure

```
vouch_checker/
├── main.py              # Entry point
├── config.py            # Constants & env vars
├── database.py          # SQLAlchemy models + DB helpers
├── helpers.py           # Shared utilities
├── handlers/
│   ├── admin.py         # Admin commands
│   ├── admin_panel.py   # Full inline admin panel
│   ├── commands.py      # User commands (/start /help /check)
│   ├── vouching.py      # Vouch logic + sentiment detection
│   ├── passive.py       # Passive user scraping & scam detection
│   └── welcome.py       # Group welcome handler
├── backup_db.sh         # Cron-friendly DB backup script
└── deploy.sh            # Server setup helper
```
