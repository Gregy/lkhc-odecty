#!/usr/bin/env bash
# Validate the Caddy configuration, then reload it (no downtime, no dropped connections).
# Run after adding/changing a file in /srv/proxy/caddy/sites/.
#   /srv/proxy/reload.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

echo "==> caddy validate"
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
echo "==> caddy reload"
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
echo "==> OK. Sites:"
ls -1 caddy/sites/*.caddy
