"""Walk PWA (`/obchuzka/`, field capture): app shell, service worker, manifest and the JSON API it syncs with.

Offline-first: the page keeps a queue in IndexedDB and replays it here. Every write is
idempotent on client-generated UUIDs (photo ``uuid``; entry ``uuid`` -> ``Reading.client_ref``),
so a retried sync cannot create duplicates.

Web Share Target: the manifest's ``share_target`` POSTs to ``/obchuzka/sdilet``. The installed
PWA's service worker intercepts that navigation, stores the files in IndexedDB and redirects
to the app, which uploads them through the normal authenticated + CSRF-protected
``/obchuzka/api/foto``. The server-side handler below only runs when no service worker
handled the POST; it is CSRF-exempt *because it does not change anything* (it discards the
body and redirects with an explanation). See ``CSRF_EXEMPT_PATHS`` (used in main.py).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import audit
from app.capture.photos import ImageRejected, read_upload, store_photo, valid_uuid
from app.capture.plausibility import check_value, missing_acks
from app.capture.readings import InputError, day_start, parse_value, write_reading
from app.csrf import get_csrf_token
from app.deps import AdminUser, CurrentUser, DbSession, require_user
from app.domain.anomalies import expected_range
from app.extraction import queue as extraction_queue
from app.extraction.hints import meter_hint_list
from app.extraction.matching import apply_matching
from app.extraction.types import ExtractionResult, problem_label
from app.models import Meter, MeterPoint, Photo, PointSkip, Reading
from app.services.network import load_network
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/obchuzka")

SHARE_PATH = "/obchuzka/sdilet"
CSRF_EXEMPT_PATHS = frozenset({SHARE_PATH})
STATIC = Path(__file__).resolve().parent.parent / "static" / "obchuzka"
APP_VERSION = "2"  # bump together with CACHE_VERSION in sw.js when the shell changes


def _s(v: Decimal | None) -> str | None:
    if v is None:
        return None
    s = format(v, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


# --- shell / PWA plumbing ------------------------------------------------------------------


@router.get("", include_in_schema=False, dependencies=[Depends(require_user)])
def obchuzka_redirect() -> Response:
    return RedirectResponse("/obchuzka/", status_code=307)


@router.get("/", name="obchuzka")
def obchuzka(request: Request, user: CurrentUser) -> Response:
    resp = templates.TemplateResponse(
        request,
        "obchuzka/app.html",
        {"page_title": "Obchůzka", "active_nav": "/obchuzka", "app_version": APP_VERSION},
    )
    resp.headers["Cache-Control"] = "no-cache, private"
    resp.headers["Vary"] = "Cookie"
    return resp


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    return FileResponse(
        STATIC / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/obchuzka/"},
    )


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> Response:
    return FileResponse(
        STATIC / "manifest.webmanifest", media_type="application/manifest+json", headers={"Cache-Control": "no-cache"}
    )


@router.post(SHARE_PATH.removeprefix("/obchuzka"), include_in_schema=False)
def share_fallback() -> Response:
    """Share-target POST that no service worker handled. Deliberately non-mutating (CSRF-exempt):
    the body is ignored and the user is sent to the app, which explains how to retry."""
    return RedirectResponse("/obchuzka/?sdileni=nezpracovano", status_code=303)


# --- API: state -------------------------------------------------------------------------------


@router.get("/api/stav", dependencies=[Depends(require_user)])
def api_stav(request: Request, user: CurrentUser) -> JSONResponse:
    return JSONResponse(
        {
            "user": user.display_name,
            "email": user.email,
            "is_admin": user.is_admin,
            "csrf_token": get_csrf_token(request),
        },
        headers={"Cache-Control": "no-store"},
    )


def _range_dict(er: Any) -> dict[str, Any]:
    if er is None:
        return {}
    return {
        "low": _s(er.low),
        "expected": _s(er.expected),
        "high": _s(er.high),
        "daily_rate": _s(er.daily_rate),
        "days": _s(er.days),
    }


@router.get("/api/body", dependencies=[Depends(require_user)])
def api_body(request: Request, db: DbSession, user: CurrentUser, den: str | None = None) -> JSONResponse:
    """Active points in walk order with meters, last readings, expected ranges and today's state."""
    try:
        day = date.fromisoformat(den) if den else date.today()
    except ValueError:
        raise HTTPException(400, "Neplatné datum.") from None
    net = load_network(db)
    at = day_start(day)
    todays = {(r.meter_id, r.register): r for r in db.scalars(select(Reading).where(Reading.taken_at == at))}
    skips = {s.meter_point_id: s for s in db.scalars(select(PointSkip).where(PointSkip.day == day))}
    points = []
    for p in net.points:
        if not p.active:
            continue
        meters = []
        for mid in p.meter_ids:
            m = net.meters[mid]
            if not m.active:
                continue
            regs = {}
            for reg in m.registers:
                series = [r for r in net.series[mid].readings.get(reg, ()) if r.taken_at < at]
                last = series[-1] if series else None
                er = expected_range(series, at, m.digits) if series else None
                today = todays.get((mid, reg))
                regs[reg] = {
                    "last": _s(last.value) if last else None,
                    "last_date": last.taken_at.date().isoformat() if last else None,
                    **_range_dict(er),
                    "today": None
                    if today is None
                    else {"value": _s(today.value), "status": today.status, "source": today.source},
                }
            meters.append(
                {
                    "id": mid,
                    "serial": m.serial,
                    "registers": list(m.registers),
                    "digits": m.digits,
                    "decimals_shown": m.decimals_shown,
                    "display_note": m.display_note,
                    "note": m.note,
                    "regs": regs,
                }
            )
        if not meters:
            continue
        skip = skips.get(p.id)
        points.append(
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "role": p.role,
                "sort_order": p.sort_order,
                "skipped": {"note": skip.note} if skip else None,
                "meters": meters,
            }
        )
    return JSONResponse(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "day": day.isoformat(),
            "user": user.display_name,
            "is_admin": user.is_admin,
            "csrf_token": get_csrf_token(request),
            "extraction": extraction_queue.extraction_enabled(request.app),
            "points": points,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/kontrola", dependencies=[Depends(require_user)])
def api_kontrola(db: DbSession, meter_id: int, register: str, value: str, den: str | None = None) -> JSONResponse:
    """Authoritative plausibility check of a value (read-only preview for the walk form)."""
    meter = db.get(Meter, meter_id)
    if meter is None:
        raise HTTPException(404)
    try:
        v = parse_value(value)
        day = date.fromisoformat(den) if den else date.today()
    except (InputError, ValueError) as exc:
        return JSONResponse({"error": str(exc), "issues": []}, status_code=400)
    if v is None:
        return JSONResponse({"issues": []})
    return JSONResponse({"issues": [i.as_dict() for i in check_value(db, meter, register, v, day)]})


# --- API: photos -------------------------------------------------------------------------------


def _parse_client_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    now = datetime.now()
    if t > now + timedelta(hours=1) or t < now - timedelta(days=366):
        return None
    return t


@router.post("/api/foto")
async def api_foto(request: Request, db: DbSession, user: AdminUser) -> JSONResponse:
    """Upload one photo from the walk (or one shared photo). Idempotent on ``uuid``."""
    form = await request.form(max_files=1)
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"ok": False, "error": "Chybí soubor."}, status_code=400)
    cu = valid_uuid(str(form.get("uuid") or ""))
    if cu is None:
        return JSONResponse({"ok": False, "error": "Chybí nebo je neplatné uuid."}, status_code=400)
    source = "share" if form.get("source") == "share" else "walk"
    walk_meter = None
    point_raw = str(form.get("point_id") or "")
    if source == "walk" and point_raw.isdigit():
        walk_meter = db.scalar(
            select(Meter).where(Meter.meter_point_id == int(point_raw), Meter.removed_on.is_(None)).order_by(Meter.id)
        )
    assert isinstance(upload, UploadFile) or hasattr(upload, "filename")
    try:
        data = await read_upload(upload)  # type: ignore[arg-type]
        sr = store_photo(
            db,
            request.app.state.settings,
            data,
            user=user,
            filename=str(form.get("filename") or getattr(upload, "filename", "") or ""),
            source=source,
            client_uuid=cu,
            client_taken_at=_parse_client_time(form.get("taken_at")),
            reported_by=str(form.get("reported_by") or ""),
            walk_meter=walk_meter,
            extraction_enabled=extraction_queue.extraction_enabled(request.app),
        )
        db.commit()
    except ImageRejected as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": exc.message, "permanent": True}, status_code=422)
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Photo).where(Photo.uuid == cu))
        if existing is None:
            return JSONResponse({"ok": False, "error": "Souběžné nahrávání, zkuste znovu."}, status_code=409)
        sr_photo, created = existing, False
    else:
        sr_photo, created = sr.photo, sr.created
    extraction_queue.notify(request.app)
    return JSONResponse(
        {
            "ok": True,
            "uuid": cu,
            "id": sr_photo.id,
            "created": created,
            "same_as": sr_photo.uuid if sr_photo.uuid != cu else None,
            "extraction_status": sr_photo.extraction_status,
            "taken_at": sr_photo.taken_at.isoformat() if sr_photo.taken_at else None,
        }
    )


@router.get("/api/foto/{photo_uuid}", dependencies=[Depends(require_user)])
def api_foto_stav(photo_uuid: str, db: DbSession) -> JSONResponse:
    """Extraction state of a walk photo + proposed values for the point's meters (for pre-filling)."""
    cu = valid_uuid(photo_uuid)
    photo = db.scalar(select(Photo).where(Photo.uuid == cu)) if cu else None
    if photo is None:
        return JSONResponse({"status": "unknown"}, status_code=404)
    out: dict[str, Any] = {"status": photo.extraction_status, "id": photo.id, "values": {}, "problems": []}
    res = ExtractionResult.from_dict(photo.extraction_result)
    if photo.extraction_status == "failed":
        out["error"] = "Rozpoznání se nezdařilo – zadejte hodnoty ručně."
    if res is None or photo.extraction_status != "done":
        return JSONResponse(out)
    focus: tuple[int, ...] = ()
    if photo.meter_id is not None:
        m = db.get(Meter, photo.meter_id)
        if m is not None:
            focus = tuple(
                db.scalars(select(Meter.id).where(Meter.meter_point_id == m.meter_point_id, Meter.removed_on.is_(None)))
            )
    apply_matching(res, meter_hint_list(db, photo.taken_at), photo.taken_at, focus_meter_ids=focus)
    out["readable"] = res.readable
    out["problems"] = [problem_label(p) for p in res.problems]
    out["mismatch"] = []
    for readout in res.meters:
        if focus and readout.meter_id is not None and readout.meter_id not in focus:
            # the photo shows another known meter than the tapped point: say so, prefill nothing for it
            other = db.get(Meter, readout.meter_id)
            out["mismatch"].append(
                f"Na fotce je elektroměr {readout.serial or (other.serial if other else '?')}"
                f"{f' ({other.meter_point.name})' if other else ''}, ne elektroměr tohoto místa – zkontrolujte, "
                "co jste vyfotili."
            )
            continue
        if focus and readout.match_method == "unknown_serial":
            out["mismatch"].append(
                f"Na fotce je výrobní číslo {readout.serial}, které neznáme – nejde o jiný (nový) elektroměr?"
            )
            continue
        if readout.meter_id is None or (focus and readout.meter_id not in focus):
            continue
        vals = out["values"].setdefault(str(readout.meter_id), {})
        for rv in readout.registers:
            if rv.value is None or rv.register is None:
                continue
            entry = {
                "value": _s(rv.value),
                "confidence": rv.confidence,
                "flags": [problem_label(p) for p in rv.problems],
            }
            if rv.second_read is not None:  # EXTRACTOR_VERIFY: the second read did not reproduce the value
                entry["flags"].append(
                    f"druhé čtení „{rv.second_read}“" if rv.second_read else "druhé čtení nic nepřečetlo"
                )
            prev = vals.get(rv.register)
            if prev is not None and prev["value"] != entry["value"]:
                prev["conflict"] = True  # two different values for one register on one photo
                continue
            vals[rv.register] = entry
    return JSONResponse(out)


# --- API: entries ------------------------------------------------------------------------------


def _entry_error(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"status": "error", "message": msg, "permanent": True}, status_code=status)


@router.post("/api/zaznam")
async def api_zaznam(request: Request, db: DbSession, user: AdminUser) -> JSONResponse:
    """Store one walk entry (readings of one point, or a skip). Idempotent on the entry ``uuid``."""
    try:
        payload = await request.json()
    except ValueError:
        return _entry_error("Neplatná data.")
    if not isinstance(payload, dict):
        return _entry_error("Neplatná data.")
    for attempt in range(2):
        try:
            return _store_entry(db, user, payload)
        except IntegrityError:  # concurrent replay of the same entry: the second run sees the first
            db.rollback()
            if attempt:
                raise
    raise AssertionError("unreachable")


def _store_entry(db: DbSession, user: Any, payload: dict[str, Any]) -> JSONResponse:
    entry_uuid = valid_uuid(payload.get("uuid"))
    if entry_uuid is None:
        return _entry_error("Chybí uuid záznamu.")
    point = db.get(MeterPoint, payload.get("point_id")) if isinstance(payload.get("point_id"), int) else None
    if point is None:
        return _entry_error("Neznámé měřicí místo.", 404)
    taken = _parse_client_time(payload.get("taken_at"))
    if taken is None:
        return _entry_error("Neplatný čas záznamu.")
    day = taken.date()
    note = str(payload.get("note") or "").strip()[:1000]

    if payload.get("skip"):
        skip = db.scalar(select(PointSkip).where(PointSkip.day == day, PointSkip.meter_point_id == point.id))
        skip_note = str(payload.get("skip_note") or note).strip()[:1000]
        if skip is None:
            skip = PointSkip(day=day, meter_point_id=point.id, note=skip_note, created_by_id=user.id)
            db.add(skip)
            db.flush()
            audit.record(
                db, user, "create", "point_skips", skip.id, {"point": point.name, "day": day, "note": skip_note}
            )
        elif skip.note != skip_note:
            skip.note = skip_note
        db.commit()
        return JSONResponse({"status": "ok", "results": [], "skipped": True})

    photo_uuids = [u for u in (valid_uuid(x) for x in payload.get("photos") or []) if u]
    photos = list(db.scalars(select(Photo).where(Photo.uuid.in_(photo_uuids)))) if photo_uuids else []
    readings = payload.get("readings") or []
    if not isinstance(readings, list) or not readings:
        return _entry_error("Záznam neobsahuje žádné hodnoty.")
    overwrite = bool(payload.get("overwrite"))
    results: list[dict[str, Any]] = []
    for item in readings:
        if not isinstance(item, dict):
            return _entry_error("Neplatná hodnota.")
        meter = db.get(Meter, item.get("meter_id")) if isinstance(item.get("meter_id"), int) else None
        reg = item.get("register")
        if meter is None or meter.meter_point_id != point.id or reg not in (meter.registers or ["T1"]):
            db.rollback()
            return _entry_error("Elektroměr nebo registr nepatří k místu.")
        try:
            value = parse_value(str(item.get("value") or ""))
        except InputError as exc:
            db.rollback()
            return _entry_error(f"{meter.serial} {reg}: {exc}")
        if value is None:
            continue
        acks = {str(a) for a in item.get("acks") or []}
        issues = check_value(db, meter, reg, value, day)
        unacked = missing_acks(issues, acks)
        status = "pending" if unacked else "confirmed"
        own_photo = next((p for p in photos if p.meter_id == meter.id), photos[0] if photos else None)
        acked_txt = "; ".join(f"[{i.code}] {i.message}" for i in issues if i.code in acks)
        res = write_reading(
            db,
            user,
            meter_id=meter.id,
            register=reg,
            day=day,
            value=value,
            source="photo" if own_photo else "manual",
            status=status,
            photo_id=own_photo.id if own_photo else None,
            note=note,
            audit_note=("obchůzka; " + (f"potvrzená varování: {acked_txt}" if acked_txt else "bez varování"))
            + (f"; NEPOTVRZENÁ varování → ke kontrole: {', '.join(i.code for i in unacked)}" if unacked else ""),
            client_ref=f"{entry_uuid}:{meter.id}:{reg}",
            overwrite=overwrite,
        )
        r_status = res.status
        if (
            res.status in ("created", "updated", "unchanged")
            and res.reading is not None
            and res.reading.status == "pending"
        ):
            r_status = "pending"
        results.append(
            {
                "meter_id": meter.id,
                "register": reg,
                "status": r_status,
                "issues": [i.as_dict() for i in unacked],
                "existing": _s(res.reading.value) if res.status == "conflict" and res.reading else None,
                "message": res.message,
            }
        )
    if not results:
        db.rollback()
        return _entry_error("Záznam neobsahuje žádné hodnoty.")
    conflict = any(r["status"] == "conflict" for r in results)
    if conflict:
        db.rollback()  # all-or-nothing per entry: the phone keeps the entry and asks the user
        return JSONResponse(
            {"status": "conflict", "results": results, "message": "Pro tento den už existuje jiný odečet."},
            status_code=409,
        )
    for p in photos:
        ch = audit.apply_changes(p, {"review_status": "confirmed" if p.review_status == "new" else p.review_status})
        if ch:
            audit.record_update(db, user, p, ch, note=f"obchůzka {entry_uuid}")
    # an entry replaces an earlier skip of the same point that day
    skip = db.scalar(select(PointSkip).where(PointSkip.day == day, PointSkip.meter_point_id == point.id))
    if skip is not None:
        db.delete(skip)
    db.commit()
    pending = any(r["status"] == "pending" for r in results)
    return JSONResponse({"status": "pending" if pending else "ok", "results": results})
