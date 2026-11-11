"""Settlement (vyúčtování) computations: results over an interval, finalize check, snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.audit import jsonable
from app.domain.aligned import BoundaryValue, Quality, QValue
from app.domain.allocation import validity_window
from app.domain.interpolation import Interpolator
from app.formatting import format_date
from app.services.consumption import (
    REGISTERS,
    IntervalResult,
    TenantPeriodTable,
    consumption_over_interval,
    tenant_consumption_table,
)
from app.services.network import Network, load_network
from app.services.periods import PeriodSpan, settlement_month_boundaries

SNAPSHOT_VERSION = 1


def settlement_boundaries(start: date, end: date) -> list[datetime]:
    """Period boundaries of a settlement: ``start``, every month start inside, ``end``."""
    return settlement_month_boundaries(start, end)


@dataclass(frozen=True, slots=True)
class BoundaryIssue:
    meter_id: int
    serial: str
    point_name: str
    register: str
    boundary: BoundaryValue

    @property
    def message(self) -> str:
        b = self.boundary
        when = f"{b.t.day}.{b.t.month}.{b.t.year}"
        if b.quality == Quality.PROVISIONAL:
            who = f"{self.point_name} ({self.serial}) {self.register}"
            return f"{who}: stav k {when} je předběžný (chybí pozdější odečet)."
        if b.after is None and b.before is not None:  # removed meter without a final reading
            return (
                f"{self.point_name} ({self.serial}) {self.register}: stav k {when} je odhad – elektroměr byl "
                f"odebrán bez konečného odečtu, spotřeba od odečtu {format_date(b.before.taken_at)} je dopočtena."
            )
        src = []
        if b.before is not None:
            src.append(f"{format_date(b.before.taken_at)}")
        if b.after is not None:
            src.append(f"{format_date(b.after.taken_at)}")
        return f"{self.point_name} ({self.serial}) {self.register}: stav k {when} je odhad z odečtů {' a '.join(src)}."


@dataclass(frozen=True, slots=True)
class FinalizeCheck:
    can_finalize: bool  # False when any relevant boundary value is provisional
    provisional: list[BoundaryIssue]
    estimated: list[BoundaryIssue]


def settlement_check(
    db: Session,
    start: date | datetime,
    end: date | datetime,
    *,
    interpolator: Interpolator | None = None,
    net: Network | None = None,
    interval: IntervalResult | None = None,
) -> FinalizeCheck:
    """Can a settlement over [start, end] be finalized? (SPEC §3: no ``provisional`` boundary components.)

    Relevant meters: those on main points and on points allocated to a non-remainder
    tenant during the interval. ``estimated`` boundaries are allowed but listed.
    """
    net = net or load_network(db)
    interval = interval or consumption_over_interval(db, start, end, interpolator=interpolator, net=net)
    t0, t1 = interval.start, interval.end
    remainder_ids = {t.id for t in net.tenants if t.is_remainder}
    relevant = set(net.main_point_ids) | {
        r.meter_point_id
        for r in net.rules
        if r.tenant_id not in remainder_ids and validity_window(r, t0, t1) is not None
    }
    prov: list[BoundaryIssue] = []
    est: list[BoundaryIssue] = []
    for row in interval.meters:
        if row.meter_point_id not in relevant or row.start.method == "after_removal":
            continue  # irrelevant point, or a meter removed before the interval
        name = net.points_by_id[row.meter_point_id].name
        for b in (row.start, row.end):
            issue = BoundaryIssue(row.meter_id, row.serial, name, row.register, b)
            if b.quality == Quality.PROVISIONAL:
                prov.append(issue)
            elif b.quality == Quality.ESTIMATED:
                est.append(issue)
    return FinalizeCheck(not prov, prov, est)


@dataclass
class SettlementResult:
    start: date
    end: date
    mode: str  # always "months" (aligned months between any start/end); kept in the snapshot format
    periods: list[PeriodSpan]
    table: TenantPeriodTable  # per period × tenant × register
    interval: IntervalResult  # whole interval: per tenant/point/meter incl. start/end states
    check: FinalizeCheck


def compute_settlement(
    db: Session, start: date, end: date, *, interpolator: Interpolator | None = None
) -> SettlementResult:
    net = load_network(db)
    b = settlement_boundaries(start, end)
    table = tenant_consumption_table(db, boundaries=b, interpolator=interpolator, net=net)
    interval = consumption_over_interval(db, start, end, interpolator=interpolator, net=net)
    check = settlement_check(db, start, end, net=net, interval=interval)
    return SettlementResult(start, end, "months", table.periods, table, interval, check)


def _q(v: QValue) -> dict[str, Any]:
    return {"value": v.value, "quality": v.quality.value, "estimated_kwh": v.estimated_kwh}


def _b(b: BoundaryValue) -> dict[str, Any]:
    return {
        "t": b.t,
        "value": b.display,
        "quality": b.quality.value,
        "method": b.method,
        "before": b.before.taken_at if b.before else None,
        "after": b.after.taken_at if b.after else None,
    }


def settlement_snapshot(result: SettlementResult) -> dict[str, Any]:
    """JSON-serializable snapshot stored in ``Settlement.snapshot`` at finalization."""
    t = result.table
    points = {p.id: p for p in result.interval.point_infos}
    data = {
        "version": SNAPSHOT_VERSION,
        "start": result.start,
        "end": result.end,
        "mode": result.mode,
        "periods": [{"key": p.key, "label": p.label, "start": p.start, "end": p.end} for p in result.periods],
        "tenants": [
            {
                "id": tn.id,
                "name": tn.name,
                "is_remainder": tn.is_remainder,
                "periods": {reg: [_q(v) for v in t.values[tn.id][reg]] for reg in REGISTERS},
                "totals": {reg: _q(result.interval.tenants[tn.id][reg]) for reg in REGISTERS},
                "points": result.interval.tenant_points.get(tn.id, []),
            }
            for tn in t.tenants
        ],
        "main": {reg: _q(v) for reg, v in result.interval.main.items()},
        "meters": [
            {
                "meter_id": r.meter_id,
                "serial": r.serial,
                "point_id": r.meter_point_id,
                "point_name": points[r.meter_point_id].name,
                "register": r.register,
                "start": _b(r.start),
                "end": _b(r.end),
                "consumption": _q(r.consumption),
                "installed_on": r.installed_on,
                "removed_on": r.removed_on,
                "note": r.note,
            }
            for r in result.interval.meters
        ],
        "check": {
            "can_finalize": result.check.can_finalize,
            "provisional": [i.message for i in result.check.provisional],
            "estimated": [i.message for i in result.check.estimated],
        },
    }
    return jsonable(data)
