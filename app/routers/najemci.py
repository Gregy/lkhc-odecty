"""Tenants (`/najemci`, `/najemci/{id}`): consumption per period and per meter point, chart (SPEC §6)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request

from app.deps import CurrentUser, DbSession, require_user
from app.domain.aligned import QZERO, QValue, point_consumption, qsum
from app.domain.allocation import validity_window
from app.routers._reports_common import MONTHS_SHORT, json_script, parse_int
from app.services import consumption_over_interval, load_network, tenant_consumption_table
from app.services.consumption import REGISTERS
from app.services.network import Network
from app.services.reports import data_coverage, data_years, qjson, year_range
from app.templating import templates

router = APIRouter(prefix="/najemci", dependencies=[Depends(require_user)])


def _shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _tenant_points(net: Network, tenant_id: int, start: date, end: date) -> list[dict]:
    out = []
    for r in net.rules:
        if r.tenant_id != tenant_id or validity_window(r, start, end) is None:
            continue
        out.append({"rule": r, "point": net.points_by_id[r.meter_point_id]})
    return out


@router.get("", name="najemci")
def najemci(request: Request, db: DbSession, user: CurrentUser):
    today = date.today()
    net = load_network(db)
    cov = data_coverage(net)
    ytd = prev = None
    year = None
    if cov.last_month:
        year = cov.last_month[0].year
        start = max(date(year, 1, 1), cov.first)
        ytd = consumption_over_interval(db, start, cov.end, net=net)
        ps, pe = _shift_year(start, -1), _shift_year(cov.end, -1)
        if cov.first and ps >= cov.first:
            prev = consumption_over_interval(db, ps, pe, net=net)
    rows = []
    for t in net.tenants:
        pts = _tenant_points(net, t.id, today, date(today.year + 1, 1, 1)) if not t.is_remainder else []
        rows.append(
            {
                "tenant": t,
                "points": [x["point"] for x in pts],
                "ytd": {reg: ytd.tenants[t.id][reg] for reg in REGISTERS} | {"total": ytd.tenant_total(t.id)}
                if ytd
                else None,
                "prev": prev.tenant_total(t.id) if prev else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "najemci/index.html",
        {
            "page_title": "Nájemci",
            "active_nav": "/najemci",
            "rows": rows,
            "year": year,
            "cov": cov,
            "today": today,
        },
    )


@router.get("/{tenant_id}", name="najemce")
def najemce(request: Request, tenant_id: int, db: DbSession, user: CurrentUser):
    today = date.today()
    net = load_network(db)
    tenant = net.tenants_by_id.get(tenant_id)
    if tenant is None:
        raise HTTPException(404, "Nájemce neexistuje.")
    cov = data_coverage(net)
    default_year = cov.last_month[0].year if cov.last_month else today.year
    year = parse_int(request.query_params.get("rok"), default_year) or default_year
    provisional = request.query_params.get("predbezne") == "1"
    rng = year_range(year, cov, include_provisional=provisional, today=today)
    ctx: dict = {
        "page_title": tenant.name,
        "active_nav": "/najemci",
        "tenant": tenant,
        "year": year,
        "years": data_years(cov, today),
        "provisional": provisional,
        "cov": cov,
        "today": today,
        "rng": rng,
        "months": [],
    }
    alloc_all = [
        {"rule": r, "point": net.points_by_id[r.meter_point_id]} for r in net.rules if r.tenant_id == tenant.id
    ]
    ctx["allocations"] = alloc_all
    if rng is None:
        return templates.TemplateResponse(request, "najemci/detail.html", ctx)

    tab = tenant_consumption_table(db, start=rng[0], end=rng[1], net=net)
    n = len(tab.periods)
    months = [
        {
            "label": s.label,
            "regs": {reg: tab.value(tenant.id, reg, i) for reg in REGISTERS},
            "total": tab.total(tenant.id, i),
        }
        for i, s in enumerate(tab.periods)
    ]
    sums = {reg: tab.period_sum(tenant.id, reg) for reg in REGISTERS} | {"total": tab.period_sum(tenant.id)}

    # previous year (same months) for the chart and comparison
    prev_tab = None
    if cov.first and date(year - 1, 1, 1) >= cov.first:
        prev_tab = tenant_consumption_table(db, start=date(year - 1, 1, 1), end=date(year, 1, 1), net=net)
    cur_vals: list = [None] * 12
    prev_vals: list = [None] * 12
    for i, s in enumerate(tab.periods):
        cur_vals[s.start.month - 1] = qjson(tab.total(tenant.id, i), 0)
    if prev_tab:
        for i, s in enumerate(prev_tab.periods):
            prev_vals[s.start.month - 1] = qjson(prev_tab.total(tenant.id, i), 0)
    chart = {
        "labels": list(MONTHS_SHORT),
        "year": year,
        "prev_year": year - 1,
        "tenant_id": tenant.id,
        "cur": cur_vals,
        "prev": prev_vals,
    }

    # breakdown per meter point (allocated tenants) or main − others (remainder)
    breakdown: list[dict] = []
    if tenant.is_remainder:
        main_row = {"label": "Hlavní elektroměr", "link": None, "values": [], "sum": QZERO, "sign": ""}
        for i in range(n):
            main_row["values"].append(tab.main["T1"][i] + tab.main["T2"][i])
        main_row["sum"] = qsum(main_row["values"])
        if net.main_point_ids:
            main_row["link"] = f"/mista/{net.main_point_ids[0]}"
        breakdown.append(main_row)
        for t in net.tenants:
            if t.is_remainder:
                continue
            vals = [tab.total(t.id, i) for i in range(n)]  # shown under "− name" (subtracted)
            breakdown.append(
                {"label": f"− {t.name}", "link": f"/najemci/{t.id}", "values": vals, "sum": qsum(vals), "tenant": t}
            )
    else:
        pt = tab.points
        for a in alloc_all:
            r = a["rule"]
            vals: list[QValue | None] = []
            for i, s in enumerate(tab.periods):
                win = validity_window(r, s.start, s.end)
                if win is None:
                    vals.append(None)
                elif win == (s.start, s.end):
                    vals.append(pt.total(r.meter_point_id, i).scaled(r.factor))
                else:
                    pc = point_consumption(
                        net.point_meters(r.meter_point_id), *win, registers=net.point_registers(r.meter_point_id)
                    )
                    vals.append(qsum(pc.values()).scaled(r.factor))
            if all(v is None for v in vals):
                continue
            breakdown.append(
                {
                    "label": a["point"].name + (f" × {r.factor}" if r.factor != 1 else ""),
                    "link": f"/mista/{r.meter_point_id}",
                    "values": vals,
                    "sum": qsum(v for v in vals if v is not None),
                }
            )
    ctx.update(
        {
            "months": months,
            "sums": sums,
            "periods": tab.periods,
            "chart_json": json_script(chart),
            "has_prev": any(prev_vals),
            "prev_sum": qsum(
                prev_tab.total(tenant.id, i)
                for i, s in enumerate(prev_tab.periods)
                if s.start.month in {p.start.month for p in tab.periods}
            )
            if prev_tab
            else None,
            "breakdown": breakdown,
        }
    )
    return templates.TemplateResponse(request, "najemci/detail.html", ctx)
