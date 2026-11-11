#!/usr/bin/env bash
# restore.sh - restore LKHC Odečty from the off-site backup made by backup.sh (Google Drive/rclone).
#
#   restore.sh list                         DB snapshots available on the remote (oldest first)
#   restore.sh [--db NAME|latest] [--no-photos] [--yes]
#
# Needs: $APP_DIR/compose.yml and .env, the image lkhc-odecty:live (deploy first - on a fresh VPS
# deploy.sh creates the volume and an empty DB), $APP_DIR/backup/backup.env + rclone/rclone.conf
# (the same remote as the backups; with rclone crypt also the same crypt passwords).
#
# Steps: stop the app -> move the current app.db(-wal/-shm) to /data/backups/pre-restore-<time>/ ->
# download the DB snapshot -> download photos (adds missing files, deletes nothing) -> fix owner
# (uid 10001) -> SQLite integrity check -> app.db -> start the app (migrations run if the snapshot
# is older) -> wait for the healthcheck -> print counts.
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/srv/lkhc-odecty}"
if [[ -f $APP_DIR/backup/backup.env ]]; then
    set -a
    # shellcheck source=/dev/null
    . "$APP_DIR/backup/backup.env"
    set +a
fi
BACKUP_REMOTE="${BACKUP_REMOTE:?set BACKUP_REMOTE in $APP_DIR/backup/backup.env}"
RCLONE_IMAGE="${RCLONE_IMAGE:-rclone/rclone:1.76.0}"
RCLONE_CONFIG_DIR="${RCLONE_CONFIG_DIR:-$APP_DIR/backup/rclone}"
DATA_VOLUME="${DATA_VOLUME:-lkhc-odecty-data}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
APP_UID=10001
read -r -a RCLONE_DOCKER_ARGS <<<"${RCLONE_DOCKER_ARGS:-}"

log() { printf '%s restore: %s\n' "$(date '+%F %T')" "$*"; }
die() {
    log "ERROR: $*" >&2
    exit 1
}

# rclone as root in its container (it writes into the volume; owner fixed afterwards). The config is
# copied into the container so a token refresh can never leave a root-owned rclone.conf behind.
rclone_rw() {
    docker run --rm -i --memory 256m -e XDG_CACHE_HOME=/tmp \
        -v "$RCLONE_CONFIG_DIR:/config-ro:ro,z" -v "$DATA_VOLUME:/data" \
        "${RCLONE_DOCKER_ARGS[@]}" --entrypoint sh "$RCLONE_IMAGE" \
        -c 'cp /config-ro/rclone.conf /tmp/rclone.conf && exec rclone --config /tmp/rclone.conf "$@"' rclone "$@"
}

list_snapshots() {
    rclone_rw lsf --files-only "$BACKUP_REMOTE/db" | grep -E '^lkhc-[0-9]{8}-[0-9]{6}\.db$' | sort
}

main() {
    local db=latest photos=1 yes=0
    while (($#)); do
        case $1 in
        list)
            list_snapshots
            return 0
            ;;
        --db)
            db=${2:?--db NAME|latest}
            shift
            ;;
        --no-photos) photos=0 ;;
        --yes) yes=1 ;;
        *)
            sed -n '2,/^set -E/p' "$0" | sed '$d; s/^# \{0,1\}//'
            exit 2
            ;;
        esac
        shift
    done

    [[ -f $APP_DIR/compose.yml ]] || die "$APP_DIR/compose.yml not found"
    [[ -f $RCLONE_CONFIG_DIR/rclone.conf ]] || die "missing $RCLONE_CONFIG_DIR/rclone.conf"
    cd "$APP_DIR"
    docker image inspect lkhc-odecty:live >/dev/null 2>&1 || die "no image lkhc-odecty:live - deploy first"

    if [[ $db == latest ]]; then
        db=$(list_snapshots | tail -n 1)
        [[ -n $db ]] || die "no DB snapshots in $BACKUP_REMOTE/db"
    fi
    [[ $db =~ ^lkhc-[0-9]{8}-[0-9]{6}\.db$ ]] || die "not a snapshot name: $db"

    log "restore $BACKUP_REMOTE/db/$db$( ((photos)) && echo " + photos") into volume $DATA_VOLUME"
    if ((!yes)); then
        read -r -p "This replaces the current database (it is kept in /data/backups/pre-restore-*). Continue? [y/N] " a
        [[ $a == [yYaA]* ]] || die "cancelled"
    fi

    if ((photos)) && ! rclone_rw lsf --dirs-only "$BACKUP_REMOTE" | grep -qx 'photos/'; then
        log "no photos/ on the remote (no photos were ever backed up) - restoring the database only"
        photos=0
    fi

    log "stopping the app"
    docker compose up --no-start app # creates the volume if needed
    docker compose stop app
    # Until the snapshot is moved into place the current database is untouched: on any failure
    # before that, start the app again on it.
    trap 'log "RESTORE FAILED - the current database was not changed; starting the app again"; docker compose up -d app' ERR

    log "downloading $db"
    rclone_rw copyto "$BACKUP_REMOTE/db/$db" /data/restore-incoming.db
    if ((photos)); then
        log "downloading photos (missing files only)"
        rclone_rw copy "$BACKUP_REMOTE/photos" /data/photos --stats 0
    fi
    docker run --rm -v "$DATA_VOLUME:/data" --entrypoint sh "$RCLONE_IMAGE" -c \
        "mkdir -p /data/photos && chown -R $APP_UID:$APP_UID /data/restore-incoming.db /data/photos"

    log "integrity check + putting the snapshot in place"
    docker compose run --rm --no-deps -T --entrypoint sh app -c '
set -eu
python - <<EOF
import sqlite3, sys
con = sqlite3.connect("/data/restore-incoming.db")
ok = con.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    sys.exit("integrity check failed: " + ok)
print("integrity_check: ok")
EOF
keep=/data/backups/pre-restore-$(date +%Y%m%d-%H%M%S)
mkdir -p "$keep"
for f in app.db app.db-wal app.db-shm; do if [ -e "/data/$f" ]; then mv "/data/$f" "$keep/"; fi; done
mv /data/restore-incoming.db /data/app.db
echo "previous database files kept in $keep"'
    trap - ERR

    log "starting the app"
    docker compose up -d --wait --wait-timeout "$WAIT_TIMEOUT" app
    docker compose exec -T app python - <<'EOF'
import os, sqlite3
con = sqlite3.connect("/data/app.db")
q = lambda sql: con.execute(sql).fetchone()[0]
files = sum(len([f for f in fs if not f.endswith(".part")]) for _, _, fs in os.walk("/data/photos"))
print(f"readings: {q('select count(*) from readings')}, photos in DB: {q('select count(*) from photos')}, "
      f"files in photos/ (incl. thumbnails): {files}, last reading: {q('select max(taken_at) from readings')}")
EOF
    log "OK: restored $db"
}

main "$@"
