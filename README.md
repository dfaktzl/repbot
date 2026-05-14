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

## Before You Deploy — What You Need

Fill these in (you should already have them):

| Value | Where to get it | Status |
|---|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram | ⚠️ You need your new token |
| `ADMIN_IDS` | Your Telegram user ID — message [@userinfobot](https://t.me/userinfobot) | ✅ Pre-filled in `.env.example` |
| `LOG_CHANNEL` | Numeric channel ID | ✅ Pre-filled (`-1003817851175`) |
| `GATEKEEPER_DB_PATH` | Path on server | ✅ Pre-filled (`/home/ubuntu/gatekeeper/gatekeeper.db`) |

## Environment Variables (full list)

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from BotFather |
| `ADMIN_IDS` | ✅ | Comma-separated Telegram user IDs |
| `LOG_CHANNEL` | ✅ | Log channel `@username` or numeric ID |
| `GATEKEEPER_DB_PATH` | ❌ | Absolute path to `gatekeeper.db` on the server |

## Deploy on OCI — Step by Step

> All commands below run **on your server** (SSH in first: `ssh ubuntu@152.69.160.198`)

### Step 1 — Run the one-time setup script (on server)

```bash
# SSH into your server first, then:
curl -sO https://raw.githubusercontent.com/dfaktzl/repbot/main/deploy.sh
sudo bash deploy.sh
```

This script will:
- Install Python, git, sqlite3
- Clone the repo to `/home/botuser/repbot`
- Set up a Python venv and install dependencies
- Create `/home/botuser/repbot/.env` from the template
- Install and enable the systemd service

### Step 2 — Add your BOT_TOKEN (on server)

The `.env.example` is already pre-filled with your `LOG_CHANNEL`, `ADMIN_IDS`, and `GATEKEEPER_DB_PATH`.
**The only value you need to paste is your new `BOT_TOKEN`.**


```bash
sudo nano /home/botuser/repbot/.env
```

Find the `BOT_TOKEN` line and replace it:

```env
BOT_TOKEN=YOUR_NEW_BOT_TOKEN_FROM_BOTFATHER
```

Save: `Ctrl+O` → Enter → `Ctrl+X`

### Step 3 — Start the bot (on server)

```bash
sudo systemctl start repbot
sudo journalctl -u repbot -f   # watch live logs
```

### Future Updates (on server — 2 commands)

```bash
cd /home/botuser/repbot
sudo -u botuser git pull
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
