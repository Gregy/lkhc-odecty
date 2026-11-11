#!/bin/sh
# Host-side backup for the docker compose deployment (e.g. from cron, in the repo directory):
#
#   0 3 * * *  cd /opt/lkhc-odecty && ./scripts/backup.sh /var/backups/lkhc-odecty >> /var/log/lkhc-backup.log 2>&1
#
# 1. inside the running container: consistent SQLite snapshot (online backup API, WAL-safe – never
#    copy app.db of a running app directly) + photos/ -> /data/backups/lkhc-<timestamp>.tar.gz
#    (the newest KEEP archives are kept in the volume),
# 2. copies the new archive out of the container to DEST (default ./zalohy).
# Copy DEST off the VPS as well (rsync/restic/...); a backup on the same disk is not a backup.
set -eu

DEST="${1:-./zalohy}"
KEEP="${KEEP:-7}"
mkdir -p "$DEST"
archive="$(docker compose exec -T app python -m app.backup --with-photos --keep "$KEEP" | tail -n 1)"
docker compose cp "app:$archive" "$DEST/"
echo "$(date -Is) záloha: $DEST/$(basename "$archive")"
