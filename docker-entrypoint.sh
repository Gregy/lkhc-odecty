#!/bin/sh
# Container entrypoint: bring the DB schema up to date, then run the given command (uvicorn by default).
# Also applies to one-off commands, e.g. `docker compose run --rm app python -m app.importer /import.xlsx`.
set -eu

if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
    python -m app.migrate
fi

exec "$@"
