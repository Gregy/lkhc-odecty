"""Confirmed corrections of the source spreadsheet, applied by the importer.

The XLSX stays exactly as exported (it is also the oracle of the tests); errors in it that the
owner has *confirmed* are listed in the private import mapping (``[[date_corrections]]`` in
``import-mapping.toml``, see ``app.importer.mapping``) and applied on import by default. Each one is
reported in the importer output. ``python -m app.importer --bez-oprav`` (``import_workbook(...,
corrections=False)``) imports the sheet as exported.

Only add entries the data owner has confirmed – never guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class DateCorrection:
    """A row date of the `Odečty` sheet that is wrong in the source: every reading of ``sheet_date`` is
    imported as ``corrected``."""

    sheet_date: date
    corrected: date
    reason: str


def date_map(corrections: tuple[DateCorrection, ...]) -> dict[date, DateCorrection]:
    return {c.sheet_date: c for c in corrections}
