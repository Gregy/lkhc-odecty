"""Settlements (`/vyuctovani`): create, draft view, protocols, XLSX, print, finalize with snapshot, reopen (SPEC §7)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from openpyxl import Workbook
from openpyxl.comments import Comment
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.deps import AdminUser, CurrentUser, DbSession, require_user
from app.models import Settlement, User
from app.routers._reports_common import download, parse_date, parse_int
from app.services import compute_settlement, load_network, reading_dates, suggest_settlement_bounds
from app.services.exports import (
    BOLD,
    MUTED,
    TITLE,
    XLSX_MIME,
    autosize,
    plain,
    sheet_title,
    to_num,
    workbook_bytes,
    worst_quality,
    write_header,
    write_legend,
    write_q,
)
from app.services.reports import (
    QUALITY_SHORT,
    d,
    data_coverage,
    document_tenant,
    document_tenant_meters,
    quality_label,
    settlement_document,
)
from app.templating import REMAINDER_TARIFF_NOTE, flash, format_date, is_marked, templates

router = APIRouter(prefix="/vyuctovani", dependencies=[Depends(require_user)])

# label of the snapshot's "mode"; "rounds" only appears in snapshots finalized before rounds were removed
MODE_LABELS = {"months": "zarovnané kalendářní měsíce", "rounds": "hranice podle obchůzek (starší vyúčtování)"}


def _get(db: Session, settlement_id: int) -> Settlement:
    s = db.get(Settlement, settlement_id)
    if s is None:
        raise HTTPException(404, "Vyúčtování neexistuje.")
    return s


def _live_document(db: Session, s: Settlement) -> dict[str, Any]:
    result = compute_settlement(db, s.period_start, s.period_end)
    return settlement_document(result, load_network(db))


def _document(db: Session, s: Settlement) -> dict[str, Any]:
    """Finalized settlements render from their snapshot, drafts from live data."""
    if s.status == "final" and s.snapshot:
        return s.snapshot
    return _live_document(db, s)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _default_year(db: Session) -> int:
    cov = data_coverage(load_network(db))
    today = date.today()
    # last calendar year fully covered by the main meter's readings
    return (cov.end.year if cov.end else today.year) - 1


def _protocol_meters(doc: dict[str, Any], td: dict[str, Any]) -> list[dict[str, Any]]:
    """Meters of a tenant protocol, without meters that were not installed during the interval."""
    start, end = str(doc["start"]), str(doc["end"])
    out = []
    for m in document_tenant_meters(doc, td):
        if m.get("removed_on") and str(m["removed_on"]) <= start:
            continue
        if m.get("installed_on") and str(m["installed_on"]) >= end:
            continue
        out.append(m)
    return out


def _context(db: Session, s: Settlement, doc: dict[str, Any]) -> dict[str, Any]:
    users = {u.id: u for u in db.scalars(select(User))}
    protocols = [{"t": td, "meters": _protocol_meters(doc, td)} for td in doc.get("tenants", [])]
    diffs: list[dict[str, Any]] = []
    if s.status == "final" and s.snapshot:
        live = _live_document(db, s)
        for td in doc.get("tenants", []):
            lt = document_tenant(live, td["id"])
            if lt is None:
                continue
            a, b = d(td["total"]["value"]) if "total" in td else None, d(lt["total"]["value"])
            if a is not None and abs(a - b) >= 1:
                diffs.append({"name": td["name"], "snapshot": a, "live": b})
    return {
        "dt": _date,
        "s": s,
        "doc": doc,
        "protocols": protocols,
        "diffs": diffs,
        "users": users,
        "MODE_LABELS": MODE_LABELS,
        "QUALITY_SHORT": QUALITY_SHORT,
    }


# --- list / create ---------------------------------------------------------------------


@router.get("", name="vyuctovani")
def vyuctovani(request: Request, db: DbSession, user: CurrentUser):
    items = list(db.scalars(select(Settlement).order_by(Settlement.period_start.desc(), Settlement.id.desc())))
    year = _default_year(db)
    years = sorted({d.year for d in reading_dates(db)} | {year}, reverse=True)
    return templates.TemplateResponse(
        request,
        "vyuctovani/index.html",
        {
            "page_title": "Vyúčtování",
            "active_nav": "/vyuctovani",
            "items": items,
            "year": year,
            "years": years,
            "bounds": suggest_settlement_bounds(db, year),
            "MODE_LABELS": MODE_LABELS,
        },
    )


@router.get("/hranice", name="vyuctovani_hranice")
def hranice(request: Request, db: DbSession, user: CurrentUser):
    """HTMX fragment: default bounds for a year and the main-meter reading days nearest to them."""
    q = request.query_params
    year = parse_int(q.get("year") or q.get("rok")) or _default_year(db)
    return templates.TemplateResponse(
        request,
        "vyuctovani/_bounds.html",
        {"year": year, "bounds": suggest_settlement_bounds(db, year)},
    )


@router.post("", name="vyuctovani_create")
def create(
    request: Request,
    db: DbSession,
    user: AdminUser,
    year: int = Form(...),
    name: str = Form(""),
    period_start: str = Form(""),
    period_end: str = Form(""),
    note: str = Form(""),
):
    if not 2000 <= year <= 2100:
        flash(request, "Neplatný rok.", "error")
        return _redirect("/vyuctovani")
    start = parse_date(period_start) or date(year, 1, 1)
    end = parse_date(period_end) or date(year + 1, 1, 1)
    if end <= start:
        flash(request, "Konec období musí být po začátku.", "error")
        return _redirect("/vyuctovani")
    s = Settlement(
        year=year,
        name=name.strip() or f"Vyúčtování {year}",
        period_start=start,
        period_end=end,
        status="draft",
        created_by_id=user.id,
        note=note.strip(),
    )
    db.add(s)
    audit.record_create(db, user, s)
    db.commit()
    flash(request, "Návrh vyúčtování vytvořen.", "success")
    return _redirect(f"/vyuctovani/{s.id}")


# --- detail ---------------------------------------------------------------------------------


@router.get("/{settlement_id}", name="vyuctovani_detail")
def detail(request: Request, settlement_id: int, db: DbSession, user: CurrentUser):
    s = _get(db, settlement_id)
    doc = _document(db, s)
    ctx = _context(db, s, doc)
    ctx.update({"page_title": s.name, "active_nav": "/vyuctovani"})
    return templates.TemplateResponse(request, "vyuctovani/detail.html", ctx)


@router.get("/{settlement_id}/tisk", name="vyuctovani_tisk")
def tisk(request: Request, settlement_id: int, db: DbSession, user: CurrentUser):
    s = _get(db, settlement_id)
    doc = _document(db, s)
    ctx = _context(db, s, doc)
    tid = parse_int(request.query_params.get("najemce"))
    if tid is not None:
        ctx["protocols"] = [p for p in ctx["protocols"] if p["t"]["id"] == tid]
        if not ctx["protocols"]:
            raise HTTPException(404, "Nájemce v tomto vyúčtování není.")
    ctx.update({"page_title": f"{s.name} – protokol", "active_nav": "/vyuctovani", "summary": tid is None})
    return templates.TemplateResponse(request, "vyuctovani/print.html", ctx)


@router.post("/{settlement_id}/upravit", name="vyuctovani_edit")
def edit(
    request: Request,
    settlement_id: int,
    db: DbSession,
    user: AdminUser,
    name: str = Form(...),
    period_start: str = Form(...),
    period_end: str = Form(...),
    note: str = Form(""),
):
    s = _get(db, settlement_id)
    if s.status != "draft":
        flash(request, "Finalizované vyúčtování nelze upravit – nejdřív ho znovu otevřete.", "error")
        return _redirect(f"/vyuctovani/{s.id}")
    start, end = parse_date(period_start), parse_date(period_end)
    if start is None or end is None or end <= start:
        flash(request, "Neplatné období.", "error")
        return _redirect(f"/vyuctovani/{s.id}")
    changes = audit.apply_changes(
        s,
        {
            "name": name.strip() or s.name,
            "period_start": start,
            "period_end": end,
            "note": note.strip(),
        },
    )
    if changes:
        audit.record_update(db, user, s, changes)
    db.commit()
    flash(request, "Uloženo." if changes else "Beze změn.", "success" if changes else "info")
    return _redirect(f"/vyuctovani/{s.id}")


@router.post("/{settlement_id}/finalizovat", name="vyuctovani_finalize")
def finalize(
    request: Request,
    settlement_id: int,
    db: DbSession,
    user: AdminUser,
    force: str = Form(""),
    note: str = Form(""),
):
    s = _get(db, settlement_id)
    if s.status != "draft":
        flash(request, "Vyúčtování už je finalizované.", "error")
        return _redirect(f"/vyuctovani/{s.id}")
    doc = _live_document(db, s)
    can = bool(doc["check"]["can_finalize"])
    forced = bool(force) and not can
    if not can and not force:
        flash(
            request,
            "Nelze finalizovat: některé stavy na hranicích jsou předběžné (chybí pozdější odečet). "
            "Doplňte odečty, nebo finalizaci vědomě vynuťte s poznámkou.",
            "error",
        )
        return _redirect(f"/vyuctovani/{s.id}")
    if forced and not note.strip():
        flash(request, "Vynucená finalizace vyžaduje poznámku (proč se finalizuje s předběžnými hodnotami).", "error")
        return _redirect(f"/vyuctovani/{s.id}")
    s.snapshot = doc
    s.snapshot_version = doc.get("version")
    s.status = "final"
    s.finalized_at = datetime.now().replace(microsecond=0)
    s.finalized_by_id = user.id
    s.finalize_forced = forced
    s.finalize_note = note.strip()
    audit.record(
        db,
        user,
        "finalize",
        "settlements",
        s.id,
        {
            "forced": forced,
            "provisional": doc["check"]["provisional"],
            "estimated_count": len(doc["check"]["estimated"]),
            "totals": {t["name"]: t["total"]["value"] for t in doc["tenants"]},
        },
        note.strip(),
    )
    db.commit()
    flash(request, "Vyúčtování finalizováno" + (" (vynuceně s předběžnými hodnotami)." if forced else "."), "success")
    return _redirect(f"/vyuctovani/{s.id}")


@router.post("/{settlement_id}/znovu-otevrit", name="vyuctovani_reopen")
def reopen(request: Request, settlement_id: int, db: DbSession, user: AdminUser, note: str = Form("")):
    s = _get(db, settlement_id)
    if s.status != "final":
        flash(request, "Vyúčtování není finalizované.", "error")
        return _redirect(f"/vyuctovani/{s.id}")
    if not note.strip():
        flash(request, "Uveďte důvod znovuotevření.", "error")
        return _redirect(f"/vyuctovani/{s.id}")
    snap = s.snapshot or {}
    audit.record(
        db,
        user,
        "reopen",
        "settlements",
        s.id,
        {
            "status": ["final", "draft"],
            "finalized_at": s.finalized_at,
            "finalized_by_id": s.finalized_by_id,
            "finalize_forced": s.finalize_forced,
            "finalize_note": s.finalize_note,
            "snapshot_totals": {t["name"]: t.get("total", {}).get("value") for t in snap.get("tenants", [])},
        },
        note.strip(),
    )
    s.status = "draft"
    s.snapshot = None
    s.snapshot_version = None
    s.finalized_at = None
    s.finalized_by_id = None
    s.finalize_forced = False
    s.finalize_note = ""
    db.commit()
    flash(request, "Vyúčtování znovu otevřeno jako návrh; hodnoty se počítají z aktuálních odečtů.", "warning")
    return _redirect(f"/vyuctovani/{s.id}")


@router.post("/{settlement_id}/smazat", name="vyuctovani_delete")
def delete(request: Request, settlement_id: int, db: DbSession, user: AdminUser):
    s = _get(db, settlement_id)
    if s.status != "draft":
        flash(request, "Finalizované vyúčtování nelze smazat.", "error")
        return _redirect(f"/vyuctovani/{s.id}")
    audit.record_delete(db, user, s)
    db.delete(s)
    db.commit()
    flash(request, "Návrh vyúčtování smazán.", "success")
    return _redirect("/vyuctovani")


# --- XLSX ---------------------------------------------------------------------------------------


def _date(v: Any) -> str:
    if not v:
        return ""
    dt = v if isinstance(v, (date, datetime)) else datetime.fromisoformat(str(v))
    return format_date(dt)


def _info_rows(ws, s: Settlement, doc: dict[str, Any], title: str) -> int:
    ws.cell(row=1, column=1, value=title).font = TITLE
    ws.cell(row=2, column=1, value="Vyúčtovací období")
    ws.cell(row=2, column=2, value=f"{_date(doc['start'])} – {_date(doc['end'])}")
    ws.cell(row=3, column=1, value="Hranice")
    ws.cell(row=3, column=2, value=MODE_LABELS.get(doc.get("mode"), doc.get("mode")))
    ws.cell(row=4, column=1, value="Stav")
    status = "finalizováno " + _date(s.finalized_at) if s.status == "final" else "NÁVRH (hodnoty z aktuálních odečtů)"
    if s.status == "final" and s.finalize_forced:
        status += " – vynuceně s předběžnými hodnotami: " + s.finalize_note
    ws.cell(row=4, column=2, value=status)
    ws.cell(row=5, column=1, value="Jednotka")
    ws.cell(row=5, column=2, value="kWh")
    return write_legend(ws, 7)


def _tenant_regs(t: dict[str, Any], t1: Any, t2: Any) -> list[Any]:
    """T1/T2 values of a tenant for a table; the remainder tenant is shown only as T1+T2 (empty T1/T2)."""
    return [None, None] if t.get("is_remainder") else [t1, t2]


def _remainder_note(ws, row: int, doc: dict[str, Any]) -> int:
    """Explanatory line when the document has a remainder tenant; returns the next free row."""
    if not any(t.get("is_remainder") for t in doc.get("tenants", [])):
        return row
    ws.cell(row=row, column=1, value="Zbytek je uveden jen v součtu T1+T2: " + REMAINDER_TARIFF_NOTE).font = MUTED
    return row + 2


def _write_q_with_quality(ws, row: int, col: int, q: dict[str, Any] | None) -> None:
    write_q(ws, row, col, q)
    ws.cell(row=row, column=col + 1, value=quality_label((q or {}).get("quality", "exact")))


@router.get("/{settlement_id}/export.xlsx", name="vyuctovani_xlsx")
def export_xlsx(request: Request, settlement_id: int, db: DbSession, user: CurrentUser):
    s = _get(db, settlement_id)
    doc = _document(db, s)
    wb = Workbook()
    used: set[str] = set()
    ws = wb.active
    ws.title = sheet_title("Souhrn", used)
    row = _info_rows(ws, s, doc, s.name)
    tenants = doc["tenants"]
    row = _remainder_note(ws, row - 1, doc)
    header = ["Období", "Od", "Do"]
    for t in tenants:
        header += [f"{t['name']} T1", f"{t['name']} T2", f"{t['name']} celkem", f"{t['name']} kvalita"]
    header += ["Hlavní T1", "Hlavní T2", "Hlavní kvalita"]
    write_header(ws, row, header)
    hdr = row
    col = 4
    for t in tenants:
        if t.get("is_remainder"):
            for c in (col, col + 1):
                ws.cell(row=row, column=c).comment = Comment(REMAINDER_TARIFF_NOTE, "LKHC Odečty")
        col += 4
    for i, p in enumerate(doc["periods"]):
        row += 1
        ws.cell(row=row, column=1, value=p["label"])
        ws.cell(row=row, column=2, value=_date(p["start"]))
        ws.cell(row=row, column=3, value=_date(p["end"]))
        col = 4
        for t in tenants:
            cells = [*_tenant_regs(t, t["periods"]["T1"][i], t["periods"]["T2"][i]), t["period_totals"][i]]
            for q in cells:
                write_q(ws, row, col, q)
                col += 1
            ws.cell(row=row, column=col, value=quality_label(worst_quality(cells)))
            col += 1
        mcells = [doc["period_main"]["T1"][i], doc["period_main"]["T2"][i]]
        for q in mcells:
            write_q(ws, row, col, q)
            col += 1
        ws.cell(row=row, column=col, value=quality_label(worst_quality(mcells)))
    row += 1
    ws.cell(row=row, column=1, value="Celkem").font = BOLD
    col = 4
    for t in tenants:
        cells = [*_tenant_regs(t, t["totals"]["T1"], t["totals"]["T2"]), t["total"]]
        for q in cells:
            write_q(ws, row, col, q)
            ws.cell(row=row, column=col).font = BOLD
            col += 1
        ws.cell(row=row, column=col, value=quality_label(worst_quality(cells)))
        col += 1
    for reg in ("T1", "T2"):
        write_q(ws, row, col, doc["main"][reg])
        col += 1
    ws.cell(row=row, column=col, value=quality_label(worst_quality(doc["main"].values())))
    ws.freeze_panes = ws.cell(row=hdr + 1, column=2)
    row += 2
    check = doc.get("check", {})
    if check.get("provisional"):
        ws.cell(row=row, column=1, value="Předběžné stavy na hranicích:").font = BOLD
        for msg in check["provisional"]:
            row += 1
            ws.cell(row=row, column=1, value=msg)
        row += 2
    autosize(ws)

    for td in tenants:
        ws = wb.create_sheet(sheet_title(td["name"], used))
        row = _info_rows(ws, s, doc, f"{s.name} – {td['name']}")
        write_header(ws, row, ["Tarif", "kWh", "Kvalita", "Odhadnuto kWh", "Původ"])
        t1, t2 = _tenant_regs(td, td["totals"]["T1"], td["totals"]["T2"])
        for reg, q in (("T1", t1), ("T2", t2), ("Celkem", td["total"])):
            row += 1
            ws.cell(row=row, column=1, value=reg)
            if q is None:  # remainder tenant: only T1+T2
                ws.cell(row=row, column=5, value="jen v součtu T1+T2: " + REMAINDER_TARIFF_NOTE)
                continue
            _write_q_with_quality(ws, row, 2, q)
            marked = is_marked(q["quality"])
            ws.cell(row=row, column=4, value=to_num(q.get("estimated_kwh")) if marked else None)
            ws.cell(row=row, column=5, value=plain(q.get("title")).replace("\n", "; ") if marked else "")
        row += 2
        meters = _protocol_meters(doc, td)
        ws.cell(row=row, column=1, value="Hlavní elektroměr" if td.get("is_remainder") else "Elektroměry").font = BOLD
        row += 1
        write_header(
            ws,
            row,
            [
                "Místo",
                "Výr. číslo",
                "Tarif",
                "Počáteční stav",
                "Kvalita",
                "Původ",
                "Konečný stav",
                "Kvalita",
                "Původ",
                "Spotřeba",
                "Kvalita",
                "Instalován",
                "Odebrán",
                "Poznámka",
            ],
        )
        for m in meters:
            row += 1
            vals = [m["point_name"], m["serial"], m["register"]]
            for j, v in enumerate(vals, start=1):
                ws.cell(row=row, column=j, value=v)
            for col, b in ((4, m["start"]), (7, m["end"])):
                write_q(
                    ws,
                    row,
                    col,
                    {"value": b["value"], "quality": b["quality"], "title": b.get("title", "")},
                    "#,##0.###",
                )
                ws.cell(row=row, column=col + 1, value=quality_label(b["quality"]))
                ws.cell(row=row, column=col + 2, value=plain(b.get("title", "")))
            _write_q_with_quality(ws, row, 10, m["consumption"])
            ws.cell(row=row, column=12, value=_date(m.get("installed_on")))
            ws.cell(row=row, column=13, value=_date(m.get("removed_on")))
            ws.cell(row=row, column=14, value=m.get("note") or "")
        row += 2
        if td.get("is_remainder") and td.get("remainder"):
            rem = td["remainder"]
            ws.cell(row=row, column=1, value="Výpočet zbytku").font = BOLD
            row += 1
            # T1+T2 only: per tariff the remainder is distorted by single-tariff sub-meters
            write_header(ws, row, ["Složka", "kWh (T1+T2)", "Kvalita"])
            totals = {t["id"]: t["total"] for t in tenants}
            lines = [("Hlavní elektroměr", doc.get("main_total"))]
            lines += [(f"− {o['name']}", totals.get(o["tenant_id"])) for o in rem["others"]]
            lines.append((f"= {td['name']}", td["total"]))
            for label, total in lines:
                row += 1
                ws.cell(row=row, column=1, value=label)
                if total is None:
                    continue
                q = dict(total)
                q["value"] = d(q["value"]) * (-1 if label.startswith("−") else 1)
                _write_q_with_quality(ws, row, 2, q)
            if rem.get("unallocated"):
                row += 2
                ws.cell(row=row, column=1, value="Z toho nealokovaná místa (informativně)").font = BOLD
                row += 1
                write_header(ws, row, ["Místo", "T1", "Kvalita", "T2", "Kvalita"])
                for u in rem["unallocated"]:
                    row += 1
                    ws.cell(row=row, column=1, value=u["point_name"])
                    _write_q_with_quality(ws, row, 2, u["values"]["T1"])
                    _write_q_with_quality(ws, row, 4, u["values"]["T2"])
        elif td.get("allocations"):
            ws.cell(row=row, column=1, value="Rozpočet po měřicích místech").font = BOLD
            row += 1
            write_header(
                ws,
                row,
                [
                    "Místo",
                    "Koeficient",
                    "Platnost od",
                    "Platnost do",
                    "T1",
                    "Kvalita",
                    "T2",
                    "Kvalita",
                    "Celkem",
                    "Kvalita",
                ],
            )
            for a in td["allocations"]:
                row += 1
                ws.cell(row=row, column=1, value=a["point_name"])
                ws.cell(row=row, column=2, value=to_num(a["factor"]))
                ws.cell(row=row, column=3, value=_date(a["window"][0]))
                ws.cell(row=row, column=4, value=_date(a["window"][1]))
                _write_q_with_quality(ws, row, 5, a["values"]["T1"])
                _write_q_with_quality(ws, row, 7, a["values"]["T2"])
                _write_q_with_quality(ws, row, 9, a["total"])
        row += 2
        ws.cell(row=row, column=1, value="Po obdobích").font = BOLD
        row += 1
        write_header(ws, row, ["Období", "T1", "Kvalita", "T2", "Kvalita", "Celkem", "Kvalita"])
        for i, p in enumerate(doc["periods"]):
            row += 1
            ws.cell(row=row, column=1, value=p["label"])
            if not td.get("is_remainder"):  # remainder: only T1+T2
                _write_q_with_quality(ws, row, 2, td["periods"]["T1"][i])
                _write_q_with_quality(ws, row, 4, td["periods"]["T2"][i])
            _write_q_with_quality(ws, row, 6, td["period_totals"][i])
        autosize(ws)
    fname = f"vyuctovani-{s.year or s.period_start.year}-{'final' if s.status == 'final' else 'navrh'}.xlsx"
    return download(workbook_bytes(wb), fname, XLSX_MIME)
