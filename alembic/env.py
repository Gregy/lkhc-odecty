"""Alembic environment: DB URL comes from app settings unless set programmatically."""

from logging.config import fileConfig

from alembic import context
from app import models  # noqa: F401  (register tables)
from app.config import get_settings
from app.db import Base, make_engine

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().sqlalchemy_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_without_fk_actions(connection)
        return
    engine = make_engine(get_url())
    with engine.connect() as conn:
        _run_without_fk_actions(conn)
    engine.dispose()


def _run_without_fk_actions(conn) -> None:
    """SQLite batch migrations re-create tables (copy, drop old, rename). With foreign keys enforced,
    dropping the old table would fire ON DELETE actions on referencing rows (e.g. SET NULL on
    readings.photo_id). So enforcement is off while migrating and the result is checked afterwards."""
    sqlite = conn.dialect.name == "sqlite"
    if sqlite:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
    context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
    if sqlite:
        broken = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"foreign key check failed after migration: {broken[:10]}")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
