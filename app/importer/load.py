"""Idempotent import of the `Odečty` sheet into the database."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.importer.corrections import DateCorrection
from app.importer.mapping import (
    FIRST_READING,
    LAST_READING,
    REPLACEMENT_INSTALL,
    ColumnSpec,
    ImportMapping,
)
from app.importer.xlsx import ParsedSheet, SheetColumn, parse_readings_sheet
from app.models import Allocation, Meter, MeterPoint, Reading, Tenant


class ImportLayoutError(ValueError):
    """The sheet does not match the expected column layout."""


class ImportRefusedError(ValueError):
    """The database already contains data entered in the app (re-import needs ``force``)."""


def app_activity(db: Session) -> dict[str, int]:
    """Traces of work done in the app (not by the importer): user actions in the audit log,
    readings from other sources. A re-import matches objects by name/serial only, so after
    such changes it could re-create deleted readings or allocations or duplicate a renamed meter."""
    from sqlalchemy import func

    from app.models import AuditLog

    return {
        k: v
        for k, v in {
            "akce uživatelů v auditu": db.scalar(select(func.count(AuditLog.id)).where(AuditLog.user_id.is_not(None)))
            or 0,
            "odečty mimo import": db.scalar(select(func.count(Reading.id)).where(Reading.source != "import")) or 0,
        }.items()
        if v
    }


@dataclass
class ImportReport:
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    corrections: list[str] = field(default_factory=list)  # confirmed source corrections applied
    unchanged_readings: int = 0
    skipped_readings: int = 0  # existing non-import readings left untouched
    warnings: list[str] = field(default_factory=list)

    def inc(self, kind: str, what: str, n: int = 1) -> None:
        d = self.created if kind == "created" else self.updated
        d[what] = d.get(what, 0) + n

    def summary(self) -> str:
        lines = ["Import dokončen."]
        for c in self.corrections:
            lines.append(f"  oprava zdroje: {c}")
        for k, v in sorted(self.created.items()):
            lines.append(f"  nově: {k}: {v}")
        for k, v in sorted(self.updated.items()):
            lines.append(f"  aktualizováno: {k}: {v}")
        lines.append(f"  odečty beze změny: {self.unchanged_readings}")
        if self.skipped_readings:
            lines.append(f"  odečty ponechány (upravené v aplikaci): {self.skipped_readings}")
        for w in self.warnings:
            lines.append(f"  VAROVÁNÍ: {w}")
        return "\n".join(lines)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _point_name(spec: ColumnSpec, col: SheetColumn) -> str:
    return spec.point or _cap(col.description)


def _check_layout(sheet: ParsedSheet, specs: tuple[ColumnSpec, ...]) -> dict[str, SheetColumn]:
    by_col = {c.col: c for c in sheet.columns}
    for spec in specs:
        col = by_col.get(spec.col)
        if col is None:
            raise ImportLayoutError(f"sloupec {spec.col} (výr. číslo {spec.expected_serial}) v listu chybí")
        if col.serial != spec.expected_serial:
            raise ImportLayoutError(
                f"sloupec {spec.col}: výr. číslo {col.serial!r}, očekáváno {spec.expected_serial!r}"
            )
    extra = set(by_col) - {s.col for s in specs}
    if extra:
        raise ImportLayoutError(f"neočekávané sloupce s elektroměry: {sorted(extra)}")
    return by_col


def _fmt(d: date) -> str:
    return f"{d.day}.{d.month}.{d.year}"


def _apply_date_corrections(
    db: Session, sheet: ParsedSheet, table: tuple[DateCorrection, ...], report: ImportReport
) -> None:
    """Rewrite confirmed wrong row dates of the sheet (``[[date_corrections]]`` of the mapping)."""
    for c in table:
        n = sum(1 for r in sheet.readings if r.day == c.sheet_date)
        if not n:
            continue
        if c.corrected in sheet.dates:
            raise ImportLayoutError(f"oprava {_fmt(c.sheet_date)} → {_fmt(c.corrected)}: cílové datum už v tabulce je")
        stale = db.scalar(
            select(Reading.id).where(
                Reading.source == "import", Reading.taken_at == datetime.combine(c.sheet_date, time.min)
            )
        )
        if stale is not None:
            raise ImportRefusedError(
                f"databáze obsahuje odečty importované k {_fmt(c.sheet_date)}, které se teď importují k "
                f"{_fmt(c.corrected)}. Nejdřív je přesuňte: python -m app.tools.move_reading_date "
                f"--from {c.sheet_date.isoformat()} --to {c.corrected.isoformat()}"
            )
        for r in sheet.readings:
            if r.day == c.sheet_date:
                r.day = c.corrected
        sheet.dates = [c.corrected if d == c.sheet_date else d for d in sheet.dates]
        report.corrections.append(f"{_fmt(c.sheet_date)} → {_fmt(c.corrected)} ({n} hodnot): {c.reason}")


def import_workbook(
    db: Session,
    path: str | Path | BinaryIO,
    user_email: str = "import",
    *,
    mapping: ImportMapping,
    force: bool = False,
    corrections: bool = True,
) -> ImportReport:
    """Import meter points, meters, tenants, allocations and readings.

    Idempotent: objects are matched by natural keys (tenant/point name, meter serial,
    reading (meter, register, taken_at)) and only missing ones are created.
    Existing metadata is never overwritten; an imported reading whose value changed in
    the file is updated (and audited) unless it was modified in the app (source != import).

    ``mapping``: the column → point/tenant/role mapping of this installation (``app.importer.mapping``,
    loaded from the private ``import-mapping.toml``).
    ``corrections`` (default on): apply the confirmed source corrections of the mapping
    (``[[date_corrections]]``, reported in ``ImportReport.corrections``); off = the sheet as exported.
    """
    activity = app_activity(db)
    if activity and not force:
        what = ", ".join(f"{k}: {v}" for k, v in activity.items())
        raise ImportRefusedError(
            f"databáze už obsahuje data zadaná v aplikaci ({what}). Opakovaný import by mohl obnovit smazané "
            "odečty či alokace nebo zdvojit přejmenovaný elektroměr. Spusťte ho jen vědomě s --force "
            "(po záloze databáze)."
        )
    sheet = parse_readings_sheet(path)
    specs = mapping.columns
    by_col = _check_layout(sheet, specs)
    report = ImportReport(warnings=list(sheet.warnings))
    if corrections:
        _apply_date_corrections(db, sheet, mapping.date_corrections, report)

    # per-column reading dates (for derived install/removal dates)
    col_days: dict[str, list[date]] = {}
    for r in sheet.readings:
        col_days.setdefault(r.col, []).append(r.day)
    registers_by_col: dict[str, list[str]] = {}
    for r in sheet.readings:
        regs = registers_by_col.setdefault(r.col, [])
        if r.register not in regs:
            regs.append(r.register)

    # counter width: default 5 digits, wider when the data already exceeds it (e.g. a 6-digit T2 register)
    digits_by_col: dict[str, int] = {}
    for r in sheet.readings:
        need = len(str(int(abs(r.value)))) if r.value else 1
        digits_by_col[r.col] = max(digits_by_col.get(r.col, 5), need)

    def first_day(col: str) -> date | None:
        return min(col_days[col]) if col_days.get(col) else None

    def last_day(col: str) -> date | None:
        return max(col_days[col]) if col_days.get(col) else None

    # --- tenants
    tenants: dict[str, Tenant] = {t.name: t for t in db.scalars(select(Tenant))}
    for order, ts in enumerate(mapping.tenants):
        if ts.name not in tenants:
            t = Tenant(
                name=ts.name,
                is_remainder=ts.is_remainder,
                color=ts.color,
                color_dark=ts.color_dark,
                sort_order=order * 10,
            )
            db.add(t)
            tenants[ts.name] = t
            report.inc("created", "nájemci")
    db.flush()

    # --- meter points
    points: dict[str, MeterPoint] = {p.name: p for p in db.scalars(select(MeterPoint))}
    order = 0
    for spec in specs:
        col = by_col[spec.col]
        name = _point_name(spec, col)
        if name in points:
            continue
        order += 10
        # a merged point (several meters) keeps meter-specific comments on the meters only
        desc = "" if spec.point else col.comment
        points[name] = MeterPoint(
            name=name,
            description=desc,
            role=spec.role,
            sort_order=order,
            note=f"Popis v tabulce: {col.description}" if col.description != name else "",
        )
        db.add(points[name])
        report.inc("created", "měřicí místa")
    db.flush()
    for spec in specs:
        if spec.parent:
            child = points[_point_name(spec, by_col[spec.col])]
            parent = points.get(spec.parent)
            if parent is not None and child.parent_point_id is None:
                child.parent_point_id = parent.id
                report.inc("updated", "vazby kontrolních míst")

    # --- meters
    meters_by_serial: dict[str, Meter] = {m.serial: m for m in db.scalars(select(Meter))}
    meter_by_col: dict[str, Meter] = {}
    installed: dict[str, date | None] = {}
    for spec in specs:
        installed[spec.col] = first_day(spec.col) if spec.installed_on == FIRST_READING else None
    for spec in specs:
        col = by_col[spec.col]
        serial = spec.serial_override or col.serial
        removed: date | None = None
        if spec.removed_on == LAST_READING:
            removed = last_day(spec.col)
        elif spec.removed_on == REPLACEMENT_INSTALL and spec.replaced_by:
            removed = installed.get(spec.replaced_by) or first_day(spec.replaced_by)
        m = meters_by_serial.get(serial)
        if m is None:
            note_parts = [p for p in (spec.note, col.comment and f"Komentář v tabulce: {col.comment}") if p]
            m = Meter(
                meter_point_id=points[_point_name(spec, col)].id,
                serial=serial,
                installed_on=installed[spec.col],
                removed_on=removed,
                digits=digits_by_col.get(spec.col, 5),
                decimals_shown=0,
                registers=registers_by_col.get(spec.col) or ["T1"],
                source_type="manual",
                source_config=None,
                note=" ".join(note_parts),
            )
            db.add(m)
            meters_by_serial[serial] = m
            report.inc("created", "elektroměry")
        else:
            # registers may grow when new data appears in the T2 column
            regs = list(m.registers or [])
            for reg in registers_by_col.get(spec.col, []):
                if reg not in regs:
                    regs.append(reg)
            if regs != list(m.registers or []):
                m.registers = sorted(regs)
                report.inc("updated", "elektroměry (registry)")
            if digits_by_col.get(spec.col, 0) > m.digits:
                m.digits = digits_by_col[spec.col]
                report.inc("updated", "elektroměry (počet míst)")
        meter_by_col[spec.col] = m
    db.flush()

    # --- allocations
    existing_alloc = {(a.tenant_id, a.meter_point_id) for a in db.scalars(select(Allocation))}
    for spec in specs:
        if not spec.tenant:
            continue
        tenant = tenants[spec.tenant]
        point = points[_point_name(spec, by_col[spec.col])]
        if (tenant.id, point.id) in existing_alloc:
            continue
        valid_from = installed.get(spec.allocation_from) if spec.allocation_from else None
        db.add(Allocation(tenant_id=tenant.id, meter_point_id=point.id, valid_from=valid_from, factor=Decimal(1)))
        existing_alloc.add((tenant.id, point.id))
        report.inc("created", "alokace")
    db.flush()

    # --- readings
    existing: dict[tuple[int, str, datetime], Reading] = {
        (r.meter_id, r.register, r.taken_at): r for r in db.scalars(select(Reading))
    }
    for sr in sheet.readings:
        meter = meter_by_col[sr.col]
        taken_at = datetime.combine(sr.day, time.min)
        key = (meter.id, sr.register, taken_at)
        cur = existing.get(key)
        if cur is None:
            r = Reading(
                meter_id=meter.id,
                register=sr.register,
                taken_at=taken_at,
                value=sr.value,
                source="import",
                status="confirmed",
                note="",
            )
            db.add(r)
            existing[key] = r
            report.inc("created", "odečty")
        elif cur.value == sr.value:
            report.unchanged_readings += 1
        elif cur.source == "import":
            audit.record_update(
                db, None, cur, {"value": (cur.value, sr.value)}, note=f"import {sr.cell}", user_email=user_email
            )
            cur.value = sr.value
            report.inc("updated", "odečty")
        else:
            report.skipped_readings += 1
            report.warnings.append(
                f"{sr.cell}: odečet byl upraven v aplikaci ({cur.value}), hodnota z tabulky {sr.value} ignorována"
            )
    db.flush()
    if report.created or report.updated:
        audit.record(
            db,
            None,
            "import",
            "workbook",
            None,
            {
                "file": Path(path).name if isinstance(path, str | Path) else getattr(path, "name", "stdin"),
                "created": report.created,
                "updated": report.updated,
                "corrections": report.corrections,
            },
            user_email=user_email,
        )
    return report
