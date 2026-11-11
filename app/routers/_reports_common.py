"""Small helpers shared by the reports-phase routers (dashboard, spotreba, mista, najemci, vyuctovani, sprava)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from urllib.parse import quote

from fastapi.responses import Response
from markupsafe import Markup

from app.domain.aligned import QValue
from app.services.periods import MONTHS_CS
from app.services.reports import qjson

MONTHS_SHORT = ("led", "úno", "bře", "dub", "kvě", "čvn", "čvc", "srp", "zář", "říj", "lis", "pro")
REPORTS_CSS = "css/reports.css"


def json_script(data: Any) -> Markup:
    """JSON safe to embed in <script type="application/json">."""
    s = json.dumps(data, ensure_ascii=False, default=str)
    return Markup(s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def parse_int(value: str | None, default: int | None = None) -> int | None:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def download(content: bytes, filename: str, media_type: str) -> Response:
    ascii_name = filename.encode("ascii", "ignore").decode() or "export"
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content, media_type=media_type, headers={"Content-Disposition": disp})


def month_label(d: date) -> str:
    return f"{MONTHS_CS[d.month - 1]} {d.year}"


def qseries(values: list[QValue], decimals: int = 1) -> list[dict[str, Any] | None]:
    return [qjson(v, decimals) for v in values]
