"""Mapping of the `Odečty` sheet columns to meter points, tenants and roles (SPEC §3).

The mapping is installation data (tenant names, meter serials) and therefore does **not** live in the
code: it is read from a TOML file, by default ``<private dir>/import-mapping.toml`` (see
``app.private``; an anonymised example is ``app/importer/import-mapping.example.toml``).

Serials, descriptions and readings are read from the spreadsheet; the mapping decides which meter
point / tenant / role each column pair belongs to. The serial in the sheet must match the mapping's
``serial`` (guards against a shifted layout).

File format (TOML, every table/key not listed here is an error)::

    # Tenants in display order. Exactly one is the remainder (main meter minus all others).
    [[tenants]]
    name = "Provozovatel"
    remainder = true
    color = "#2a78d6"          # optional, light theme; default: palette slot by position
    color_dark = "#3987e5"     # optional, dark theme

    [[tenants]]
    name = "Nájemce A"

    # One entry per meter column of the sheet (T1 column letter; T2 is the next column).
    [[columns]]
    col = "B"
    serial = "12345678"        # serial expected in row 2 of the sheet
    role = "main"              # main | sub (default) | control
    note = "Hlavní elektroměr"  # optional meter note

    [[columns]]
    col = "D"
    serial = "2345678"
    tenant = "Nájemce A"       # allocated tenant; omit = unallocated (falls to the remainder)

    # Optional keys of a column:
    #   point = "Kiosek"                 meter point name (default: capitalised description, row 3);
    #                                    several columns with the same point = meters replacing each other
    #   serial_override = "123-B"        serial stored in the DB (two columns with the same serial)
    #   installed_on = "first_reading"   installation date = first reading in the column
    #   removed_on = "last_reading"      removal date = last reading in the column, or
    #              = "replacement_install"  = installation date of the column in `replaced_by`
    #   replaced_by = "AB"               column of the replacement meter
    #   allocation_from = "AB"           the allocation starts at installed_on of that column
    #   parent = "Kontrolní místo"       parent meter point name (control balance)

    # Confirmed errors of the sheet, applied on import (``--bez-oprav`` imports the sheet as is).
    # Only entries the data owner has confirmed – never guess.
    [[date_corrections]]
    sheet_date = 2026-02-03    # TOML date: the wrong row date in the sheet
    corrected = 2026-03-03     # every reading of that row is imported with this date
    reason = "překlep v tabulce – potvrdil správce"
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.importer.corrections import DateCorrection

# Marker values for dates derived from the data
FIRST_READING = "first_reading"
LAST_READING = "last_reading"
REPLACEMENT_INSTALL = "replacement_install"  # = installed_on of the meter in `replaced_by`

ROLES = ("main", "sub", "control")

# Categorical palette (light, dark), slots in order; validated for adjacent-pair CVD separation.
# Tenants without explicit colours get the slot of their position in the mapping.
TENANT_PALETTE: tuple[tuple[str, str], ...] = (
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#008300", "#008300"),
    ("#6250d6", "#9085e9"),
    ("#e34948", "#e66767"),
)


class MappingError(ValueError):
    """The mapping file is missing or invalid."""


@dataclass(frozen=True)
class ColumnSpec:
    col: str  # T1 column letter; T2 is the next column
    expected_serial: str
    point: str | None = None  # meter point name; None = capitalized description from the file
    role: str = "sub"
    tenant: str | None = None
    serial_override: str | None = None
    installed_on: str | None = None  # FIRST_READING or None (unknown)
    removed_on: str | None = None  # LAST_READING / REPLACEMENT_INSTALL or None
    replaced_by: str | None = None  # column letter of the replacement meter
    allocation_from: str | None = None  # column letter whose installed_on starts the allocation
    parent: str | None = None  # parent meter point name (for control balances)
    note: str = ""


@dataclass(frozen=True)
class TenantSpec:
    name: str
    is_remainder: bool
    color: str
    color_dark: str


@dataclass(frozen=True)
class ImportMapping:
    columns: tuple[ColumnSpec, ...]
    tenants: tuple[TenantSpec, ...]
    date_corrections: tuple[DateCorrection, ...] = field(default=())
    source: str = ""  # where it was loaded from (for messages)

    @property
    def remainder(self) -> TenantSpec:
        return next(t for t in self.tenants if t.is_remainder)


_COL_RE = re.compile(r"^[A-Z]{1,3}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_TENANT_KEYS = {"name", "remainder", "color", "color_dark"}
_COLUMN_KEYS = {
    "col",
    "serial",
    "point",
    "role",
    "tenant",
    "serial_override",
    "installed_on",
    "removed_on",
    "replaced_by",
    "allocation_from",
    "parent",
    "note",
}
_CORRECTION_KEYS = {"sheet_date", "corrected", "reason"}


def _str(entry: dict[str, Any], key: str, where: str, *, required: bool = False) -> str | None:
    v = entry.get(key)
    if v is None:
        if required:
            raise MappingError(f"{where}: chybí klíč {key!r}")
        return None
    if isinstance(v, int) and not isinstance(v, bool) and key == "serial":
        v = str(v)  # serial written as a TOML integer
    if not isinstance(v, str) or not v.strip():
        raise MappingError(f"{where}: {key!r} musí být neprázdný text")
    return v.strip()


def _check_keys(entry: object, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise MappingError(f"{where}: očekávána tabulka")
    unknown = set(entry) - allowed
    if unknown:
        raise MappingError(f"{where}: neznámé klíče {sorted(unknown)}")
    return entry


def parse_mapping(data: dict[str, Any], source: str = "") -> ImportMapping:
    """Validate a parsed TOML document (see the module docstring) into an ``ImportMapping``."""
    unknown = set(data) - {"tenants", "columns", "date_corrections"}
    if unknown:
        raise MappingError(f"neznámé sekce {sorted(unknown)}")

    tenants: list[TenantSpec] = []
    for i, raw in enumerate(data.get("tenants") or []):
        where = f"tenants[{i}]"
        e = _check_keys(raw, _TENANT_KEYS, where)
        name = _str(e, "name", where, required=True)
        assert name is not None
        rem = e.get("remainder", False)
        if not isinstance(rem, bool):
            raise MappingError(f"{where}: 'remainder' musí být true/false")
        slot = TENANT_PALETTE[i % len(TENANT_PALETTE)]
        color = _str(e, "color", where) or slot[0]
        color_dark = _str(e, "color_dark", where) or slot[1]
        for c in (color, color_dark):
            if not _COLOR_RE.match(c):
                raise MappingError(f"{where}: barva {c!r} není ve tvaru #rrggbb")
        if any(t.name == name for t in tenants):
            raise MappingError(f"{where}: nájemce {name!r} je uveden dvakrát")
        tenants.append(TenantSpec(name, rem, color, color_dark))
    if sum(t.is_remainder for t in tenants) != 1:
        raise MappingError("právě jeden nájemce musí mít remainder = true")
    tenant_names = {t.name for t in tenants}

    columns: list[ColumnSpec] = []
    for i, raw in enumerate(data.get("columns") or []):
        where = f"columns[{i}]"
        e = _check_keys(raw, _COLUMN_KEYS, where)
        col = _str(e, "col", where, required=True)
        assert col is not None
        col = col.upper()
        where = f"columns[{i}] ({col})"
        if not _COL_RE.match(col):
            raise MappingError(f"{where}: neplatné písmeno sloupce")
        role = _str(e, "role", where) or "sub"
        if role not in ROLES:
            raise MappingError(f"{where}: role musí být jedna z {ROLES}")
        tenant = _str(e, "tenant", where)
        if tenant is not None and tenant not in tenant_names:
            raise MappingError(f"{where}: nájemce {tenant!r} není v [[tenants]]")
        installed_on = _str(e, "installed_on", where)
        if installed_on not in (None, FIRST_READING):
            raise MappingError(f"{where}: installed_on může být jen {FIRST_READING!r}")
        removed_on = _str(e, "removed_on", where)
        if removed_on not in (None, LAST_READING, REPLACEMENT_INSTALL):
            raise MappingError(f"{where}: removed_on může být {LAST_READING!r} nebo {REPLACEMENT_INSTALL!r}")
        replaced_by = _str(e, "replaced_by", where)
        if removed_on == REPLACEMENT_INSTALL and not replaced_by:
            raise MappingError(f"{where}: removed_on = {REPLACEMENT_INSTALL!r} vyžaduje replaced_by")
        note = e.get("note", "")
        if not isinstance(note, str):
            raise MappingError(f"{where}: 'note' musí být text")
        if any(c.col == col for c in columns):
            raise MappingError(f"{where}: sloupec je uveden dvakrát")
        columns.append(
            ColumnSpec(
                col=col,
                expected_serial=_str(e, "serial", where, required=True) or "",
                point=_str(e, "point", where),
                role=role,
                tenant=tenant,
                serial_override=_str(e, "serial_override", where),
                installed_on=installed_on,
                removed_on=removed_on,
                replaced_by=replaced_by.upper() if replaced_by else None,
                allocation_from=(_str(e, "allocation_from", where) or "").upper() or None,
                parent=_str(e, "parent", where),
                note=note.strip(),
            )
        )
    if not columns:
        raise MappingError("mapování neobsahuje žádné [[columns]]")
    letters = {c.col for c in columns}
    for c in columns:
        for key, ref in (("replaced_by", c.replaced_by), ("allocation_from", c.allocation_from)):
            if ref and ref not in letters:
                raise MappingError(f"columns ({c.col}): {key} = {ref!r} není mezi sloupci")
    serials = [c.serial_override or c.expected_serial for c in columns]
    dup = {s for s in serials if serials.count(s) > 1}
    if dup:
        raise MappingError(f"výrobní čísla v databázi by se opakovala {sorted(dup)} – použijte serial_override")

    corrections: list[DateCorrection] = []
    for i, raw in enumerate(data.get("date_corrections") or []):
        where = f"date_corrections[{i}]"
        e = _check_keys(raw, _CORRECTION_KEYS, where)
        a, b = e.get("sheet_date"), e.get("corrected")
        if not (isinstance(a, date) and isinstance(b, date)) or a == b:
            raise MappingError(f"{where}: sheet_date a corrected musí být různá data (TOML datum, např. 2026-02-03)")
        corrections.append(DateCorrection(sheet_date=a, corrected=b, reason=_str(e, "reason", where) or ""))
    return ImportMapping(tuple(columns), tuple(tenants), tuple(corrections), source)


def load_mapping(path: str | Path) -> ImportMapping:
    """Read and validate the mapping file; ``MappingError`` with a readable message otherwise."""
    p = Path(path)
    if not p.is_file():
        raise MappingError(
            f"soubor s mapováním sloupců {p} neexistuje. Importér potřebuje mapování sloupců na nájemce a "
            "místa (soukromá data instalace): zadejte --mapping SOUBOR nebo --private-dir ADRESÁŘ "
            "(proměnná LKHC_PRIVATE_DIR); formát viz app/importer/import-mapping.example.toml."
        )
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise MappingError(f"{p}: neplatný TOML: {exc}") from exc
    try:
        return parse_mapping(data, str(p))
    except MappingError as exc:
        raise MappingError(f"{p}: {exc}") from exc
