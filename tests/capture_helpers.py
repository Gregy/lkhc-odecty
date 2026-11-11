"""Helpers for capture/extraction tests (synthetic images, fake extractor wiring)."""

from __future__ import annotations

import hashlib
import io
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from PIL import ExifTags, Image

from app.extraction.extractors import FakeExtractor


def make_jpeg(
    *,
    taken: datetime | None = None,
    offset: str | None = None,
    orientation: int | None = None,
    size: tuple[int, int] = (400, 300),
    color: tuple[int, int, int] = (120, 130, 140),
    fmt: str = "JPEG",
) -> bytes:
    """A small synthetic photo, optionally with EXIF DateTimeOriginal / OffsetTimeOriginal / Orientation.

    ``color`` makes the bytes (and so the sha256) unique per test photo.
    """
    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    if orientation is not None:
        exif[ExifTags.Base.Orientation] = orientation
    if taken is not None:
        ifd = exif.get_ifd(ExifTags.IFD.Exif)
        ifd[ExifTags.Base.DateTimeOriginal] = taken.strftime("%Y:%m:%d %H:%M:%S")
        if offset:
            ifd[ExifTags.Base.OffsetTimeOriginal] = offset
    out = io.BytesIO()
    kwargs: dict[str, Any] = {"exif": exif.tobytes()} if (taken or orientation) else {}
    im.save(out, fmt, **kwargs)
    return out.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fake_extractor(app: FastAPI) -> FakeExtractor:
    """Install (or return) the FakeExtractor used by the app's queue."""
    ex = getattr(app.state, "extractor", None)
    if not isinstance(ex, FakeExtractor):
        ex = FakeExtractor()
        app.state.extractor = ex
    return ex


def answer(*meters: dict[str, Any], readable: bool = True, problems: list[str] | None = None) -> dict[str, Any]:
    """Model-JSON shaped answer: answer({"serial": "3906127", "registers": [{"digits_seen": "01912", ...}]})."""
    ms = []
    for m in meters:
        regs = []
        for r in m.get("registers", []):
            regs.append({"label_seen": "", "register": None, "confidence": 0.95, **r})
        ms.append({"serial_confidence": 0.95 if m.get("serial") else 0.0, "kind": "lcd", **m, "registers": regs})
    return {"readable": readable, "problems": problems or [], "meters": ms}


def upload(client, data: bytes, name: str = "foto.jpg", reported_by: str = "") -> dict[str, Any]:
    r = client.post(
        "/odecty/davka",
        files={"files": (name, data, "image/jpeg")},
        data={"reported_by": reported_by},
        headers={"Accept": "application/json"},
    )
    assert r.status_code in (200, 207, 400), r.text
    return r.json()["results"][0]
