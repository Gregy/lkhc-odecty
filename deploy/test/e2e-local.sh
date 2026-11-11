#!/usr/bin/env bash
# e2e-local.sh - exercise the deploy/ tooling against local Docker (no VPS, no Let's Encrypt, no Drive).
#
#   deploy/test/e2e-local.sh all        everything below, then cleanup
#   deploy/test/e2e-local.sh <phase>... setup proxy app headers upload second deploy backup restore cleanup
#
# Layout: a scratch copy of /srv in $T (default: deploy/test/.work, removed by cleanup), with
# compose.override.yml files that only change what cannot work locally: high ports on 127.0.0.1,
# Caddy's own CA (local_certs) instead of Let's Encrypt, SELinux ":z" on bind mounts, and test
# project/volume names (lkhctest-*). Google Drive is an rclone "local" remote in the volume
# lkhctest-drive. Requires: docker, curl, python3 (+ Pillow via uv for test photos), the
# lkhc-odecty:latest image and the XLSX export (E2E_XLSX, default private/*.xlsx; read-only, stdin).
# Never touches ./data or the user's compose project.
# shellcheck disable=SC2016,SC2012  # single quotes are intentional (bash -c / remote snippets)
set -Eeuo pipefail

REPO=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)
T="${T:-$REPO/deploy/test/.work}"
HOST=odecty.example.org
DOMAIN=example.org
ADMIN=admin@example.org
# the real export (private), read-only via stdin
XLSX="${E2E_XLSX:-$(ls "$REPO"/private/*.xlsx "$REPO"/*.xlsx 2>/dev/null | head -n 1 || true)}"
HTTPS_PORT=18443
HTTP_PORT=18080
URL="https://$HOST:$HTTPS_PORT"
TAG_OLD=20261101-000000-e2eold
TAG_NEW=20261102-000000-e2enew
TAG_BROKEN=20261103-000000-e2ebroken
TAG_MIG=20261104-000000-e2emig
APP="$T/srv/lkhc-odecty"
PROXY="$T/srv/proxy"
RCLONE_IMAGE=rclone/rclone:1.76.0
export APP_DIR="$APP" PUBLIC_HEALTH_URL="$URL/healthz" CURL_CA_BUNDLE="$T/root.crt" WAIT_TIMEOUT=60
# keep every test image (other dated lkhc-odecty tags on this machine would otherwise push them out)
export KEEP_IMAGES=100

pass=0
fail=0
ok() {
    pass=$((pass + 1))
    printf '  \e[32mPASS\e[0m %s\n' "$*"
}
bad() {
    fail=$((fail + 1))
    printf '  \e[31mFAIL\e[0m %s\n' "$*"
}
check() { # description, command...
    local d=$1
    shift
    if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi
}
phase() { printf '\n\e[1m### %s\e[0m\n' "$*"; }
c() { curl -sS --resolve "$HOST:$HTTPS_PORT:127.0.0.1" --resolve "$HOST:$HTTP_PORT:127.0.0.1" "$@"; }
app_dc() { (cd "$APP" && docker compose "$@"); }
proxy_dc() { (cd "$PROXY" && docker compose "$@"); }
sql() { app_dc exec -T app python -c "import sqlite3,sys; print(sqlite3.connect('/data/app.db').execute(sys.argv[1]).fetchone()[0])" "$1"; }
img_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null; }

# --- setup: scratch /srv, overrides, images -------------------------------------------------------
setup() {
    phase "setup ($T)"
    rm -rf "$T"
    mkdir -p "$T/srv"
    cp -r "$REPO/deploy/proxy" "$PROXY"
    cp "$REPO/deploy/proxy/env.example" "$PROXY/.env"
    sed -i "s|^LKHC_ODECTY_HOST=.*|LKHC_ODECTY_HOST=$HOST|" "$PROXY/.env"
    echo "COMPOSE_PROJECT_NAME=lkhctest-proxy" >>"$PROXY/.env"
    cp "$PROXY/caddy/global.d/pre-dns.caddy.example" "$PROXY/caddy/global.d/pre-dns.caddy" # Caddy's own CA
    cat >"$PROXY/compose.override.yml" <<EOF
services:
  caddy:
    ports: !override
      - "127.0.0.1:$HTTP_PORT:80"
      - "127.0.0.1:$HTTPS_PORT:443"
      - "127.0.0.1:$HTTPS_PORT:443/udp"
    volumes: !override
      - ./caddy:/etc/caddy:ro,z
      - caddy-data:/data
      - caddy-config:/config
EOF
    mkdir -p "$APP/backup/rclone"
    chmod 0700 "$APP/backup" "$APP/backup/rclone"
    install -m 0644 "$REPO/deploy/lkhc-odecty/compose.yml" "$APP/compose.yml"
    install -m 0755 "$REPO/deploy/deploy.sh" "$REPO/deploy/backup/backup.sh" "$REPO/deploy/backup/restore.sh" "$APP/"
    {
        sed -e "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')|" \
            -e 's|^GOOGLE_CLIENT_ID=.*|GOOGLE_CLIENT_ID=e2e-test-client.apps.googleusercontent.com|' \
            -e 's|^GOOGLE_CLIENT_SECRET=.*|GOOGLE_CLIENT_SECRET=e2e-test-secret|' \
            -e "s|^BASE_URL=.*|BASE_URL=https://$HOST|" -e "s|^ALLOWED_DOMAIN=.*|ALLOWED_DOMAIN=$DOMAIN|" \
            -e "s|^ADMIN_EMAILS=.*|ADMIN_EMAILS=$ADMIN|" \
            "$REPO/deploy/lkhc-odecty/env.production"
        echo "COMPOSE_PROJECT_NAME=lkhctest-app"
        echo "DATA_VOLUME=lkhctest-data"
    } >"$APP/.env"
    chmod 0600 "$APP/.env"

    docker network inspect proxy >/dev/null 2>&1 || docker network create --subnet 10.250.0.0/24 --ip-range 10.250.0.128/25 proxy >/dev/null
    docker tag lkhc-odecty:latest "lkhc-odecty:$TAG_OLD"
    echo "  building lkhc-odecty:$TAG_NEW from the working tree"
    docker build -q -t "lkhc-odecty:$TAG_NEW" "$REPO" >/dev/null
    # broken release: changes the schema revision (as a migration would), then never becomes healthy
    mkdir -p "$T/broken"
    cat >"$T/broken/Dockerfile" <<'EOF'
ARG BASE
FROM ${BASE}
CMD ["sh", "-c", "python -c \"import sqlite3; c=sqlite3.connect('/data/app.db'); c.execute(\\\"update alembic_version set version_num='ffff_broken'\\\"); c.commit()\" && sleep infinity"]
EOF
    docker build -q --build-arg "BASE=lkhc-odecty:$TAG_NEW" -t "lkhc-odecty:$TAG_BROKEN" "$T/broken" >/dev/null
    # release with a real (no-op) migration 0004, for the migration-aware manual rollback
    mkdir -p "$T/mig"
    cat >"$T/mig/0004_e2e_noop.py" <<'EOF'
"""e2e test: no-op migration"""
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
EOF
    printf 'ARG BASE\nFROM ${BASE}\nCOPY 0004_e2e_noop.py /app/alembic/versions/\n' >"$T/mig/Dockerfile"
    docker build -q --build-arg "BASE=lkhc-odecty:$TAG_NEW" -t "lkhc-odecty:$TAG_MIG" "$T/mig" >/dev/null
    ok "scratch tree + images"
}

# --- proxy ---------------------------------------------------------------------------------------
proxy() {
    phase "proxy stack"
    proxy_dc up -d --wait
    local i
    for i in $(seq 30); do
        proxy_dc exec -T caddy cat /data/caddy/pki/authorities/local/root.crt >"$T/root.crt" 2>/dev/null && break
        sleep 1
    done
    check "Caddy local CA root exported" test -s "$T/root.crt"
    check "caddy validate" proxy_dc exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
    check "Caddy has the fixed proxy IP 10.250.0.2" \
        test "$(docker inspect -f '{{(index .NetworkSettings.Networks "proxy").IPAddress}}' lkhctest-proxy-caddy-1)" = 10.250.0.2
}

# --- app: first deploy, import, test user -----------------------------------------------------------
app() {
    phase "app: first deploy (variant B tag) + data import"
    "$APP/deploy.sh" --image "$TAG_OLD"
    check "live = $TAG_OLD" test "$(img_id lkhc-odecty:live)" = "$(img_id "lkhc-odecty:$TAG_OLD")"
    check "app container publishes no host ports" test -z "$(docker port lkhctest-app-app-1)"
    [[ -f $XLSX ]] || { bad "no XLSX export (E2E_XLSX)"; return 1; }
    # A copy of private/ (if any) is mounted at /private; E2E_IMPORT_ARGS adds importer options,
    # e.g. E2E_IMPORT_ARGS="--private-dir /private" when the mapping lives there.
    local priv=()
    if [[ -d $REPO/private ]]; then
        rm -rf "$T/private" && cp -r "$REPO/private" "$T/private" && priv=(-v "$T/private:/private:ro,z")
    fi
    read -r -a extra <<<"${E2E_IMPORT_ARGS:-}"
    app_dc run --rm -T --no-deps "${priv[@]}" app python -m app.importer - "${extra[@]}" <"$XLSX" | tail -n 3
    app_dc exec -T -e "E2E_ADMIN=$ADMIN" app python - <<'EOF'
import os
from app.auth import login_or_create_user
from app.config import get_settings
from app.db import make_engine, make_sessionmaker
s = get_settings()
db = make_sessionmaker(make_engine(s.sqlalchemy_url))()
u = login_or_create_user(db, os.environ["E2E_ADMIN"], "Admin (e2e)", s)
db.commit()
print("admin user", u.id, u.role)
EOF
    check "readings imported" test "$(sql 'select count(*) from readings')" -gt 100
}

# A session cookie for the admin, signed with the app's SECRET_KEY (as after a Google login).
session_cookie() {
    app_dc exec -T app python - <<'EOF'
import base64, json, time
from itsdangerous import TimestampSigner
from app.config import get_settings
s = get_settings()
data = {"user_id": 1, "login_at": int(time.time()), "csrf_token": "e2e-csrf-token"}
print(TimestampSigner(s.secret_key).sign(base64.b64encode(json.dumps(data).encode())).decode())
EOF
}

# --- headers, redirects, cookies --------------------------------------------------------------------
headers() {
    phase "headers / redirects / cookies through Caddy"
    local h loc
    h=$(c -o /dev/null -D - "http://$HOST:$HTTP_PORT/odecty/davka")
    loc=$(sed -n 's/^[Ll]ocation: *//p' <<<"$h" | tr -d '\r')
    check "http -> https redirect (308, $loc)" grep -q "^HTTP/1.1 308" <<<"$h"
    check "redirect target https://$HOST/odecty/davka" test "$loc" = "https://$HOST/odecty/davka"

    h=$(c -o /dev/null -D - "$URL/healthz")
    check "healthz 200 over HTTP/2" grep -q "^HTTP/2 200" <<<"$h"
    check "HSTS from the app" grep -qi "^strict-transport-security: max-age=31536000" <<<"$h"
    check "X-Frame-Options / nosniff" bash -c 'grep -qi "^x-frame-options: DENY" <<<"$1" && grep -qi "^x-content-type-options: nosniff" <<<"$1"' _ "$h"
    check "no Server header" bash -c '! grep -qi "^server:" <<<"$1"' _ "$h"
    check "Alt-Svc advertises HTTP/3" grep -qi '^alt-svc: h3=' <<<"$h"

    h=$(c -o /dev/null -D - "$URL/odecty/davka/")
    loc=$(sed -n 's/^location: *//Ip' <<<"$h" | tr -d '\r')
    check "app builds https URLs from X-Forwarded-Proto ($loc)" test "$loc" = "https://$HOST:$HTTPS_PORT/odecty/davka"

    h=$(c -o /dev/null -D - "$URL/auth/google")
    loc=$(sed -n 's/^location: *//Ip' <<<"$h" | tr -d '\r')
    check "OAuth redirect_uri = https://$HOST/auth/callback" grep -q "redirect_uri=https%3A%2F%2F$HOST%2Fauth%2Fcallback" <<<"$loc"
    check "OAuth hd=$DOMAIN" grep -q "hd=$DOMAIN" <<<"$loc"
    check "session cookie Secure; HttpOnly; SameSite=lax" bash -c \
        'grep -i "^set-cookie: lkhc_session=" <<<"$1" | grep -qi "secure" && grep -i "^set-cookie: lkhc_session=" <<<"$1" | grep -qi "httponly" && grep -i "^set-cookie: lkhc_session=" <<<"$1" | grep -qi "samesite=lax"' _ "$h"

    COOKIE=$(session_cookie)
    h=$(c -o /dev/null -D - -b "lkhc_session=$COOKIE" "$URL/sprava")
    check "logged-in admin page 200 (forged session = what a Google login yields)" grep -q "^HTTP/2 200" <<<"$h"

    c -o /dev/null "$URL/healthz?e2e-xff-check"
    sleep 1
    local line gw
    gw=$(docker network inspect -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' proxy)
    line=$(app_dc logs --no-color app | grep 'e2e-xff-check' | tail -n 1)
    check "app logs the real client (here the Docker gateway $gw), not Caddy's 10.250.0.2: ${line##*app-1  | }" \
        grep -q "$gw:" <<<"$line"
}

# --- uploads -------------------------------------------------------------------------------------------
make_photos() {
    mkdir -p "$T/photos"
    (cd "$REPO" && uv run python - "$T/photos" <<'EOF'
import os, sys
from PIL import Image
out = sys.argv[1]
for i in range(8):
    # a real 12 MP JPEG of a few MB (noise does not compress), unique per file
    im = Image.effect_noise((4000, 3000), 20 + i).convert("RGB")
    im.save(f"{out}/IMG_2026110{i}_0{i}.jpg", quality=72)
print("photos:", sorted(os.path.getsize(f"{out}/{f}") // 1024 for f in os.listdir(out)), "KiB")
EOF
    )
}

upload() {
    phase "uploads through Caddy"
    make_photos
    local args=() f code body total
    for f in "$T"/photos/*.jpg; do args+=(-F "files=@$f;type=image/jpeg"); done
    total=$(du -cb "$T"/photos/*.jpg | tail -n 1 | cut -f1)
    body=$(c -b "lkhc_session=$COOKIE" -H "X-CSRF-Token: e2e-csrf-token" -H "Accept: application/json" \
        -w '\n%{http_code} %{time_total}s' "${args[@]}" -F "reported_by=e2e" "$URL/odecty/davka")
    code=$(tail -n 1 <<<"$body")
    check "one multipart request with 8 photos ($((total / 1024 / 1024)) MB): ${code}" grep -q '^200 ' <<<"$code"
    check "all 8 stored" test "$(head -n -1 <<<"$body" | python3 -c 'import json,sys; print(sum(r["ok"] for r in json.load(sys.stdin)["results"]))')" = 8

    f=$(ls "$T"/photos/*.jpg | head -n 1)
    code=$(c -b "lkhc_session=$COOKIE" -H "X-CSRF-Token: e2e-csrf-token" -H "Accept: application/json" \
        --limit-rate 150k -o /dev/null -w '%{http_code} %{time_total}s' -F "files=@$f;type=image/jpeg" "$URL/odecty/davka")
    check "slow upload (150 kB/s, like weak mobile data): $code" grep -q '^200 ' <<<"$code"

    # Web Share Target without a service worker: all shared photos in one POST, CSRF-exempt, discarded
    code=$(c -o /dev/null -w '%{http_code}' "${args[@]}" "${args[@]}" "$URL/obchuzka/sdilet")
    check "share-target fallback POST with 16 photos (~$((2 * total / 1024 / 1024)) MB) reaches the app: $code" test "$code" = 303

    head -c $((110 * 1024 * 1024)) /dev/zero >"$T/big.bin"
    code=$(c -o /dev/null -w '%{http_code}' -b "lkhc_session=$COOKIE" -H "X-CSRF-Token: e2e-csrf-token" \
        -F "files=@$T/big.bin;type=image/jpeg" "$URL/odecty/davka")
    check "110 MB body stopped by Caddy's request_body limit: $code" test "$code" = 413
    rm -f "$T/big.bin"
    check "8 photo files in the volume (the slow re-upload was deduplicated)" test "$(app_dc exec -T app sh -c 'find /data/photos -name "*.jpg" ! -name "*_t.jpg" | wc -l')" -eq 8
}

# --- second app via the documented procedure -------------------------------------------------------
second() {
    phase "second app (deploy/app-template) + reload.sh"
    mkdir -p "$T/srv/whoami"
    sed -e 's/^name: myapp/name: lkhctest-second/' -e 's/nginx:1.29-alpine/nginx:alpine/' -e 's/\[myapp\]/[second]/' \
        "$REPO/deploy/app-template/compose.yml" >"$T/srv/whoami/compose.yml"
    sed -e 's/myapp.example.org/second.example.org/' -e 's/myapp:80/second:80/' \
        "$REPO/deploy/app-template/site.caddy" >"$PROXY/caddy/sites/second.example.org.caddy"
    (cd "$T/srv/whoami" && docker compose up -d --wait)
    "$PROXY/reload.sh"
    local h
    h=$(curl -sS --resolve "second.example.org:$HTTPS_PORT:127.0.0.1" -o /dev/null -D - "https://second.example.org:$HTTPS_PORT/")
    check "second.example.org served by the second app" grep -qi "^HTTP/2 200" <<<"$h"
    check "odecty still fine" test "$(c -o /dev/null -w '%{http_code}' "$URL/healthz")" = 200
    # spoofing from a neighbour on the proxy network: X-Forwarded-* must be ignored
    (cd "$T/srv/whoami" && docker compose exec -T web wget -q -O /dev/null \
        --header 'X-Forwarded-For: 6.6.6.6' --header 'X-Forwarded-Proto: https' 'http://lkhc-odecty:8000/healthz?e2e-spoof') || true
    sleep 1
    check "X-Forwarded-For from another container is not trusted" bash -c \
        '! grep "e2e-spoof" <<<"$1" | grep -q "6.6.6.6"' _ "$(app_dc logs --no-color app)"
    check "second app publishes no host ports" test -z "$(docker port lkhctest-second-web-1)"
}

# --- deploy.sh -------------------------------------------------------------------------------------------
fake_ssh() {
    mkdir -p "$T/bin"
    printf '#!/bin/sh\nshift\nexec bash -c "$*"\n' >"$T/bin/ssh"
    printf '#!/bin/sh\n# last arg host:dir -> dir\neval dest=\\${$#}\nn=$(($#-1))\nfor a; do [ $n -le 0 ] && break; cp "$a" "${dest#*:}"; n=$((n-1)); done\n' >"$T/bin/scp"
    chmod +x "$T/bin/ssh" "$T/bin/scp"
}

deploy() {
    phase "deploy.sh"
    local before
    before=$(sql 'select count(*) from readings')

    echo "-- variant B: push-image.sh (build + docker save | ssh docker load), ssh/scp faked locally"
    fake_ssh
    SSH="$T/bin/ssh" SCP="$T/bin/scp" REMOTE_DIR="$APP" PLATFORM=linux/amd64 \
        "$REPO/deploy/push-image.sh" deploy@vps.example
    local pushed
    pushed=$(docker image ls lkhc-odecty --format '{{.Tag}}' | grep -E '^[0-9]{8}-[0-9]{6}-local$' | sort | tail -n 1)
    check "push-image.sh deployed $pushed" test "$(img_id lkhc-odecty:live)" = "$(img_id "lkhc-odecty:$pushed")"
    check "previous = $TAG_OLD" test "$(img_id lkhc-odecty:previous)" = "$(img_id "lkhc-odecty:$TAG_OLD")"

    echo "-- variant A: deploy.sh --git (clone of a scratch repo with the working tree)"
    rm -rf "$T/gitsrc"
    mkdir -p "$T/gitsrc"
    (cd "$REPO" && tar --exclude=./data --exclude=./examples --exclude=./.venv --exclude=./deploy/test/.work --exclude=./private \
        --exclude='*.xlsx' --exclude=__pycache__ --exclude=.ruff_cache --exclude=.pytest_cache -cf - .) | tar -xf - -C "$T/gitsrc"
    git -C "$T/gitsrc" init -q -b main
    git -C "$T/gitsrc" -c user.email=e2e@test -c user.name=e2e add -A
    git -C "$T/gitsrc" -c user.email=e2e@test -c user.name=e2e commit -qm "e2e snapshot"
    git clone -q "$T/gitsrc" "$APP/src"
    "$APP/deploy.sh" --git
    local sha
    sha=$(git -C "$APP/src" rev-parse --short=7 HEAD)
    check "--git built and deployed a *-$sha image" bash -c 'docker image ls lkhc-odecty --format "{{.Tag}}" | grep -q -- "-$1\$"' _ "$sha"

    echo "-- forced health-check failure -> automatic rollback (incl. DB snapshot restore)"
    local live_before rc=0
    live_before=$(img_id lkhc-odecty:live)
    "$APP/deploy.sh" --image "$TAG_BROKEN" || rc=$?
    check "deploy of the broken image exits non-zero ($rc)" test "$rc" -ne 0
    check "live is the previous image again" test "$(img_id lkhc-odecty:live)" = "$live_before"
    check "app healthy after rollback" test "$(c -o /dev/null -w '%{http_code}' "$URL/healthz")" = 200
    check "DB schema revision restored (0003)" test "$(sql 'select version_num from alembic_version')" = 0003
    check "readings intact ($before)" test "$(sql 'select count(*) from readings')" = "$before"
    check "history records FAILED + rolled-back" bash -c 'grep -q FAILED "$1" && grep -q rolled-back "$1"' _ "$APP/deploy-history"

    echo "-- release with a migration, then manual rollback"
    "$APP/deploy.sh" --image "$TAG_MIG"
    check "migration release live, DB at 0004" test "$(sql 'select version_num from alembic_version')" = 0004
    rc=0
    "$APP/deploy.sh" --rollback || rc=$?
    check "--rollback refuses to start the old code on the migrated DB" test "$rc" -ne 0
    local snap
    snap=$(app_dc exec -T app sh -c 'ls -1t /data/backups/pre-deploy/ | head -n 1')
    "$APP/deploy.sh" --rollback --restore-db "/data/backups/pre-deploy/$snap"
    check "--rollback --restore-db: live = image before the migration" test "$(img_id lkhc-odecty:live)" = "$live_before"
    check "DB back at 0003, readings intact" bash -c '[ "$1" = 0003 ] && [ "$2" = "$3" ]' _ \
        "$(sql 'select version_num from alembic_version')" "$(sql 'select count(*) from readings')" "$before"
    "$APP/deploy.sh" --status
    KEEP_IMAGES=2 "$APP/deploy.sh" --image "$TAG_NEW" >/dev/null
    check "old dated tags pruned (KEEP_IMAGES=2)" test "$(docker image ls lkhc-odecty --format '{{.Tag}}' | grep -cE '^[0-9]{8}-')" -le 3
}

# --- backup / restore ---------------------------------------------------------------------------------
rclone_setup() {
    docker volume create lkhctest-drive >/dev/null
    docker run --rm -v lkhctest-drive:/drive busybox chown "$(id -u):$(id -g)" /drive
    local p1 p2
    p1=$(docker run --rm "$RCLONE_IMAGE" obscure e2e-crypt-password)
    p2=$(docker run --rm "$RCLONE_IMAGE" obscure e2e-crypt-salt)
    cat >"$APP/backup/rclone/rclone.conf" <<EOF
# test: a local directory stands in for Google Drive
[gdrive]
type = local

[gdrive-crypt]
type = crypt
remote = gdrive:/drive/crypt
password = $p1
password2 = $p2
EOF
    chmod 0600 "$APP/backup/rclone/rclone.conf"
    cat >"$APP/backup/backup.env" <<'EOF'
BACKUP_REMOTE=gdrive:/drive/lkhc
RCLONE_DOCKER_ARGS="-v lkhctest-drive:/drive"
DATA_VOLUME=lkhctest-data
EOF
}
drive() { docker run --rm -v lkhctest-drive:/drive busybox sh -c "$1"; }
photo_sums() { app_dc exec -T app sh -c 'cd /data/photos && find . -type f ! -name "*.part" | sort | xargs sha256sum' | sha256sum; }

backup() {
    phase "backup.sh (rclone local remote standing in for Drive)"
    rclone_setup
    echo "-- retention planner (synthetic: one snapshot per day for 400 days + an extra one today)"
    local kept
    kept=$(for i in $(seq 0 399); do date -d "2026-11-11 -$i day" +lkhc-%Y%m%d-033000.db; done
           echo lkhc-20261111-120000.db)
    kept=$(comm -23 <(sort <<<"$kept") <(KEEP_DAILY=14 KEEP_WEEKLY=8 KEEP_MONTHLY=12 "$APP/backup.sh" plan-retention <<<"$kept" | sort))
    echo "  kept: $(head -n 3 <<<"$kept" | tr '\n' ' ')... $(tail -n 2 <<<"$kept" | tr '\n' ' ')"
    check "retention keeps 14 daily + older weekly/monthly: $(wc -l <<<"$kept") files" test "$(wc -l <<<"$kept")" -ge 26 -a "$(wc -l <<<"$kept")" -le 34
    check "oldest kept is ~12 months back" bash -c '[[ $(head -n1 <<<"$1") > lkhc-20251101 ]]' _ "$kept"

    # old snapshots already on the "Drive" (as after a year of nightly runs)
    drive 'mkdir -p /drive/lkhc/db && for i in $(seq 1 60); do d=$(date -d "@$(( $(date +%s) - i*86400 ))" +%Y%m%d); touch /drive/lkhc/db/lkhc-$d-033000.db; done; touch /drive/lkhc/db/README-keep-me.txt; chown -R '"$(id -u):$(id -g)"' /drive/lkhc'
    "$APP/backup.sh" run
    check "status.json result ok" grep -q '"result": "ok"' "$APP/backup/status.json"
    check "backup.sh check passes" "$APP/backup.sh" check
    check "status copy in the volume" app_dc exec -T app grep -q '"result": "ok"' /data/backups/offsite-status.json
    local n
    n=$(drive 'ls /drive/lkhc/db | grep -c "^lkhc-"')
    check "retention pruned 60+1 snapshots to $n" test "$n" -ge 20 -a "$n" -le 24
    check "unknown files in db/ are never deleted" drive 'test -f /drive/lkhc/db/README-keep-me.txt'
    check "photos uploaded" test "$(drive 'find /drive/lkhc/photos -type f | wc -l')" = "$(app_dc exec -T app sh -c 'find /data/photos -type f ! -name "*.part" | wc -l')"
    "$APP/backup.sh" run >/dev/null
    check "second run is incremental (no photo re-upload) and still ok" grep -q '"result": "ok"' "$APP/backup/status.json"

    echo "-- crypt remote"
    sed -i 's|^BACKUP_REMOTE=.*|BACKUP_REMOTE=gdrive-crypt:|' "$APP/backup/backup.env"
    "$APP/backup.sh" run
    check "crypt: names on the 'Drive' are encrypted" bash -c '! docker run --rm -v lkhctest-drive:/drive busybox find /drive/crypt | grep -qE "photos|lkhc-"'

    echo "-- failure is visible"
    sed -i 's|^BACKUP_REMOTE=.*|BACKUP_REMOTE=nosuchremote:x|' "$APP/backup/backup.env"
    local rc=0
    "$APP/backup.sh" run || rc=$?
    check "failing run exits non-zero ($rc)" test "$rc" -ne 0
    check "backup.sh check reports the failed run" bash -c '! "$1" check' _ "$APP/backup.sh"
    check "status.json says failed + step" grep -q '"failed_step": "upload-db"' "$APP/backup/status.json"
    check "last_success kept from the previous run" bash -c '! grep -q "\"last_success\": \"\"" "$1"' _ "$APP/backup/status.json"
    sed -i 's|^BACKUP_REMOTE=.*|BACKUP_REMOTE=gdrive:/drive/lkhc|' "$APP/backup/backup.env"
}

restore() {
    phase "restore.sh: wipe the volume, fresh deploy, restore, compare"
    local readings photos sums
    readings=$(sql 'select count(*) from readings')
    photos=$(sql 'select count(*) from photos')
    sums=$(photo_sums)
    "$APP/backup.sh" run >/dev/null # latest state into the plain remote
    app_dc down
    docker volume rm lkhctest-data >/dev/null
    check "volume wiped" bash -c '! docker volume inspect lkhctest-data'
    "$APP/deploy.sh" --restart # = fresh VPS: empty volume, migrations create an empty DB
    check "fresh app has no readings" test "$(sql 'select count(*) from readings')" = 0
    "$APP/restore.sh" list | tail -n 3
    "$APP/restore.sh" --yes
    check "healthy through Caddy after restore" test "$(c -o /dev/null -w '%{http_code}' "$URL/healthz")" = 200
    check "readings intact ($readings)" test "$(sql 'select count(*) from readings')" = "$readings"
    check "photo rows intact ($photos)" test "$(sql 'select count(*) from photos')" = "$photos"
    check "photo files byte-identical" test "$(photo_sums)" = "$sums"
    check "restored files owned by the app user" test "$(app_dc exec -T app sh -c 'find /data -not -user 10001 | wc -l')" = 0
    COOKIE=$(session_cookie)
    local id
    id=$(sql 'select max(id) from photos')
    check "a restored photo is served by the app" test "$(c -o /dev/null -w '%{http_code}' -b "lkhc_session=$COOKIE" "$URL/fotky/$id/soubor")" = 200

    echo "-- restore from the encrypted remote into a wiped volume"
    app_dc down
    docker volume rm lkhctest-data >/dev/null
    "$APP/deploy.sh" --restart >/dev/null
    BACKUP_REMOTE=gdrive-crypt: "$APP/restore.sh" --yes >/dev/null
    check "crypt restore: readings + photos intact" bash -c '[ "$1" = "$2" ] && [ "$3" = "$4" ]' _ \
        "$(sql 'select count(*) from readings')" "$readings" "$(photo_sums)" "$sums"
}

cleanup() {
    phase "cleanup"
    [[ -d $T/srv/whoami ]] && (cd "$T/srv/whoami" && docker compose down -v --remove-orphans) || true
    [[ -f $APP/compose.yml ]] && (cd "$APP" && docker compose down --remove-orphans) || true
    [[ -f $PROXY/compose.yml ]] && (cd "$PROXY" && docker compose down -v --remove-orphans) || true
    docker volume rm lkhctest-data lkhctest-drive 2>/dev/null || true
    docker network rm proxy 2>/dev/null || true
    docker image ls lkhc-odecty --format '{{.Tag}}' | grep -vx latest | sed 's/^/lkhc-odecty:/' | xargs -r docker image rm >/dev/null || true
    rm -rf "$T"
    ok "removed test containers, volumes, network, lkhc-odecty:* tags except latest, $T"
}

summary() {
    printf '\n\e[1m%d passed, %d failed\e[0m\n' "$pass" "$fail"
    ((fail == 0))
}

COOKIE=""
if [[ ${1:-} == all ]]; then
    set -- setup proxy app headers upload second deploy backup restore cleanup
fi
[[ $# -gt 0 ]] || { sed -n '2,/^set -E/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 2; }
for p in "$@"; do
    case $p in
    headers | upload | second | restore) [[ -n $COOKIE ]] || COOKIE=$(session_cookie 2>/dev/null || true) ;;
    esac
    "$p"
done
summary
