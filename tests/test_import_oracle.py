"""Oracle: the raw per-day view must reproduce the cached values of the original sheets.

Runs against the synthetic workbook (``tests/synthetic.py``, always) and against the real spreadsheet of
the installation (``[real]``: opt-in, skipped with a reason when the private dir is not available).

Compared cell by cell:
- `Spotřeba` (meters × T1/T2 per reading-day period) vs. ``raw_consumption_table``
- `Rozpočítání se sazbou` (tenants × T1/T2) vs. ``raw_allocation_table``
- `Rozpočítání bez sazby` (tenants, T1+T2) vs. ``raw_allocation_table`` totals

Documented, narrowly-checked exceptions (each asserted, not skipped silently):
- E1 trailing open row: the sheets' FILTER(...A6:A & "-" & A7:A, A6:A <> "") emits a row for the last
  reading date with an empty end ("<last date>-"); it has no period. Asserted to be all blank/zero.
- E2 first period: the sheets' FILTER starts at Odečty!A6 (the second reading day), so the period
  A5–A6 (first two date rows) is missing in the sheet. Ours has it; asserted all-None
  (no meter was read on both dates).
- E3 blank vs. zero: in `Rozpočítání` a sum/difference of several blank ("") Spotřeba cells
  evaluates to 0 in Google Sheets, while a single-reference column stays blank. We return None
  ("no data"). Allowed only when the sheet shows exactly 0 and *every* contributing Spotřeba
  cell of that row is blank.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db import make_engine, make_sessionmaker
from app.importer.mapping import ColumnSpec, ImportMapping
from app.importer.xlsx import parse_readings_sheet
from app.services.network import load_network
from app.services.raw_table import raw_allocation_table, raw_consumption_table
from tests.conftest import import_template, sqlite_url
from tests.xlsx_oracle import OracleWorkbook, next_col, read_rows, to_decimal_or_none

SPOTREBA = "Spotřeba"
SE_SAZBOU = "Rozpočítání se sazbou"
BEZ_SAZBY = "Rozpočítání bez sazby"


@dataclass
class Case:
    db: Session
    xlsx: Path
    mapping: ImportMapping
    oracle: OracleWorkbook

    @property
    def specs(self) -> tuple[ColumnSpec, ...]:
        return self.mapping.columns

    @property
    def closed_periods(self) -> int:
        # reading days minus the first (E2, missing in the sheet) and the last (E1, open)
        return len(parse_readings_sheet(self.xlsx).dates) - 2


@pytest.fixture(scope="module", params=["synthetic", "real"])
def case(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Case]:
    if request.param == "synthetic":
        xlsx: Path = request.getfixturevalue("xlsx_path")
        mapping: ImportMapping = request.getfixturevalue("import_mapping")
        db_path: Path = request.getfixturevalue("_imported_template")
    else:
        xlsx, mapping = request.getfixturevalue("real_private")
        db_path = import_template(tmp_path_factory.mktemp("real") / "real.db", xlsx, mapping, corrections=False)
    engine = make_engine(sqlite_url(db_path))
    db = make_sessionmaker(engine)()
    try:
        yield Case(db, xlsx, mapping, OracleWorkbook(xlsx))
    finally:
        db.close()
        engine.dispose()


def test_sheet_rows_are_the_reading_days(case: Case) -> None:
    """The raw view's rows are derived from reading days; for the import they must equal the sheet rows."""
    from app.services.periods import reading_dates

    assert reading_dates(case.db) == sorted(parse_readings_sheet(case.xlsx).dates)


def _spec_cols(specs: tuple[ColumnSpec, ...]) -> list[tuple[str, str, str]]:
    """(sheet column, serial in DB, register) for all meter columns."""
    out = []
    for s in specs:
        serial = s.serial_override or s.expected_serial
        out.append((s.col, serial, "T1"))
        out.append((next_col(s.col), serial, "T2"))
    return out


def _tenant_source_cols(mapping: ImportMapping, tenant: str, register: str) -> list[str]:
    """Spotřeba columns contributing to a tenant's formula in `Rozpočítání`."""
    reg_off = 0 if register == "T1" else 1
    if tenant == mapping.remainder.name:
        specs = [s for s in mapping.columns if s.role == "main" or s.tenant]
    else:
        specs = [s for s in mapping.columns if s.tenant == tenant]
    return [s.col if reg_off == 0 else next_col(s.col) for s in specs]


def _blank(v: object) -> bool:
    return v is None or v == ""


def test_spotreba_matches(case: Case) -> None:
    net = load_network(case.db)
    table = raw_consumption_table(case.db, net=net)
    by_serial = {m.serial: m for m in net.meters.values()}
    cols = _spec_cols(case.specs)
    rows = read_rows(case.oracle, SPOTREBA, 4, [c for c, _, _ in cols])
    ours = {(p.start, p.end): i for i, p in enumerate(table.periods)}
    stats: Counter[str] = Counter()
    mismatches: list[str] = []

    sheet_keys = set()
    for row in rows:
        if row.end is None:  # E1
            assert all(_blank(v) for v in row.cells.values()), f"row {row.row} not blank"
            stats["E1_trailing_open_row_cells"] += len(row.cells)
            continue
        key = (row.start, row.end)
        sheet_keys.add(key)
        assert key in ours, f"sheet period {key} not among our periods"
        idx = ours[key]
        for col, serial, reg in cols:
            meter = by_serial[serial]
            mine = table.cell(idx, meter.id, reg).value if reg in meter.registers else None
            theirs = to_decimal_or_none(row.cells[col])
            stats["compared"] += 1
            if mine != theirs:
                mismatches.append(
                    f"{SPOTREBA}!{col}{row.row} ({row.start}–{row.end} {serial} {reg}): {theirs} != {mine}"
                )

    extra = [k for k in ours if k not in sheet_keys]
    for k in extra:  # E2
        idx = ours[k]
        assert k == (table.periods[0].start, table.periods[0].end), f"unexpected extra period {k}"
        assert all(c.value is None for c in table.cells[idx]), "first period must be all-None"
        stats["E2_first_period_cells"] += len(table.cells[idx])

    print(f"\n{SPOTREBA}: {dict(stats)}")
    assert not mismatches, "\n".join(mismatches)
    assert stats["compared"] == case.closed_periods * len(case.specs) * 2  # periods × meters × 2 registers
    assert len(extra) == 1


def _allocation_check(case: Case, sheet: str, first_row: int, split: bool) -> Counter[str]:
    oracle = case.oracle
    net = load_network(case.db)
    table = raw_allocation_table(case.db, net=net)
    tenants = {t.name: t for t in net.tenants}
    ws = oracle.formulas[sheet]
    # header row 1: tenant name in the T1 column (se sazbou: pairs; bez sazby: single columns)
    header: dict[str, str] = {}
    for cell in ws[1]:
        if cell.value:
            header[str(cell.value)] = cell.column_letter
    assert set(header) == set(tenants), header
    cols: list[tuple[str, str, str]] = []  # (sheet col, tenant, register or "total")
    for name, col in header.items():
        if split:
            cols += [(col, name, "T1"), (next_col(col), name, "T2")]
        else:
            cols.append((col, name, "total"))
    rows = read_rows(oracle, sheet, first_row, [c for c, _, _ in cols])
    sp_cols = sorted({c for s in case.specs for c in (s.col, next_col(s.col))})
    sp_rows = {(r.start, r.end): r for r in read_rows(oracle, SPOTREBA, 4, sp_cols)}
    ours = {(p.start, p.end): i for i, p in enumerate(table.periods)}

    stats: Counter[str] = Counter()
    mismatches: list[str] = []
    for row in rows:
        if row.end is None:  # E1
            assert all(_blank(v) or v == 0 for v in row.cells.values()), f"row {row.row} not blank/zero"
            stats["E1_trailing_open_row_cells"] += len(row.cells)
            continue
        idx = ours[(row.start, row.end)]
        sp = sp_rows[(row.start, row.end)]
        for col, name, reg in cols:
            mine = table.value(idx, tenants[name].id, reg)
            theirs = to_decimal_or_none(row.cells[col])
            stats["compared"] += 1
            if mine == theirs:
                continue
            regs = ["T1", "T2"] if reg == "total" else [reg]
            sources = [c for r in regs for c in _tenant_source_cols(case.mapping, name, r)]
            if mine is None and theirs == Decimal(0) and all(_blank(sp.cells[c]) for c in sources):  # E3
                stats["E3_blank_vs_zero"] += 1
                continue
            mismatches.append(f"{sheet}!{col}{row.row} ({row.start}–{row.end} {name} {reg}): {theirs} != {mine}")
    # E2: our first period is absent in the sheet and has no data at all
    first = table.periods[0]
    assert (first.start, first.end) not in {(r.start, r.end) for r in rows}
    assert all(v is None for t in table.values[0].values() for v in t.values())
    print(f"\n{sheet}: {dict(stats)}")
    assert not mismatches, "\n".join(mismatches)
    return stats


def test_rozpocitani_se_sazbou_matches(case: Case) -> None:
    stats = _allocation_check(case, SE_SAZBOU, 3, split=True)
    assert stats["compared"] == case.closed_periods * len(case.mapping.tenants) * 2


def test_rozpocitani_bez_sazby_matches(case: Case) -> None:
    stats = _allocation_check(case, BEZ_SAZBY, 2, split=False)
    assert stats["compared"] == case.closed_periods * len(case.mapping.tenants)


def test_dummy_parser_cross_checked(case: Case) -> None:
    """The DUMMYFUNCTION last-argument parser agrees with openpyxl's cached values."""
    cols = [c for c, _, _ in _spec_cols(case.specs)]
    before = case.oracle.cross_checked
    read_rows(case.oracle, SPOTREBA, 4, cols)
    assert case.oracle.cross_checked - before > case.closed_periods * len(cols)
