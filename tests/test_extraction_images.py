"""Upload validation, EXIF time, orientation, downscale, file-name date suggestions."""

from __future__ import annotations

import io
from datetime import datetime

import pytest
from PIL import Image

from app.extraction.images import (
    ImageRejected,
    downscale_jpeg,
    inspect_image,
    parse_exif_datetime,
    suggest_from_filename,
)
from tests.capture_helpers import make_jpeg


def test_exif_time_with_offset_is_converted_to_prague() -> None:
    # 16:48:24 at +01:00 is Prague local time already (winter)
    info = inspect_image(make_jpeg(taken=datetime(2026, 11, 8, 16, 48, 24), offset="+01:00"))
    assert info.exif_taken_at == datetime(2026, 11, 8, 16, 48, 24)
    # UTC timestamp -> local (summer, +2 h)
    info = inspect_image(make_jpeg(taken=datetime(2026, 8, 29, 11, 29, 18), offset="+00:00"))
    assert info.exif_taken_at == datetime(2026, 8, 29, 13, 29, 18)


def test_exif_time_without_offset_taken_as_local() -> None:
    info = inspect_image(make_jpeg(taken=datetime(2026, 10, 1, 12, 28, 28)))
    assert info.exif_taken_at == datetime(2026, 10, 1, 12, 28, 28)


def test_no_exif_means_no_time() -> None:
    info = inspect_image(make_jpeg())
    assert info.exif_taken_at is None
    assert info.content_type == "image/jpeg" and info.extension == "jpg"


@pytest.mark.parametrize("raw", ["0000:00:00 00:00:00", "garbage", "", None, "2026:13:40 10:00:00"])
def test_bad_exif_strings(raw: object) -> None:
    assert parse_exif_datetime(raw) is None


def test_orientation_swaps_dimensions_and_downscale_applies_it() -> None:
    data = make_jpeg(size=(400, 300), orientation=6)
    info = inspect_image(data)
    assert (info.width, info.height) == (300, 400)
    small = Image.open(io.BytesIO(downscale_jpeg(data, 200)))
    assert small.size == (150, 200)
    assert not small.getexif()  # metadata stripped from the derived image


def test_png_and_webp_accepted() -> None:
    assert inspect_image(make_jpeg(fmt="PNG")).content_type == "image/png"
    assert inspect_image(make_jpeg(fmt="WEBP")).content_type == "image/webp"


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image at all", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", b"GIF89a" + b"\x00" * 50],
)
def test_non_images_rejected(data: bytes) -> None:
    with pytest.raises(ImageRejected):
        inspect_image(data)


def test_truncated_jpeg_rejected() -> None:
    data = make_jpeg(size=(800, 600))
    with pytest.raises(ImageRejected):
        inspect_image(data[: len(data) // 3])


def test_decompression_bomb_rejected() -> None:
    im = Image.new("1", (10_000, 10_000))  # 100 MP, compresses to a few kB
    buf = io.BytesIO()
    im.save(buf, "PNG")
    assert len(buf.getvalue()) < 100_000
    with pytest.raises(ImageRejected):
        inspect_image(buf.getvalue())


def test_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.extraction.images as images

    monkeypatch.setattr(images, "MAX_UPLOAD_BYTES", 1000)
    with pytest.raises(ImageRejected, match="velký"):
        images.inspect_image(make_jpeg(size=(800, 600), color=(1, 2, 3)) + b"\x00" * 2000)


def test_filename_suggestions() -> None:
    s = suggest_from_filename("IMG-20261105-WA0012.jpg")
    assert s is not None and s.value == datetime(2026, 11, 5) and not s.has_time and s.source == "whatsapp"
    s = suggest_from_filename("20261108_164823.jpg")
    assert s is not None and s.value == datetime(2026, 11, 8, 16, 48, 23) and s.has_time
    assert suggest_from_filename("IMG-20261340-WA0001.jpg") is None
    assert suggest_from_filename("foto.jpg") is None
    assert suggest_from_filename(None) is None
