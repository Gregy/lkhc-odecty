#!/usr/bin/env bash
# push-image.sh - variant B (no registry, nothing built on the VPS): run on your computer in the
# repository. Builds the image, streams it over SSH into `docker load` on the VPS, copies the managed
# files (compose.yml, deploy.sh, backup.sh, restore.sh) and runs deploy.sh there.
#
#   deploy/push-image.sh [user@host] [--tag EXISTING_TAG] [--no-deploy]
#
#   user@host       default: DEPLOY_HOST from deploy/local.env
#   --tag TAG       ship the existing local image lkhc-odecty:TAG instead of building one
#   --no-deploy     only upload; then on the VPS: /srv/lkhc-odecty/deploy.sh --image TAG
#
# Environment: REMOTE_DIR [/srv/lkhc-odecty], PLATFORM [linux/amd64], SSH [ssh], SCP [scp].
# The whole image (~250 MB, ~90 MB compressed) is sent every time - fine for an occasional deploy.
set -Eeuo pipefail

REMOTE_DIR="${REMOTE_DIR:-/srv/lkhc-odecty}"
PLATFORM="${PLATFORM:-linux/amd64}"
SSH="${SSH:-ssh}"
SCP="${SCP:-scp}"
TAG_RE='^[0-9]{8}-[0-9]{6}-[0-9a-z]+$'

log() { printf '%s push: %s\n' "$(date '+%F %T')" "$*"; }
die() {
    log "ERROR: $*" >&2
    exit 1
}

cd "$(dirname "$(readlink -f "$0")")/.."
[[ -f Dockerfile && -f deploy/deploy.sh ]] || die "run from the lkhc-odecty repository"
if [[ -f deploy/local.env ]]; then
    # shellcheck source=/dev/null
    . deploy/local.env
fi

host="${DEPLOY_HOST:-}" tag="" deploy=1
while (($#)); do
    case $1 in
    --tag)
        tag=${2:?--tag needs a tag}
        shift
        ;;
    --no-deploy) deploy=0 ;;
    -*) die "unknown option $1" ;;
    *) host=$1 ;;
    esac
    shift
done
[[ -n $host ]] || die "no target: pass user@host or set DEPLOY_HOST in deploy/local.env"

if [[ -n $tag ]]; then
    [[ $tag =~ $TAG_RE ]] || die "tag must look like YYYYmmdd-HHMMSS-<rev> (deploy.sh keeps/prunes those)"
    docker image inspect "lkhc-odecty:$tag" >/dev/null || die "no local image lkhc-odecty:$tag"
else
    rev=local
    if git rev-parse --git-dir >/dev/null 2>&1; then
        rev=$(git rev-parse --short=7 HEAD)
        if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
            rev="${rev}dirty"
            log "WARNING: uncommitted changes are included in the image (tag ...-${rev})"
        fi
    fi
    tag="$(date +%Y%m%d-%H%M%S)-$rev"
    log "building lkhc-odecty:$tag for $PLATFORM"
    docker build --pull --platform "$PLATFORM" -t "lkhc-odecty:$tag" .
fi

log "uploading lkhc-odecty:$tag to $host (docker save | gzip | ssh docker load)"
docker save "lkhc-odecty:$tag" | gzip -1 | $SSH "$host" 'gunzip | docker load'

log "copying compose.yml, deploy.sh, backup.sh, restore.sh to $host:$REMOTE_DIR/"
$SCP deploy/lkhc-odecty/compose.yml deploy/deploy.sh deploy/backup/backup.sh deploy/backup/restore.sh \
    "$host:$REMOTE_DIR/"

if ((!deploy)); then
    log "done; deploy on the VPS with: $REMOTE_DIR/deploy.sh --image $tag"
    exit 0
fi
log "deploying"
$SSH "$host" "$REMOTE_DIR/deploy.sh --image $tag"
