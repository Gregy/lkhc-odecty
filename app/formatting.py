"""cs-CZ formatting of numbers and dates (pure, no app dependencies).

Shared by the Jinja filters (``app.templating``) and by Czech messages built in the
pure domain (``app.domain.anomalies``), so every user-facing number looks the same:
non-breaking space as thousands separator, decimal comma, proper minus sign.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

NBSP = "\xa0"


def format_number(value: Any, decimals: int = 0, *, sign: bool = False) -> str:
    """cs-CZ number: non-breaking space as thousands separator, decimal comma. None -> '–'."""
    if value is None or value == "":
        return "–"
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    q = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    d = d.quantize(q, rounding=ROUND_HALF_UP)
    if d == 0:
        d = abs(d)  # avoid "-0"
    s = f"{abs(d):,.{decimals}f}".replace(",", NBSP).replace(".", ",")
    if d < 0:
        s = "−" + s  # proper minus sign
    elif sign and d > 0:
        s = "+" + s
    return s


def format_reading(value: Any) -> str:
    """A counter state / kWh amount as read: whole number, or up to 3 decimals without trailing zeros."""
    if value is None or value == "":
        return "–"
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    d = d.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    decimals = 0
    for k in (3, 2, 1):
        if d != d.quantize(Decimal(1).scaleb(-(k - 1))):
            decimals = k
            break
    return format_number(d, decimals)


def format_kwh(value: Any, decimals: int = 0) -> str:
    v = format_number(value, decimals)
    return v if v == "–" else f"{v}{NBSP}kWh"


def format_date(value: date | datetime | None, with_year: bool = True) -> str:
    if value is None:
        return "–"
    s = f"{value.day}.{NBSP}{value.month}."
    return f"{s}{NBSP}{value.year}" if with_year else s


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "–"
    return f"{format_date(value)} {value:%H:%M}"
