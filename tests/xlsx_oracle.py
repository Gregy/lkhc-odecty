"""Read cached values of the oracle sheets of the original Google-exported XLSX.

Google Sheets exports array formulas as ``=IFERROR(__xludf.DUMMYFUNCTION("..."), <cached>)``;
the cached value is the last argument. Plain formulas / array formulas without that
wrapper use openpyxl's cached value (``data_only=True``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

DUMMY_PREFIX = "=IFERROR(__xludf.DUMMYFUNCTION("
_LAST_ARG = re.compile(r',\s*("(?:[^"]|"")*"|-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?|TRUE|FALSE)\)\s*$')


def parse_dummy_cached(formula: str) -> object:
    """Cached value (last IFERROR argument) of a Google-exported DUMMYFUNCTION formula."""
    m = _LAST_ARG.search(formula)
    if not m:
        raise ValueError(f"cannot parse cached value from {formula[:80]!r}")
    raw = m.group(1)
    if raw.startswith('"'):
        return raw[1:-1].replace('""', '"')
    if raw in ("TRUE", "FALSE"):
        return raw == "TRUE"
    return float(raw)


class OracleWorkbook:
    def __init__(self, path: str | Path) -> None:
        self.formulas = openpyxl.load_workbook(path, data_only=False)
        self.cached = openpyxl.load_workbook(path, data_only=True)
        self.cross_checked = 0  # DUMMYFUNCTION values verified against openpyxl's cached value

    def value(self, sheet: str, coord: str) -> object:
        cell = self.formulas[sheet][coord]
        cached = self.cached[sheet][coord].value
        v = cell.value
        if isinstance(v, ArrayFormula):
            return cached
        if isinstance(v, str) and v.startswith(DUMMY_PREFIX):
            parsed = parse_dummy_cached(v)
            # cross-check the parser against openpyxl's cached value ("" may be exported as None)
            if not (parsed == cached or (parsed == "" and cached is None) or _num_eq(parsed, cached)):
                raise AssertionError(f"{sheet}!{coord}: parsed {parsed!r} != cached {cached!r}")
            self.cross_checked += 1
            return parsed
        if isinstance(v, str) and v.startswith("="):
            return cached
        return v


def _num_eq(a: object, b: object) -> bool:
    try:
        return a is not None and b is not None and float(a) == float(b)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return False


def to_decimal_or_none(v: object) -> Decimal | None:
    """Sheet value -> Decimal; blank ("" / None) -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError(v)
    if isinstance(v, (int, float)):
        return Decimal(repr(v))
    return Decimal(str(v))


def parse_label(label: str) -> tuple[date, date | None]:
    """'1.1.2024-13.1.2024' -> (start, end); '3.6.2026-' -> (start, None)."""
    a, _, b = label.partition("-")

    def d(s: str) -> date:
        dd, mm, yy = (int(x) for x in s.strip().split("."))
        return date(yy, mm, dd)

    return d(a), (d(b) if b.strip() else None)


@dataclass
class OracleRow:
    row: int
    start: date
    end: date | None
    cells: dict[str, object]  # column letter -> cached value


def read_rows(wb: OracleWorkbook, sheet: str, first_row: int, columns: list[str]) -> list[OracleRow]:
    ws = wb.formulas[sheet]
    out: list[OracleRow] = []
    for r in range(first_row, ws.max_row + 1):
        label = wb.value(sheet, f"A{r}")
        if label in (None, ""):
            break
        start, end = parse_label(str(label))
        out.append(OracleRow(r, start, end, {c: wb.value(sheet, f"{c}{r}") for c in columns}))
    return out


def next_col(col: str) -> str:
    return get_column_letter(column_index_from_string(col) + 1)
