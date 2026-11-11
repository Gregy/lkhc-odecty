"""SQLite URL handling: data directory creation and the file path used by backups/migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from app.backup import sqlite_path
from app.db import make_engine, sqlite_file


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///data/app.db", (Path("data/app.db"), False)),
        ("sqlite:////abs/app.db?timeout=5", (Path("/abs/app.db"), False)),
        ("sqlite:///file:data/app.db?mode=ro&uri=true", (Path("data/app.db"), True)),
        ("sqlite:///:memory:", (None, False)),
        ("sqlite://", (None, False)),
        ("postgresql://u@h/db", (None, False)),
    ],
)
def test_sqlite_file(url: str, expected: tuple[Path | None, bool]) -> None:
    assert sqlite_file(url) == expected
    assert sqlite_path(url) == expected[0]


def test_new_database_creates_its_directory(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path}/new/sub/app.db")
    with engine.connect() as conn:
        conn.execute(text("select 1"))
    engine.dispose()
    assert (tmp_path / "new" / "sub" / "app.db").is_file()


def test_read_only_uri_creates_no_stray_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A read-only URI used to create a directory literally named "file:data" in the working directory.
    (tmp_path / "data").mkdir()
    con = sqlite3.connect(tmp_path / "data" / "app.db")
    con.execute("pragma journal_mode=wal")  # as the app's database (a read-only open cannot switch it)
    con.execute("create table t (x)")
    con.close()
    monkeypatch.chdir(tmp_path)
    engine = make_engine("sqlite:///file:data/app.db?mode=ro&uri=true")
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from t")).scalar() == 0
    engine.dispose()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data"]
