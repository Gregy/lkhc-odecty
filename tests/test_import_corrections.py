"""Confirmed source-data corrections (``[[date_corrections]]`` of the import mapping) and the date-move tool."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import services as s
from app.importer import ImportRefusedError, import_workbook
from app.importer.mapping import ImportMapping
from app.models import AuditLog, PointSkip, Reading
from app.tools.move_reading_date import MoveRefusedError, main, move_reading_date
from tests import synthetic as syn

TYPO, FIXED = syn.TYPO, syn.FIXED


def _count(db: Session, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _on(day: date):
    return Reading.taken_at == datetime.combine(day, datetime.min.time())


def test_corrected_import(
    imported_corrected_db: Session, imported_db: Session, xlsx_path: Path, import_mapping: ImportMapping
) -> None:
    db = imported_corrected_db
    assert _count(db, Reading, _on(TYPO)) == 0
    assert _count(db, Reading, _on(FIXED)) == _count(imported_db, Reading, _on(TYPO)) > 0
    # same number of readings and reading days as the import of the sheet as exported
    assert _count(db, Reading) == _count(imported_db, Reading)
    assert len(s.reading_dates(db)) == len(s.reading_dates(imported_db))
    assert TYPO not in s.reading_dates(db) and FIXED in s.reading_dates(db)
    # the 1.6.-3.7. segment is a normal month: no spike anomaly / alert from it
    assert not [a for a in s.meter_anomalies(db) if a.anomaly.curr.taken_at.date() == FIXED]
    assert not [a for a in s.alerts(db, now=datetime(2026, 7, 10)) if a.kind == "anomaly" and "03.07.2026" in a.message]
    # the correction is reported and the import stays idempotent
    log = db.scalar(select(AuditLog).where(AuditLog.action == "import"))
    assert "3.6.2026 → 3.7.2026" in log.changes["corrections"][0]
    again = import_workbook(db, xlsx_path, mapping=import_mapping)
    assert again.created == {} and again.updated == {} and again.corrections


def test_corrected_import_refuses_db_with_uncorrected_readings(
    imported_db: Session, xlsx_path: Path, import_mapping: ImportMapping
) -> None:
    with pytest.raises(ImportRefusedError, match="move_reading_date"):
        import_workbook(imported_db, xlsx_path, mapping=import_mapping)


def test_move_tool_then_corrected_reimport_has_no_changes(
    imported_db: Session, xlsx_path: Path, import_mapping: ImportMapping
) -> None:
    n = _count(imported_db, Reading, _on(TYPO))
    point_id = imported_db.scalar(select(Reading).where(_on(TYPO))).meter.meter_point_id
    imported_db.add(PointSkip(meter_point_id=point_id, day=TYPO, note="zamčeno"))
    imported_db.commit()
    res = move_reading_date(imported_db, TYPO, FIXED, note="překlep potvrzen")
    imported_db.commit()
    assert res.readings == n and res.skips == 1
    assert _count(imported_db, Reading, _on(TYPO)) == 0 and _count(imported_db, Reading, _on(FIXED)) == n
    assert _count(imported_db, PointSkip, PointSkip.day == FIXED) == 1
    assert _count(imported_db, AuditLog, AuditLog.action == "move_date") == 1
    assert _count(imported_db, AuditLog, AuditLog.entity_type == "readings", AuditLog.action == "update") == n
    report = import_workbook(imported_db, xlsx_path, mapping=import_mapping, force=True)
    assert report.created == {} and report.updated == {} and report.corrections
    # moving onto a day that already has readings is refused, nothing changes
    with pytest.raises(MoveRefusedError, match="koliduje"):
        move_reading_date(imported_db, date(2026, 6, 1), FIXED)
    imported_db.rollback()
    assert _count(imported_db, Reading, _on(date(2026, 6, 1))) == n


def test_move_cli_backs_up_first(imported_db_url: str) -> None:
    path = Path(imported_db_url.removeprefix("sqlite:///"))
    assert main(["--from", "2026-06-03", "--to", "2026-07-03", "--database-url", imported_db_url, "--dry-run"]) == 0
    assert not (path.parent / "backups").exists()
    assert main(["--from", "2026-06-03", "--to", "2026-07-03", "--database-url", imported_db_url]) == 0
    assert len(list((path.parent / "backups").glob("pre-move-2026-06-03-2026-07-03-*.db"))) == 1
    assert main(["--from", "2026-06-03", "--to", "2026-07-03", "--database-url", imported_db_url]) == 1  # nothing left
