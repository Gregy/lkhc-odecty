"""Consistent backups of the SQLite database (and optionally the photos).

A plain file copy of a live WAL database can be inconsistent (part of the data still sits in
``app.db-wal``). This uses SQLite's online backup API, which is safe while the app is running.

CLI (inside the container: ``docker compose exec app python -m app.backup ...``)::

    python -m app.backup                      # DATA_DIR/backups/lkhc-YYYYmmdd-HHMMSS.db
    python -m app.backup --with-photos        # ... .tar.gz with the DB snapshot + photos/
    python -m app.backup --dest /backups --keep 14

Restore: stop the app, replace DATA_DIR/app.db with the snapshot (delete app.db-wal / app.db-shm),
unpack photos/ into DATA_DIR, start the app (migrations run on start).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.db import sqlite_file


def sqlite_path(url: str) -> Path | None:
    """File path of a ``sqlite:///...`` URL (None for other databases / in-memory)."""
    if not url.startswith("sqlite"):
        return None
    return sqlite_file(url)[0]


def snapshot_db(src: Path, dest: Path) -> Path:
    """Copy the database ``src`` to ``dest`` with the online backup API (consistent, WAL-safe)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
            ok = target.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError(f"integrity check of the snapshot failed: {ok}")
            target.execute("PRAGMA journal_mode=DELETE")  # a single self-contained file
        finally:
            target.close()
    finally:
        source.close()
    tmp.replace(dest)
    return dest


def prune(directory: Path, pattern: str, keep: int) -> list[Path]:
    files = sorted(directory.glob(pattern))
    old = files[:-keep] if keep > 0 else []
    for f in old:
        f.unlink(missing_ok=True)
    return old


def backup(dest_dir: Path, *, with_photos: bool = False, keep: int = 0) -> Path:
    settings = get_settings()
    db = sqlite_path(settings.sqlalchemy_url)
    if db is None or not db.exists():
        raise SystemExit(f"Databáze nenalezena: {settings.sqlalchemy_url}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not with_photos:
        out = snapshot_db(db, dest_dir / f"lkhc-{stamp}.db")
        if keep:
            prune(dest_dir, "lkhc-*.db", keep)
        return out
    out = dest_dir / f"lkhc-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(dir=dest_dir) as tmp:
        snap = snapshot_db(db, Path(tmp) / "app.db")
        part = out.with_name(out.name + ".part")
        with tarfile.open(part, "w:gz") as tar:
            tar.add(snap, arcname="app.db")
            if settings.photos_dir.exists():
                tar.add(settings.photos_dir, arcname="photos")
        part.replace(out)
    if keep:
        prune(dest_dir, "lkhc-*.tar.gz", keep)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description="Záloha databáze (a fotek).")
    parser.add_argument("--dest", type=Path, help="cílový adresář (default DATA_DIR/backups)")
    parser.add_argument("--with-photos", action="store_true", help="tar.gz se snímkem DB a adresářem photos/")
    parser.add_argument("--keep", type=int, default=0, help="ponechat jen N nejnovějších záloh v cíli (0 = vše)")
    args = parser.parse_args(argv)
    dest = args.dest or get_settings().data_dir / "backups"
    out = backup(dest, with_photos=args.with_photos, keep=args.keep)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
