"""Photos (`/fotky`, `/fotky/{id}`): browser, detail, authenticated file serving (SPEC §5)."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select

from app import audit
from app.capture.photos import resolve_path
from app.capture.review import METHOD_LABELS, card_key_for_photo, photo_problems
from app.deps import AdminUser, DbSession, require_user
from app.extraction import queue as extraction_queue
from app.extraction.hints import meter_hint_list
from app.extraction.images import suggest_from_filename
from app.extraction.matching import apply_matching
from app.extraction.types import ExtractionResult, problem_label
from app.models import Meter, Photo, Reading
from app.templating import flash, templates

router = APIRouter(prefix="/fotky", dependencies=[Depends(require_user)])

PAGE_SIZE = 60
REVIEW_LABELS = {"new": "ke kontrole", "confirmed": "potvrzeno", "discarded": "zahozeno"}
EXTRACTION_LABELS = {"queued": "ve frontě", "done": "rozpoznáno", "failed": "chyba", "skipped": "bez rozpoznání"}
SOURCE_LABELS = {"walk": "obchůzka", "upload": "dávka", "share": "sdíleno"}

LABELS = {"review": REVIEW_LABELS, "extraction": EXTRACTION_LABELS, "source": SOURCE_LABELS}


def _photo_or_404(db: DbSession, photo_id: int) -> Photo:
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(404, "Fotka nenalezena.")
    return photo


@router.get("", name="fotky")
def fotky(request: Request, db: DbSession, stav: str = "vse", strana: int = 1):
    q = select(Photo)
    if stav in REVIEW_LABELS:
        q = q.where(Photo.review_status == stav)
    elif stav in ("queued", "failed"):
        q = q.where(Photo.extraction_status == stav)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    strana = max(1, strana)
    photos = list(
        db.scalars(
            q.order_by(Photo.taken_at.desc().nulls_last(), Photo.id.desc())
            .offset((strana - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )
    meters = {m.id: m for m in db.scalars(select(Meter))}
    counts = dict(db.execute(select(Photo.review_status, func.count()).group_by(Photo.review_status)).all())
    return templates.TemplateResponse(
        request,
        "fotky/index.html",
        {
            "page_title": "Fotky",
            "active_nav": "/sprava",
            "photos": photos,
            "meters": meters,
            "stav": stav,
            "strana": strana,
            "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "total": total,
            "counts": counts,
            "labels": LABELS,
        },
    )


def _same_meter_photos(db: DbSession, photo: Photo, meter_ids: set[int]) -> list[Photo]:
    """Photos (not discarded) of the same meter(s) taken the same day - assigned to it, or with a reading of it."""
    if photo.taken_at is None or not meter_ids:
        return []
    start = datetime.combine(photo.taken_at.date(), time.min)
    end = start + timedelta(days=1)
    with_reading = select(Reading.photo_id).where(Reading.meter_id.in_(meter_ids), Reading.photo_id.is_not(None))
    return list(
        db.scalars(
            select(Photo)
            .where(
                Photo.taken_at >= start,
                Photo.taken_at < end,
                Photo.review_status != "discarded",
                or_(Photo.meter_id.in_(meter_ids), Photo.id.in_(with_reading)),
            )
            .order_by(Photo.taken_at, Photo.id)
        )
    )


@router.get("/{photo_id}", name="fotka")
def fotka(request: Request, photo_id: int, db: DbSession):
    photo = _photo_or_404(db, photo_id)
    result = ExtractionResult.from_dict(photo.extraction_result)
    matched = None
    if result is not None:
        matched = ExtractionResult.from_dict(photo.extraction_result)
        assert matched is not None
        apply_matching(matched, meter_hint_list(db, photo.taken_at), photo.taken_at)
    readings = list(
        db.scalars(select(Reading).where(Reading.photo_id == photo.id).order_by(Reading.meter_id, Reading.register))
    )
    meter_ids = {photo.meter_id} | {r.meter_id for r in readings}
    if matched is not None:
        meter_ids |= {m.meter_id for m in matched.meters}
    same_meter = _same_meter_photos(db, photo, {mid for mid in meter_ids if mid is not None})
    meters = {m.id: m for m in db.scalars(select(Meter))}
    raw_json = json.dumps(photo.extraction_result, ensure_ascii=False, indent=2) if photo.extraction_result else ""
    return templates.TemplateResponse(
        request,
        "fotky/detail.html",
        {
            "page_title": f"Fotka #{photo.id}",
            "active_nav": "/sprava",
            "photo": photo,
            "matched": matched,
            "readings": readings,
            "same_meter": same_meter,
            "card_key": card_key_for_photo(db, photo.id) if photo.review_status == "new" else None,
            "meters": meters,
            "raw_json": raw_json,
            "problems": photo_problems(photo),
            "problem_label": problem_label,
            "method_labels": METHOD_LABELS,
            "suggested": suggest_from_filename(photo.original_filename),
            "labels": LABELS,
        },
    )


def _file_response(request: Request, rel: str | None, media_type: str) -> Response:
    path = resolve_path(request.app.state.settings, rel)
    if path is None or not path.is_file():
        raise HTTPException(404, "Soubor fotky chybí.")
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=604800",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/{photo_id}/soubor", name="fotka_soubor")
def fotka_soubor(request: Request, photo_id: int, db: DbSession) -> Response:
    photo = _photo_or_404(db, photo_id)
    return _file_response(request, photo.path, photo.content_type)


@router.get("/{photo_id}/nahled", name="fotka_nahled")
def fotka_nahled(request: Request, photo_id: int, db: DbSession) -> Response:
    photo = _photo_or_404(db, photo_id)
    if photo.thumb_path:
        return _file_response(request, photo.thumb_path, "image/jpeg")
    return _file_response(request, photo.path, photo.content_type)


@router.post("/{photo_id}/znovu", name="fotka_znovu")
def fotka_znovu(request: Request, photo_id: int, db: DbSession, user: AdminUser) -> Response:
    """Queue the photo for extraction again (e.g. after a model / prompt change or a failure)."""
    photo = _photo_or_404(db, photo_id)
    if not extraction_queue.extraction_enabled(request.app):
        flash(request, "Rozpoznávání není zapnuté (EXTRACTOR=none).", "warning")
        return RedirectResponse(f"/fotky/{photo.id}", status_code=303)
    ch = audit.apply_changes(photo, {"extraction_status": "queued", "extraction_attempts": 0, "extraction_error": None})
    if photo.match_method not in ("walk", "manual"):
        ch.update(audit.apply_changes(photo, {"meter_id": None, "match_method": None}))
    if ch:
        audit.record_update(db, user, photo, ch, note="znovu rozpoznat")
    db.commit()
    extraction_queue.notify(request.app)
    flash(request, "Fotka je znovu ve frontě rozpoznávání.", "success")
    return RedirectResponse(f"/fotky/{photo.id}", status_code=303)


@router.post("/{photo_id}/stav", name="fotka_stav")
async def fotka_stav(request: Request, photo_id: int, db: DbSession, user: AdminUser) -> Response:
    """Discard a photo from the review queue, or return a discarded one to it."""
    form = await request.form()
    target: Any = form.get("review_status")
    photo = _photo_or_404(db, photo_id)
    allowed = {"new": ("discarded",), "discarded": ("new",)}
    if target not in allowed.get(photo.review_status, ()):
        flash(request, "Tuto změnu stavu nelze provést.", "error")
        return RedirectResponse(f"/fotky/{photo.id}", status_code=303)
    ch = audit.apply_changes(photo, {"review_status": target})
    audit.record_update(db, user, photo, ch)
    db.commit()
    flash(request, "Fotka vrácena do kontroly." if target == "new" else "Fotka zahozena.", "success")
    return RedirectResponse(f"/fotky/{photo.id}", status_code=303)
