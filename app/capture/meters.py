"""Creating a meter from the review (unknown serial): new meter on a point, or a replacement."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.capture.readings import InputError, write_reading
from app.formatting import format_date
from app.models import Meter, MeterPoint, Reading, User


def create_meter(
    db: Session,
    user: User,
    *,
    serial: str,
    point_id: int | None,
    new_point_name: str = "",
    registers: list[str],
    digits: int = 5,
    decimals_shown: int = 0,
    installed_on: date | None = None,
    replace_meter_id: int | None = None,
    display_note: str = "",
    initial: dict[str, Decimal] | None = None,
    old_final: dict[str, Decimal] | None = None,
) -> Meter:
    """Add a meter (no commit). ``replace_meter_id``: the old meter on the same point gets
    ``removed_on`` = ``installed_on`` (like an imported meter replacement; a replacement keeps the point's history).

    Like the replacement in /sprava: ``initial`` = the new meter's state at ``installed_on`` (typically 0),
    ``old_final`` = the old meter's last state at the replacement. Without them the consumption between the
    old meter's last reading and the replacement is estimated (extrapolated up to the removal), and the new
    meter counts from its first reading."""
    serial = serial.strip()
    if not serial:
        raise InputError("Zadejte výrobní číslo.")
    if db.scalar(select(Meter).where(func.lower(Meter.serial) == serial.lower())):
        raise InputError(f"Elektroměr {serial} už existuje.")
    regs = [r for r in ("T1", "T2") if r in registers] or ["T1"]
    if not 1 <= digits <= 12 or not 0 <= decimals_shown <= 3:
        raise InputError("Neplatný počet míst.")
    if point_id:
        point = db.get(MeterPoint, point_id)
        if point is None:
            raise InputError("Neznámé měřicí místo.")
    else:
        name = new_point_name.strip()
        if not name:
            raise InputError("Vyberte měřicí místo nebo zadejte název nového.")
        if db.scalar(select(MeterPoint).where(MeterPoint.name == name)):
            raise InputError(f"Měřicí místo „{name}“ už existuje – vyberte ho ze seznamu.")
        last = db.scalar(select(func.max(MeterPoint.sort_order))) or 0
        point = MeterPoint(name=name, sort_order=last + 10, role="sub", active=True, description="", note="")
        db.add(point)
        db.flush()
        audit.record_create(db, user, point, note="založeno z kontroly fotek")
    if replace_meter_id:
        old = db.get(Meter, replace_meter_id)
        if old is None or old.meter_point_id != point.id:
            raise InputError("Nahrazovaný elektroměr nepatří k vybranému místu.")
        if installed_on is None:
            raise InputError("Pro výměnu zadejte datum instalace.")
        last_old = db.scalar(
            select(func.max(Reading.taken_at)).where(Reading.meter_id == old.id, Reading.status == "confirmed")
        )
        if last_old is not None and last_old.date() > installed_on:
            raise InputError(f"Starý elektroměr {old.serial} má odečet po datu výměny ({format_date(last_old)}).")
        if old.removed_on is None:
            ch = audit.apply_changes(old, {"removed_on": installed_on})
            audit.record_update(db, user, old, ch, note=f"výměna za {serial}")
    meter = Meter(
        meter_point_id=point.id,
        serial=serial,
        registers=regs,
        digits=digits,
        decimals_shown=decimals_shown,
        installed_on=installed_on,
        display_note=display_note.strip(),
        source_type="manual",
        note="založeno z kontroly fotek",
    )
    db.add(meter)
    db.flush()
    audit.record_create(db, user, meter, note="založeno z kontroly fotek")
    if (initial or old_final) and installed_on is None:
        raise InputError("Počáteční / konečný stav vyžaduje datum instalace.")
    targets = [(meter, initial or {})]
    if replace_meter_id and old_final:
        targets.append((old, old_final))
    for m, values in targets:
        for reg, value in values.items():
            if reg not in (m.registers or ["T1"]):
                continue
            res = write_reading(
                db,
                user,
                meter_id=m.id,
                register=reg,
                day=installed_on,
                value=value,
                source="manual",
                note=f"výměna elektroměru ({'počáteční' if m is meter else 'konečný'} stav)",
                audit_note="výměna z kontroly fotek",
            )
            if res.status == "conflict":
                raise InputError(f"{m.serial} {reg}: {res.message}")
    return meter
