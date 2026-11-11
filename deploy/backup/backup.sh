#!/usr/bin/env bash
# backup.sh - off-site backup of LKHC Odečty to Google Drive with rclone (run nightly by the systemd
# timer lkhc-odecty-backup.timer as the deploy user; can be run by hand any time).
#
#   backup.sh run              snapshot + upload + retention + status file (exit 1 on any failure)
#   backup.sh check            print the status; exit 1 if the last run failed or the last success is
#                              older than MAX_AGE_HOURS
#   backup.sh plan-retention   (testing) read snapshot names on stdin, print the ones retention deletes
#
# What it does (`run`):
#   1. consistent DB snapshot with the app's online backup (python -m app.backup, WAL-safe) into
#      the volume: /data/backups/nightly/lkhc-YYYYmmdd-HHMMSS.db (the newest LOCAL_KEEP are kept)
#   2. rclone copyto  -> $BACKUP_REMOTE/db/lkhc-YYYYmmdd-HHMMSS.db   (dated snapshots)
#   3. rclone copy    -> $BACKUP_REMOTE/photos/                      (incremental, never deletes)
#   4. retention of db/: newest snapshot of each of the last KEEP_DAILY days, KEEP_WEEKLY ISO weeks
#      and KEEP_MONTHLY months is kept, other lkhc-*.db files are deleted. Photos are not pruned.
#   5. status: $APP_DIR/backup/status.json (+ a copy in the volume: /data/backups/offsite-status.json)
#      and an optional ping to HC_PING_URL (e.g. healthchecks.io; "/fail" is appended on failure).
# rclone runs from the pinned rclone/rclone image (nothing to install on the host), with the data
# volume mounted read-only and the config from $APP_DIR/backup/rclone/rclone.conf.
#
# Configuration: $APP_DIR/backup/backup.env (see backup.env.example), APP_DIR [/srv/lkhc-odecty].
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/srv/lkhc-odecty}"
if [[ -f $APP_DIR/backup/backup.env ]]; then
    set -a
    # shellcheck source=/dev/null
    . "$APP_DIR/backup/backup.env"
    set +a
fi
BACKUP_REMOTE="${BACKUP_REMOTE:-}"
RCLONE_IMAGE="${RCLONE_IMAGE:-rclone/rclone:1.76.0}"
RCLONE_CONFIG_DIR="${RCLONE_CONFIG_DIR:-$APP_DIR/backup/rclone}"
DATA_VOLUME="${DATA_VOLUME:-lkhc-odecty-data}"
KEEP_DAILY="${KEEP_DAILY:-14}"
KEEP_WEEKLY="${KEEP_WEEKLY:-8}"
KEEP_MONTHLY="${KEEP_MONTHLY:-12}"
LOCAL_KEEP="${LOCAL_KEEP:-3}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"
STATUS_FILE="${STATUS_FILE:-$APP_DIR/backup/status.json}"
HC_PING_URL="${HC_PING_URL:-}"
# Extra `docker run` arguments for the rclone container (word-split), e.g. a bind mount for a
# local/NAS remote. Normally empty.
read -r -a RCLONE_DOCKER_ARGS <<<"${RCLONE_DOCKER_ARGS:-}"

log() { printf '%s lkhc-backup: %s\n' "$(date '+%F %T')" "$*"; }

rclone() {
    docker run --rm -i --user "$(id -u):$(id -g)" --memory 256m \
        -e RCLONE_CONFIG=/config/rclone/rclone.conf -e XDG_CACHE_HOME=/tmp \
        -v "$RCLONE_CONFIG_DIR:/config/rclone:z" -v "$DATA_VOLUME:/data:ro" \
        "${RCLONE_DOCKER_ARGS[@]}" "$RCLONE_IMAGE" "$@"
}

# Snapshot names (any order) on stdin -> names to delete on stdout. Names that do not look like
# lkhc-YYYYmmdd-HHMMSS.db are never deleted.
plan_retention() {
    local name day week month keep nd=0 nw=0 nm=0
    local -A days=() weeks=() months=()
    while read -r name; do
        [[ $name =~ ^lkhc-([0-9]{8})-[0-9]{6}\.db$ ]] || continue
        day=${BASH_REMATCH[1]}
        week=$(date -d "$day" +%G-W%V)
        month=${day:0:6}
        keep=0
        if [[ -z ${days[$day]:-} ]]; then
            days[$day]=1
            nd=$((nd + 1))
            if ((nd <= KEEP_DAILY)); then keep=1; fi
        fi
        if [[ -z ${weeks[$week]:-} ]]; then
            weeks[$week]=1
            nw=$((nw + 1))
            if ((nw <= KEEP_WEEKLY)); then keep=1; fi
        fi
        if [[ -z ${months[$month]:-} ]]; then
            months[$month]=1
            nm=$((nm + 1))
            if ((nm <= KEEP_MONTHLY)); then keep=1; fi
        fi
        if ((keep == 0)); then printf '%s\n' "$name"; fi
    done < <(sort -r)
}

status_value() { # key -> value from the status file (one "key": value per line)
    [[ -f $STATUS_FILE ]] || return 0
    sed -n "s/^  \"$1\": \"\{0,1\}\([^\"]*\)\"\{0,1\},\{0,1\}\$/\1/p" "$STATUS_FILE"
}

write_status() { # result failed_step
    local now last_success tmp
    now=$(date -Is)
    last_success=$(status_value last_success)
    [[ $1 != ok ]] || last_success=$now
    tmp="$STATUS_FILE.part"
    cat >"$tmp" <<EOF
{
  "app": "lkhc-odecty",
  "result": "$1",
  "last_run": "$now",
  "last_success": "${last_success}",
  "failed_step": "$2",
  "remote": "$BACKUP_REMOTE",
  "db_snapshot": "${SNAPSHOT_NAME:-}",
  "db_snapshots_remote": ${DB_COUNT:-0},
  "photos_remote_files": ${PHOTO_FILES:-0},
  "photos_remote_bytes": ${PHOTO_BYTES:-0}
}
EOF
    mv "$tmp" "$STATUS_FILE"
    # Best effort: a copy the app can read from its own volume.
    (cd "$APP_DIR" && docker compose exec -T app sh -c \
        'cat > /data/backups/offsite-status.json.part && mv /data/backups/offsite-status.json.part /data/backups/offsite-status.json' \
        <"$STATUS_FILE") >/dev/null 2>&1 || true
}

ping_hc() {
    [[ -n $HC_PING_URL ]] || return 0
    curl -fsS -m 10 --retry 3 -o /dev/null "$HC_PING_URL$1" || log "warning: ping to $HC_PING_URL$1 failed"
}

STEP=start
on_error() {
    local rc=$?
    trap - ERR
    log "BACKUP FAILED in step '$STEP' (exit $rc)" >&2
    write_status failed "$STEP" || true
    ping_hc /fail
    exit "$rc"
}

run() {
    trap on_error ERR
    STEP=config
    if [[ -z $BACKUP_REMOTE ]]; then
        log "BACKUP_REMOTE is not set ($APP_DIR/backup/backup.env)" >&2
        false
    fi
    if [[ ! -f $RCLONE_CONFIG_DIR/rclone.conf ]]; then
        log "missing $RCLONE_CONFIG_DIR/rclone.conf (README: rclone authorisation)" >&2
        false
    fi
    cd "$APP_DIR"
    ping_hc /start

    STEP=snapshot
    local snap delete
    if [[ -z $(docker compose ps --status running -q app) ]]; then
        log "the app container is not running ($APP_DIR: docker compose ps)" >&2
        false
    fi
    snap=$(docker compose exec -T app python -m app.backup --dest /data/backups/nightly --keep "$LOCAL_KEEP" | tail -n 1)
    SNAPSHOT_NAME=$(basename "$snap")
    [[ $SNAPSHOT_NAME =~ ^lkhc-[0-9]{8}-[0-9]{6}\.db$ ]] || false
    log "DB snapshot $snap"

    STEP=upload-db
    rclone copyto "/data/backups/nightly/$SNAPSHOT_NAME" "$BACKUP_REMOTE/db/$SNAPSHOT_NAME"
    log "uploaded $BACKUP_REMOTE/db/$SNAPSHOT_NAME"

    STEP=upload-photos
    local photos=1
    docker compose exec -T app test -d /data/photos || photos=0
    if ((photos)); then
        # -v logs each newly copied file (normally a handful per night)
        rclone copy /data/photos "$BACKUP_REMOTE/photos" --exclude '*.part' --stats 0 -v
        log "photos copied to $BACKUP_REMOTE/photos"
    else
        log "no photos yet (/data/photos does not exist)"
    fi

    STEP=retention
    delete=$(rclone lsf --files-only "$BACKUP_REMOTE/db" | plan_retention)
    if [[ -n $delete ]]; then
        log "retention: deleting $(wc -l <<<"$delete") old DB snapshot(s): $(tr '\n' ' ' <<<"$delete")"
        rclone delete --files-from - "$BACKUP_REMOTE/db" <<<"$delete"
    fi

    STEP=summary
    DB_COUNT=$(rclone lsf --files-only "$BACKUP_REMOTE/db" | grep -c '^lkhc-.*\.db$' || true)
    PHOTO_FILES=0 PHOTO_BYTES=0
    if ((photos)); then
        read -r PHOTO_FILES PHOTO_BYTES < <(rclone size --json "$BACKUP_REMOTE/photos" |
            sed -n 's/.*"count":\([0-9]*\).*"bytes":\([0-9]*\).*/\1 \2/p') || true
    fi
    write_status ok ""
    ping_hc ""
    log "OK: $DB_COUNT DB snapshots, $PHOTO_FILES photos ($PHOTO_BYTES bytes) in $BACKUP_REMOTE"
}

check() {
    [[ -f $STATUS_FILE ]] || { echo "no backup status yet ($STATUS_FILE)"; return 1; }
    cat "$STATUS_FILE"
    local last age result
    result=$(status_value result)
    if [[ $result != ok ]]; then
        echo "LAST BACKUP RUN FAILED (step: $(status_value failed_step)) - journalctl -u lkhc-odecty-backup"
        return 1
    fi
    last=$(status_value last_success)
    [[ -n $last ]] || { echo "BACKUP NEVER SUCCEEDED"; return 1; }
    age=$((($(date +%s) - $(date -d "$last" +%s)) / 3600))
    if ((age > MAX_AGE_HOURS)); then
        echo "LAST SUCCESSFUL BACKUP IS ${age} h OLD (limit ${MAX_AGE_HOURS} h)"
        return 1
    fi
    echo "OK: last successful backup ${age} h ago"
}

case "${1:-}" in
run) run ;;
check) check ;;
plan-retention) plan_retention ;;
*)
    sed -n '2,/^set -E/p' "$0" | sed '$d; s/^# \{0,1\}//'
    exit 2
    ;;
esac
