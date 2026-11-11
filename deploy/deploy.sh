#!/usr/bin/env bash
# deploy.sh - update / roll back LKHC Odečty on the VPS. No registry: the image is either built here
# from a git checkout (--git) or was loaded with `docker load` by push-image.sh (--image).
#
#   deploy.sh --git [REF]      fetch /srv/lkhc-odecty/src, check out REF (default origin/main), build, switch
#   deploy.sh --image TAG      switch to lkhc-odecty:TAG (already loaded, see push-image.sh)
#   deploy.sh --rollback [--restore-db /data/backups/pre-deploy/lkhc-....db]
#                              switch back to lkhc-odecty:previous
#   deploy.sh --restart        recreate the container (after editing .env), with the health check
#   deploy.sh --status         images, container, DB revision, deploy history
#
# Switching (--git/--image):
#   1. snapshot of the DB (online backup) -> /data/backups/pre-deploy/ ; remember its schema revision
#   2. tag live -> previous, TAG -> live; `docker compose up -d --wait` (the container entrypoint
#      makes its own pre-migrate snapshot and migrates the DB, the healthcheck must pass)
#   3. GET PUBLIC_HEALTH_URL (BASE_URL/healthz) through Caddy on this host
#   4. failure -> tag previous -> live; if the failed version already migrated the DB, put the
#      snapshot from step 1 back (the migrated DB is kept in /data/backups/failed-deploy-*); start
#      the previous version and check it again. Exit code 1 either way.
# Images are tagged YYYYmmdd-HHMMSS-<commit>; the newest KEEP_IMAGES of them are kept.
#
# Environment (defaults in brackets): APP_DIR [/srv/lkhc-odecty], IMAGE [lkhc-odecty],
# GIT_BRANCH [main], KEEP_IMAGES [5], WAIT_TIMEOUT [180 s],
# PUBLIC_HEALTH_URL [BASE_URL from $APP_DIR/.env + /healthz; empty = skip the check through the proxy].
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/srv/lkhc-odecty}"
IMAGE="${IMAGE:-lkhc-odecty}"
GIT_BRANCH="${GIT_BRANCH:-main}"
KEEP_IMAGES="${KEEP_IMAGES:-5}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
TAG_RE='^[0-9]{8}-[0-9]{6}-[0-9a-z]+'

# Repo file -> file in APP_DIR. Updated by --git (and copied by push-image.sh).
MANAGED_FILES=(
    "deploy/lkhc-odecty/compose.yml:compose.yml:0644"
    "deploy/deploy.sh:deploy.sh:0755"
    "deploy/backup/backup.sh:backup.sh:0755"
    "deploy/backup/restore.sh:restore.sh:0755"
)

log() { printf '%s deploy: %s\n' "$(date '+%F %T')" "$*"; }
die() {
    log "ERROR: $*" >&2
    exit 1
}
image_id() { docker image inspect --format '{{.Id}}' "$1" 2>/dev/null; }

# Schema revision of /data/app.db ("none" = no database, "unknown" = could not be read).
# Works whether the app runs or not.
db_revision() {
    local out
    out=$(docker compose run --rm --no-deps -T --entrypoint python app -c '
import os, sqlite3
p = "/data/app.db"
rev = "none"
if os.path.exists(p):
    con = sqlite3.connect(p)
    try:
        rev = con.execute("select version_num from alembic_version").fetchone()[0]
    except Exception:
        rev = "unversioned"
    con.close()
print(rev)' 2>/dev/null) || out=unknown
    printf '%s\n' "${out##*$'\n'}"
}

# Does image $1 know DB revision $2 (i.e. can it start on this DB)?
image_knows_revision() {
    [[ $2 == none ]] && return 0
    docker run --rm --entrypoint python "$1" -c '
import sys
from alembic.script import ScriptDirectory
from app.migrate import alembic_config
ScriptDirectory.from_config(alembic_config("sqlite://")).get_revision(sys.argv[1])' "$2" >/dev/null 2>&1
}

# Put a DB snapshot (path inside the volume) back as /data/app.db; the current DB files are moved
# to /data/backups/<label>-<time>/.
restore_snapshot() {
    local snap=$1 label=$2
    log "restoring database snapshot $snap (current DB -> /data/backups/$label-*)"
    docker compose run --rm --no-deps -T --entrypoint sh app -c '
set -eu
snap=$1; keep=/data/backups/$2-$(date +%Y%m%d-%H%M%S)
test -f "$snap"
mkdir -p "$keep"
for f in app.db app.db-wal app.db-shm; do if [ -e "/data/$f" ]; then mv "/data/$f" "$keep/"; fi; done
cp "$snap" /data/app.db.part && mv /data/app.db.part /data/app.db
echo "previous database files kept in $keep"' sh "$snap" "$label"
}

public_check() {
    [[ -n $PUBLIC_HEALTH_URL ]] || return 0
    local re='^(https?)://([^/:]+)(:([0-9]+))?(/.*)?$' scheme host port
    [[ $PUBLIC_HEALTH_URL =~ $re ]] || die "PUBLIC_HEALTH_URL is not a URL: $PUBLIC_HEALTH_URL"
    scheme=${BASH_REMATCH[1]} host=${BASH_REMATCH[2]} port=${BASH_REMATCH[4]}
    if [[ -z $port ]]; then
        if [[ $scheme == https ]]; then port=443; else port=80; fi
    fi
    log "checking $PUBLIC_HEALTH_URL through the proxy on this host"
    curl -fsS --max-time 10 --retry 6 --retry-delay 2 --retry-all-errors \
        --resolve "$host:$port:127.0.0.1" -o /dev/null "$PUBLIC_HEALTH_URL"
}

start_and_check() {
    log "starting $IMAGE:live (waiting up to ${WAIT_TIMEOUT}s for the healthcheck)"
    docker compose up -d --no-build --force-recreate --wait --wait-timeout "$WAIT_TIMEOUT" app &&
        public_check
}

prune_images() {
    local tags t
    mapfile -t tags < <(docker image ls "$IMAGE" --format '{{.Tag}}' | grep -E "$TAG_RE" | sort -r)
    for t in "${tags[@]:KEEP_IMAGES}"; do
        log "removing old image tag $IMAGE:$t"
        docker image rm "$IMAGE:$t" >/dev/null || true # still used as live/previous -> only untagged
    done
}

switch_to() {
    local tag=$1 new_id live_id prev_rev="" now_rev snap=""
    new_id=$(image_id "$IMAGE:$tag") || die "image $IMAGE:$tag not found"
    live_id=$(image_id "$IMAGE:live" || true)
    if [[ -n $live_id && $new_id == "$live_id" ]]; then
        log "$IMAGE:$tag is already live; making sure it runs"
        start_and_check || die "the live version is not healthy"
        return 0
    fi

    if [[ -n $live_id ]]; then
        prev_rev=$(db_revision)
        [[ $prev_rev != unknown ]] || die "cannot read the DB schema revision; nothing changed"
        if [[ $prev_rev != none ]]; then
            snap=$(docker compose run --rm --no-deps -T --entrypoint python app \
                -m app.backup --dest /data/backups/pre-deploy --keep 10 | tail -n 1)
            log "DB snapshot before deploy: $snap (schema revision $prev_rev)"
        fi
        docker tag "$IMAGE:live" "$IMAGE:previous"
    else
        log "first deployment (no $IMAGE:live yet)"
    fi
    docker tag "$IMAGE:$tag" "$IMAGE:live"

    if start_and_check; then
        printf '%s %s ok\n' "$(date -Is)" "$tag" >>"$APP_DIR/deploy-history"
        log "OK: $IMAGE:$tag is live"
        prune_images
        return 0
    fi

    printf '%s %s FAILED\n' "$(date -Is)" "$tag" >>"$APP_DIR/deploy-history"
    log "new version $tag is NOT healthy; last log lines:"
    docker compose logs --no-color --tail 60 app >&2 || true
    if [[ -z $live_id ]]; then
        docker compose stop app || true
        die "first deployment failed, nothing to roll back to (container stopped)"
    fi

    log "ROLLBACK to the previous image"
    docker tag "$IMAGE:previous" "$IMAGE:live"
    docker compose stop app
    now_rev=$(db_revision)
    if [[ $now_rev != "$prev_rev" ]]; then
        [[ -n $snap ]] || die "DB schema changed ($prev_rev -> $now_rev) and there is no snapshot; restore manually"
        log "the failed version changed the DB schema ($prev_rev -> $now_rev)"
        restore_snapshot "$snap" failed-deploy
    fi
    start_and_check || die "ROLLBACK FAILED as well - see 'docker compose logs app' (README: Návrat zpět)"
    printf '%s %s rolled-back\n' "$(date -Is)" "$(image_id "$IMAGE:live")" >>"$APP_DIR/deploy-history"
    die "deployment of $tag failed; the previous version runs again"
}

sync_managed_files() {
    local src=$1 entry from to mode
    for entry in "${MANAGED_FILES[@]}"; do
        IFS=: read -r from to mode <<<"$entry"
        if ! cmp -s "$src/$from" "$APP_DIR/$to"; then
            log "updating $APP_DIR/$to from the repository"
            diff -u "$APP_DIR/$to" "$src/$from" || true
            install -m "$mode" "$src/$from" "$APP_DIR/$to"
        fi
    done
}

deploy_git() {
    local ref=${1:-origin/$GIT_BRANCH} src="$APP_DIR/src" sha tag
    [[ -d $src/.git ]] || die "$src is not a git checkout (see README, variant A)"
    [[ -z $(git -C "$src" status --porcelain --untracked-files=no) ]] || die "local changes in $src"
    log "git fetch + checkout $ref"
    git -C "$src" fetch --prune --tags origin
    git -C "$src" checkout --quiet --detach "$ref"
    sha=$(git -C "$src" rev-parse --short=7 HEAD)
    git -C "$src" log -1 --format='%h %ci %s' | sed 's/^/    /'
    sync_managed_files "$src"
    tag="$(date +%Y%m%d-%H%M%S)-$sha"
    log "building $IMAGE:$tag"
    docker build --pull -t "$IMAGE:$tag" \
        --label "org.opencontainers.image.revision=$(git -C "$src" rev-parse HEAD)" "$src"
    switch_to "$tag"
}

rollback() {
    local restore_db=${1:-} prev_id live_id rev
    prev_id=$(image_id "$IMAGE:previous") || die "there is no $IMAGE:previous"
    live_id=$(image_id "$IMAGE:live") || die "there is no $IMAGE:live"
    [[ $prev_id != "$live_id" ]] || die "$IMAGE:previous is the same image as live"
    rev=$(db_revision)
    if [[ -z $restore_db ]] && ! image_knows_revision "$IMAGE:previous" "$rev"; then
        log "the database is at schema revision $rev, which the previous version does not know"
        log "(the newer version migrated it). Roll back together with a snapshot from before that deploy:"
        docker compose run --rm --no-deps -T --entrypoint sh app -c 'ls -1t /data/backups/pre-deploy/ 2>/dev/null | head -n 5' |
            sed 's|^|    deploy.sh --rollback --restore-db /data/backups/pre-deploy/|'
        die "refusing to start the previous version on a newer database (changes made since the snapshot would be lost)"
    fi
    log "rolling back: $IMAGE:previous -> live"
    docker tag "$IMAGE:previous" "$IMAGE:live"
    docker tag "$live_id" "$IMAGE:previous"
    docker compose stop app
    [[ -z $restore_db ]] || restore_snapshot "$restore_db" rollback
    start_and_check || die "the rolled-back version is not healthy either - see 'docker compose logs app'"
    printf '%s %s manual-rollback\n' "$(date -Is)" "$prev_id" >>"$APP_DIR/deploy-history"
    log "OK: previous version is live (the one rolled back from is now $IMAGE:previous)"
}

status() {
    echo "== images"
    docker image ls "$IMAGE" --format 'table {{.Tag}}\t{{.ID}}\t{{.CreatedSince}}'
    echo "live:     $(image_id "$IMAGE:live" || echo -)"
    echo "previous: $(image_id "$IMAGE:previous" || echo -)"
    echo "== container"
    docker compose ps app
    echo "== database schema revision: $(db_revision)"
    echo "== deploy history (last 10)"
    tail -n 10 "$APP_DIR/deploy-history" 2>/dev/null || echo "(none)"
}

main() {
    [[ -f $APP_DIR/compose.yml ]] || die "$APP_DIR/compose.yml not found (APP_DIR=$APP_DIR)"
    if [[ -z ${PUBLIC_HEALTH_URL+set} ]]; then
        PUBLIC_HEALTH_URL=$(sed -n 's|^BASE_URL=\(.*[^/]\)/*$|\1/healthz|p' "$APP_DIR/.env" 2>/dev/null | tail -n 1)
    fi
    cd "$APP_DIR"
    exec 9>"$APP_DIR/.deploy.lock"
    flock -n 9 || die "another deploy.sh is running"

    case "${1:-}" in
    --git) deploy_git "${2:-}" ;;
    --image)
        [[ -n ${2:-} ]] || die "usage: deploy.sh --image TAG"
        switch_to "$2"
        ;;
    --rollback)
        if [[ ${2:-} == --restore-db ]]; then
            [[ -n ${3:-} ]] || die "usage: deploy.sh --rollback --restore-db /data/backups/pre-deploy/FILE"
            rollback "$3"
        else
            rollback
        fi
        ;;
    --restart) start_and_check || die "not healthy after restart - see 'docker compose logs app'" ;;
    --status) status ;;
    *)
        sed -n '2,/^set -E/p' "$0" | sed '$d; s/^# \{0,1\}//'
        exit 2
        ;;
    esac
}

# Everything is in functions and main runs last: a `git checkout` that replaces this file while it
# runs cannot change what bash executes.
main "$@"
