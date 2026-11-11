"""Synthetic stand-in for the original Google-exported spreadsheet (+ its import mapping).

The real workbook and mapping are private installation data (``private/``); the test suite builds
this fake one instead: invented tenants ("Nájemce A" …), invented serials and descriptions and
generated readings, laid out exactly like the original and with every quirk the importer
(``app.importer``) and the oracle (``tests/xlsx_oracle.py``, ``tests/test_import_oracle.py``) handle:

Sheet `Odečty` (parsed by the importer)
- title row, row 2 serials (numbers stored as floats, one alphanumeric serial with a space, an
  11-digit serial), row 3 descriptions (lower-case ones get capitalised), row 4 ``T1/T2 [kWh]`` headers;
  rows 2–3 merged per T1/T2 pair (two pairs left unmerged); dates as ``d.m.yyyy`` strings
- description comments: a plain note and Excel "threaded comment" boilerplate (stripped by the importer)
- T1/T2 column pairs: three sub-meters and the main meter with both registers, the others T1 only
- 6-digit T2 counters (counter width derived from the data) and a 5-digit counter that rolls over
- a replaced meter (old column ends on its last reading, new one starts at 0 on the same point)
- a duplicate serial (an out-of-service meter and its replacement with the same serial in the sheet →
  ``serial_override`` "-B", same point, allocation valid from the new meter's first reading)
- missing readings everywhere (meters not read on every reading day, the main meter skipped once),
  a meter whose readings stop months before the end (stale / provisional), meters starting late
- an unallocated sub-meter (falls into the remainder), a control meter with a child point
  (``parent``), a remainder tenant (main meter minus everybody else; its T1 is negative in some months
  because single-register sub-meters count all hours as T1)
- a usage spike (one month at four times the usual rate) that the anomaly detection reports
- a mistyped row date (``TYPO``, two days after the previous row, really ``FIXED``) with a confirmed
  ``[[date_corrections]]`` entry in the mapping
- extra sheets the importer ignores

Oracle sheets (Google Sheets array formulas, cached values computed here with Google's semantics)
- `Spotřeba`: header rows = ``ArrayFormula`` copy of `Odečty` rows 2–4 (cached), the period labels
  ``FILTER('Odečty'!A6:A & "-" & 'Odečty'!A7:A, …)`` and the per-meter consumption
  ``IF(end <> "", MOD(end - last non-blank value at or before start, 100000), "")`` exported as
  ``=IFERROR(__xludf.DUMMYFUNCTION("…"), <cached>)``; E1: the trailing open row ``"<last date>-"`` and an
  unlabelled artefact row are blank; E2: the periods start at `Odečty`!A6, so the first period (A5–A6) is
  missing
- `Rozpočítání se sazbou` (merged tenant headers, T1/T2 per tenant, ``ArrayFormula`` anchors + exported
  plain values) and `Rozpočítání bez sazby` (T1+T2): a single-reference tenant column stays blank,
  E3: a sum/difference of blank cells becomes 0; trailing zero rows below the last label

Everything is deterministic (seeded); ``build_workbook(path)`` writes the XLSX,
``mapping_toml()`` the matching ``import-mapping.toml``.
"""

from __future__ import annotations

import math
import random
import re
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import openpyxl
from openpyxl.comments import Comment
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

# --- tenants --------------------------------------------------------------------------------------
REMAINDER = "Provozovatel"
A, B, C, D, E, F = (f"Nájemce {x}" for x in "ABCDEF")
TENANTS = (REMAINDER, A, B, C, D, E, F)  # display / sheet order

# --- reading days (sheet row 5 onward) ------------------------------------------------------------
_DATES_TEXT = """
    30.12.2023 1.1.2024 13.1.2024 31.1.2024 3.3.2024 1.4.2024 4.5.2024 1.6.2024 1.7.2024 2.8.2024 2.9.2024
    1.10.2024 1.11.2024 30.11.2024 2.1.2025 1.2.2025 2.3.2025 30.4.2025 1.6.2025 1.7.2025 2.8.2025 1.9.2025
    3.10.2025 2.11.2025 1.12.2025 4.1.2026 2.2.2026 1.3.2026 1.4.2026 3.5.2026 1.6.2026 3.6.2026
"""
SHEET_DATES = tuple(_DATES_TEXT.split())


def _d(s: str) -> date:
    dd, mm, yy = (int(x) for x in s.split("."))
    return date(yy, mm, dd)


DATES = tuple(_d(s) for s in SHEET_DATES)
TYPO, FIXED = date(2026, 6, 3), date(2026, 7, 3)  # last sheet row is mistyped; confirmed correction
TRUE_DATES = tuple(FIXED if d == TYPO else d for d in DATES)
FIRST_ROW = 5


# --- meter columns --------------------------------------------------------------------------------
@dataclass(frozen=True)
class Col:
    col: str
    serial: str  # in the sheet (row 2)
    description: str  # row 3
    tenant: str | None = None
    role: str = "sub"
    t1: str = ""  # presence of T1 readings per sheet row ("#" = read)
    t2: str = ""  # presence of T2 readings ("" = T1 only)
    extra: str = ""  # extra mapping TOML lines

    @property
    def t2_col(self) -> str:
        return get_column_letter(column_index_from_string(self.col) + 1)


MAIN_SERIAL = "52814903"
PARA_SERIAL = "3906127"  # Nájemce A věž (D): read almost every time
ACCU_SERIAL = "4172856"  # Nájemce A akumulačky (F): T1+T2
HALL_SERIAL = "6038419"  # Nájemce A plechová hala (H): long gaps
OLD_OFFICE_SERIAL = "27350"  # Nájemce B kancelář (J): replaced on 1.12.2025, 6-digit T2
NEW_OFFICE_SERIAL = "64918203"  # Nájemce B kancelář (L): the replacement, starts at 0
HANGAR_SERIAL = "5820734"  # Nájemce B hangár (N): no reading after 2.8.2025, 6-digit T2
E_SERIAL = "73601548"  # Nájemce E (P)
ROLLOVER_SERIAL = "481097"  # za skříní dílna (R): 5-digit counter rolls over in Feb 2024
CORRIDOR_SERIAL = "69253071"  # Nájemce A z chodby (T): LCD module
CORRIDOR2_SERIAL = "38170625"  # Nájemce A z chodby 2 (V): unallocated
WORKSHOP_SERIAL = "15937402"  # Dílna hangár (X): from 2.8.2025, plain comment
KIOSK_SERIAL = "26481357"  # kiosek (Z, out of service) and its replacement (AB) – same serial in the sheet
KIOSK_NEW_SERIAL = KIOSK_SERIAL + "-B"
C_SERIAL = "84726051"  # Nájemce C (AD)
F_SERIAL = "KX 207M"  # Hangár Nájemce F (AF): alphanumeric, child of the control point
CONTROL_SERIAL = "30418276559"  # Kontrolní západní hangáry (AH): control meter, 11 digits

MAIN_POINT = "Distribuce"
PARA_POINT = f"{A} věž"
ACCU_POINT = f"{A} akumulačky"
HALL_POINT = f"{A} plechová hala"
OFFICE_POINT = f"{B} kancelář"
HANGAR_POINT = f"{B} hangár"
E_POINT = E
ROLLOVER_POINT = "Za skříní dílna"
CORRIDOR_POINT = f"{A} z chodby"
CORRIDOR2_POINT = f"{A} z chodby 2"
WORKSHOP_POINT = "Dílna hangár"
KIOSK_POINT = "Kiosek"
C_POINT = C
F_POINT = f"Hangár {F}"
CONTROL_POINT = "Kontrolní západní hangáry"

COLUMNS = (
    Col(
        "B",
        MAIN_SERIAL,
        MAIN_POINT,
        role="main",
        t1=".###########################.###",
        t2=".###########################.###",
        extra='note = "Hlavní elektroměr (T1+T2)."',
    ),
    Col("D", PARA_SERIAL, PARA_POINT, A, t1="#..###.#########################"),
    Col(
        "F",
        ACCU_SERIAL,
        ACCU_POINT,
        A,
        t1="#..#####.....#####.......#.#.#..",
        t2="#..#####.....#####.......#.#.#..",
    ),
    Col("H", HALL_SERIAL, HALL_POINT, A, t1="#.##########.##..#######.###.###"),
    Col(
        "J",
        OLD_OFFICE_SERIAL,
        OFFICE_POINT,
        B,
        t1=".########################.......",
        t2=".########################.......",
        extra=f'removed_on = "last_reading"\nnote = "Nahrazen elektroměrem {NEW_OFFICE_SERIAL}."',
    ),
    Col(
        "L",
        NEW_OFFICE_SERIAL,
        OFFICE_POINT,
        B,
        t1="........................########",
        extra='installed_on = "first_reading"\nnote = "Nový elektroměr na stejném místě po výměně (počáteční stav 0)."',
    ),
    Col(
        "N",
        HANGAR_SERIAL,
        HANGAR_POINT,
        B,
        t1=".#.##################...........",
        t2=".#.##################...........",
    ),
    Col("P", E_SERIAL, E, E, t1="#.##############################"),
    Col(
        "R",
        ROLLOVER_SERIAL,
        "za skříní dílna",
        A,
        t1="#.####.#####################.###",
        extra='note = "Počitadlo 5 míst, v datech přetekl."',
    ),
    Col("T", CORRIDOR_SERIAL, CORRIDOR_POINT, A, t1="....############################"),
    Col(
        "V",
        CORRIDOR2_SERIAL,
        CORRIDOR2_POINT,
        None,
        t1="....############################",
        extra='note = "Bez alokace (spotřeba spadá do zbytku)."',
    ),
    Col("X", WORKSHOP_SERIAL, WORKSHOP_POINT, B, t1="....................############"),
    Col(
        "Z",
        KIOSK_SERIAL,
        "kiosek",
        None,
        t1=".......#########................",
        extra=f'point = "{KIOSK_POINT}"\nremoved_on = "replacement_install"\nreplaced_by = "AB"\n'
        'note = "Mimo provoz, měřil špatně. Datum vyřazení neznámé – nastaveno na instalaci náhradního měřáku."',
    ),
    Col(
        "AB",
        KIOSK_SERIAL,
        "kiosek",
        D,
        t1=".................###############",
        extra=f'point = "{KIOSK_POINT}"\nserial_override = "{KIOSK_NEW_SERIAL}"\ninstalled_on = "first_reading"\n'
        'allocation_from = "AB"\nnote = "OVĚŘIT výrobní číslo: v tabulce stejné jako u vyřazeného měřáku."',
    ),
    Col("AD", C_SERIAL, C, C, t1=".......#####################.###"),
    Col(
        "AF",
        F_SERIAL,
        F_POINT,
        F,
        t1="...................#############",
        extra=f'parent = "{CONTROL_POINT}"',
    ),
    Col("AH", CONTROL_SERIAL, CONTROL_POINT, None, role="control", t1="......................##########"),
)
BY_COL = {c.col: c for c in COLUMNS}
UNMERGED = ("R", "AF")  # pairs whose header cells are not merged (like in the original)
COMMENTS = {
    "X": " venku za dílnou\n(kontrolní součet T1 a T2 k hangáru)",
    "Z": "mimo provoz, meri spatne",
    "AH": "Součtový pro hangár nájemce F a dílnu",
}
THREADED = ("Z", "AH")
_THREADED_BOILERPLATE = (
    "[Threaded comment]\n Your version of Excel allows you to read this threaded comment; however, any edits "
    "to it will get removed if the file is opened in a newer version of Excel. Learn more: "
    "https://go.microsoft.com/fwlink/?linkid=870924\nComment:\n\t{}\n"
)


# --- generated readings ---------------------------------------------------------------------------
SEED = 20240101
DAY0 = date(2023, 12, 1)
DAY_END = date(2026, 8, 1)
_NDAYS = (DAY_END - DAY0).days

# (base kWh/day, seasonal amplitude, start value); single-register sub-meters count all hours in T1
_RATES: dict[tuple[str, str], tuple[float, float, float]] = {
    ("D", "T1"): (1.1, 0.25, 812),
    ("F", "T1"): (0.0, 0.0, 10473),  # T1 of the storage heaters never moves
    ("F", "T2"): (9.0, 0.8, 24518),
    ("H", "T1"): (0.25, 0.25, 17630),
    ("J", "T1"): (0.65, 0.25, 8240),
    ("J", "T2"): (0.55, 0.25, 112870),  # 6-digit counter
    ("L", "T1"): (1.3, 0.25, 0),
    ("N", "T1"): (2.1, 0.25, 27415),
    ("N", "T2"): (0.12, 0.25, 171230),  # 6-digit counter
    ("P", "T1"): (5.2, 0.25, 4890),
    ("R", "T1"): (9.5, 0.8, 0),  # start chosen so that the counter rolls over in February 2024
    ("T", "T1"): (2.4, 0.25, 960),
    ("V", "T1"): (0.0, 0.0, 3),
    ("X", "T1"): (1.9, 0.25, 655),
    ("Z", "T1"): (0.07, 0.25, 3),
    ("AB", "T1"): (0.75, 0.25, 0),
    ("AD", "T1"): (1.4, 0.25, 7),
    ("AF", "T1"): (0.45, 0.25, 1),
}
_CONTROL_EXTRA = 0.8  # kWh/day measured by the control meter besides its child
_MAIN_OWN = 15.0  # kWh/day of the remainder tenant itself
_MAIN_T1_SHARE = 0.45  # single-register sub-meters count all hours as T1 -> the remainder's T1 goes negative
# usage spike (detected as an anomaly): a heater left on behind the corridor meter for a month
_EVENTS: dict[tuple[str, str], tuple[date, date, float]] = {("T", "T1"): (date(2025, 12, 1), date(2026, 1, 4), 4.0)}
_MAIN_START = {"T1": 17240, "T2": 21380}


def _cumulative(
    rng: random.Random, base: float, amp: float, event: tuple[date, date, float] | None = None
) -> list[float]:
    month_noise: dict[tuple[int, int], float] = {}
    out, acc = [], 0.0
    for i in range(_NDAYS + 1):
        out.append(acc)
        day = DAY0 + timedelta(days=i)
        k = (day.year, day.month)
        if k not in month_noise:
            month_noise[k] = rng.uniform(0.75, 1.25)
        season = 1 + amp * math.cos(2 * math.pi * (day.timetuple().tm_yday - 20) / 365.25)
        boost = event[2] if event and event[0] <= day < event[1] else 1.0
        acc += base * season * month_noise[k] * boost
    return out


def _idx(d: date) -> int:
    return (d - DAY0).days


def _generate() -> dict[tuple[str, str], dict[int, float]]:
    """Sheet values {(T1 column, register): {sheet row index: value}}."""
    rng = random.Random(SEED)
    cum = {k: _cumulative(rng, base, amp, _EVENTS.get(k)) for k, (base, amp, _) in sorted(_RATES.items())}
    first_true = {c.col: TRUE_DATES[c.t1.index("#")] for c in COLUMNS}
    last_true = {c.col: TRUE_DATES[c.t1.rindex("#")] for c in COLUMNS}

    def active(col: str, i: int) -> bool:
        if col == "J":
            return i < _idx(last_true["J"])
        if col == "L":
            return i >= _idx(first_true["L"])
        if col == "Z":
            return i < _idx(first_true["AB"])
        if col == "AB":
            return i >= _idx(first_true["AB"])
        return True

    def counter(col: str, reg: str, i: int) -> float:
        _, _, start = _RATES[(col, reg)]
        c = cum[(col, reg)]
        if col in ("L", "AB"):  # new meters start at 0 on their first reading
            return start + c[i] - c[_idx(first_true[col])]
        return start + c[i]

    # main meter: all sub-meters (as they were active) + the remainder's own consumption
    total = [0.0] * (_NDAYS + 1)
    for (col, _reg), c in cum.items():
        acc = 0.0
        for i in range(1, _NDAYS + 1):
            if active(col, i - 1):
                acc += c[i] - c[i - 1]
            total[i] += acc
    for i in range(_NDAYS + 1):
        total[i] = 1.08 * total[i] + _MAIN_OWN * i + _CONTROL_EXTRA * i
    af = cum[("AF", "T1")]

    # rollover meter: ~99 800 on 31.1.2024, past 100 000 by 3.3.2024
    r_start = 99_800 - cum[("R", "T1")][_idx(date(2024, 1, 31))]

    values: dict[tuple[str, str], dict[int, float]] = {}
    for c in COLUMNS:
        for reg, presence in (("T1", c.t1), ("T2", c.t2)):
            if not presence:
                continue
            vals: dict[int, float] = {}
            for row, mark in enumerate(presence):
                if mark != "#":
                    continue
                i = _idx(TRUE_DATES[row])
                if c.col == "B":
                    share = _MAIN_T1_SHARE if reg == "T1" else 1 - _MAIN_T1_SHARE
                    v = _MAIN_START[reg] + share * total[i]
                elif c.col == "AH":
                    v = 41 + (af[i] - af[_idx(first_true["AH"])]) + _CONTROL_EXTRA * (i - _idx(first_true["AH"]))
                elif c.col == "R":
                    v = (r_start + cum[("R", "T1")][i]) % 100_000
                else:
                    v = counter(c.col, reg, i)
                vals[row] = float(round(v))
            values[(c.col, reg)] = vals
    # the generated data really has the advertised quirks
    r = values[("R", "T1")]
    assert r[3] > 99_000 and r[4] < 1_000, "rollover between 31.1. and 3.3.2024"
    assert all(len(str(int(v))) == 6 for v in values[("J", "T2")].values())
    assert values[("L", "T1")][SHEET_DATES.index("1.12.2025")] == 0
    return values


VALUES = _generate()


def value(col: str, day: date | str, reg: str = "T1") -> float | None:
    """Sheet value of column ``col`` on sheet date ``day`` (as in the sheet, i.e. ``TYPO`` not ``FIXED``)."""
    d = _d(day) if isinstance(day, str) else day
    return VALUES.get((col, reg), {}).get(DATES.index(d))


def last_value(col: str, reg: str = "T1", before: date | None = None) -> tuple[date, float]:
    """(sheet date, value) of the last reading of ``col`` (strictly before ``before`` if given)."""
    rows = sorted(VALUES[(col, reg)].items())
    if before is not None:
        rows = [(r, v) for r, v in rows if DATES[r] < before]
    r, v = rows[-1]
    return DATES[r], v


def serial_of(col: str) -> str:
    """Serial in the DB (``serial_override`` for the duplicate)."""
    return KIOSK_NEW_SERIAL if col == "AB" else BY_COL[col].serial


# --- Google Sheets semantics of the oracle sheets ------------------------------------------------
Blank = None  # "" in the sheet


def spotreba() -> list[tuple[str, dict[str, float | None]]]:
    """Rows of `Spotřeba`: (label, {sheet column: value or None}) – labels start at `Odečty`!A6."""
    out = []
    n = len(DATES)
    for s in range(1, n):  # start row index (0 = sheet row 5); FILTER starts at A6
        label = f"{SHEET_DATES[s]}-{SHEET_DATES[s + 1] if s + 1 < n else ''}"
        cells: dict[str, float | None] = {}
        for c in COLUMNS:
            for reg, letter in (("T1", c.col), ("T2", c.t2_col)):
                vals = VALUES.get((c.col, reg), {})
                end = vals.get(s + 1)
                prev = [vals[r] for r in range(s + 1) if r in vals]  # VLOOKUP: last non-blank at/before start
                cells[letter] = (end - prev[-1]) % 100_000 if end is not None and prev else Blank
        out.append((label, cells))
    return out


def _sum(vals: list[float | None]) -> float | None:
    """A sheet formula over Spotřeba cells: one reference keeps a blank, a sum/difference turns it into 0."""
    if len(vals) == 1:
        return vals[0]
    return sum(v or 0 for v in vals)


def tenant_columns(tenant: str) -> list[str]:
    return [c.col for c in COLUMNS if c.tenant == tenant]


def rozpocitani(row: dict[str, float | None]) -> dict[str, dict[str, float | None]]:
    """`Rozpočítání se sazbou` of one Spotřeba row: {tenant: {T1, T2}}."""
    out: dict[str, dict[str, float | None]] = {}
    for t in TENANTS[1:]:
        out[t] = {
            "T1": _sum([row[c] for c in tenant_columns(t)]),
            "T2": _sum([row[BY_COL[c].t2_col] for c in tenant_columns(t)]),
        }
    main = BY_COL["B"]
    out[REMAINDER] = {
        "T1": (row["B"] or 0) - sum(out[t]["T1"] or 0 for t in TENANTS[1:]),
        "T2": (row[main.t2_col] or 0) - sum(out[t]["T2"] or 0 for t in TENANTS[1:]),
    }
    return {t: out[t] for t in TENANTS}


# --- writing the workbook -------------------------------------------------------------------------
def _dummy(inner: str, cached: object) -> str:
    if cached is None or cached == "":
        lit = '""'
    elif isinstance(cached, str):
        lit = '"' + cached.replace('"', '""') + '"'
    else:
        lit = repr(float(cached))
    return f'=IFERROR(__xludf.DUMMYFUNCTION("{inner}"),{lit})'


def _consumption_formula(letter: str) -> str:
    r = f"'Odečty'!{letter}"
    return (
        f'ArrayFormula(IFERROR(IF({r}7:{letter}1527<>"""",MOD({r}7:{letter}1527-IF(ISBLANK({r}6:{letter}1527), '
        f"VLOOKUP(ROW({r}6:{letter}1527), FILTER({{ROW({r}5:{letter}1527), {r}5:{letter}1527}}, "
        f'NOT(ISBLANK({r}5:{letter}1527))), 2, 1), {r}6:{letter}1527), 100000),""""),""""))'
    )


_LABELS_FORMULA = 'ARRAYFORMULA(FILTER(\'Odečty\'!A6:A1527&""-""&\'Odečty\'!A7:A1527,\'Odečty\'!A6:A1527<>""""))'
_COMPUTED = '""COMPUTED_VALUE""'


def build_workbook(path: str | Path) -> Path:
    """Write the synthetic workbook to ``path`` (formula cells carry Google's cached values)."""
    path = Path(path)
    wb = openpyxl.Workbook()
    wb.active.title = "Grafy"
    cached: dict[str, dict[str, object]] = {}  # sheet -> coordinate -> cached value of formula cells

    # sheet order like the original: charts, allocations, consumption, readings, old sheets
    ws_bez = wb.create_sheet("Rozpočítání bez sazby")
    ws_se = wb.create_sheet("Rozpočítání se sazbou")
    ws_sp = wb.create_sheet("Spotřeba")
    ws = wb.create_sheet("Odečty")
    old = wb.create_sheet("Stará data")
    old["A1"] = "Archiv starších odečtů (importér list ignoruje)"

    # Odečty
    ws["A1"] = "Odečty elektroměrů"
    ws["C1"] = "T1 - základní\nT2 - snížená"
    ws["A2"], ws["A3"], ws["A4"] = "Číslo elektroměru:", "Popis:", "Datum"
    for c in COLUMNS:
        ws[f"{c.col}2"] = float(c.serial) if c.serial.isdigit() else c.serial
        ws[f"{c.col}3"] = c.description
        ws[f"{c.col}4"], ws[f"{c.t2_col}4"] = "T1 [kWh]", "T2 [kWh]"
        if c.col in COMMENTS:
            text = COMMENTS[c.col]
            ws[f"{c.col}3"].comment = Comment(_THREADED_BOILERPLATE.format(text) if c.col in THREADED else text, "")
        for (col, reg), vals in VALUES.items():
            if col != c.col:
                continue
            letter = c.col if reg == "T1" else c.t2_col
            for row, v in vals.items():
                ws[f"{letter}{FIRST_ROW + row}"] = v
    for row, s in enumerate(SHEET_DATES):
        ws[f"A{FIRST_ROW + row}"] = s
    for c in COLUMNS:
        if c.col not in UNMERGED:
            ws.merge_cells(f"{c.col}2:{c.t2_col}2")
            ws.merge_cells(f"{c.col}3:{c.t2_col}3")

    # Spotřeba: header = array copy of Odečty rows 2-4
    sp_c: dict[str, object] = {}
    last_letter = get_column_letter(column_index_from_string(COLUMNS[-1].col) + 1)
    ws_sp["A1"] = ArrayFormula(f"A1:{last_letter}3", "='Odečty'!A2:AP4")
    sp_c["A1"] = "Číslo elektroměru:"
    ws_sp["A2"], ws_sp["A3"] = "Popis:", "Datum"
    for c in COLUMNS:
        ws_sp[f"{c.col}1"] = ws[f"{c.col}2"].value
        ws_sp[f"{c.col}2"] = c.description
        ws_sp[f"{c.col}3"], ws_sp[f"{c.t2_col}3"] = "T1 [kWh]", "T2 [kWh]"
        if c.col not in UNMERGED:
            ws_sp.merge_cells(f"{c.col}1:{c.t2_col}1")
            ws_sp.merge_cells(f"{c.col}2:{c.t2_col}2")
    letters = [x for c in COLUMNS for x in (c.col, c.t2_col)]
    rows = spotreba()
    for k, (label, cells) in enumerate(rows):
        r = 4 + k
        ws_sp[f"A{r}"] = _dummy(_LABELS_FORMULA if k == 0 else _COMPUTED, label)
        sp_c[f"A{r}"] = label
        for x in letters:
            v = cells[x]
            ws_sp[f"{x}{r}"] = _dummy(_consumption_formula(x) if k == 0 else _COMPUTED, v)
            sp_c[f"{x}{r}"] = "" if v is None else v
    r = 4 + len(rows)  # artefact row below the open row: blank values, no label
    for x in letters:
        ws_sp[f"{x}{r}"] = _dummy(_COMPUTED, None)
        sp_c[f"{x}{r}"] = ""
    cached["Spotřeba"] = sp_c

    # Rozpočítání se sazbou: tenant pairs from column B
    se_c: dict[str, object] = {}
    t_col = {t: get_column_letter(2 + 2 * i) for i, t in enumerate(TENANTS)}
    for t, x in t_col.items():
        ws_se[f"{x}1"] = t
        y = get_column_letter(column_index_from_string(x) + 1)
        ws_se.merge_cells(f"{x}1:{y}1")
        ws_se[f"{x}2"], ws_se[f"{y}2"] = "T1 [kWh]", "T2 [kWh]"
    alloc = [(label, rozpocitani(cells)) for label, cells in rows]
    blank_row = rozpocitani(dict.fromkeys(letters))
    for k, (label, vals) in enumerate([*alloc, ("", blank_row)]):
        r = 3 + k
        if label:
            if k == 0:
                ws_se[f"A{r}"] = ArrayFormula(f"A{r}:A1526", "='Spotřeba'!A4:A2026")
                se_c[f"A{r}"] = label
            else:
                ws_se[f"A{r}"] = label
        for t, x in t_col.items():
            y = get_column_letter(column_index_from_string(x) + 1)
            for reg, letter in (("T1", x), ("T2", y)):
                v = vals[t][reg]
                v = "" if v is None else float(v)
                if k == 0:
                    ws_se[f"{letter}{r}"] = ArrayFormula(f"{letter}{r}:{letter}2026", f"='Spotřeba'!{letter}4")
                    se_c[f"{letter}{r}"] = v
                else:
                    ws_se[f"{letter}{r}"] = v
    cached["Rozpočítání se sazbou"] = se_c

    # Rozpočítání bez sazby: T1 + T2 per tenant from column B
    bez_c: dict[str, object] = {}
    for i, t in enumerate(TENANTS):
        ws_bez[f"{get_column_letter(2 + i)}1"] = t
    for k, (label, vals) in enumerate([*alloc, ("", blank_row)]):
        r = 2 + k
        if label:
            ws_bez[f"A{r}"] = _dummy(_LABELS_FORMULA if k == 0 else _COMPUTED, label)
            bez_c[f"A{r}"] = label
        for i, t in enumerate(TENANTS):
            letter = get_column_letter(2 + i)
            v = float((vals[t]["T1"] or 0) + (vals[t]["T2"] or 0))
            if k == 0:
                ws_bez[f"{letter}{r}"] = ArrayFormula(
                    f"{letter}{r}:{letter}2025", f"='Rozpočítání se sazbou'!{letter}3"
                )
                bez_c[f"{letter}{r}"] = v
            else:
                ws_bez[f"{letter}{r}"] = v
    cached["Rozpočítání bez sazby"] = bez_c

    wb.save(path)
    _inject_cached_values(path, [s.title for s in wb.worksheets], cached)
    return path


_CELL_RE = re.compile(r'<c r="([A-Z]+[0-9]+)"((?: [a-z]+="[^"]*")*)>(<f[^>]*>.*?</f>)<v ?/>(</c>)', re.S)


def _inject_cached_values(path: Path, titles: list[str], cached: dict[str, dict[str, object]]) -> None:
    """openpyxl writes formulas without results; add Google's cached values like the export has them."""
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            m = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", item.filename)
            if m:
                values = cached.get(titles[int(m.group(1)) - 1], {})

                def fill(mt: re.Match[str], values: dict[str, object] = values) -> str:
                    coord, attrs, formula, end = mt.groups()
                    v = values.get(coord)
                    if v is None:
                        return mt.group(0)
                    if isinstance(v, str):
                        return f'<c r="{coord}"{attrs} t="str">{formula}<v>{escape(v)}</v>{end}'
                    num = int(v) if float(v).is_integer() else v
                    return f'<c r="{coord}"{attrs}>{formula}<v>{num}</v>{end}'

                data = _CELL_RE.sub(fill, data.decode("utf-8")).encode("utf-8")
            dst.writestr(item, data)
    tmp.replace(path)


# --- the matching import mapping ------------------------------------------------------------------
def mapping_toml() -> str:
    lines = ["# Synthetic import mapping (tests/synthetic.py)", ""]
    for t in TENANTS:
        lines += ["[[tenants]]", f'name = "{t}"']
        if t == REMAINDER:
            lines.append("remainder = true")
        lines.append("")
    for c in COLUMNS:
        lines += ["[[columns]]", f'col = "{c.col}"', f'serial = "{c.serial}"']
        if c.role != "sub":
            lines.append(f'role = "{c.role}"')
        if c.tenant:
            lines.append(f'tenant = "{c.tenant}"')
        if c.extra:
            lines += c.extra.splitlines()
        lines.append("")
    lines += [
        "[[date_corrections]]",
        f"sheet_date = {TYPO.isoformat()}",
        f"corrected = {FIXED.isoformat()}",
        f'reason = "překlep v tabulce: řádek „{_fmt(TYPO)}“ je ve skutečnosti {_fmt(FIXED)} – potvrzeno"',
        "",
    ]
    return "\n".join(lines)


def _fmt(d: date) -> str:
    return f"{d.day}.{d.month}.{d.year}"


def write_private_dir(directory: str | Path) -> tuple[Path, Path]:
    """A complete synthetic private directory: (xlsx, import-mapping.toml)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    mapping = directory / "import-mapping.toml"
    mapping.write_text(mapping_toml(), encoding="utf-8")
    return build_workbook(directory / "synthetic.xlsx"), mapping


if __name__ == "__main__":  # python -m tests.synthetic OUT_DIR – e.g. to try the app with fake data
    import sys

    x, m = write_private_dir(sys.argv[1] if len(sys.argv) > 1 else "synthetic-private")
    print(f"{x}\n{m}")
