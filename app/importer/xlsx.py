"""Parsing of the `Odečty` sheet (no DB access)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

SHEET_READINGS = "Odečty"
ROW_SERIAL = 2
ROW_DESCRIPTION = 3
FIRST_DATA_ROW = 5

_DATE_RE = re.compile(r"^\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*$")


@dataclass
class SheetColumn:
    col: str  # T1 column letter
    t2_col: str
    serial: str
    description: str
    comment: str = ""


@dataclass
class SheetReading:
    col: str  # T1 column letter of the meter
    register: str  # T1 | T2
    day: date
    value: Decimal
    cell: str


@dataclass
class ParsedSheet:
    columns: list[SheetColumn]
    dates: list[date]  # distinct row dates in sheet order
    readings: list[SheetReading]
    warnings: list[str] = field(default_factory=list)


def parse_cs_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        m = _DATE_RE.match(value)
        if m:
            d, mth, y = (int(x) for x in m.groups())
            try:
                return date(y, mth, d)
            except ValueError:  # e.g. 32.13.2026: not a date -> the row is reported and skipped
                return None
    return None


def format_serial(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def clean_comment(text: str | None) -> str:
    """Strip Excel's threaded-comment boilerplate."""
    if not text:
        return ""
    if "Comment:" in text:
        text = text.split("Comment:", 1)[1]
    return " ".join(text.split())


def to_decimal(value: object) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"unexpected boolean {value!r}")
    if isinstance(value, (int, float)):
        return Decimal(repr(value)).normalize() if isinstance(value, float) else Decimal(value)
    try:
        return Decimal(str(value).strip().replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def parse_readings_sheet(path: str | Path | BinaryIO) -> ParsedSheet:
    wb = openpyxl.load_workbook(path, data_only=True)
    if SHEET_READINGS not in wb.sheetnames:
        raise ValueError(f"sheet {SHEET_READINGS!r} not found in {path}")
    ws = wb[SHEET_READINGS]

    columns: list[SheetColumn] = []
    idx = column_index_from_string("B")
    while idx <= ws.max_column:
        col = get_column_letter(idx)
        serial = ws.cell(ROW_SERIAL, idx).value
        if serial is None or str(serial).strip() == "":
            idx += 2
            continue
        desc_cell = ws.cell(ROW_DESCRIPTION, idx)
        columns.append(
            SheetColumn(
                col=col,
                t2_col=get_column_letter(idx + 1),
                serial=format_serial(serial),
                description=str(desc_cell.value or "").strip(),
                comment=clean_comment(desc_cell.comment.text if desc_cell.comment else None),
            )
        )
        idx += 2

    warnings: list[str] = []
    dates: list[date] = []
    readings: list[SheetReading] = []
    for row in range(FIRST_DATA_ROW, ws.max_row + 1):
        raw_date = ws.cell(row, 1).value
        values = []
        for c in columns:
            for reg, letter in (("T1", c.col), ("T2", c.t2_col)):
                cell = ws[f"{letter}{row}"]
                values.append((c.col, reg, cell))
        has_value = any(cell.value not in (None, "") for _, _, cell in values)
        day = parse_cs_date(raw_date)
        if day is None:
            if raw_date not in (None, "") or has_value:
                warnings.append(f"řádek {row}: neplatné datum {raw_date!r} – řádek přeskočen")
            continue
        if day in dates:
            warnings.append(f"řádek {row}: duplicitní datum {day} – hodnoty sloučeny")
        else:
            dates.append(day)
        for col, reg, cell in values:
            try:
                v = to_decimal(cell.value)
            except ValueError as exc:
                warnings.append(f"buňka {cell.coordinate}: {exc} – přeskočeno")
                continue
            if v is not None:
                readings.append(SheetReading(col, reg, day, v, cell.coordinate))
    return ParsedSheet(columns=columns, dates=dates, readings=readings, warnings=warnings)
