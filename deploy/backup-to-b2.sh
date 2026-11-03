#!/bin/sh
# Rsync the latest Kartis SQLite snapshot to Backblaze B2 (or any
# S3-compatible bucket via rclone). Run from cron — daily at 03:30 is
# fine (Kartis itself snapshots kartis.db at 03:00 via APScheduler).
#
# One-time setup:
#   sudo apt install rclone
#   rclone config   # add a remote called "b2" pointing at your bucket
#
# Cron line:
#   30 3 * * * /opt/kartis/deploy/backup-to-b2.sh >> /var/log/kartis-backup.log 2>&1

set -eu

BACKUP_DIR="${KARTIS_BACKUP_DIR:-/opt/kartis/backups}"
REMOTE="${KARTIS_B2_REMOTE:-b2:kartis-backups}"

rclone copy --max-age 36h "$BACKUP_DIR" "$REMOTE"
echo "$(date -Iseconds) synced $BACKUP_DIR to $REMOTE"
