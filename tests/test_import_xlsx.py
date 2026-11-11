"""Importer: layout, entities, special cases, idempotency, mapping file, CLI, schema/migration consistency.

Runs on the synthetic workbook + mapping of ``tests/synthetic.py`` (same layout as the original sheet).
"""

import io
import shutil
import sys
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path

import openpyxl
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Base, make_engine
from app.importer import ImportLayoutError, ImportRefusedError, import_workbook
from app.importer.mapping import ImportMapping, MappingError, load_mapping
from app.importer.xlsx import clean_comment, parse_cs_date, parse_readings_sheet
from app.models import Allocation, AuditLog, Meter, MeterPoint, Reading, Tenant
from tests import synthetic as syn
from tests.conftest import ROOT


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_parse_helpers() -> None:
    assert parse_cs_date("1.12.2025") == date(2025, 12, 1)
    assert parse_cs_date(" 4. 1. 2026 ") == date(2026, 1, 4)
    assert parse_cs_date("XXXX") is None
    raw = "[Threaded comment]\n blah blah\nComment:\n\tmimo provoz, meri spatne\n"
    assert clean_comment(raw) == "mimo provoz, meri spatne"


def test_parse_sheet(xlsx_path: Path) -> None:
    sheet = parse_readings_sheet(xlsx_path)
    assert len(sheet.columns) == 17
    assert len(sheet.dates) == 32 and sheet.dates[0] == date(2023, 12, 30)
    assert len(sheet.readings) == 436
    assert sheet.warnings == []
    by_col = {c.col: c for c in sheet.columns}
    assert by_col["AF"].serial == syn.F_SERIAL and by_col["AH"].serial == syn.CONTROL_SERIAL
    assert by_col["Z"].comment == "mimo provoz, meri spatne"  # threaded-comment boilerplate stripped


def test_parse_sheet_warnings(xlsx_path: Path, tmp_path: Path) -> None:
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Odečty"]
    ws["A37"], ws["B37"] = "32.13.2026", 1  # looks like a date but is not one, with a value
    ws["A38"], ws["D38"] = "3.6.2026", 1  # duplicate date
    ws["P36"] = "n/a"  # not a number
    bad = tmp_path / "warn.xlsx"
    wb.save(bad)
    w = parse_readings_sheet(bad).warnings
    assert any("řádek 37" in x and "neplatné datum" in x for x in w)
    assert any("řádek 38" in x and "duplicitní datum" in x for x in w)
    assert any("P36" in x and "not a number" in x for x in w)


def test_import_entities(imported_db: Session) -> None:
    db = imported_db
    assert count(db, Tenant) == 7
    assert count(db, MeterPoint) == 15
    assert count(db, Meter) == 17
    assert count(db, Allocation) == 12
    assert count(db, Reading) == 436
    rem = db.scalar(select(Tenant).where(Tenant.name == syn.REMAINDER))
    assert rem.is_remainder and rem.color and rem.color_dark
    assert db.scalar(select(func.count()).select_from(Tenant).where(Tenant.is_remainder)) == 1
    assert [t.name for t in db.scalars(select(Tenant).order_by(Tenant.sort_order))] == list(syn.TENANTS)
    assert {r.source for r in db.scalars(select(Reading))} == {"import"}
    # lower-case description capitalised, point names from the sheet
    names = {p.name for p in db.scalars(select(MeterPoint))}
    assert {syn.ROLLOVER_POINT, syn.MAIN_POINT, syn.KIOSK_POINT, syn.CONTROL_POINT} <= names


def test_import_special_meters(imported_db: Session) -> None:
    db = imported_db
    by_serial = {m.serial: m for m in db.scalars(select(Meter))}
    main = by_serial[syn.MAIN_SERIAL]
    assert main.meter_point.role == "main" and main.registers == ["T1", "T2"]
    # office replacement: same point, old removed on its last reading, new from 1.12.2025
    old, new = by_serial[syn.OLD_OFFICE_SERIAL], by_serial[syn.NEW_OFFICE_SERIAL]
    assert old.meter_point_id == new.meter_point_id
    assert old.removed_on == date(2025, 12, 1) and new.installed_on == date(2025, 12, 1)
    assert old.registers == ["T1", "T2"] and new.registers == ["T1"]
    # duplicate kiosk serial -> second meter -B on the kiosk point, allocated from its first reading
    z, ab = by_serial[syn.KIOSK_SERIAL], by_serial[syn.KIOSK_NEW_SERIAL]
    assert z.meter_point_id == ab.meter_point_id and z.meter_point.name == syn.KIOSK_POINT
    assert "OVĚŘIT" in ab.note and ab.installed_on == date(2025, 4, 30) and z.removed_on == date(2025, 4, 30)
    alloc = db.scalar(select(Allocation).where(Allocation.meter_point_id == ab.meter_point_id))
    assert alloc.tenant.name == syn.D and alloc.valid_from == date(2025, 4, 30)
    # unallocated / control points
    for serial in (syn.CORRIDOR2_SERIAL, syn.CONTROL_SERIAL):
        pid = by_serial[serial].meter_point_id
        assert db.scalar(select(func.count()).select_from(Allocation).where(Allocation.meter_point_id == pid)) == 0
    control = by_serial[syn.CONTROL_SERIAL].meter_point
    assert control.role == "control"
    assert by_serial[syn.F_SERIAL].meter_point.parent_point_id == control.id
    # comment of the sheet kept on the point
    assert "venku za dílnou" in by_serial[syn.WORKSHOP_SERIAL].meter_point.description
    # rollover meter value kept as read
    r = db.scalar(
        select(Reading).where(
            Reading.meter_id == by_serial[syn.ROLLOVER_SERIAL].id, Reading.taken_at == datetime(2024, 3, 3)
        )
    )
    assert r.value == D(int(syn.value("R", "3.3.2024")))


def test_tenant_allocation_matches_sheet_formulas(imported_db: Session) -> None:
    """A = D+F+H+R+T, B = J+L+N+X, C = AD, D = AB (kiosk point), E = P, F = AF."""
    expected = {
        syn.A: {syn.PARA_SERIAL, syn.ACCU_SERIAL, syn.HALL_SERIAL, syn.ROLLOVER_SERIAL, syn.CORRIDOR_SERIAL},
        syn.B: {syn.OLD_OFFICE_SERIAL, syn.NEW_OFFICE_SERIAL, syn.HANGAR_SERIAL, syn.WORKSHOP_SERIAL},
        syn.C: {syn.C_SERIAL},
        syn.D: {syn.KIOSK_SERIAL, syn.KIOSK_NEW_SERIAL},  # same point; allocation valid from the new meter
        syn.E: {syn.E_SERIAL},
        syn.F: {syn.F_SERIAL},
    }
    for tenant, serials in expected.items():
        points = [
            a.meter_point for a in imported_db.scalars(select(Allocation).join(Tenant).where(Tenant.name == tenant))
        ]
        assert {m.serial for p in points for m in p.meters} == serials, tenant


def test_import_is_idempotent(imported_db: Session, xlsx_path: Path, import_mapping: ImportMapping) -> None:
    before = {m: count(imported_db, m) for m in (Tenant, MeterPoint, Meter, Allocation, Reading)}
    report = import_workbook(imported_db, xlsx_path, mapping=import_mapping, corrections=False)
    imported_db.commit()
    assert report.created == {} and report.updated == {}
    assert report.unchanged_readings == 436
    assert {m: count(imported_db, m) for m in before} == before


def test_reimport_updates_changed_import_values_but_keeps_app_edits(
    imported_db: Session, xlsx_path: Path, import_mapping: ImportMapping, tmp_path: Path
) -> None:
    main_last = syn.value("B", "3.6.2026")
    para_last = syn.value("D", "3.6.2026")
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Odečty"]
    assert ws["B36"].value == main_last
    ws["B36"] = main_last + 1  # main T1, 3.6.2026
    ws["D36"] = para_last + 1  # tower meter, 3.6.2026 - edited in the app below
    changed = tmp_path / "changed.xlsx"
    wb.save(changed)
    main = imported_db.scalar(select(Meter).where(Meter.serial == syn.MAIN_SERIAL))
    para = imported_db.scalar(select(Meter).where(Meter.serial == syn.PARA_SERIAL))
    edited = imported_db.scalar(
        select(Reading).where(Reading.meter_id == para.id, Reading.taken_at == datetime(2026, 6, 3))
    )
    edited.value, edited.source = D(int(para_last) - 1), "manual"
    imported_db.commit()

    with pytest.raises(ImportRefusedError, match="--force"):  # app data present: refuse unless forced
        import_workbook(imported_db, changed, mapping=import_mapping, corrections=False)
    report = import_workbook(imported_db, changed, mapping=import_mapping, force=True, corrections=False)
    imported_db.commit()
    assert report.updated == {"odečty": 1} and report.skipped_readings == 1
    r = imported_db.scalar(select(Reading).where(Reading.meter_id == main.id, Reading.taken_at == datetime(2026, 6, 3)))
    assert r.value == main_last + 1
    imported_db.refresh(edited)
    assert edited.value == int(para_last) - 1
    assert imported_db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_type == "readings")) == 1


def test_import_rejects_shifted_layout(
    db: Session, xlsx_path: Path, import_mapping: ImportMapping, tmp_path: Path
) -> None:
    wb = openpyxl.load_workbook(xlsx_path)
    wb["Odečty"]["D2"] = 1234567
    bad = tmp_path / "bad.xlsx"
    wb.save(bad)
    with pytest.raises(ImportLayoutError):
        import_workbook(db, bad, mapping=import_mapping)


# --- mapping file ---------------------------------------------------------------------------------


def test_example_mapping_loads() -> None:
    m = load_mapping(ROOT / "app" / "importer" / "import-mapping.example.toml")
    assert m.remainder.name and m.columns and m.date_corrections
    assert all(t.color.startswith("#") and t.color_dark.startswith("#") for t in m.tenants)


def test_mapping_is_validated(mapping_path: Path, tmp_path: Path) -> None:
    text = mapping_path.read_text(encoding="utf-8")
    bad = tmp_path / "bad.toml"
    bad.write_text(text.replace('col = "D"', 'col = "D"\ncolour = "red"', 1), encoding="utf-8")
    with pytest.raises(MappingError, match="neznámé klíče"):
        load_mapping(bad)
    bad.write_text(text.replace(f'tenant = "{syn.E}"', 'tenant = "Nikdo"', 1), encoding="utf-8")
    with pytest.raises(MappingError, match="Nikdo"):
        load_mapping(bad)
    bad.write_text(text.replace("remainder = true", ""), encoding="utf-8")
    with pytest.raises(MappingError, match="remainder"):
        load_mapping(bad)
    with pytest.raises(MappingError, match="neexistuje"):
        load_mapping(tmp_path / "missing.toml")


def test_mapping_carries_the_corrections(import_mapping: ImportMapping) -> None:
    (c,) = import_mapping.date_corrections
    assert (c.sheet_date, c.corrected) == (syn.TYPO, syn.FIXED) and c.reason


# --- CLI ------------------------------------------------------------------------------------------


def test_cli(db_url: str, xlsx_path: Path, mapping_path: Path) -> None:
    from app.importer.__main__ import main

    assert main([str(xlsx_path), "--mapping", str(mapping_path), "--database-url", db_url]) == 0
    assert main([str(xlsx_path), "--mapping", str(mapping_path), "--database-url", db_url]) == 0


def test_cli_private_dir_and_stdin(
    db_url: str, xlsx_path: Path, mapping_path: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """Container use: XLSX on stdin, mapping from a private dir mounted anywhere (option or env var)."""
    from app.importer.__main__ import main

    priv = tmp_path / "mounted-private"
    priv.mkdir()
    shutil.copyfile(mapping_path, priv / "import-mapping.toml")

    class Stdin:
        buffer = io.BytesIO(xlsx_path.read_bytes())

    monkeypatch.setattr(sys, "stdin", Stdin)
    assert main(["-", "--private-dir", str(priv), "--database-url", db_url]) == 0
    out = capsys.readouterr().out
    assert "nově: odečty: 436" in out and "oprava zdroje" in out
    monkeypatch.setenv("LKHC_PRIVATE_DIR", str(priv))
    assert main([str(xlsx_path), "--database-url", db_url]) == 0
    assert "odečty beze změny: 436" in capsys.readouterr().out


def test_cli_missing_mapping_is_a_clear_error(
    db_url: str, xlsx_path: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from app.importer.__main__ import main

    monkeypatch.setenv("LKHC_PRIVATE_DIR", str(tmp_path / "nowhere"))
    assert main([str(xlsx_path), "--database-url", db_url]) == 2
    err = capsys.readouterr().err
    assert "import-mapping.toml" in err and "--mapping" in err and "--private-dir" in err
    assert main([str(xlsx_path), "--mapping", str(tmp_path / "x.toml"), "--database-url", db_url]) == 2


# --- schema ---------------------------------------------------------------------------------------


def test_migration_matches_models(db_url: str) -> None:
    """Alembic head == models (no pending autogenerate diff; KWh is stored as Float)."""
    engine = make_engine(db_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == []


def test_sqlite_pragmas(db: Session) -> None:
    assert db.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    assert db.connection().exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
