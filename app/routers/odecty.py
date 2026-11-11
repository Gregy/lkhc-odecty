"""Readings grid, manual entry, batch photo upload and the review queue (SPEC §5)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import audit
from app.capture.meters import create_meter
from app.capture.photos import ImageRejected, read_upload, store_photo
from app.capture.plausibility import Issue, ack_note, check_value, missing_acks
from app.capture.readings import (
    InputError,
    day_start,
    existing_reading,
    parse_day,
    parse_value,
    write_reading,
)
from app.capture.review import (
    DOUBT_MESSAGES,
    Card,
    build_cards,
    bulk_confirm,
    confirm_card,
    discard_photos,
    display_value,
    pending_readings,
    photo_problems,
    plain_value,
    rows_from_form,
)
from app.deps import AdminUser, CurrentUser, DbSession, require_user
from app.extraction import queue as extraction_queue
from app.extraction.images import MAX_UPLOAD_BYTES
from app.extraction.types import problem_label
from app.models import AuditLog, Meter, MeterPoint, Photo, Reading
from app.services.network import load_network
from app.services.readings import meter_expected_range
from app.templating import flash, format_reading, templates

router = APIRouter(prefix="/odecty", dependencies=[Depends(require_user)])

SOURCE_SHORT = {"manual": "R", "photo": "F", "import": "I", "live": "D"}
MAX_FILES_PER_REQUEST = 100


def _is_htmx(request: Request) -> bool:
    return bool(request.headers.get("hx-request"))


def _meter_options(db: DbSession) -> list[dict[str, Any]]:
    """Meters grouped by point (active first) for selects."""
    out = []
    for p in db.scalars(select(MeterPoint).order_by(MeterPoint.sort_order, MeterPoint.id)):
        for m in p.meters:
            label = f"{p.name} – {m.serial}" + (" (odebrán)" if m.removed_on else "")
            out.append(
                {
                    "id": m.id,
                    "label": label,
                    "registers": m.registers,
                    "active": m.removed_on is None and p.active,
                    "point_id": p.id,
                    "point": p.name,
                    "serial": m.serial,
                    "decimals": m.decimals_shown,
                }
            )
    out.sort(key=lambda o: not o["active"])
    return out


# --- readings grid ------------------------------------------------------------------------------


@router.get("", name="odecty")
def odecty(request: Request, db: DbSession, rok: str | None = None, odebrane: int = 0):
    years = sorted(
        {int(y) for (y,) in db.execute(select(func.strftime("%Y", Reading.taken_at)).distinct()) if y}, reverse=True
    )
    today = date.today()
    if rok and rok.isdigit():
        start, end = date(int(rok), 1, 1), date(int(rok) + 1, 1, 1)
        title = f"rok {rok}"
    elif rok == "vse":
        start, end = date(1900, 1, 1), date(9999, 1, 1)
        title = "vše"
    else:
        start, end = today - timedelta(days=400), today + timedelta(days=1)
        title = "posledních 13 měsíců"
    net = load_network(db, include_pending=True)
    readings = list(
        db.scalars(
            select(Reading)
            .where(Reading.taken_at >= day_start(start), Reading.taken_at < day_start(end))
            .order_by(Reading.taken_at.desc())
        )
    )
    with_data = {r.meter_id for r in readings}
    columns = [c for c in net.columns() if odebrane or net.meters[c.meter_id].active or c.meter_id in with_data]
    rows: dict[date, dict[tuple[int, str], Reading]] = defaultdict(dict)
    for r in readings:
        rows[r.taken_at.date()][(r.meter_id, r.register)] = r
    days = sorted(rows, reverse=True)
    # group header: one cell per meter spanning its registers
    groups: list[dict[str, Any]] = []
    for c in columns:
        if groups and groups[-1]["meter_id"] == c.meter_id:
            groups[-1]["span"] += 1
        else:
            m = net.meters[c.meter_id]
            groups.append(
                {"meter_id": c.meter_id, "serial": c.serial, "point": c.point_name, "span": 1, "active": m.active}
            )
    return templates.TemplateResponse(
        request,
        "odecty/grid.html",
        {
            "page_title": "Odečty",
            "active_nav": "/odecty",
            "columns": columns,
            "groups": groups,
            "days": days,
            "rows": rows,
            "years": years,
            "rok": rok or "",
            "range_title": title,
            "odebrane": odebrane,
            "source_short": SOURCE_SHORT,
            "decimals": {mid: m.decimals_shown for mid, m in net.meters.items()},
        },
    )


def _cell_context(
    db: DbSession,
    meter: Meter,
    register: str,
    day: date,
    *,
    value_raw: str | None = None,
    issues: list[Issue] | None = None,
    acks: set[str] | None = None,
    errors: list[str] | None = None,
    saved: str | None = None,
    note: str | None = None,
    reported_by: str | None = None,
) -> dict[str, Any]:
    reading = existing_reading(db, meter.id, register, day)
    history = []
    if reading is not None:
        history = list(
            db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "readings", AuditLog.entity_id == reading.id)
                .order_by(AuditLog.at.desc())
                .limit(20)
            )
        )
    er = meter_expected_range(db, meter.id, register, day)
    return {
        "meter": meter,
        "register": register,
        "day": day,
        "reading": reading,
        "value_raw": value_raw if value_raw is not None else ("" if reading is None else _plain(reading.value)),
        "note": note if note is not None else (reading.note if reading else ""),
        "reported_by": reported_by if reported_by is not None else (reading.reported_by if reading else ""),
        "issues": issues or [],
        "acks": acks or set(),
        "errors": errors or [],
        "history": history,
        "expected": er,
        "saved": saved,
        "source_short": SOURCE_SHORT,
    }


def _readings_word(n: int) -> str:
    """Czech plural: 1 odečet, 2–4 odečty, 0 / 5+ odečtů."""
    return "odečet" if n == 1 else ("odečty" if 2 <= n <= 4 else "odečtů")


def _plain(v: Any) -> str:
    s = format(v, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


@router.get("/bunka", name="odecty_bunka")
def bunka(request: Request, db: DbSession, user: AdminUser, meter_id: int, register: str, den: str):
    meter = db.get(Meter, meter_id)
    if meter is None or register not in ("T1", "T2"):
        raise HTTPException(404)
    try:
        day = parse_day(den)
    except InputError as exc:
        raise HTTPException(400, str(exc)) from None
    assert day is not None
    return templates.TemplateResponse(request, "odecty/_cell_editor.html", _cell_context(db, meter, register, day))


@router.post("/bunka", name="odecty_bunka_ulozit")
async def bunka_ulozit(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    try:
        meter = db.get(Meter, int(str(form.get("meter_id"))))
        register = str(form.get("register"))
        day = parse_day(str(form.get("den") or ""))
    except ValueError, TypeError, InputError:
        raise HTTPException(400, "Neplatný požadavek.") from None
    if meter is None or register not in ("T1", "T2") or day is None:
        raise HTTPException(404)
    action = str(form.get("action") or "save")
    raw = str(form.get("value") or "")
    note = str(form.get("note") or "").strip()
    reported_by = str(form.get("reported_by") or "").strip()[:200]
    acks = {str(a) for a in form.getlist("ack")}
    reading = existing_reading(db, meter.id, register, day)
    ctx_extra: dict[str, Any] = {"value_raw": raw, "note": note, "reported_by": reported_by, "acks": acks}
    if action == "delete":
        if reading is None:
            raise HTTPException(404)
        audit.record_delete(db, user, reading, note=str(form.get("reason") or ""))
        db.delete(reading)
        db.commit()
        ctx = _cell_context(db, meter, register, day, saved="Odečet smazán.", value_raw="")
        return _cell_response(request, ctx)
    try:
        value = parse_value(raw)
    except InputError as exc:
        return templates.TemplateResponse(
            request, "odecty/_cell_editor.html", _cell_context(db, meter, register, day, errors=[str(exc)], **ctx_extra)
        )
    if value is None:
        return templates.TemplateResponse(
            request,
            "odecty/_cell_editor.html",
            _cell_context(db, meter, register, day, errors=["Zadejte hodnotu (nebo odečet smažte)."], **ctx_extra),
        )
    issues = check_value(db, meter, register, value, day, exclude_reading_id=reading.id if reading else None)
    missing = missing_acks(issues, acks)
    if missing:
        return templates.TemplateResponse(
            request, "odecty/_cell_editor.html", _cell_context(db, meter, register, day, issues=issues, **ctx_extra)
        )
    if reading is None:
        write_reading(
            db,
            user,
            meter_id=meter.id,
            register=register,
            day=day,
            value=value,
            source="manual",
            reported_by=reported_by,
            note=note,
            audit_note=ack_note(issues),
        )
    else:
        values: dict[str, object] = {"value": value, "note": note, "reported_by": reported_by, "status": "confirmed"}
        if value != reading.value and reading.source == "import":
            values["source"] = "manual"  # corrected in the app: a re-import must not revert it
        changes = audit.apply_changes(reading, values)
        if changes:
            audit.record_update(db, user, reading, changes, note=ack_note(issues))
    db.commit()
    ctx = _cell_context(db, meter, register, day, saved="Uloženo.")
    return _cell_response(request, ctx)


def _cell_response(request: Request, ctx: dict[str, Any]) -> Response:
    ctx["oob_cell"] = True
    return templates.TemplateResponse(request, "odecty/_cell_editor.html", ctx)


# --- manual entry ---------------------------------------------------------------------------------


@router.get("/novy", name="odecty_novy")
def novy(request: Request, db: DbSession, user: AdminUser, meter_id: int | None = None, den: str | None = None):
    return templates.TemplateResponse(
        request,
        "odecty/novy.html",
        {
            "page_title": "Nový odečet",
            "active_nav": "/odecty",
            "meters": _meter_options(db),
            "form": {
                "meter_id": meter_id,
                "den": den or "",
                "T1": "",
                "T2": "",
                "reported_by": "",
                "note": "",
            },
            "issues": {},
            "errors": [],
            "acks": set(),
        },
    )


@router.post("/novy", name="odecty_novy_ulozit")
async def novy_ulozit(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    f = {k: str(form.get(k) or "").strip() for k in ("meter_id", "den", "T1", "T2", "reported_by", "note")}
    acks = {str(a) for a in form.getlist("ack")}
    errors: list[str] = []
    issues: dict[str, list[Issue]] = {}
    meter = db.get(Meter, int(f["meter_id"])) if f["meter_id"].isdigit() else None
    day = None
    if meter is None:
        errors.append("Vyberte elektroměr.")
    try:
        day = parse_day(f["den"])
        if day is None:
            errors.append("Zadejte skutečné datum odečtu.")
        elif day > date.today():
            errors.append("Datum nesmí být v budoucnosti.")
    except InputError as exc:
        errors.append(str(exc))
    values: dict[str, Any] = {}
    for reg in ("T1", "T2"):
        try:
            v = parse_value(f[reg])
        except InputError as exc:
            errors.append(f"{reg}: {exc}")
            continue
        if v is None:
            continue
        if meter is not None and reg not in meter.registers:
            errors.append(f"Elektroměr nemá registr {reg}.")
            continue
        values[reg] = v
    if not values and not errors:
        errors.append("Zadejte alespoň jednu hodnotu.")
    if not errors and meter is not None and day is not None:
        for reg, v in values.items():
            iss = check_value(db, meter, reg, v, day)
            cur = existing_reading(db, meter.id, reg, day)
            if cur is not None and cur.value != v:
                iss.append(
                    Issue(
                        "duplicate",
                        "warning",
                        f"Pro tento den už je zapsáno {format_reading(cur.value)}\xa0kWh – potvrzením se přepíše.",
                    )
                )
            if iss:
                issues[reg] = iss
        if any(missing_acks(i, acks) for i in issues.values()):
            errors.append("Zkontrolujte varování a potvrďte je.")
    if errors:
        return templates.TemplateResponse(
            request,
            "odecty/novy.html",
            {
                "page_title": "Nový odečet",
                "active_nav": "/odecty",
                "meters": _meter_options(db),
                "form": f,
                "issues": issues,
                "errors": errors,
                "acks": acks,
            },
            status_code=422,
        )
    assert meter is not None and day is not None
    for reg, v in values.items():
        write_reading(
            db,
            user,
            meter_id=meter.id,
            register=reg,
            day=day,
            value=v,
            source="manual",
            reported_by=f["reported_by"][:200],
            note=f["note"],
            audit_note=ack_note(issues.get(reg, [])),
            overwrite="duplicate" in acks,
        )
    db.commit()
    flash(request, f"Odečet {meter.serial} k {day.day}. {day.month}. {day.year} uložen.", "success")
    return RedirectResponse(f"/odecty?rok={day.year}#d-{day.isoformat()}", status_code=303)


@router.get("/historie", name="odecty_historie")
def historie(request: Request, db: DbSession, strana: int = 1):
    q = select(AuditLog).where(AuditLog.entity_type.in_(("readings", "photos", "meters", "point_skips")))
    entries = list(
        db.scalars(q.order_by(AuditLog.at.desc(), AuditLog.id.desc()).offset((max(strana, 1) - 1) * 100).limit(100))
    )
    reading_ids = [e.entity_id for e in entries if e.entity_type == "readings" and e.entity_id]
    readings = {r.id: r for r in db.scalars(select(Reading).where(Reading.id.in_(reading_ids)))}
    meters = {m.id: m for m in db.scalars(select(Meter))}
    return templates.TemplateResponse(
        request,
        "odecty/historie.html",
        {
            "page_title": "Historie změn",
            "active_nav": "/odecty",
            "entries": entries,
            "readings": readings,
            "meters": meters,
            "strana": max(strana, 1),
        },
    )


# --- batch upload ------------------------------------------------------------------------------------


@router.get("/davka", name="odecty_davka")
def davka(request: Request, db: DbSession, user: CurrentUser):
    recent = list(db.scalars(select(Photo).where(Photo.source == "upload").order_by(Photo.id.desc()).limit(12)))
    return templates.TemplateResponse(
        request,
        "odecty/davka.html",
        {
            "page_title": "Dávka fotek",
            "active_nav": "/odecty/davka",
            "recent": recent,
            "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "extraction_on": extraction_queue.extraction_enabled(request.app),
        },
    )


def upload_result(sr_photo: Photo, created: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "id": sr_photo.id,
        "uuid": sr_photo.uuid,
        "created": created,
        "taken_at": sr_photo.taken_at.isoformat() if sr_photo.taken_at else None,
        "exif": sr_photo.exif_taken_at is not None,
        "series_id": sr_photo.series_id,
        "extraction_status": sr_photo.extraction_status,
        "url": f"/fotky/{sr_photo.id}",
    }


@router.post("/davka", name="odecty_davka_nahrat")
async def davka_nahrat(request: Request, db: DbSession, user: AdminUser):
    length = int(request.headers.get("content-length") or 0)
    if length > MAX_UPLOAD_BYTES * MAX_FILES_PER_REQUEST:
        raise HTTPException(413, "Požadavek je příliš velký.")
    form = await request.form(max_files=MAX_FILES_PER_REQUEST)
    reported_by = str(form.get("reported_by") or "").strip()
    source = "share" if form.get("source") == "share" else "upload"
    files = [f for f in form.getlist("files") if isinstance(f, UploadFile) or hasattr(f, "read")]
    results: list[dict[str, Any]] = []
    enabled = extraction_queue.extraction_enabled(request.app)
    client_uuid = str(form.get("uuid") or "") or None if len(files) == 1 else None
    for up in files:
        name = getattr(up, "filename", "") or ""
        try:
            data = await read_upload(up)
            sr = store_photo(
                db,
                request.app.state.settings,
                data,
                user=user,
                filename=name,
                source=source,
                client_uuid=client_uuid,
                reported_by=reported_by,
                extraction_enabled=enabled,
            )
            db.commit()
            res = upload_result(sr.photo, sr.created)
            res["filename"] = name
            if sr.duplicate_of is not None:
                res["duplicate"] = True
            results.append(res)
        except ImageRejected as exc:
            db.rollback()
            results.append({"ok": False, "filename": name, "error": exc.message})
        except IntegrityError:
            db.rollback()
            results.append({"ok": False, "filename": name, "error": "Souběžné nahrání téže fotky, zkuste znovu."})
    extraction_queue.notify(request.app)
    if "application/json" in request.headers.get("accept", ""):
        status = (
            200 if all(r["ok"] for r in results) and results else (400 if not any(r["ok"] for r in results) else 207)
        )
        return JSONResponse({"results": results}, status_code=status)
    ok = sum(1 for r in results if r["ok"])
    bad = [f"{r['filename']}: {r['error']}" for r in results if not r["ok"]]
    if ok:
        flash(request, f"Nahráno fotek: {ok}.", "success")
    for b in bad:
        flash(request, b, "error")
    return RedirectResponse("/odecty/kontrola" if ok else "/odecty/davka", status_code=303)


# --- review queue ---------------------------------------------------------------------------------------


def _review_context(request: Request, db: DbSession) -> dict[str, Any]:
    return {
        "meters": _meter_options(db),
        "points": list(db.scalars(select(MeterPoint).order_by(MeterPoint.sort_order))),
        "problem_label": problem_label,
        "photo_problems": photo_problems,
        "doubt_messages": DOUBT_MESSAGES,
        "today": date.today(),
        "fmtv": display_value,
        "plain": plain_value,
    }


@router.get("/kontrola", name="odecty_kontrola")
def kontrola(request: Request, db: DbSession, user: CurrentUser):
    cards = build_cards(db)
    pend = pending_readings(db)
    ctx = _review_context(request, db)
    ctx.update(
        {
            "page_title": "Kontrola odečtů",
            "active_nav": "/odecty/kontrola",
            "cards": cards,
            "pending": pend,
            "clean_count": sum(1 for c in cards if c.clean),
            "extraction_on": extraction_queue.extraction_enabled(request.app),
            "queued": db.scalar(select(func.count(Photo.id)).where(Photo.extraction_status == "queued")) or 0,
        }
    )
    return templates.TemplateResponse(request, "odecty/kontrola.html", ctx)


def _render_card(request: Request, db: DbSession, card: Card, status_code: int = 200, **extra: Any) -> Response:
    ctx = _review_context(request, db)
    ctx.update({"card": card, **extra})
    return templates.TemplateResponse(request, "odecty/_card.html", ctx, status_code=status_code)


@router.get("/kontrola/karta/{key}", name="odecty_karta")
def karta(request: Request, key: str, db: DbSession, user: CurrentUser):
    cards = build_cards(db, keys=[key])
    if not cards:
        return Response("", status_code=200)
    return _render_card(request, db, cards[0])


@router.post("/kontrola/potvrdit", name="odecty_potvrdit")
async def potvrdit(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    key = str(form.get("card") or "")
    cards = build_cards(db, keys=[key])
    if not cards:
        flash(request, "Karta už není ve frontě (možná ji někdo mezitím potvrdil).", "warning")
        if _is_htmx(request):
            return Response("", headers={"HX-Refresh": "true"})
        return RedirectResponse("/odecty/kontrola", status_code=303)
    card = cards[0]
    posted_ids = str(form.get("photo_ids") or "")
    if posted_ids != ",".join(str(i) for i in card.photo_ids):
        card.errors.append("Do karty mezitím přibyly nebo z ní ubyly fotky – zkontrolujte ji znovu.")
        return _render_card(request, db, card, status_code=409)
    rows = rows_from_form(db, card, form, form.getlist)
    out = confirm_card(
        db,
        user,
        card,
        rows,
        day_raw=str(form.get("den") or "") or None,
        reported_by=str(form.get("reported_by") or ""),
        note=str(form.get("note") or ""),
    )
    if not out.ok:
        db.rollback()
        return _render_card(
            request,
            db,
            card,
            status_code=422,
            posted=True,
            form_day=str(form.get("den") or ""),
            form_reported_by=str(form.get("reported_by") or ""),
        )
    db.commit()
    msg = (
        f"Potvrzeno: zapsáno {out.written} {_readings_word(out.written)}"
        + (f", {out.unchanged} už bylo zapsáno" if out.unchanged else "")
        + "."
    )
    if _is_htmx(request):
        return templates.TemplateResponse(request, "odecty/_card_done.html", {"message": msg, "card": card})
    flash(request, msg, "success")
    return RedirectResponse("/odecty/kontrola", status_code=303)


@router.post("/kontrola/hromadne", name="odecty_hromadne")
async def hromadne(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    keys = [str(k) for k in form.getlist("card")]
    photo_sets = {k: str(form.get(f"photos_{k}") or "") for k in keys}
    done, written, skipped = bulk_confirm(db, user, keys, photo_sets)
    flash(request, f"Hromadně potvrzeno karet: {done} (odečtů: {written}).", "success" if done else "info")
    for s in skipped[:10]:
        flash(request, f"Přeskočeno – {s}", "warning")
    return RedirectResponse("/odecty/kontrola", status_code=303)


@router.post("/kontrola/zahodit", name="odecty_zahodit")
async def zahodit(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    ids = [int(x) for x in str(form.get("photo_ids") or "").split(",") if x.strip().isdigit()]
    n = discard_photos(db, user, ids)
    db.commit()
    msg = f"Zahozeno fotek: {n} (najdete je ve Fotkách)."
    if _is_htmx(request):
        return templates.TemplateResponse(request, "odecty/_card_done.html", {"message": msg, "card": None})
    flash(request, msg, "info")
    return RedirectResponse("/odecty/kontrola", status_code=303)


def _reg_values(form: Any, prefix: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for reg in ("T1", "T2"):
        v = parse_value(str(form.get(prefix + reg) or ""))
        if v is not None:
            out[reg] = v
    return out


@router.post("/kontrola/elektromer", name="odecty_novy_elektromer")
async def novy_elektromer(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    try:
        installed = parse_day(str(form.get("installed_on") or "")) if form.get("installed_on") else None
        replace = str(form.get("replace_meter_id") or "")
        point = str(form.get("point_id") or "")
        meter = create_meter(
            db,
            user,
            serial=str(form.get("serial") or ""),
            point_id=int(point) if point.isdigit() else None,
            new_point_name=str(form.get("new_point_name") or ""),
            registers=[str(r) for r in form.getlist("registers")],
            digits=int(str(form.get("digits") or 5)),
            decimals_shown=int(str(form.get("decimals_shown") or 0)),
            installed_on=installed,
            replace_meter_id=int(replace) if replace.isdigit() else None,
            display_note=str(form.get("display_note") or ""),
            initial=_reg_values(form, "initial_"),
            old_final=_reg_values(form, "old_final_"),
        )
        db.commit()
        flash(
            request, f"Elektroměr {meter.serial} založen. Fotky se k němu teď přiřadí podle výrobního čísla.", "success"
        )
    except (InputError, ValueError) as exc:
        db.rollback()
        flash(request, str(exc) or "Neplatné údaje.", "error")
    except IntegrityError:
        db.rollback()
        flash(request, "Elektroměr s tímto číslem už existuje.", "error")
    back = str(form.get("back") or "")
    return RedirectResponse(
        "/odecty/kontrola" + (f"#karta-{back}" if back.replace("_", "").isalnum() else ""), status_code=303
    )


@router.post("/kontrola/cekajici/{reading_id}", name="odecty_cekajici")
async def cekajici(request: Request, reading_id: int, db: DbSession, user: AdminUser):
    """Pending reading (from the walk with unacknowledged warnings): confirm, correct or delete."""
    form = await request.form()
    r = db.get(Reading, reading_id)
    if r is None or r.status != "pending":
        flash(request, "Odečet už není nepotvrzený.", "warning")
        return RedirectResponse("/odecty/kontrola#cekajici", status_code=303)
    action = str(form.get("action") or "confirm")
    if action == "delete":
        audit.record_delete(db, user, r, note="nepotvrzený odečet smazán v kontrole")
        db.delete(r)
        db.commit()
        flash(request, "Nepotvrzený odečet smazán.", "info")
        return RedirectResponse("/odecty/kontrola#cekajici", status_code=303)
    try:
        parsed = parse_value(str(form.get("value") or ""))
        value = r.value if parsed is None else parsed
    except InputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/odecty/kontrola#cekajici", status_code=303)
    issues = check_value(db, r.meter, r.register, value, r.taken_at, exclude_reading_id=r.id)
    acks = {str(a) for a in form.getlist("ack")}
    missing = missing_acks(issues, acks)
    if missing:
        flash(
            request,
            f"{r.meter.serial} {r.register}: potvrďte varování – " + "; ".join(i.message for i in missing),
            "error",
        )
        return RedirectResponse("/odecty/kontrola#cekajici", status_code=303)
    ch = audit.apply_changes(r, {"value": value, "status": "confirmed"})
    audit.record_update(db, user, r, ch, note=ack_note(issues) or "potvrzeno v kontrole")
    db.commit()
    flash(request, f"Odečet {r.meter.serial} {r.register} potvrzen.", "success")
    return RedirectResponse("/odecty/kontrola#cekajici", status_code=303)
