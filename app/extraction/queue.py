"""Persistent in-process extraction queue driven by ``Photo.extraction_status``.

- Uploads only mark a photo ``queued`` and call ``notify(app)``; they never wait for the model.
- A daemon thread processes ``queued`` photos one by one (oldest first). After a restart the
  thread picks up whatever is still ``queued`` (``install`` hooks it into the app lifespan).
- Each claim increments ``extraction_attempts``; a photo that crashed the worker
  ``MAX_ATTEMPTS`` times is marked ``failed``. Failures never block manual entry.
- Tests call ``process_pending(app)`` synchronously instead of running the thread.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.capture.photos import read_photo_bytes
from app.extraction.extractors import make_extractor
from app.extraction.hints import build_hints
from app.extraction.images import downscale_jpeg
from app.extraction.matching import apply_matching
from app.extraction.types import ExtractionResult, ReadingExtractor
from app.models import Meter, Photo

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
IDLE_WAIT_S = 30.0


def get_extractor(app: FastAPI) -> ReadingExtractor:
    ex = getattr(app.state, "extractor", None)
    if ex is None:
        ex = make_extractor(app.state.settings)
        app.state.extractor = ex
    return ex


def extraction_enabled(app: FastAPI) -> bool:
    return getattr(app.state.settings, "extractor", "none") != "none"


def focus_meters(db: Session, photo: Photo) -> tuple[int, ...]:
    """Walk photos: active meters of the point the photo was taken at."""
    if photo.match_method != "walk" or photo.meter_id is None:
        return ()
    m = db.get(Meter, photo.meter_id)
    if m is None:
        return ()
    ids = db.scalars(select(Meter.id).where(Meter.meter_point_id == m.meter_point_id, Meter.removed_on.is_(None)))
    return tuple(ids) or (m.id,)


def _claim(db: Session, photo_id: int, attempts: int) -> bool:
    res = db.execute(
        update(Photo)
        .where(Photo.id == photo_id, Photo.extraction_status == "queued", Photo.extraction_attempts == attempts)
        .values(extraction_attempts=attempts + 1)
    )
    db.commit()
    return res.rowcount == 1


def process_photo(app: FastAPI, photo_id: int) -> str | None:
    """Run extraction for one queued photo. Returns the new status (None if not claimed)."""
    settings = app.state.settings
    factory: sessionmaker[Session] = app.state.sessionmaker
    with factory() as db:
        photo = db.get(Photo, photo_id)
        if photo is None or photo.extraction_status != "queued":
            return None
        if photo.extraction_attempts >= MAX_ATTEMPTS:
            photo.extraction_status = "failed"
            photo.extraction_error = (photo.extraction_error or "") + " Opakované selhání zpracování."
            db.commit()
            return "failed"
        if not _claim(db, photo.id, photo.extraction_attempts):
            return None
        data = read_photo_bytes(settings, photo)
        at = photo.taken_at
        focus = focus_meters(db, photo)
        hints = build_hints(db, at, focus_meter_ids=focus)
        hints = dataclasses.replace(hints, image_sha256=photo.sha256)
    # --- slow part, outside any DB transaction
    if data is None:
        result = ExtractionResult(problems=["model_error"], error="soubor fotky chybí")
    else:
        try:
            prepared = downscale_jpeg(data, int(getattr(settings, "extractor_max_side", 1600)))
            result = get_extractor(app).extract(prepared, hints)
        except Exception as exc:  # extractors should not raise; be defensive anyway
            log.exception("extraction of photo %s failed", photo_id)
            result = ExtractionResult(problems=["model_error"], error=f"{type(exc).__name__}: {exc}")
    with factory() as db:
        photo = db.get(Photo, photo_id)
        if photo is None:
            return None
        photo.extraction_result = result.to_dict()
        photo.extraction_model = (result.model or "")[:200] or None
        photo.extracted_at = datetime.now()
        if result.error:
            photo.extraction_status = "failed"
            photo.extraction_error = result.error[:2000]
        else:
            photo.extraction_status = "done"
            photo.extraction_error = None
            if photo.match_method not in ("walk", "manual") and photo.review_status == "new":
                matched = apply_matching(ExtractionResult.from_dict(result.to_dict()) or result, hints.meters, at)
                first = next((m for m in matched.meters if m.meter_id), None)
                photo.meter_id = first.meter_id if first else None
                photo.match_method = first.match_method if first else None
        db.commit()
        return photo.extraction_status


def pending_ids(db: Session, limit: int = 50) -> list[int]:
    return list(db.scalars(select(Photo.id).where(Photo.extraction_status == "queued").order_by(Photo.id).limit(limit)))


def process_pending(app: FastAPI, limit: int = 1000) -> int:
    """Process queued photos synchronously (tests, CLI). Returns how many were processed."""
    n = 0
    while n < limit:
        with app.state.sessionmaker() as db:
            ids = pending_ids(db)
        if not ids:
            break
        progressed = False
        for pid in ids:
            if process_photo(app, pid) is not None:
                progressed = True
                n += 1
        if not progressed:
            break
    return n


class Worker:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="extraction-worker", daemon=True)

    def run(self) -> None:
        log.info("extraction worker started (%s)", getattr(self.app.state.settings, "extractor", "?"))
        while not self.stop.is_set():
            try:
                with self.app.state.sessionmaker() as db:
                    ids = pending_ids(db, limit=10)
                for pid in ids:
                    if self.stop.is_set():
                        break
                    process_photo(self.app, pid)
                if ids:
                    continue
            except Exception:
                log.exception("extraction worker loop error")
            self.wake.wait(IDLE_WAIT_S)
            self.wake.clear()


def notify(app: FastAPI) -> None:
    """Wake the worker after new photos were queued (no-op when it does not run, e.g. in tests)."""
    w: Worker | None = getattr(app.state, "extraction_worker", None)
    if w is not None:
        w.wake.set()


def start(app: FastAPI) -> Worker | None:
    if not extraction_enabled(app):
        return None
    w = getattr(app.state, "extraction_worker", None)
    if w is None:
        w = Worker(app)
        app.state.extraction_worker = w
        w.thread.start()
    return w


def stop(app: FastAPI) -> None:
    w: Worker | None = getattr(app.state, "extraction_worker", None)
    if w is not None:
        w.stop.set()
        w.wake.set()
        w.thread.join(timeout=5)
        app.state.extraction_worker = None


def install(app: FastAPI) -> None:
    """Run the worker during the app lifespan (wraps the existing lifespan context)."""
    previous = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[Any]:
        start(app)
        try:
            async with previous(a) as state:
                yield state
        finally:
            stop(app)

    app.router.lifespan_context = lifespan
