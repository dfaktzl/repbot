# Vouch Bot v3.2 — Reputation Tracker

Telegram reputation tracking bot — 45,000+ legacy vouches, persistent SQLite database, full admin panel.

**Bot ID (token prefix):** `8581140481` — uniquely identifies this Vouch/Rep bot from the Gatekeeper bot (`8502950869`).

## Features

- Explicit vouching (`+vouch`, `-vouch`, `vouch+`, `+ vouch`, bare `vouch` in a reply, `+1`, etc.)
- **Auto-polarity correction**: `+vouch he scammed me` auto-flips to `-1` if comment has 2+ negative keywords
- Optional sentiment-based auto-detection (togglable via admin panel, off by default on restart)
- Legacy vouch import (2020–2023)
- Full admin panel via `/panel` in DMs
- **Admin vouch flip**: change any vouch from positive to negative (or vice versa) during review
- Editable user-facing messages (no code changes needed)
- GateKeeper Bot cross-reference on `/check`

## Bot Tokens (Separation of Concerns)

| Bot | Token Prefix | Purpose |
|-----|-------------|---------|
| **Vouch/Rep Bot** | `8581140481:AAE_...` | Reputation tracking, vouch history, admin panel |
| **Gatekeeper** | `8502950869:AAGb...` | Access control, join requests, White Channel management |

Each bot runs as a separate systemd service (`repbot.service` vs `gatekeeper.service`) on the same OCI instance.

## Before You Deploy — What You Need

| Value | Where to get it | Status |
|---|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram | ✅ Pre-filled in `.env.example` (token prefix `8581140481`) |
| `ADMIN_IDS` | Your Telegram user ID — message [@userinfobot](https://t.me/userinfobot) | ✅ Pre-filled in `.env.example` |
| `LOG_CHANNEL` | Numeric channel ID | ✅ Pre-filled (`-1003817851175`) |
| `GATEKEEPER_DB_PATH` | Path on server | ✅ Pre-filled (`/home/ubuntu/gatekeeper/gatekeeper.db`) |

## Environment Variables (full list)

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Vouch bot token (prefix `8581140481`) |
| `ADMIN_IDS` | ✅ | Comma-separated Telegram user IDs |
| `LOG_CHANNEL` | ✅ | Log channel `@username` or numeric ID |
| `GATEKEEPER_DB_PATH` | ❌ | Absolute path to `gatekeeper.db` (for `/check` cross-reference) |

---

## 🚀 Deploy on OCI — Full Step-by-Step

### Prerequisites (Windows)

Your SSH key is at: `C:\Users\defak\Downloads\botcurrentpriv.key`

---

### Step 1 — SSH into the Server (from Windows PowerShell)

```powershell
# Fix key permissions first (only needed once)
icacls "C:\Users\defak\Downloads\botcurrentpriv.key" /inheritance:r /grant:r "$($env:USERNAME):(R)"

# Connect to your OCI instance
ssh -i "C:\Users\defak\Downloads\botcurrentpriv.key" ubuntu@<YOUR_OCI_PUBLIC_IP>
```

---

### Step 2 — Run the One-Time Setup Script (on server)

```bash
curl -sO https://raw.githubusercontent.com/dfaktzl/repbot/main/deploy.sh
sudo bash deploy.sh
```

This script will:
- Install Python, git, sqlite3
- Create a `botuser` system account
- Clone the repo to `/home/botuser/repbot`
- Set up a Python venv and install dependencies
- Copy `.env.example` → `.env` (pre-filled with all values except `BOT_TOKEN`)
- Install and enable the `repbot` systemd service

---

### Step 3 — Add your BOT_TOKEN (on server)

The `.env.example` is pre-filled with `LOG_CHANNEL`, `ADMIN_IDS`, and `GATEKEEPER_DB_PATH`.
**The only value you need to paste is your `BOT_TOKEN`.**

```bash
sudo nano /home/botuser/repbot/.env
```

Find the `BOT_TOKEN` line and replace it:

```env
BOT_TOKEN=8581140481:AAE_gCNuLulkGdb0eDcjOVQ1DW9Ewy3GQ9g
```

Save: `Ctrl+O` → Enter → `Ctrl+X`

---

### Step 4 — Start the Bot (on server)

```bash
sudo systemctl start repbot
sudo journalctl -u repbot -f   # watch live logs
```

---

### Step 5 — Verify Both Bots Are Running

```bash
sudo systemctl status repbot       # Vouch/Rep bot (token: 8581140481)
sudo systemctl status gatekeeper   # Gatekeeper bot (token: 8502950869)
```

---

### Future Updates (2 commands)

```bash
cd /home/botuser/repbot
sudo -u botuser git pull
sudo systemctl restart repbot
```

---

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

## Troubleshooting

| Problem | Fix |
|---|---|
| `Permission denied (publickey)` | Fix key permissions: `icacls "C:\Users\defak\Downloads\botcurrentpriv.key" /inheritance:r /grant:r "$($env:USERNAME):(R)"` |
| Bot not responding | `sudo journalctl -u repbot -f` to see errors |
| DB locked error | Check only one instance is running: `ps aux | grep python` |
| Wrong bot running | Check token prefix: `8581140481` = Vouch bot, `8502950869` = Gatekeeper |
