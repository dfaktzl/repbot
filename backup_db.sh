#!/bin/bash
# backup_db.sh — Automated daily database backup
# Add to crontab: 0 3 * * * /home/botuser/bot/backup_db.sh

BACKUP_DIR="/home/botuser/backups"
DB_PATH="/home/botuser/bot/bot_database.db"
DATE=$(date +%Y-%m-%d_%H%M)
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"

# Use SQLite's backup command for safe copy (doesn't lock the DB)
sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/bot_database_$DATE.db'"

# Compress
gzip "$BACKUP_DIR/bot_database_$DATE.db"

# Delete backups older than KEEP_DAYS
find "$BACKUP_DIR" -name "bot_database_*.db.gz" -mtime +$KEEP_DAYS -delete

echo "[$(date)] Backup complete: bot_database_$DATE.db.gz"
