"""Photo storage and ingestion (all upload paths: walk PWA, batch upload, share target).

Files are stored under ``DATA_DIR/photos/YYYY/MM/<server uuid>.<ext>`` with a
server-generated name; the client's file name is kept only as metadata (for the
WhatsApp date suggestion). Photos are served only through authenticated routes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.config import Settings
from app.extraction.images import MAX_UPLOAD_BYTES, ImageRejected, inspect_image, thumbnail_jpeg
from app.extraction.series import regroup_series
from app.models import Meter, Photo, User

log = logging.getLogger(__name__)

PHOTO_SOURCES = ("walk", "upload", "share")


def valid_uuid(value: str | None) -> str | None:
    """Canonical form of a client-supplied UUID, None if it is not one."""
    if not value:
        return None
    try:
        return str(uuidlib.UUID(str(value)))
    except ValueError, AttributeError, TypeError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def resolve_path(settings: Settings, rel: str | None) -> Path | None:
    """Absolute path of a stored file; refuses anything outside DATA_DIR/photos."""
    if not rel:
        return None
    root = settings.photos_dir.resolve()
    p = (settings.data_dir / rel).resolve()
    if root != p and root not in p.parents:
        return None
    return p


def read_photo_bytes(settings: Settings, photo: Photo) -> bytes | None:
    p = resolve_path(settings, photo.path)
    if p is None or not p.is_file():
        return None
    return p.read_bytes()


async def read_upload(upload: Any, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read an UploadFile in chunks, refusing more than ``limit`` bytes."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ImageRejected(f"Soubor je příliš velký (max {limit // (1024 * 1024)} MB).")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class StoreResult:
    photo: Photo
    created: bool
    duplicate_of: Photo | None = None


def store_photo(
    db: Session,
    settings: Settings,
    data: bytes,
    *,
    user: User | None,
    filename: str = "",
    source: str = "upload",
    client_uuid: str | None = None,
    client_taken_at: datetime | None = None,
    reported_by: str = "",
    walk_meter: Meter | None = None,
    extraction_enabled: bool = True,
) -> StoreResult:
    """Validate and store an uploaded photo (no commit). Idempotent on client UUID and content hash.

    ``client_taken_at`` is used only when the image has no EXIF time *and* the photo comes
    from the walk PWA (taken on the device a moment ago; the client re-encodes the image,
    which drops EXIF). Otherwise a missing time stays None and the review asks for it.
    Raises ``ImageRejected``.
    """
    if source not in PHOTO_SOURCES:
        source = "upload"
    cu = valid_uuid(client_uuid)
    if cu:
        existing = db.scalar(select(Photo).where(Photo.uuid == cu))
        if existing is not None:
            return StoreResult(existing, False)
    info = inspect_image(data, settings.tz)
    sha = hashlib.sha256(data).hexdigest()
    dup = db.scalar(select(Photo).where(Photo.sha256 == sha).order_by(Photo.id))
    if dup is not None and dup.review_status != "discarded":
        return StoreResult(dup, False, duplicate_of=dup)

    taken_at = info.exif_taken_at
    if taken_at is None and source == "walk" and client_taken_at is not None:
        taken_at = client_taken_at
    stamp = taken_at or datetime.now()
    file_id = uuidlib.uuid4().hex
    rel_dir = Path("photos") / f"{stamp:%Y}" / f"{stamp:%m}"
    rel = rel_dir / f"{file_id}.{info.extension}"
    rel_thumb = rel_dir / f"{file_id}_t.jpg"
    _write_atomic(settings.data_dir / rel, data)
    try:
        _write_atomic(settings.data_dir / rel_thumb, thumbnail_jpeg(data))
        thumb = rel_thumb.as_posix()
    except Exception:  # thumbnail is optional
        log.exception("thumbnail failed")
        thumb = None

    photo = Photo(
        uuid=cu or str(uuidlib.uuid4()),
        path=rel.as_posix(),
        thumb_path=thumb,
        original_filename=Path(filename or "").name[:200],
        content_type=info.content_type,
        size_bytes=len(data),
        sha256=sha,
        width=info.width,
        height=info.height,
        exif_taken_at=info.exif_taken_at,
        taken_at=taken_at,
        uploaded_by_id=user.id if user else None,
        uploaded_at=datetime.now(),
        extraction_status="queued" if extraction_enabled else "skipped",
        source=source,
        reported_by=reported_by.strip()[:200],
        review_status="new",
    )
    if walk_meter is not None:
        photo.meter_id = walk_meter.id
        photo.match_method = "walk"
    db.add(photo)
    db.flush()
    audit.record(db, user, "upload", "photos", photo.id, {"source": source, "filename": photo.original_filename})
    regroup_series(db, taken_at)
    return StoreResult(photo, True)


__all__ = [
    "ImageRejected",
    "StoreResult",
    "read_photo_bytes",
    "read_upload",
    "resolve_path",
    "store_photo",
    "valid_uuid",
]
