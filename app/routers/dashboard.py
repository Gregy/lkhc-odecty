"""Dashboard (`/`): data freshness, consumption per tenant, monthly chart, balance, alerts (SPEC §6)."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.deps import CurrentUser, DbSession, require_user
from app.domain.aligned import QZERO, qsum
from app.models import Photo
from app.routers._reports_common import MONTHS_SHORT, json_script
from app.services import (
    alerts,
    consumption_over_interval,
    control_balances,
    load_network,
    meter_staleness,
    tenant_consumption_table,
)
from app.services.consumption import REGISTERS
from app.services.reports import data_coverage, qjson
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_user)])


def _shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29.2.
        return d.replace(year=d.year + years, day=28)


@router.get("/", name="dashboard")
def dashboard(request: Request, db: DbSession, user: CurrentUser):
    now = datetime.now()
    today = now.date()
    net = load_network(db)
    cov = data_coverage(net)
    days_main = (today - cov.last_main_reading.date()).days if cov.last_main_reading else None
    days_data = (today - cov.last_reading.date()).days if cov.last_reading else None
    stale = [s for s in meter_staleness(db, now, net=net) if s.staleness.overdue]
    all_alerts = alerts(db, now, net=net)
    other_alerts = [a for a in all_alerts if a.kind not in ("stale_meter", "pending_photos", "pending_readings")]
    pending_photos = db.scalar(select(func.count(Photo.id)).where(Photo.review_status == "new")) or 0
    pending_readings = next((a for a in all_alerts if a.kind == "pending_readings"), None)

    ctx: dict = {
        "page_title": "Přehled",
        "active_nav": "/",
        "cov": cov,
        "today": today,
        "days_main": days_main,
        "days_data": days_data,
        "stale": stale,
        "alerts": other_alerts,
        "pending_photos": pending_photos,
        "pending_readings": pending_readings,
        "tenants": net.tenants,
        "has_data": cov.last_month is not None,
    }
    if cov.last_month is None:
        return templates.TemplateResponse(request, "dashboard/index.html", ctx)

    lm_start, lm_end = cov.last_month
    year = lm_start.year
    ytd_start = max(date(year, 1, 1), cov.first or date(year, 1, 1))
    last = consumption_over_interval(db, lm_start, lm_end, net=net)
    ytd = consumption_over_interval(db, ytd_start, lm_end, net=net)
    prev_start, prev_end = _shift_year(ytd_start, -1), _shift_year(lm_end, -1)
    prev = (
        consumption_over_interval(db, prev_start, prev_end, net=net)
        if cov.first is not None and prev_start >= cov.first
        else None
    )
    rows = []
    for t in net.tenants:
        rows.append(
            {
                "tenant": t,
                "last": {reg: last.tenants[t.id][reg] for reg in REGISTERS} | {"total": last.tenant_total(t.id)},
                "ytd": {reg: ytd.tenants[t.id][reg] for reg in REGISTERS} | {"total": ytd.tenant_total(t.id)},
                "prev": prev.tenant_total(t.id) if prev else None,
            }
        )
    totals = {
        "last": {reg: last.main.get(reg, QZERO) for reg in REGISTERS},
        "ytd": {reg: ytd.main.get(reg, QZERO) for reg in REGISTERS},
    }
    totals["last"]["total"] = qsum(totals["last"][r] for r in REGISTERS)
    totals["ytd"]["total"] = qsum(totals["ytd"][r] for r in REGISTERS)
    totals["prev"] = qsum(prev.main.values()) if prev else None

    # monthly chart: current year (up to coverage) stacked per tenant + previous year's total
    cur = tenant_consumption_table(db, start=ytd_start, end=lm_end, net=net)
    prev_tab = None
    py_start = max(date(year - 1, 1, 1), cov.first or date(year - 1, 1, 1))
    if py_start < date(year, 1, 1):
        prev_tab = tenant_consumption_table(db, start=py_start, end=date(year, 1, 1), net=net)

    def by_month(tab, fn):
        out = [None] * 12
        if tab is None:
            return out
        for i, span in enumerate(tab.periods):
            out[span.start.month - 1] = fn(i)
        return out

    chart = {
        "labels": list(MONTHS_SHORT),
        "year": year,
        "prev_year": year - 1,
        "series": [
            {
                "id": t.id,
                "name": t.name,
                "values": by_month(cur, lambda i, t=t: qjson(cur.total(t.id, i), 0)),
            }
            for t in net.tenants
        ],
        "total": by_month(cur, lambda i: qjson(cur.main["T1"][i] + cur.main["T2"][i], 0)),
        "prev_total": by_month(prev_tab, lambda i: qjson(prev_tab.main["T1"][i] + prev_tab.main["T2"][i], 0)),
    }
    names = {p.id: p.name for p in net.points}
    balance_last = {
        (r.control_point_id, r.register): r
        for r in (control_balances(cur.points, len(cur.periods) - 1) if cur.periods else [])
    }
    balance = [
        {
            "point": net.points_by_id[r.control_point_id],
            "children": [names[c] for c in r.child_point_ids],
            "register": r.register,
            "last": balance_last.get((r.control_point_id, r.register)),
            "ytd": r,
        }
        for r in control_balances(cur.points)
    ]
    ctx.update(
        {
            "year": year,
            "lm_start": lm_start,
            "lm_end": lm_end,
            "ytd_start": ytd_start,
            "prev_start": prev_start,
            "prev_end": prev_end,
            "rows": rows,
            "totals": totals,
            "chart_json": json_script(chart),
            "chart_has_prev": any(v is not None for v in chart["prev_total"]),
            "balance": balance,
            "cur": cur,
        }
    )
    return templates.TemplateResponse(request, "dashboard/index.html", ctx)
