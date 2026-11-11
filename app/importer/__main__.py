"""CLI: import the `Odečty` sheet of the original spreadsheet.

The column mapping (tenants, meter serials, confirmed source corrections) is private installation
data, read from ``--mapping FILE``, default ``<private dir>/import-mapping.toml``; the private dir is
``--private-dir`` > ``$LKHC_PRIVATE_DIR`` > ``./private`` (see ``app.private``)::

    uv run python -m app.importer private/<sheet>.xlsx
    uv run python -m app.importer --private-dir /elsewhere /elsewhere/<sheet>.xlsx

``-`` reads the workbook from stdin – handy for containers (no bind mount of the XLSX needed); the
mapping comes from a mounted private directory::

    docker compose run --rm -T -v "$PWD/private:/private:ro,z" app \\
        python -m app.importer --private-dir /private - < private/<sheet>.xlsx
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile

from sqlalchemy import inspect

from app.config import get_settings
from app.db import make_engine, make_sessionmaker, session_scope
from app.importer.load import ImportLayoutError, import_workbook
from app.importer.mapping import MappingError, load_mapping
from app.private import ENV_VAR, MAPPING_FILE, private_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.importer", description="Import listu 'Odečty' z XLSX.")
    parser.add_argument("path", help="cesta k XLSX souboru, nebo - (číst ze stdin)")
    parser.add_argument(
        "--private-dir",
        help=f"adresář se soukromými daty instalace (výchozí ${ENV_VAR}, jinak ./private)",
    )
    parser.add_argument("--mapping", help=f"soubor s mapováním sloupců (výchozí <soukromý adresář>/{MAPPING_FILE})")
    parser.add_argument("--database-url", help="přepíše DATABASE_URL / DATA_DIR")
    parser.add_argument(
        "--force", action="store_true", help="importovat i do databáze, ve které už jsou data zadaná v aplikaci"
    )
    parser.add_argument(
        "--bez-oprav",
        dest="corrections",
        action="store_false",
        help="neaplikovat potvrzené opravy zdrojových dat ([[date_corrections]] v mapování) – tabulka tak, jak je",
    )
    args = parser.parse_args(argv)

    try:
        mapping = load_mapping(args.mapping or private_dir(args.private_dir) / MAPPING_FILE)
    except MappingError as exc:
        print(f"Import selhal: {exc}", file=sys.stderr)
        return 2

    settings = get_settings()
    url = args.database_url or settings.sqlalchemy_url
    engine = make_engine(url)
    if not inspect(engine).has_table("readings"):
        print(f"Databáze {url} nemá schéma – nejdřív spusťte: python -m app.migrate", file=sys.stderr)
        return 2
    try:
        with session_scope(make_sessionmaker(engine)) as db:
            source = args.path
            if source == "-":
                source = io.BytesIO(sys.stdin.buffer.read())
                source.name = "stdin.xlsx"
            report = import_workbook(db, source, mapping=mapping, force=args.force, corrections=args.corrections)
    except (ImportLayoutError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Import selhal: {exc}", file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
