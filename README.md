# Vouch Bot v3.2

Telegram reputation tracking bot — 45,000+ legacy vouches, persistent SQLite database, full admin panel.

## Features
- Explicit vouching (`+vouch`, `-vouch`, `vouch+`, `+ vouch`, bare `vouch` in a reply, `+1`, etc.)
- **Auto-polarity correction**: `+vouch he scammed me` auto-flips to `-1` if comment has 2+ negative keywords
- Optional sentiment-based auto-detection (togglable via admin panel, off by default on restart)
- Legacy vouch import (2020–2023)
- Full admin panel via `/panel` in DMs
- **Admin vouch flip**: change any vouch from positive to negative (or vice versa) during review
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
git clone https://github.com/dfaktzl/repbot.git
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

- **📋 Vouch Queue** — One vouch at a time: approve / reject / delete / 🔄 **Flip ±** polarity with live score adjustment
- **✏️ Edit Messages** — Change any user-facing message without touching code; reply with new text to save
- **⚙️ Settings** — Toggle sentiment vouching on/off (persistent across restarts)
- **👥 User Tools** — Flagged users list with quick command reference
- **📊 DB Stats** — Live database statistics
- **📣 Broadcast** — Send message to all known users

## Vouch Trigger Variants

All of the following are accepted:

| Input | Direction |
|---|---|
| `+vouch Great trader` | ✅ Positive |
| `-vouch Scammed me` | ❌ Negative |
| `+ vouch` / `- vouch` | Spaced operators ✅/❌ |
| `vouch+` / `vouch-` | Suffix style ✅/❌ |
| `+rep` / `-rep` / `+1` / `-1` | Shorthand ✅/❌ |
| `vouch` (bare, in a reply) | ✅ Positive |
| `/vouch @user comment` | ✅ Positive (command) |
| `+vouch scammed ripped off` | ❌ Auto-flipped by content |

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
