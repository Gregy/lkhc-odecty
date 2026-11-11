"""Consumption tables (`/spotreba`) - aligned periods (primary) and raw per-day view, exports (SPEC §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.deps import CurrentUser, DbSession, require_user
from app.domain.aligned import QValue, qsum, worst
from app.domain.consumption import sum_optional
from app.routers._reports_common import download
from app.services import (
    load_network,
    raw_allocation_table,
    raw_consumption_table,
    resolve_spans,
    suggest_settlement_bounds,
    tenant_consumption_table,
)
from app.services.consumption import REGISTERS
from app.services.exports import (
    BOLD,
    CSV_MIME,
    MUTED,
    NUM_FMT,
    TITLE,
    XLSX_MIME,
    autosize,
    csv_bytes,
    plain,
    to_num,
    workbook_bytes,
    write_header,
    write_legend,
    write_q,
)
from app.services.network import Network
from app.services.reports import data_coverage, data_years, qdict, quality_label, year_range
from app.templating import REMAINDER_TARIFF_NOTE, format_date, is_marked, templates

router = APIRouter(prefix="/spotreba", dependencies=[Depends(require_user)])


@dataclass
class Col:
    label: str
    group: str  # tenant / point name (header group)
    register: str  # T1 | T2 | total
    tenant_id: int | None = None
    point_id: int | None = None
    is_main: bool = False
    title: str = ""  # header tooltip


@dataclass
class Row:
    label: str
    start: date
    end: date
    cells: list[Any]  # QValue (aligned) | Decimal | None (raw)


@dataclass
class ConsTable:
    kind: str  # aligned | raw
    title: str
    cols: list[Col]
    rows: list[Row]
    sums: list[Any] = field(default_factory=list)

    @property
    def groups(self) -> list[tuple[str, int, Col]]:
        """(group label, colspan, first column) for the two-level header."""
        out: list[tuple[str, int, Col]] = []
        for c in self.cols:
            if out and out[-1][0] == c.group:
                out[-1] = (c.group, out[-1][1] + 1, out[-1][2])
            else:
                out.append((c.group, 1, c))
        return out


@dataclass
class Params:
    view: str  # zarovnane | dny (raw per reading day)
    split: bool
    year: int | None  # None = all
    granularity: str  # mesice | hlavni (between main-meter reading days; aligned only)
    provisional: bool

    def query(self, **over: Any) -> str:
        v = {
            "pohled": self.view,
            "sazba": "1" if self.split else "0",
            "rok": str(self.year) if self.year else "vse",
            "obdobi": self.granularity,
            "predbezne": "1" if self.provisional else "",
        }
        v.update(over)
        return "&".join(f"{k}={val}" for k, val in v.items() if val not in ("", None))


def _params(request: Request, default_year: int | None) -> Params:
    q = request.query_params
    view = {"obchuzky": "dny"}.get(q.get("pohled", ""), q.get("pohled", "zarovnane"))  # old links
    if view not in ("zarovnane", "dny"):
        view = "zarovnane"
    rok = q.get("rok")
    if rok == "vse":
        year = None
    else:
        try:
            year = int(rok) if rok else default_year
        except ValueError:
            year = default_year
    gran = {"obchuzky": "hlavni"}.get(q.get("obdobi", ""), q.get("obdobi", "mesice"))  # old links
    if gran not in ("mesice", "hlavni"):
        gran = "mesice"
    return Params(view, q.get("sazba", "1") != "0", year, gran, q.get("predbezne") == "1")


def _value_cols(net: Network, split: bool, *, points: bool, remainder_total: bool = False) -> list[Col]:
    """Value columns. ``remainder_total`` (aligned view): the remainder tenant gets only a T1+T2 column even
    when split by tariff (see REMAINDER_TARIFF_NOTE); the raw "like the sheet" view keeps T1/T2 for it."""
    regs = list(REGISTERS) if split else ["total"]
    cols: list[Col] = []
    if points:
        for p in net.points:
            if not p.meter_ids:
                continue
            preg = net.point_registers(p.id) if split else ["total"]
            for reg in preg:
                cols.append(Col(reg if split else "kWh", p.name, reg, point_id=p.id, is_main=p.role == "main"))
        return cols
    for t in net.tenants:
        # a tenant without any T2 meter gets no (always zero) T2 column
        pids = net.main_point_ids if t.is_remainder else {r.meter_point_id for r in net.rules if r.tenant_id == t.id}
        has = {reg for pid in pids for reg in net.point_registers(pid)}
        if split and remainder_total and t.is_remainder:
            cols.append(Col("T1+T2", t.name, "total", tenant_id=t.id, title=REMAINDER_TARIFF_NOTE))
            continue
        for reg in regs:
            if split and reg != "T1" and reg not in has:
                continue
            cols.append(Col(reg if split else "kWh", t.name, reg, tenant_id=t.id))
    for reg in regs:
        cols.append(Col(reg if split else "kWh", "Hlavní elektroměr", reg, is_main=True))
    return cols


def _aligned_tables(db: Session, net: Network, p: Params, today: date) -> tuple[ConsTable, ConsTable, str]:
    cov = data_coverage(net)
    note = ""
    spans_kw: dict[str, Any]
    if p.granularity == "hlavni":
        if p.year:
            b = suggest_settlement_bounds(db, p.year)
            spans_kw = {
                "granularity": "main",
                "start": b.main_near_start or b.start,
                "end": b.main_near_end or b.end,
            }
        else:
            spans_kw = {"granularity": "main"}
    else:
        if p.year:
            rng = year_range(p.year, cov, include_provisional=p.provisional, today=today)
        elif cov.first and cov.end:
            end = max(cov.end, date(today.year, today.month, 1)) if p.provisional else cov.end
            rng = (cov.first, end) if end > cov.first else None
        else:
            rng = None
        if rng is None:
            spans_kw = {"boundaries": []}
        else:
            spans_kw = {"granularity": "month", "start": rng[0], "end": rng[1]}
            if not p.provisional and p.year and cov.end and cov.end < date(p.year + 1, 1, 1):
                note = (
                    f"Měsíce po {format_date(cov.end)} nejsou zobrazeny – hlavní elektroměr "
                    "pro ně ještě nemá odečty (byly by jen předběžné)."
                )
    if spans_kw.get("boundaries") == [] or not resolve_spans(db, **spans_kw):
        empty_t = ConsTable("aligned", "Nájemci", _value_cols(net, p.split, points=False, remainder_total=True), [])
        empty_p = ConsTable("aligned", "Měřicí místa", _value_cols(net, p.split, points=True), [])
        return empty_t, empty_p, note or "Pro zvolené období nejsou data."
    tt = tenant_consumption_table(db, net=net, **spans_kw)
    pt = tt.points
    tcols = _value_cols(net, p.split, points=False, remainder_total=True)
    pcols = _value_cols(net, p.split, points=True)

    def tcell(c: Col, i: int) -> QValue:
        if c.is_main:
            if c.register == "total":
                return tt.main["T1"][i] + tt.main["T2"][i]
            return tt.main[c.register][i]
        if c.register == "total":
            return tt.total(c.tenant_id, i)
        return tt.value(c.tenant_id, c.register, i)

    def pcell(c: Col, i: int) -> QValue:
        if c.register == "total":
            return pt.total(c.point_id, i)
        return pt.value(c.point_id, c.register, i)

    trows = [Row(s.label, s.start_date, s.end_date, [tcell(c, i) for c in tcols]) for i, s in enumerate(tt.periods)]
    prows = [Row(s.label, s.start_date, s.end_date, [pcell(c, i) for c in pcols]) for i, s in enumerate(pt.periods)]
    ttab = ConsTable("aligned", "Nájemci", tcols, trows, [qsum(r.cells[j] for r in trows) for j in range(len(tcols))])
    ptab = ConsTable(
        "aligned", "Měřicí místa", pcols, prows, [qsum(r.cells[j] for r in prows) for j in range(len(pcols))]
    )
    return ttab, ptab, note


def _raw_tables(db: Session, net: Network, p: Params) -> tuple[ConsTable, ConsTable, str]:
    start = date(p.year, 1, 1) if p.year else None
    end = date(p.year, 12, 31) if p.year else None
    at = raw_allocation_table(db, start, end, net=net)
    rc = raw_consumption_table(db, start, end, net=net)
    tcols = _value_cols(net, p.split, points=False)
    pcols = _value_cols(net, p.split, points=True)

    def tcell(c: Col, i: int) -> Decimal | None:
        if c.is_main:
            if c.register == "total":
                return sum_optional(at.results[i][r].main for r in REGISTERS)
            return at.results[i][c.register].main
        return at.value(i, c.tenant_id, c.register)

    def pcell(c: Col, i: int) -> Decimal | None:
        if c.register == "total":
            return sum_optional(rc.point_value(i, c.point_id, r) for r in REGISTERS)
        return rc.point_value(i, c.point_id, c.register)

    trows = [Row(per.label, per.start, per.end, [tcell(c, i) for c in tcols]) for i, per in enumerate(at.periods)]
    prows = [Row(per.label, per.start, per.end, [pcell(c, i) for c in pcols]) for i, per in enumerate(rc.periods)]

    def sums(rows: list[Row], n: int) -> list[Decimal | None]:
        return [sum_optional(r.cells[j] for r in rows) for j in range(n)]

    return (
        ConsTable("raw", "Nájemci", tcols, trows, sums(trows, len(tcols))),
        ConsTable("raw", "Měřicí místa", pcols, prows, sums(prows, len(pcols))),
        "",
    )


def _build(request: Request, db: Session) -> tuple[Params, ConsTable, ConsTable, str, dict[str, Any]]:
    today = date.today()
    net = load_network(db)
    cov = data_coverage(net)
    default_year = cov.last_month[0].year if cov.last_month else today.year
    p = _params(request, default_year)
    if p.view == "dny":
        ttab, ptab, note = _raw_tables(db, net, p)
    else:
        ttab, ptab, note = _aligned_tables(db, net, p, today)
    return p, ttab, ptab, note, {"net": net, "cov": cov, "years": data_years(cov, today), "today": today}


@router.get("", name="spotreba")
def spotreba(request: Request, db: DbSession, user: CurrentUser):
    p, ttab, ptab, note, extra = _build(request, db)
    return templates.TemplateResponse(
        request,
        "spotreba/index.html",
        {
            "page_title": "Spotřeba",
            "active_nav": "/spotreba",
            "p": p,
            "ttab": ttab,
            "ptab": ptab,
            "note": note,
            **extra,
        },
    )


# --- exports -------------------------------------------------------------------------


def _describe(p: Params) -> str:
    view = "zarovnaná období" if p.view == "zarovnane" else "surově po dnech odečtů"
    if p.view == "zarovnane":
        view += ", " + ("kalendářní měsíce" if p.granularity == "mesice" else "mezi odečty hlavního elektroměru")
    return f"{view}; {'rok ' + str(p.year) if p.year else 'všechna data'}; {'T1/T2' if p.split else 'bez sazby'}"


def _filename(p: Params, ext: str) -> str:
    view = "zarovnane" if p.view == "zarovnane" else "po-dnech-odectu"
    return f"spotreba-{p.year or 'vse'}-{view}{'' if p.split else '-bez-sazby'}.{ext}"


def _qd(v: Any) -> dict[str, Any] | None:
    if v is None:
        return None
    if isinstance(v, QValue):
        return qdict(v)
    return {"value": v, "quality": "exact", "title": ""}


def _write_wide(ws, tab: ConsTable, p: Params, title: str) -> None:
    ws.cell(row=1, column=1, value=title).font = TITLE
    ws.cell(row=2, column=1, value=_describe(p))
    row = write_legend(ws, 4, raw=tab.kind == "raw")
    if any(c.title for c in tab.cols):  # the remainder tenant is exported only as T1+T2
        note = "Zbytek je uveden jen v součtu T1+T2: " + REMAINDER_TARIFF_NOTE
        ws.cell(row=row - 1, column=1, value=note).font = MUTED
        row += 1
    write_header(ws, row, ["Období", "Od", "Do"] + [f"{c.group} {c.label}".strip() for c in tab.cols])
    if tab.kind == "aligned":
        ws.cell(row=row, column=4 + len(tab.cols), value="Kvalita (nejhorší v řádku)").font = BOLD
    hdr = row
    for r in tab.rows:
        row += 1
        ws.cell(row=row, column=1, value=r.label)
        ws.cell(row=row, column=2, value=r.start).number_format = "d.m.yyyy"
        ws.cell(row=row, column=3, value=r.end).number_format = "d.m.yyyy"
        for j, v in enumerate(r.cells):
            if tab.kind == "aligned":
                write_q(ws, row, 4 + j, _qd(v))
            else:
                ws.cell(row=row, column=4 + j, value=to_num(v)).number_format = NUM_FMT
        if tab.kind == "aligned":
            ws.cell(row=row, column=4 + len(tab.cols), value=quality_label(worst(c.quality for c in r.cells)))
    if tab.rows:
        row += 1
        ws.cell(row=row, column=1, value="Součet").font = BOLD
        for j, v in enumerate(tab.sums):
            if tab.kind == "aligned":
                write_q(ws, row, 4 + j, _qd(v))
            else:
                ws.cell(row=row, column=4 + j, value=to_num(v)).number_format = NUM_FMT
    ws.freeze_panes = ws.cell(row=hdr + 1, column=2)
    autosize(ws)


def _long_rows(tab: ConsTable, kind_label: str) -> list[list[Any]]:
    out = []
    for r in tab.rows:
        for c, v in zip(tab.cols, r.cells, strict=True):
            reg = "T1+T2" if c.register == "total" else c.register
            if isinstance(v, QValue):
                out.append(
                    [
                        kind_label,
                        c.group,
                        reg,
                        r.label,
                        r.start,
                        r.end,
                        v.value,
                        quality_label(v.quality),
                        v.estimated_kwh if is_marked(v.quality) else None,
                        plain(qdict(v)["title"]).replace("\n", "; ") if is_marked(v.quality) else "",
                    ]
                )
            else:
                out.append(
                    [
                        kind_label,
                        c.group,
                        reg,
                        r.label,
                        r.start,
                        r.end,
                        v,
                        "surově (po dnech odečtů)",
                        None,
                        "bez dat" if v is None else "",
                    ]
                )
    return out


LONG_HEADER = ["Typ", "Nájemce / místo", "Tarif", "Období", "Od", "Do", "kWh", "Kvalita", "Odhadnuto kWh", "Původ"]


@router.get("/export.xlsx", name="spotreba_xlsx")
def export_xlsx(request: Request, db: DbSession, user: CurrentUser):
    p, ttab, ptab, _note, _extra = _build(request, db)
    wb = Workbook()
    ws = wb.active
    ws.title = "Nájemci"
    _write_wide(ws, ttab, p, "Spotřeba po nájemcích (kWh)")
    _write_wide(wb.create_sheet("Měřicí místa"), ptab, p, "Spotřeba po měřicích místech (kWh)")
    data = wb.create_sheet("Data")
    write_header(data, 1, LONG_HEADER)
    for i, r in enumerate(_long_rows(ttab, "nájemce") + _long_rows(ptab, "místo"), start=2):
        for j, v in enumerate(r, start=1):
            c = data.cell(row=i, column=j, value=to_num(v) if isinstance(v, Decimal) else v)
            if isinstance(v, date):
                c.number_format = "d.m.yyyy"
    data.freeze_panes = "A2"
    autosize(data)
    return download(workbook_bytes(wb), _filename(p, "xlsx"), XLSX_MIME)


@router.get("/export.csv", name="spotreba_csv")
def export_csv(request: Request, db: DbSession, user: CurrentUser):
    p, ttab, ptab, _note, _extra = _build(request, db)
    rows = _long_rows(ttab, "nájemce") + _long_rows(ptab, "místo")
    rows = [[f"{v.day}.{v.month}.{v.year}" if isinstance(v, date) else v for v in r] for r in rows]
    return download(csv_bytes(LONG_HEADER, rows), _filename(p, "csv"), CSV_MIME)
