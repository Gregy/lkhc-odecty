"""Image handling: validation of untrusted uploads, EXIF time, orientation, downscaling.

Uploads are untrusted: the type is detected from the content (Pillow), not from the
client's filename or Content-Type; file size and pixel count are capped
(decompression bombs) before any full decode.
"""

from __future__ import annotations

import io
import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # a 50 MP phone JPEG is ~15-25 MB
MAX_PIXELS = 60_000_000  # 3000x4000 = 12 MP; generous but bounded
# PNG/WebP cannot be draft-decoded at reduced size (unlike JPEG), so thumbnails decode them fully:
# keep their pixel budget at a phone-screenshot scale.
MAX_PIXELS_LOSSLESS = 25_000_000
ALLOWED_FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}
THUMB_SIDE = 480
DISPLAY_SIDE = 1600

# Pillow's own bomb guard raises for > 2x MAX_IMAGE_PIXELS; we check explicitly before decoding.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageRejected(ValueError):  # noqa: N818
    """Upload is not an acceptable image; ``message`` is Czech and user facing."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ImageInfo:
    format: str  # JPEG | PNG | WEBP
    content_type: str
    extension: str
    width: int  # as displayed (after EXIF orientation)
    height: int
    exif_taken_at: datetime | None  # naive local time


def inspect_image(data: bytes, tz: str = "Europe/Prague") -> ImageInfo:
    """Validate an uploaded image and read its metadata. Raises ImageRejected."""
    if not data:
        raise ImageRejected("Prázdný soubor.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageRejected(f"Soubor je příliš velký (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            fmt = im.format or ""
            if fmt not in ALLOWED_FORMATS:
                raise ImageRejected("Nepodporovaný formát obrázku (povoleno JPEG, PNG, WebP).")
            w, h = im.size
            limit = MAX_PIXELS if fmt == "JPEG" else MAX_PIXELS_LOSSLESS
            if w <= 0 or h <= 0 or w * h > limit:
                raise ImageRejected("Obrázek má příliš mnoho pixelů.")
            im.verify()  # structural check without full decode (must come right after open)
        # verify() leaves the image unusable; a real decode of a reduced version proves the data decodes
        with Image.open(io.BytesIO(data)) as im2:
            exif = im2.getexif()
            im2.draft("RGB", (256, 256))
            im2.load()
    except ImageRejected:
        raise
    except UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning:
        raise ImageRejected("Soubor není platný obrázek.") from None
    except Exception:  # truncated / corrupt data (OSError, SyntaxError, ...)
        raise ImageRejected("Soubor není platný obrázek (poškozená data).") from None
    orientation = exif.get(ExifTags.Base.Orientation, 1) if exif else 1
    if orientation in (5, 6, 7, 8):
        w, h = h, w
    ctype, ext = ALLOWED_FORMATS[fmt]
    return ImageInfo(fmt, ctype, ext, w, h, exif_datetime(exif, tz) if exif else None)


def exif_datetime(exif: Image.Exif, tz: str = "Europe/Prague") -> datetime | None:
    """DateTimeOriginal (else DateTimeDigitized / DateTime) as naive local time.

    With OffsetTimeOriginal the value is converted to ``tz``; without it the camera's
    local time is taken as is.
    """
    try:
        ifd = exif.get_ifd(ExifTags.IFD.Exif)
    except Exception:
        ifd = {}
    pairs = (
        (ifd.get(ExifTags.Base.DateTimeOriginal), ifd.get(ExifTags.Base.OffsetTimeOriginal)),
        (ifd.get(ExifTags.Base.DateTimeDigitized), ifd.get(ExifTags.Base.OffsetTimeDigitized)),
        (exif.get(ExifTags.Base.DateTime), ifd.get(ExifTags.Base.OffsetTime)),
    )
    for raw, offset in pairs:
        dt = parse_exif_datetime(raw, offset, tz)
        if dt is not None:
            return dt
    return None


_EXIF_RE = re.compile(r"^\s*(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_OFFSET_RE = re.compile(r"^\s*([+-])(\d{2}):?(\d{2})\s*$")


def parse_exif_datetime(raw: object, offset: object = None, tz: str = "Europe/Prague") -> datetime | None:
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "ignore")
    if not isinstance(raw, str):
        return None
    m = _EXIF_RE.match(raw.strip("\x00 "))
    if not m:
        return None
    try:
        dt = datetime(*(int(x) for x in m.groups()))
    except ValueError:  # "0000:00:00 00:00:00" etc.
        return None
    if dt.year < 2000:
        return None
    if isinstance(offset, bytes):
        offset = offset.decode("ascii", "ignore")
    om = _OFFSET_RE.match(offset) if isinstance(offset, str) else None
    if om:
        from datetime import timedelta, timezone

        sign = 1 if om.group(1) == "+" else -1
        off = timezone(sign * timedelta(hours=int(om.group(2)), minutes=int(om.group(3))))
        dt = dt.replace(tzinfo=off).astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return dt


# --- dates suggested by file names (never used silently) -------------------------

_WA_RE = re.compile(r"(?:IMG|VID|PTT)-(\d{4})(\d{2})(\d{2})-WA\d+", re.IGNORECASE)
_CAM_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)")


@dataclass(frozen=True)
class SuggestedTime:
    value: datetime
    has_time: bool  # False = only the date is known (WhatsApp)
    source: str  # "whatsapp" | "filename"


def suggest_from_filename(name: str | None) -> SuggestedTime | None:
    """WhatsApp ``IMG-YYYYMMDD-WAnnnn.jpg`` -> date only; camera ``YYYYMMDD_HHMMSS`` -> date+time."""
    if not name:
        return None
    m = _WA_RE.search(name)
    if m:
        try:
            return SuggestedTime(datetime(int(m[1]), int(m[2]), int(m[3])), False, "whatsapp")
        except ValueError:
            return None
    m = _CAM_RE.search(name)
    if m:
        try:
            return SuggestedTime(datetime(*(int(x) for x in m.groups())), True, "filename")
        except ValueError:
            return None
    return None


def is_whatsapp_name(name: str | None) -> bool:
    return bool(name and _WA_RE.search(name))


# --- derived images ---------------------------------------------------------------


def _open_oriented(data: bytes, max_side: int) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (max_side, max_side))  # fast JPEG downscale while decoding
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im


def downscale_jpeg(data: bytes, max_side: int = 1600, quality: int = 85, *, autocontrast: bool = False) -> bytes:
    """EXIF-oriented JPEG with the longer side <= ``max_side`` (metadata stripped).

    ``autocontrast`` stretches each channel (1 % cut-off): fogged glass in front of a display turns
    into a grey veil that this removes (only for the copy sent to the model).
    """
    im = _open_oriented(data, max_side)
    if autocontrast:
        im = ImageOps.autocontrast(im, cutoff=1)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def thumbnail_jpeg(data: bytes, side: int = THUMB_SIDE) -> bytes:
    return downscale_jpeg(data, side, quality=78)


def as_date(v: datetime | date | None) -> date | None:
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v
