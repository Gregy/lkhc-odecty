"""Writing readings from capture flows (walk, review, grid, manual form) with audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.db import normalize_kwh
from app.formatting import format_reading
from app.models import Reading, User


class InputError(ValueError):
    """User input error; message is Czech."""


def parse_value(raw: str | None) -> Decimal | None:
    """Parse a user-typed kWh value ("19 212,5", "19212.5"); None for empty. Raises InputError."""
    if raw is None:
        return None
    s = str(raw).strip().replace(" ", "").replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise InputError(f"„{raw}“ není číslo.") from None
    if not d.is_finite() or d < 0:
        raise InputError("Hodnota musí být nezáporné číslo.")
    if d >= Decimal(10) ** 12:
        raise InputError("Hodnota je příliš velká.")
    if d != d.quantize(Decimal("0.001")):
        raise InputError("Nejvýše 3 desetinná místa.")
    return normalize_kwh(d)


def parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    s = raw.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise InputError(f"Neplatné datum „{raw}“.")


def day_start(d: date | datetime) -> datetime:
    return datetime.combine(d.date() if isinstance(d, datetime) else d, time.min)


@dataclass
class WriteResult:
    status: str  # created | updated | unchanged | conflict
    reading: Reading | None
    message: str = ""


def existing_reading(db: Session, meter_id: int, register: str, d: date) -> Reading | None:
    return db.scalar(
        select(Reading).where(
            Reading.meter_id == meter_id, Reading.register == register, Reading.taken_at == day_start(d)
        )
    )


def write_reading(
    db: Session,
    user: User | None,
    *,
    meter_id: int,
    register: str,
    day: date,
    value: Decimal,
    source: str,
    status: str = "confirmed",
    photo_id: int | None = None,
    reported_by: str = "",
    note: str = "",
    audit_note: str = "",
    client_ref: str | None = None,
    overwrite: bool = False,
) -> WriteResult:
    """Create (or, with ``overwrite``, update) the reading of a meter/register on ``day``.

    One reading per meter, register and day (stored at 00:00). An existing reading with
    the same value is left alone (idempotent); a different value is a conflict unless
    ``overwrite`` is set or the existing row was written by the same client submission.
    """
    value = normalize_kwh(value)
    cur = existing_reading(db, meter_id, register, day)
    if cur is not None:
        same_client = client_ref is not None and cur.client_ref == client_ref
        if cur.value == value:
            if cur.status == "pending" and status == "confirmed":
                changes = audit.apply_changes(cur, {"status": "confirmed"})
                audit.record_update(db, user, cur, changes, note=audit_note)
                return WriteResult("updated", cur)
            return WriteResult("unchanged", cur)
        if not (overwrite or same_client):
            return WriteResult("conflict", cur, f"Pro tento den už existuje odečet {format_reading(cur.value)}\xa0kWh.")
        if cur.status == "confirmed" and status != "confirmed":
            # never demote a confirmed value to an unconfirmed one (it would silently drop out of all
            # calculations, and discarding the pending entry would lose the original value)
            return WriteResult(
                "conflict",
                cur,
                f"Pro tento den už je potvrzený odečet {format_reading(cur.value)}\xa0kWh – "
                "pro přepsání nejdřív potvrďte varování.",
            )
        values: dict[str, object] = {"value": value, "status": status, "source": source}
        if photo_id is not None:
            values["photo_id"] = photo_id
        if reported_by:
            values["reported_by"] = reported_by
        if note:
            values["note"] = note
        changes = audit.apply_changes(cur, values)
        if changes:
            audit.record_update(db, user, cur, changes, note=audit_note)
        return WriteResult("updated", cur)
    if client_ref is not None:
        other = db.scalar(select(Reading).where(Reading.client_ref == client_ref))
        if other is not None:  # same submission moved to another day/meter: keep the first one
            return WriteResult("unchanged", other)
    r = Reading(
        meter_id=meter_id,
        register=register,
        taken_at=day_start(day),
        value=value,
        source=source,
        status=status,
        photo_id=photo_id,
        created_by_id=user.id if user else None,
        reported_by=reported_by,
        note=note,
        client_ref=client_ref,
    )
    db.add(r)
    db.flush()
    audit.record_create(db, user, r, note=audit_note)
    return WriteResult("created", r)
