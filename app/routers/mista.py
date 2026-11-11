"""Meter points (`/mista`, `/mista/{id}`): list, reading history, charts, replacements, photos (SPEC §6)."""

from __future__ import annotations

import itertools
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select

from app.deps import CurrentUser, DbSession, require_user
from app.domain.aligned import QZERO, qsum
from app.domain.consumption import register_delta
from app.models import Allocation, Meter, MeterPoint, Photo, Reading
from app.routers._reports_common import json_script
from app.services import load_network, meter_anomalies, meter_staleness, point_consumption_table
from app.services.reports import data_coverage, qjson
from app.templating import templates

router = APIRouter(prefix="/mista", dependencies=[Depends(require_user)])

ROLE_LABELS = {"main": "hlavní", "sub": "podružný", "control": "kontrolní"}
SOURCE_LABELS = {"manual": "ruční odečet", "live": "dálkový odečet"}


def _allocations(db, point_id: int | None = None) -> list[Allocation]:
    q = select(Allocation).order_by(Allocation.meter_point_id, Allocation.valid_from)
    if point_id is not None:
        q = q.where(Allocation.meter_point_id == point_id)
    return list(db.scalars(q))


def _active(a: Allocation, today: date) -> bool:
    return (a.valid_from is None or a.valid_from <= today) and (a.valid_to is None or today < a.valid_to)


@router.get("", name="mista")
def mista(request: Request, db: DbSession, user: CurrentUser):
    now = datetime.now()
    today = now.date()
    net = load_network(db)
    cov = data_coverage(net)
    stale = {}
    for st in meter_staleness(db, now, net=net):
        key = st.meter.meter_point_id
        prev = stale.get(key)
        if prev is None or (st.staleness.days_since or 0) > (prev.staleness.days_since or 0):
            stale[key] = st
    last_month = None
    if cov.last_month:
        pt = point_consumption_table(db, start=cov.last_month[0], end=cov.last_month[1], net=net)
        last_month = {p.id: (pt.total(p.id, 0) if pt.periods else QZERO) for p in net.points}
    allocs: dict[int, list] = {}
    for a in _allocations(db):
        if _active(a, today):
            allocs.setdefault(a.meter_point_id, []).append(net.tenants_by_id.get(a.tenant_id))
    rows = []
    for p in net.points:
        meters = [net.meters[mid] for mid in p.meter_ids]
        rows.append(
            {
                "point": p,
                "meters": meters,
                "active_meters": [m for m in meters if m.active],
                "tenants": [t for t in allocs.get(p.id, []) if t],
                "stale": stale.get(p.id),
                "last_month": last_month.get(p.id) if last_month else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "mista/index.html",
        {
            "page_title": "Měřicí místa",
            "active_nav": "/mista",
            "rows": rows,
            "cov": cov,
            "today": today,
            "ROLE_LABELS": ROLE_LABELS,
            "SOURCE_LABELS": SOURCE_LABELS,
        },
    )


@router.get("/{point_id}", name="misto")
def misto(request: Request, point_id: int, db: DbSession, user: CurrentUser):
    point = db.get(MeterPoint, point_id)
    if point is None:
        raise HTTPException(404, "Měřicí místo neexistuje.")
    now = datetime.now()
    today = now.date()
    net = load_network(db)
    cov = data_coverage(net)
    info = net.points_by_id[point_id]
    meters = list(db.scalars(select(Meter).where(Meter.meter_point_id == point_id).order_by(Meter.id)))
    meter_ids = [m.id for m in meters]
    meter_by_id = {m.id: m for m in meters}
    allocations = _allocations(db, point_id)
    current_tenant = next((net.tenants_by_id.get(a.tenant_id) for a in allocations if _active(a, today)), None)

    # reading history (incl. pending), grouped by meter + time; raw delta to the previous confirmed reading
    readings = (
        list(
            db.scalars(
                select(Reading).where(Reading.meter_id.in_(meter_ids)).order_by(Reading.taken_at, Reading.register)
            )
        )
        if meter_ids
        else []
    )
    prev_val: dict[tuple[int, str], object] = {}
    grouped: dict[tuple[int, datetime], dict] = {}
    for r in readings:
        key = (r.meter_id, r.taken_at)
        g = grouped.setdefault(key, {"meter": meter_by_id[r.meter_id], "taken_at": r.taken_at, "regs": {}, "notes": []})
        delta = None
        if r.status == "confirmed":
            pv = prev_val.get((r.meter_id, r.register))
            if pv is not None:
                delta = register_delta(pv, r.value, meter_by_id[r.meter_id].digits)
            prev_val[(r.meter_id, r.register)] = r.value
        g["regs"][r.register] = {"r": r, "delta": delta}
        for txt in (r.note, f"nahlásil: {r.reported_by}" if r.reported_by else ""):
            if txt and txt not in g["notes"]:
                g["notes"].append(txt)
    history = sorted(grouped.values(), key=lambda g: (g["taken_at"], g["meter"].id), reverse=True)
    registers = net.point_registers(point_id) if info.meter_ids else ["T1"]

    # charts: counter states (confirmed readings) + aligned monthly consumption
    state_series = []
    for m in meters:
        for reg in m.registers or ["T1"]:
            pts = [
                [int(rp.taken_at.timestamp() * 1000), float(rp.value), rp.source]
                for rp in net.series[m.id].readings.get(reg, ())
            ]
            if pts:
                state_series.append({"label": f"{m.serial} {reg}", "register": reg, "points": pts})
    monthly = None
    month_rows = []
    if cov.first and cov.end and cov.end > cov.first and info.meter_ids:
        pt = point_consumption_table(db, start=cov.first, end=cov.end, net=net)
        n = len(pt.periods)
        monthly = {
            "labels": [f"{s.start.month}/{str(s.start.year)[2:]}" for s in pt.periods],
            "tenant_id": current_tenant.id if current_tenant else None,
            "series": [
                {"label": reg, "values": [qjson(pt.value(point_id, reg, i), 0) for i in range(n)]} for reg in registers
            ],
        }
        for i in reversed(range(n)):
            month_rows.append(
                {
                    "label": pt.periods[i].label,
                    "regs": {reg: pt.value(point_id, reg, i) for reg in registers},
                    "total": qsum(pt.value(point_id, reg, i) for reg in registers),
                }
            )

    photo_ids = {r.photo_id for r in readings if r.photo_id}
    photos = []
    if meter_ids:
        cond = Photo.meter_id.in_(meter_ids)
        if photo_ids:
            cond = or_(cond, Photo.id.in_(photo_ids))
        photos = list(db.scalars(select(Photo).where(cond).order_by(Photo.taken_at.desc()).limit(60)))
    anomalies = [a for a in meter_anomalies(db, net=net) if a.meter.meter_point_id == point_id]
    stale = [s for s in meter_staleness(db, now, net=net) if s.meter.meter_point_id == point_id]
    replacements = []
    ordered = sorted(meters, key=lambda m: (m.installed_on or date.min, m.id))
    for a, b in itertools.pairwise(ordered):
        replacements.append({"old": a, "new": b, "date": b.installed_on or a.removed_on})
    children = [p for p in net.points if p.parent_point_id == point_id]
    parent = net.points_by_id.get(info.parent_point_id) if info.parent_point_id else None
    return templates.TemplateResponse(
        request,
        "mista/detail.html",
        {
            "page_title": point.name,
            "active_nav": "/mista",
            "point": point,
            "info": info,
            "meters": meters,
            "allocations": allocations,
            "tenants_by_id": net.tenants_by_id,
            "current_tenant": current_tenant,
            "history": history,
            "registers": registers,
            "state_json": json_script(
                {"series": state_series, "tenant_id": current_tenant.id if current_tenant else None}
            ),
            "has_states": bool(state_series),
            "monthly_json": json_script(monthly) if monthly else None,
            "month_rows": month_rows,
            "photos": photos,
            "anomalies": anomalies,
            "stale": stale,
            "replacements": replacements,
            "children": children,
            "parent": parent,
            "cov": cov,
            "today": today,
            "ROLE_LABELS": ROLE_LABELS,
            "SOURCE_LABELS": SOURCE_LABELS,
        },
    )
