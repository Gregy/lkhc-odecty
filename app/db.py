"""Database engine/session setup and shared column types."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, Float, create_engine, event
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

KWH_PLACES = 3
_KWH_QUANT = Decimal(1).scaleb(-KWH_PLACES)


class KWh(TypeDecorator[Decimal]):
    """Decimal kWh value stored as a SQLite REAL, exact to 3 decimal places.

    SQLite has no decimal type; values are rounded to 3 places on write and
    returned as `Decimal` quantized to 3 places, so round-trips are exact.
    """

    impl = Float
    cache_ok = True

    def process_bind_param(self, value: Decimal | int | float | str | None, dialect: Dialect) -> float | None:
        if value is None:
            return None
        return float(Decimal(str(value)).quantize(_KWH_QUANT))

    def process_result_value(self, value: float | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(repr(round(value, KWH_PLACES))).quantize(_KWH_QUANT)


def normalize_kwh(value: Decimal) -> Decimal:
    """Quantize to the stored precision (3 places)."""
    return Decimal(value).quantize(_KWH_QUANT)


class Base(DeclarativeBase):
    pass


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def sqlite_file(url: str) -> tuple[Path | None, bool]:
    """(database file of a SQLite URL or None, whether it is a ``file:`` URI filename).

    Parsed with SQLAlchemy, so a query string (``?timeout=5``) is not part of the path. With
    ``?uri=true`` the database is an SQLite URI such as ``file:data/app.db`` (``mode=ro`` etc.);
    its path is returned without the ``file:`` prefix.
    """
    u = make_url(url)
    db = u.database
    if not u.drivername.startswith("sqlite") or not db or db == ":memory:":
        return None, False
    if db.startswith("file:"):
        path = db.removeprefix("file:").split("?", 1)[0]
        return (Path(path) if path and path != ":memory:" else None), True
    return Path(db), False


def make_engine(url: str) -> Engine:
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        path, is_uri = sqlite_file(url)
        # Create the data directory for a new database file, but never for an SQLite URI
        # (``file:...?mode=ro&uri=true``): that opens an existing database, and the naive
        # parent of "file:data/app.db?..." used to be created as a stray directory "file:data".
        if path is not None and not is_uri:
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope for scripts: commit on success, rollback on error."""
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
