"""Administration (`/sprava`, admin only): tenants, meter points (walk order), meters (incl. replacement),
allocations, users/roles and the audit log. Every mutation is audited (app.audit)."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.deps import AdminUser, DbSession, require_admin
from app.domain.consumption import as_datetime
from app.models import (
    POINT_ROLES,
    SOURCE_TYPES,
    USER_ROLES,
    Allocation,
    AuditLog,
    Meter,
    MeterPoint,
    Reading,
    Tenant,
    User,
)
from app.routers._reports_common import parse_int
from app.sources import available_sources
from app.templating import flash, format_reading, templates

router = APIRouter(prefix="/sprava", dependencies=[Depends(require_admin)])

SECTIONS = [
    ("Přehled", "/sprava"),
    ("Nájemci", "/sprava/najemci"),
    ("Měřicí místa a elektroměry", "/sprava/mista"),
    ("Alokace", "/sprava/alokace"),
    ("Uživatelé", "/sprava/uzivatele"),
    ("Audit", "/sprava/audit"),
]
ROLE_LABELS = {"main": "hlavní", "sub": "podružný", "control": "kontrolní"}
USER_ROLE_LABELS = {"admin": "administrátor", "reader": "čtenář"}
SOURCE_LABELS = {"manual": "ruční odečet", "live": "dálkový odečet"}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ENTITY_LABELS = {
    "tenants": "nájemce",
    "meter_points": "měřicí místo",
    "meters": "elektroměr",
    "allocations": "alokace",
    "users": "uživatel",
    "readings": "odečet",
    "settlements": "vyúčtování",
    "rounds": "obchůzka (zrušeno, archiv)",
    "point_skips": "přeskočené místo",
    "photos": "fotka",
}


class FormError(Exception):
    pass


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _render(request: Request, template: str, section: str, **ctx: Any):
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_nav": "/sprava",
            "section": section,
            "sections": SECTIONS,
            "ROLE_LABELS": ROLE_LABELS,
            "USER_ROLE_LABELS": USER_ROLE_LABELS,
            "SOURCE_LABELS": SOURCE_LABELS,
            **ctx,
        },
    )


# --- form parsing -------------------------------------------------------------------


def _str(form: Any, key: str, *, required: bool = False, label: str = "") -> str:
    v = str(form.get(key) or "").strip()
    if required and not v:
        raise FormError(f"Vyplňte pole „{label or key}“.")
    return v


def _int(
    form: Any, key: str, *, lo: int | None = None, hi: int | None = None, default: int | None = None, label: str = ""
) -> int | None:
    raw = _str(form, key)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError as e:
        raise FormError(f"„{label or key}“ musí být celé číslo.") from e
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise FormError(f"„{label or key}“ musí být v rozsahu {lo}–{hi}.")
    return v


def _date(form: Any, key: str, *, label: str = "") -> date | None:
    raw = _str(form, key)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise FormError(f"„{label or key}“ není platné datum.") from e


def _dec(form: Any, key: str, *, label: str = "", default: Decimal | None = None) -> Decimal | None:
    raw = _str(form, key).replace(" ", "").replace("\xa0", "").replace(",", ".")
    if not raw:
        return default
    try:
        v = Decimal(raw)
    except InvalidOperation as e:
        raise FormError(f"„{label or key}“ musí být číslo.") from e
    if not v.is_finite():
        raise FormError(f"„{label or key}“ musí být číslo.")
    return v


def _bool(form: Any, key: str) -> bool:
    return str(form.get(key) or "") in ("1", "on", "true", "yes")


def _msg(e: Exception) -> str:
    if isinstance(e, IntegrityError):
        return "Záznam koliduje s existujícím (duplicitní název / výrobní číslo / odečet)."
    return str(e)


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise FormError("Záznam koliduje s existujícím (duplicitní název / výrobní číslo / odečet).") from e


def _get(db: Session, model: Any, obj_id: int) -> Any:
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, "Záznam neexistuje.")
    return obj


# --- overview -------------------------------------------------------------------------


@router.get("", name="sprava")
def sprava(request: Request, db: DbSession, user: AdminUser):
    counts = {
        "tenants": db.scalar(select(func.count(Tenant.id))),
        "points": db.scalar(select(func.count(MeterPoint.id))),
        "meters": db.scalar(select(func.count(Meter.id))),
        "allocations": db.scalar(select(func.count(Allocation.id))),
        "users": db.scalar(select(func.count(User.id))),
    }
    recent = list(db.scalars(select(AuditLog).order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(10)))
    return _render(
        request,
        "sprava/index.html",
        "/sprava",
        page_title="Správa",
        counts=counts,
        recent=recent,
        fmt_changes=_fmt_changes,
        ENTITY_LABELS=ENTITY_LABELS,
    )


# --- tenants ----------------------------------------------------------------------------


def _tenant_values(db: Session, form: Any, current: Tenant | None) -> dict[str, Any]:
    name = _str(form, "name", required=True, label="Název")
    color = _str(form, "color") or None
    color_dark = _str(form, "color_dark") or None
    for c in (color, color_dark):
        if c and not HEX.match(c):
            raise FormError("Barva musí být ve tvaru #rrggbb.")
    is_remainder = _bool(form, "is_remainder")
    if is_remainder:
        other = db.scalar(select(Tenant).where(Tenant.is_remainder.is_(True)))
        if other is not None and (current is None or other.id != current.id):
            raise FormError(f"Zbytkový nájemce už existuje ({other.name}); může být jen jeden.")
    return {
        "name": name,
        "is_remainder": is_remainder,
        "color": color,
        "color_dark": color_dark,
        "sort_order": _int(form, "sort_order", default=0, label="Pořadí"),
        "active": _bool(form, "active"),
        "note": _str(form, "note"),
    }


@router.get("/najemci", name="sprava_najemci")
def najemci(request: Request, db: DbSession, user: AdminUser):
    tenants = list(db.scalars(select(Tenant).order_by(Tenant.sort_order, Tenant.id)))
    n_alloc = dict(
        db.execute(select(Allocation.tenant_id, func.count(Allocation.id)).group_by(Allocation.tenant_id)).all()
    )
    return _render(
        request,
        "sprava/najemci.html",
        "/sprava/najemci",
        page_title="Správa – nájemci",
        tenants=tenants,
        n_alloc=n_alloc,
        t=None,
    )


@router.post("/najemci", name="sprava_najemci_create")
async def najemci_create(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    try:
        t = Tenant(**_tenant_values(db, form, None))
        db.add(t)
        audit.record_create(db, user, t)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect("/sprava/najemci")
    flash(request, f"Nájemce {t.name} vytvořen.", "success")
    return _redirect("/sprava/najemci")


@router.get("/najemci/{tenant_id}", name="sprava_najemce")
def najemce(request: Request, tenant_id: int, db: DbSession, user: AdminUser):
    t = _get(db, Tenant, tenant_id)
    allocs = list(db.scalars(select(Allocation).where(Allocation.tenant_id == t.id)))
    return _render(
        request, "sprava/najemce.html", "/sprava/najemci", page_title=f"Správa – {t.name}", t=t, allocs=allocs
    )


@router.post("/najemci/{tenant_id}", name="sprava_najemce_update")
async def najemce_update(request: Request, tenant_id: int, db: DbSession, user: AdminUser):
    t = _get(db, Tenant, tenant_id)
    form = await request.form()
    try:
        changes = audit.apply_changes(t, _tenant_values(db, form, t))
        if changes:
            audit.record_update(db, user, t, changes)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect(f"/sprava/najemci/{tenant_id}")
    flash(request, "Uloženo." if changes else "Beze změn.", "success" if changes else "info")
    return _redirect("/sprava/najemci")


@router.post("/najemci/{tenant_id}/smazat", name="sprava_najemce_delete")
def najemce_delete(request: Request, tenant_id: int, db: DbSession, user: AdminUser):
    t = _get(db, Tenant, tenant_id)
    if db.scalar(select(func.count(Allocation.id)).where(Allocation.tenant_id == t.id)):
        flash(request, "Nájemce má alokace – nejdřív je odstraňte, nebo ho jen označte jako neaktivního.", "error")
        return _redirect(f"/sprava/najemci/{tenant_id}")
    audit.record_delete(db, user, t)
    db.delete(t)
    db.commit()
    flash(request, f"Nájemce {t.name} smazán.", "success")
    return _redirect("/sprava/najemci")


# --- meter points ------------------------------------------------------------------------


def _point_values(db: Session, form: Any, current: MeterPoint | None) -> dict[str, Any]:
    role = _str(form, "role") or "sub"
    if role not in POINT_ROLES:
        raise FormError("Neplatná role.")
    parent_id = _int(form, "parent_point_id")
    if parent_id is not None:
        parent = db.get(MeterPoint, parent_id)
        if parent is None or (current is not None and parent.id == current.id):
            raise FormError("Neplatné nadřazené místo.")
    return {
        "name": _str(form, "name", required=True, label="Název"),
        "description": _str(form, "description"),
        "role": role,
        "parent_point_id": parent_id,
        "sort_order": _int(form, "sort_order", default=0, label="Pořadí"),
        "active": _bool(form, "active"),
        "note": _str(form, "note"),
    }


@router.get("/mista", name="sprava_mista")
def mista(request: Request, db: DbSession, user: AdminUser):
    points = list(db.scalars(select(MeterPoint).order_by(MeterPoint.sort_order, MeterPoint.id)))
    return _render(
        request,
        "sprava/mista.html",
        "/sprava/mista",
        page_title="Správa – měřicí místa",
        points=points,
        p=None,
        all_points=points,
    )


@router.post("/mista", name="sprava_mista_create")
async def mista_create(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    try:
        p = MeterPoint(**_point_values(db, form, None))
        db.add(p)
        audit.record_create(db, user, p)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect("/sprava/mista")
    flash(request, f"Měřicí místo {p.name} vytvořeno – přidejte mu elektroměr.", "success")
    return _redirect(f"/sprava/mista/{p.id}")


@router.post("/mista/poradi", name="sprava_mista_order")
async def mista_order(request: Request, db: DbSession, user: AdminUser):
    """Bulk update of the walk order (sort_order) of all points."""
    form = await request.form()
    changed = 0
    try:
        for p in db.scalars(select(MeterPoint)):
            key = f"order_{p.id}"
            if key not in form:
                continue
            v = _int(form, key, label=f"Pořadí {p.name}")
            if v is None:
                continue
            ch = audit.apply_changes(p, {"sort_order": v})
            if ch:
                audit.record_update(db, user, p, ch, note="pořadí obchůzky")
                changed += 1
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect("/sprava/mista")
    flash(request, f"Pořadí obchůzky uloženo ({changed} změn).", "success")
    return _redirect("/sprava/mista")


def _point_page(request: Request, db: Session, p: MeterPoint):
    points = list(db.scalars(select(MeterPoint).order_by(MeterPoint.sort_order, MeterPoint.id)))
    meters = list(db.scalars(select(Meter).where(Meter.meter_point_id == p.id).order_by(Meter.id)))
    n_read = (
        dict(
            db.execute(
                select(Reading.meter_id, func.count(Reading.id))
                .where(Reading.meter_id.in_([m.id for m in meters]))
                .group_by(Reading.meter_id)
            ).all()
        )
        if meters
        else {}
    )
    last = {}
    for m in meters:
        for reg in m.registers or ["T1"]:
            r = db.scalar(
                select(Reading)
                .where(Reading.meter_id == m.id, Reading.register == reg, Reading.status == "confirmed")
                .order_by(Reading.taken_at.desc())
            )
            if r is not None:
                last[(m.id, reg)] = r
    allocs = list(db.scalars(select(Allocation).where(Allocation.meter_point_id == p.id)))
    tenants = {t.id: t for t in db.scalars(select(Tenant))}
    return _render(
        request,
        "sprava/misto.html",
        "/sprava/mista",
        page_title=f"Správa – {p.name}",
        p=p,
        all_points=points,
        meters=meters,
        n_read=n_read,
        last=last,
        allocs=allocs,
        tenants=tenants,
        sources=available_sources(),
        today=date.today(),
    )


@router.get("/mista/{point_id}", name="sprava_misto")
def misto(request: Request, point_id: int, db: DbSession, user: AdminUser):
    return _point_page(request, db, _get(db, MeterPoint, point_id))


@router.post("/mista/{point_id}", name="sprava_misto_update")
async def misto_update(request: Request, point_id: int, db: DbSession, user: AdminUser):
    p = _get(db, MeterPoint, point_id)
    form = await request.form()
    try:
        changes = audit.apply_changes(p, _point_values(db, form, p))
        if changes:
            audit.record_update(db, user, p, changes)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect(f"/sprava/mista/{point_id}")
    flash(request, "Uloženo." if changes else "Beze změn.", "success" if changes else "info")
    return _redirect(f"/sprava/mista/{point_id}")


@router.post("/mista/{point_id}/smazat", name="sprava_misto_delete")
def misto_delete(request: Request, point_id: int, db: DbSession, user: AdminUser):
    p = _get(db, MeterPoint, point_id)
    if db.scalar(select(func.count(Meter.id)).where(Meter.meter_point_id == p.id)):
        flash(request, "Místo má elektroměry – nelze smazat. Označte ho jako neaktivní.", "error")
        return _redirect(f"/sprava/mista/{point_id}")
    for a in db.scalars(select(Allocation).where(Allocation.meter_point_id == p.id)):
        audit.record_delete(db, user, a, note=f"spolu s místem {p.name}")
        db.delete(a)
    audit.record_delete(db, user, p)
    db.delete(p)
    db.commit()
    flash(request, f"Místo {p.name} smazáno.", "success")
    return _redirect("/sprava/mista")


# --- meters -------------------------------------------------------------------------------


def _meter_values(form: Any, *, prefix: str = "") -> dict[str, Any]:
    source_type = _str(form, prefix + "source_type") or "manual"
    if source_type not in SOURCE_TYPES:
        raise FormError("Neplatný typ zdroje.")
    raw_cfg = _str(form, prefix + "source_config")
    cfg = None
    if raw_cfg:
        try:
            cfg = json.loads(raw_cfg)
        except ValueError as e:
            raise FormError("Konfigurace zdroje musí být platný JSON.") from e
        if not isinstance(cfg, dict):
            raise FormError("Konfigurace zdroje musí být JSON objekt.")
    installed = _date(form, prefix + "installed_on", label="Instalován")
    removed = _date(form, prefix + "removed_on", label="Odebrán")
    if installed and removed and removed < installed:
        raise FormError("Datum odebrání je před instalací.")
    return {
        "serial": _str(form, prefix + "serial", required=True, label="Výrobní číslo"),
        "registers": [r for r in ("T1", "T2") if _bool(form, f"{prefix}reg_{r}")] or None,
        "digits": _int(form, prefix + "digits", lo=1, hi=12, default=5, label="Počet míst"),
        "decimals_shown": _int(form, prefix + "decimals_shown", lo=0, hi=3, default=0, label="Desetinná místa"),
        "installed_on": installed,
        "removed_on": removed,
        "source_type": source_type,
        "source_config": cfg,
        "note": _str(form, prefix + "note"),
        "display_note": _str(form, prefix + "display_note"),
    }


def _check_registers(values: dict[str, Any]) -> dict[str, Any]:
    if not values["registers"]:
        raise FormError("Elektroměr musí mít aspoň jeden registr.")
    return values


@router.post("/mista/{point_id}/elektromery", name="sprava_meter_create")
async def meter_create(request: Request, point_id: int, db: DbSession, user: AdminUser):
    p = _get(db, MeterPoint, point_id)
    form = await request.form()
    try:
        m = Meter(meter_point_id=p.id, **_check_registers(_meter_values(form)))
        db.add(m)
        audit.record_create(db, user, m)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect(f"/sprava/mista/{point_id}")
    flash(request, f"Elektroměr {m.serial} přidán.", "success")
    return _redirect(f"/sprava/mista/{point_id}")


@router.get("/elektromery/{meter_id}", name="sprava_meter")
def meter(request: Request, meter_id: int, db: DbSession, user: AdminUser):
    m = _get(db, Meter, meter_id)
    n = db.scalar(select(func.count(Reading.id)).where(Reading.meter_id == m.id))
    points = list(db.scalars(select(MeterPoint).order_by(MeterPoint.sort_order, MeterPoint.id)))
    return _render(
        request,
        "sprava/elektromer.html",
        "/sprava/mista",
        page_title=f"Správa – elektroměr {m.serial}",
        m=m,
        n_readings=n,
        points=points,
        sources=available_sources(),
    )


@router.post("/elektromery/{meter_id}", name="sprava_meter_update")
async def meter_update(request: Request, meter_id: int, db: DbSession, user: AdminUser):
    m = _get(db, Meter, meter_id)
    form = await request.form()
    try:
        values = _check_registers(_meter_values(form))
        new_point = _int(form, "meter_point_id")
        if new_point is not None and new_point != m.meter_point_id:
            if db.get(MeterPoint, new_point) is None:
                raise FormError("Neplatné měřicí místo.")
            values["meter_point_id"] = new_point
        removed_regs = set(m.registers or []) - set(values["registers"])
        if removed_regs and db.scalar(
            select(func.count(Reading.id)).where(Reading.meter_id == m.id, Reading.register.in_(removed_regs))
        ):
            raise FormError(f"Registr {', '.join(sorted(removed_regs))} má odečty – nelze ho odebrat.")
        changes = audit.apply_changes(m, values)
        if changes:
            audit.record_update(db, user, m, changes)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect(f"/sprava/elektromery/{meter_id}")
    flash(request, "Uloženo." if changes else "Beze změn.", "success" if changes else "info")
    return _redirect(f"/sprava/mista/{m.meter_point_id}")


@router.post("/elektromery/{meter_id}/smazat", name="sprava_meter_delete")
def meter_delete(request: Request, meter_id: int, db: DbSession, user: AdminUser):
    m = _get(db, Meter, meter_id)
    if db.scalar(select(func.count(Reading.id)).where(Reading.meter_id == m.id)):
        flash(request, "Elektroměr má odečty – nelze smazat. Nastavte datum odebrání.", "error")
        return _redirect(f"/sprava/elektromery/{meter_id}")
    pid = m.meter_point_id
    audit.record_delete(db, user, m)
    db.delete(m)
    db.commit()
    flash(request, f"Elektroměr {m.serial} smazán.", "success")
    return _redirect(f"/sprava/mista/{pid}")


@router.post("/mista/{point_id}/vymena", name="sprava_meter_replace")
async def meter_replace(request: Request, point_id: int, db: DbSession, user: AdminUser):
    """Record a meter replacement: old meter removed on date D (optional final readings),
    new meter installed on D (initial readings, default 0)."""
    p = _get(db, MeterPoint, point_id)
    form = await request.form()
    try:
        old = db.get(Meter, _int(form, "old_meter_id") or 0)
        if old is None or old.meter_point_id != p.id:
            raise FormError("Vyberte vyměňovaný elektroměr tohoto místa.")
        if old.removed_on is not None:
            raise FormError(f"Elektroměr {old.serial} už je odebrán ({old.removed_on:%d.%m.%Y}).")
        when = _date(form, "date", label="Datum výměny")
        if when is None:
            raise FormError("Vyplňte datum výměny.")
        last_old = db.scalar(select(func.max(Reading.taken_at)).where(Reading.meter_id == old.id))
        if last_old is not None and last_old.date() > when:
            raise FormError(f"Starý elektroměr má odečet po datu výměny ({last_old:%d.%m.%Y}).")
        values = _check_registers(_meter_values(form, prefix="new_"))
        values["installed_on"] = when
        values["removed_on"] = None
        new = Meter(meter_point_id=p.id, **values)
        db.add(new)
        ch = audit.apply_changes(old, {"removed_on": when})
        audit.record_update(db, user, old, ch, note=f"výměna za {new.serial}")
        audit.record_create(db, user, new, note=f"výměna za {old.serial}")
        when_dt = as_datetime(when)
        note = _str(form, "note") or f"Výměna elektroměru {old.serial} → {new.serial}"
        for meter_obj, prefix, regs in (
            (old, "old_final_", old.registers or ["T1"]),
            (new, "new_initial_", new.registers),
        ):
            for reg in regs:
                v = _dec(form, prefix + reg, label=f"Stav {reg}")
                if v is None:
                    continue
                if v < 0:
                    raise FormError("Stav nesmí být záporný.")
                exists = (
                    db.scalar(
                        select(Reading).where(
                            Reading.meter_id == meter_obj.id, Reading.register == reg, Reading.taken_at == when_dt
                        )
                    )
                    if meter_obj.id
                    else None
                )
                if exists is not None:
                    if exists.value != v:
                        raise FormError(
                            f"{meter_obj.serial} {reg} už má k {when:%d.%m.%Y} jiný odečet "
                            f"({format_reading(exists.value)})."
                        )
                    continue
                r = Reading(
                    meter=meter_obj,
                    register=reg,
                    taken_at=when_dt,
                    value=v,
                    source="manual",
                    status="confirmed",
                    created_by_id=user.id,
                    note=note,
                )
                db.add(r)
                audit.record_create(db, user, r, note=note)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect(f"/sprava/mista/{point_id}")
    flash(request, f"Výměna zaznamenána: {old.serial} → {new.serial} k {when:%d.%m.%Y}.", "success")
    return _redirect(f"/sprava/mista/{point_id}")


# --- allocations ----------------------------------------------------------------------------


def _check_overlap(db: Session, values: dict[str, Any], current_id: int | None) -> None:
    """Refuse allocating more than 100 % of a meter point at the same time (double counting).

    The remainder tenant's own rules are ignored by the calculation, so they do not count here.
    """
    tenant = db.get(Tenant, values["tenant_id"])
    if tenant is None or tenant.is_remainder:
        return
    vf, vt = values["valid_from"], values["valid_to"]
    total = values["factor"]
    clashes: list[str] = []
    q = (
        select(Allocation)
        .join(Tenant, Tenant.id == Allocation.tenant_id)
        .where(Allocation.meter_point_id == values["meter_point_id"], Tenant.is_remainder.is_(False))
    )
    for other in db.scalars(q):
        if other.id == current_id:
            continue
        if (vt is not None and other.valid_from is not None and other.valid_from >= vt) or (
            vf is not None and other.valid_to is not None and other.valid_to <= vf
        ):
            continue  # windows [from, to) do not overlap
        if other.tenant_id == values["tenant_id"]:
            raise FormError(
                f"Nájemce {tenant.name} už má toto místo alokované v překrývajícím se období – "
                "upravte platnost stávající alokace."
            )
        total += other.factor
        clashes.append(other.tenant.name)
    if total > 1:
        raise FormError(
            f"Místo by bylo v překrývajícím se období alokováno víc než ze 100 % (součet koeficientů {total}; "
            f"také: {', '.join(clashes)}). Nejdřív ukončete platnost stávající alokace."
        )


def _allocation_values(db: Session, form: Any, current_id: int | None = None) -> dict[str, Any]:
    tid = _int(form, "tenant_id")
    pid = _int(form, "meter_point_id")
    if tid is None or db.get(Tenant, tid) is None:
        raise FormError("Vyberte nájemce.")
    if pid is None or db.get(MeterPoint, pid) is None:
        raise FormError("Vyberte měřicí místo.")
    vf = _date(form, "valid_from", label="Platnost od")
    vt = _date(form, "valid_to", label="Platnost do")
    if vf and vt and vt <= vf:
        raise FormError("„Platnost do“ musí být po „Platnost od“.")
    factor = _dec(form, "factor", label="Koeficient", default=Decimal(1))
    if factor == 0:
        raise FormError("Koeficient nesmí být 0 (alokaci raději smažte).")
    values = {
        "tenant_id": tid,
        "meter_point_id": pid,
        "valid_from": vf,
        "valid_to": vt,
        "factor": factor,
        "note": _str(form, "note"),
    }
    _check_overlap(db, values, current_id)
    return values


def _allocation_page(request: Request, db: Session, a: Allocation | None):
    allocs = list(
        db.scalars(select(Allocation).join(MeterPoint).order_by(MeterPoint.sort_order, Allocation.valid_from))
    )
    tenants = list(db.scalars(select(Tenant).order_by(Tenant.sort_order, Tenant.id)))
    points = list(db.scalars(select(MeterPoint).order_by(MeterPoint.sort_order, MeterPoint.id)))
    allocated = {x.meter_point_id for x in allocs}
    return _render(
        request,
        "sprava/alokace.html",
        "/sprava/alokace",
        page_title="Správa – alokace",
        allocs=allocs,
        tenants=tenants,
        points=points,
        a=a,
        unallocated=[p for p in points if p.id not in allocated and p.role == "sub"],
        today=date.today(),
    )


@router.get("/alokace", name="sprava_alokace")
def alokace(request: Request, db: DbSession, user: AdminUser):
    return _allocation_page(request, db, None)


@router.get("/alokace/{allocation_id}", name="sprava_alokace_edit")
def alokace_edit(request: Request, allocation_id: int, db: DbSession, user: AdminUser):
    return _allocation_page(request, db, _get(db, Allocation, allocation_id))


@router.post("/alokace", name="sprava_alokace_create")
async def alokace_create(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    try:
        a = Allocation(**_allocation_values(db, form))
        db.add(a)
        audit.record_create(db, user, a)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect("/sprava/alokace")
    flash(request, "Alokace přidána.", "success")
    return _redirect("/sprava/alokace")


@router.post("/alokace/{allocation_id}", name="sprava_alokace_update")
async def alokace_update(request: Request, allocation_id: int, db: DbSession, user: AdminUser):
    a = _get(db, Allocation, allocation_id)
    form = await request.form()
    try:
        changes = audit.apply_changes(a, _allocation_values(db, form, a.id))
        if changes:
            audit.record_update(db, user, a, changes)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect(f"/sprava/alokace/{allocation_id}")
    flash(request, "Uloženo." if changes else "Beze změn.", "success" if changes else "info")
    return _redirect("/sprava/alokace")


@router.post("/alokace/{allocation_id}/smazat", name="sprava_alokace_delete")
def alokace_delete(request: Request, allocation_id: int, db: DbSession, user: AdminUser):
    a = _get(db, Allocation, allocation_id)
    audit.record_delete(db, user, a)
    db.delete(a)
    db.commit()
    flash(request, "Alokace smazána.", "success")
    return _redirect("/sprava/alokace")


# --- users ---------------------------------------------------------------------------------


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count(User.id)).where(User.role == "admin", User.active.is_(True))) or 0


@router.get("/uzivatele", name="sprava_uzivatele")
def uzivatele(request: Request, db: DbSession, user: AdminUser):
    users = list(db.scalars(select(User).order_by(User.active.desc(), User.email)))
    settings = request.app.state.settings
    return _render(
        request,
        "sprava/uzivatele.html",
        "/sprava/uzivatele",
        page_title="Správa – uživatelé",
        users=users,
        admin_emails={e.lower() for e in settings.admin_emails},
        domain=settings.allowed_domain,
    )


@router.post("/uzivatele", name="sprava_uzivatele_create")
async def uzivatele_create(request: Request, db: DbSession, user: AdminUser):
    form = await request.form()
    domain = request.app.state.settings.allowed_domain
    try:
        email = _str(form, "email", required=True, label="E-mail").lower()
        if "@" not in email or (domain and not email.endswith("@" + domain.lower())):
            raise FormError(f"E-mail musí být v doméně {domain}.")
        role = _str(form, "role") or "reader"
        if role not in USER_ROLES:
            raise FormError("Neplatná role.")
        u = User(email=email, name=_str(form, "name"), role=role, active=True)
        db.add(u)
        audit.record_create(db, user, u)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect("/sprava/uzivatele")
    flash(request, f"Uživatel {u.email} přidán; přihlásí se Google účtem.", "success")
    return _redirect("/sprava/uzivatele")


@router.post("/uzivatele/{user_id}", name="sprava_uzivatel_update")
async def uzivatel_update(request: Request, user_id: int, db: DbSession, user: AdminUser):
    u = _get(db, User, user_id)
    form = await request.form()
    try:
        role = _str(form, "role") or u.role
        if role not in USER_ROLES:
            raise FormError("Neplatná role.")
        active = _bool(form, "active")
        if u.id == user.id and (role != "admin" or not active):
            raise FormError("Nemůžete odebrat roli administrátora ani deaktivovat sami sebe.")
        if u.role == "admin" and u.active and (role != "admin" or not active) and _active_admins(db) <= 1:
            raise FormError("Musí zůstat aspoň jeden aktivní administrátor.")
        changes = audit.apply_changes(u, {"role": role, "active": active, "name": _str(form, "name")})
        if changes:
            audit.record_update(db, user, u, changes)
        _commit(db)
    except (FormError, IntegrityError) as e:
        db.rollback()
        flash(request, _msg(e), "error")
        return _redirect("/sprava/uzivatele")
    flash(request, f"{u.email}: uloženo." if changes else "Beze změn.", "success" if changes else "info")
    return _redirect("/sprava/uzivatele")


# --- audit log -------------------------------------------------------------------------------

AUDIT_PAGE = 50


@router.get("/audit", name="sprava_audit")
def audit_log(request: Request, db: DbSession, user: AdminUser):
    q = request.query_params
    entity = q.get("typ") or ""
    page = max(1, parse_int(q.get("strana"), 1) or 1)
    stmt = select(AuditLog)
    count = select(func.count(AuditLog.id))
    if entity:
        stmt = stmt.where(AuditLog.entity_type == entity)
        count = count.where(AuditLog.entity_type == entity)
    eid = parse_int(q.get("id"))
    if eid is not None:
        stmt = stmt.where(AuditLog.entity_id == eid)
        count = count.where(AuditLog.entity_id == eid)
    total = db.scalar(count) or 0
    rows = list(
        db.scalars(
            stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc()).offset((page - 1) * AUDIT_PAGE).limit(AUDIT_PAGE)
        )
    )
    types = [t for (t,) in db.execute(select(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type))]
    return _render(
        request,
        "sprava/audit.html",
        "/sprava/audit",
        page_title="Správa – audit",
        rows=rows,
        types=types,
        entity=entity,
        entity_id=eid,
        page=page,
        pages=max(1, (total + AUDIT_PAGE - 1) // AUDIT_PAGE),
        total=total,
        ENTITY_LABELS=ENTITY_LABELS,
        fmt_changes=_fmt_changes,
    )


def _fmt_changes(entry: AuditLog) -> str:
    ch = entry.changes
    if not ch:
        return ""
    if entry.action == "update" and all(isinstance(v, list) and len(v) == 2 for v in ch.values()):
        return "\n".join(f"{k}: {_short(v[0])} → {_short(v[1])}" for k, v in ch.items())
    return "\n".join(f"{k}: {_short(v)}" for k, v in ch.items() if k not in ("created_at", "updated_at"))


def _short(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else str(v)
    return s if len(s) <= 160 else s[:157] + "…"
