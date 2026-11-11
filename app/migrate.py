"""Bring the database schema to the current version (run before the web server starts).

``python -m app.migrate`` (the container entrypoint runs it on every start):

- empty / new database: creates the schema (``alembic upgrade head``);
- database at an older revision: first writes a consistent snapshot to
  ``DATA_DIR/backups/pre-migrate-<rev>-<timestamp>.db``, then upgrades;
- already at head: does nothing.

Single container + SQLite: the app is the only writer and migrations run before uvicorn, so
there is no concurrent schema change. Running several app replicas is not supported.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from alembic import command
from app.backup import snapshot_db, sqlite_path
from app.config import get_settings
from app.db import make_engine

log = logging.getLogger("app.migrate")
ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def revisions(url: str) -> tuple[str | None, str]:
    """(current revision of the database or None, head revision of the code)."""
    head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
            has_tables = bool(inspect(conn).get_table_names())
    finally:
        engine.dispose()
    if current is None and has_tables:
        current = "unversioned"
    return current, head or ""


def migrate(url: str | None = None) -> str:
    url = url or get_settings().sqlalchemy_url
    current, head = revisions(url)
    if current == head:
        return f"Databáze je aktuální (revize {head})."
    db_file = sqlite_path(url)
    msg = ""
    if current is not None and db_file is not None and db_file.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        snap = snapshot_db(db_file, db_file.parent / "backups" / f"pre-migrate-{current}-{stamp}.db")
        msg = f"Záloha před migrací: {snap}\n"
    command.upgrade(alembic_config(url), "head")
    return msg + f"Migrace {current or '(prázdná databáze)'} → {head} hotová."


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        print(migrate(), flush=True)
    except Exception as exc:  # pragma: no cover - surfaced in the container log
        print(f"Migrace selhala: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
