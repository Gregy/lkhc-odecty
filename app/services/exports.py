"""XLSX / CSV export helpers for the reports pages (consumption tables, settlements).

Quality is never conveyed by styling alone: every exported value table carries a
quality column (or a long-format sheet with one) and a legend.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.services.reports import QUALITY_LEGEND, QUALITY_SHORT

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv; charset=utf-8"

BOLD = Font(bold=True)
TITLE = Font(bold=True, size=14)
MUTED = Font(color="666666", italic=True)
FILLS = {
    "estimated": PatternFill("solid", fgColor="FDF1D6"),
    "provisional": PatternFill("solid", fgColor="FBE4E2"),
}
HEADER_FILL = PatternFill("solid", fgColor="F0EFEC")
NUM_FMT = "#,##0"
NUM_FMT_3 = "#,##0.###"


def sheet_title(name: str, used: set[str]) -> str:
    """Valid, unique worksheet title (max 31 chars, no []:*?/\\)."""
    base = re.sub(r"[\[\]:*?/\\]", "-", name).strip() or "List"
    base = base[:31]
    title, i = base, 2
    while title.lower() in used:
        suffix = f" ({i})"
        title = base[: 31 - len(suffix)] + suffix
        i += 1
    used.add(title.lower())
    return title


def to_num(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    dv = value if isinstance(value, Decimal) else Decimal(str(value))
    if dv == dv.to_integral_value():
        return int(dv)
    return float(dv)


def write_header(ws: Worksheet, row: int, labels: Sequence[str]) -> None:
    for j, label in enumerate(labels, start=1):
        c = ws.cell(row=row, column=j, value=label)
        c.font = BOLD
        c.fill = HEADER_FILL
        c.alignment = Alignment(wrap_text=True, vertical="top")


def write_legend(ws: Worksheet, row: int, *, raw: bool = False) -> int:
    """Quality legend starting at ``row``; returns the next free row."""
    ws.cell(row=row, column=1, value="Kvalita hodnot:").font = BOLD
    row += 1
    if raw:
        ws.cell(
            row=row,
            column=1,
            value="Surový pohled po dnech odečtů: rozdíly skutečných odečtů; když měřák v daný den odečten nebyl, "
            "jeho spotřeba se celá připíše období příštího odečtu (jako v původní tabulce). Bez interpolace.",
        ).font = MUTED
        return row + 2
    for label, text in QUALITY_LEGEND:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=text).font = MUTED
        row += 1
    ws.cell(
        row=row,
        column=1,
        value="Odhadnuté a předběžné buňky jsou podbarvené a mají komentář s původem; kvalitu uvádí i sloupec "
        "„Kvalita“.",
    ).font = MUTED
    return row + 2


def write_q(ws: Worksheet, row: int, col: int, q: dict[str, Any] | None, fmt: str = NUM_FMT) -> None:
    """Write a snapshot-style value {value, quality, title} with fill + comment for non-exact values."""
    if q is None:
        ws.cell(row=row, column=col, value=None)
        return
    c = ws.cell(row=row, column=col, value=to_num(q.get("value")))
    c.number_format = fmt
    quality = q.get("quality", "exact")
    if quality in FILLS:
        c.fill = FILLS[quality]
        c.font = Font(italic=True)
        title = plain(q.get("title") or QUALITY_SHORT.get(quality, ""))
        if title:
            c.comment = Comment(title[:2000], "LKHC Odečty")


def plain(text: str | None) -> str:
    """Display text for spreadsheets: no non-breaking spaces."""
    return (text or "").replace("\xa0", " ")


def worst_quality(values: Iterable[dict[str, Any] | None]) -> str:
    rank = {"exact": 0, "estimated": 1, "provisional": 2}
    out = "exact"
    for v in values:
        if v and rank.get(v.get("quality", "exact"), 0) > rank[out]:
            out = v["quality"]
    return out


def autosize(ws: Worksheet, min_width: int = 8, max_width: int = 60) -> None:
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is None:
                continue
            n = len(str(c.value))
            if n > 80:  # long notes/legend lines shouldn't blow up the column
                continue
            widths[c.column] = max(widths.get(c.column, 0), n)
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = max(min_width, min(max_width, w + 2))


FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def neutralize_formulas(wb: Workbook) -> None:
    """Store every text cell as text: openpyxl turns any string starting with "=" into a formula, so a
    tenant/point name or note like ``=HYPERLINK(...)`` (or our own "= <remainder tenant>" label) would become
    live in Excel.
    The app never writes real formulas."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.data_type == "f":
                    c.data_type = "s"


def workbook_bytes(wb: Workbook) -> bytes:
    neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_bytes(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    """CSV for Czech Excel: UTF-8 with BOM, semicolon separator, decimal comma."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([csv_cell(h) for h in header])
    for r in rows:
        w.writerow([csv_cell(v) for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def csv_cell(v: Any) -> Any:
    if isinstance(v, str):
        v = plain(v)
        # spreadsheet formula injection: text starting with = + - @ would be evaluated on opening
        return "'" + v if v.startswith(FORMULA_TRIGGERS) else v
    if isinstance(v, (Decimal, float)):
        num = to_num(v)
        return str(num).replace(".", ",") if num is not None else ""
    if v is None:
        return ""
    return v
