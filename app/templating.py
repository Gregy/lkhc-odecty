"""Shared Jinja2 environment: filters (cs-CZ formatting), globals, flash messages.

Routers render pages with::

    from app.templating import templates
    return templates.TemplateResponse(request, "spotreba/index.html", {"page_title": "Spotřeba", ...})

Every template context automatically gets: ``current_user``, ``csrf_token``,
``nav``, ``tenant_palette`` and ``settings``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select

from app.csrf import get_csrf_token
from app.domain.aligned import Quality, QValue
from app.formatting import (  # noqa: F401 - re-exported for routers/services
    NBSP,
    format_date,
    format_datetime,
    format_kwh,
    format_month,
    format_number,
    format_reading,
    is_marked,
    quality_mark,
)
from app.models import Tenant

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


# (label, url, admin_only); `active_nav` in a page context should equal the url.
# Místa, Nájemci and Fotky are not top-level tabs: they are reached from the Správa page (visible to readers
# too, with just those read-only pages) and their pages set active_nav = "/sprava".
NAV: list[tuple[str, str, bool]] = [
    ("Přehled", "/", False),
    ("Odečty", "/odecty", False),
    ("Spotřeba", "/spotreba", False),
    ("Obchůzka", "/obchuzka", False),
    ("Dávka", "/odecty/davka", True),  # upload is admin-only
    ("Kontrola", "/odecty/kontrola", False),
    ("Vyúčtování", "/vyuctovani", False),
    ("Správa", "/sprava", False),  # readers get the read-only part (Místa, Nájemci, Fotky)
]

# The remainder tenant (main − others, computed per register) is shown only as T1+T2: single-tariff
# sub-meters count fully into T1, so its T1 part is too low (even negative) and its T2 part too high.
REMAINDER_TARIFF_NOTE = (
    "Jednotarifní podružné elektroměry se počítají celé do T1, proto má rozdělení zbytku na tarify smysl "
    "jen v součtu (T1+T2)."
)

QUALITY_LABELS = {
    Quality.EXACT: "přesná hodnota",
    Quality.ESTIMATED: "odhad (interpolace)",
    Quality.PROVISIONAL: "předběžné (extrapolace)",
}


# --- formatting --------------------------------------------------------------


def stored_decimals(value: Any) -> int:
    """Number of significant decimals actually stored (19212.900 -> 1), max 3."""
    if value is None:
        return 0
    s = format(value, "f")
    return min(3, len(s.split(".")[1].rstrip("0"))) if "." in s else 0


def quality_title(q: QValue | Quality | None) -> str:
    """Tooltip text describing quality and provenance, e.g. 'odhad z odečtů 2.11.2025 a 4.1.2026'."""
    if q is None:
        return ""
    if isinstance(q, Quality):
        return QUALITY_LABELS[q]
    if q.quality == Quality.EXACT:
        return QUALITY_LABELS[Quality.EXACT]
    notes: list[str] = []
    for p in q.parts:
        for b in (p.start, p.end):
            if b.quality == Quality.EXACT:
                continue
            who = f"{p.serial} {p.register}" if p.serial else p.register
            if b.method == "interpolated" and b.before and b.after:
                notes.append(
                    f"{who}: stav k {format_date(b.t)} odhadnut z odečtů {format_date(b.before.taken_at)} "
                    f"a {format_date(b.after.taken_at)}"
                )
            elif b.method in ("extrapolated", "after_removal") and b.before and b.quality == Quality.ESTIMATED:
                notes.append(
                    f"{who}: odebrán bez konečného odečtu, stav k {format_date(b.t)} dopočten "
                    f"od odečtu {format_date(b.before.taken_at)}"
                )
            elif b.method == "extrapolated" and b.before:
                notes.append(
                    f"{who}: stav k {format_date(b.t)} předběžný, poslední odečet {format_date(b.before.taken_at)}"
                )
            elif b.method == "no_data":
                notes.append(f"{who}: bez odečtů")
    head = QUALITY_LABELS[q.quality]
    if q.estimated_kwh:
        head += f", odhadnuto {format_number(q.share_estimated * 100)} %"
    uniq = list(dict.fromkeys(notes))
    # the reason for "provisional" must not be cut off by the 8-line limit: list those notes first
    uniq.sort(key=lambda n: 0 if ("předběžný" in n or "bez odečtů" in n) else 1)
    return "\n".join([head, *uniq[:8], *(["…"] if len(uniq) > 8 else [])])


def qvalue_html(q: QValue | None, decimals: int = 0, unit: bool = False) -> Markup:
    """Number with quality marker. Only provisional (extrapolated) values are marked: '≈' + italic + dotted
    underline, focusable for a tap/keyboard provenance popover. Interpolated (estimated) values look like
    exact ones (the provenance stays in the hover title)."""
    if q is None:
        return Markup('<span class="num muted">–</span>')
    txt = format_kwh(q.value, decimals) if unit else format_number(q.value, decimals)
    neg = " negative" if q.value < 0 else ""
    if not is_marked(q.quality):
        return Markup('<span class="num{}" title="{}">{}</span>').format(neg, quality_title(q), txt)
    return Markup('<span class="num q-provisional{}" title="{}" tabindex="0">{}{}</span>').format(
        neg, quality_title(q), quality_mark(q.quality), txt
    )


# --- flash messages -------------------------------------------------------------


def flash(request: Request, message: str, category: str = "info") -> None:
    """Queue a message for the next rendered page. category: info | success | warning | error."""
    # Reassign (not setdefault().append()): Starlette's Session only marks itself modified on
    # __setitem__, so appending to an existing list would silently drop the message.
    request.session["_flashes"] = [*request.session.get("_flashes", []), [category, message]]


def get_flashed_messages(request: Request) -> list[tuple[str, str]]:
    msgs = request.session.pop("_flashes", []) if "session" in request.scope else []
    return [(c, m) for c, m in msgs]


# --- context -------------------------------------------------------------------------


def tenant_palette(request: Request) -> list[dict[str, Any]]:
    """Tenants with their stable colours (also exported as CSS variables / JSON in base.html)."""
    factory = getattr(request.app.state, "sessionmaker", None)
    if factory is None:
        return []
    try:
        with factory() as db:
            rows = db.scalars(select(Tenant).order_by(Tenant.sort_order, Tenant.id)).all()
            return [
                {
                    "id": t.id,
                    "name": t.name,
                    "light": t.color or "#888888",
                    "dark": t.color_dark or t.color or "#999999",
                    "is_remainder": t.is_remainder,
                }
                for t in rows
            ]
    except Exception:  # DB not migrated yet etc. - the shell must still render
        log.exception("cannot load tenant palette")
        return []


def _context(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    has_session = "session" in request.scope
    palette = tenant_palette(request)
    return {
        "current_user": user,
        "csrf_token": get_csrf_token(request) if has_session else "",
        "nav": [(label, url) for label, url, admin in NAV if not admin or (user is not None and user.is_admin)],
        "tenant_palette": palette,
        "tenant_palette_json": Markup(
            json.dumps({str(t["id"]): t for t in palette}, ensure_ascii=False).replace("<", "\\u003c")
        ),
        "settings": getattr(request.app.state, "settings", None),
        "flashes": get_flashed_messages(request) if has_session else [],
    }


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_context])
_env = templates.env
_env.filters.update(
    {
        "num": format_number,
        "reading": format_reading,
        "decimals": stored_decimals,
        "kwh": format_kwh,
        "date_cs": format_date,
        "datetime_cs": format_datetime,
        "month_cs": format_month,
        "qmark": quality_mark,
        "quality_title": quality_title,
        "qvalue": qvalue_html,
    }
)
_env.globals.update(
    {"Quality": Quality, "QUALITY_LABELS": QUALITY_LABELS, "REMAINDER_TARIFF_NOTE": REMAINDER_TARIFF_NOTE}
)


def render_stub(request: Request, title: str, active_nav: str, description: str = "") -> Any:
    """Placeholder page used by router stubs ("připravuje se")."""
    return templates.TemplateResponse(
        request,
        "_stub.html",
        {"page_title": title, "active_nav": active_nav, "description": description},
    )
