"""Load the metering network (points, meters, tenants, allocations, readings) into memory.

All service calculations run on this snapshot; the data set is small (hundreds to
a few thousand readings), so loading everything per request is cheap and keeps the
domain code pure. Only ``confirmed`` readings are used for calculations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.aligned import MeterSeries
from app.domain.allocation import AllocationRule, TenantRef
from app.domain.consumption import ReadingPoint
from app.models import Allocation, Meter, MeterPoint, Reading, Tenant


@dataclass(frozen=True, slots=True)
class TenantInfo:
    id: int
    name: str
    is_remainder: bool
    color: str | None
    color_dark: str | None
    sort_order: int
    active: bool

    @property
    def ref(self) -> TenantRef:
        return TenantRef(self.id, self.name, self.is_remainder)


@dataclass(frozen=True, slots=True)
class MeterInfo:
    id: int
    meter_point_id: int
    serial: str
    registers: tuple[str, ...]
    digits: int
    decimals_shown: int
    installed_on: date | None
    removed_on: date | None
    source_type: str
    note: str
    display_note: str

    @property
    def active(self) -> bool:
        return self.removed_on is None


@dataclass(frozen=True, slots=True)
class PointInfo:
    id: int
    name: str
    description: str
    role: str  # main | sub | control
    sort_order: int
    parent_point_id: int | None
    active: bool
    meter_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MeterColumn:
    """One meter × register column (like the spreadsheet)."""

    meter_id: int
    register: str
    serial: str
    meter_point_id: int
    point_name: str

    @property
    def key(self) -> tuple[int, str]:
        return (self.meter_id, self.register)


@dataclass
class Network:
    tenants: list[TenantInfo]  # by sort_order
    points: list[PointInfo]  # by sort_order (walk order)
    meters: dict[int, MeterInfo]
    rules: list[AllocationRule]
    series: dict[int, MeterSeries]  # meter_id -> readings by register
    points_by_id: dict[int, PointInfo] = field(default_factory=dict)
    tenants_by_id: dict[int, TenantInfo] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.points_by_id = {p.id: p for p in self.points}
        self.tenants_by_id = {t.id: t for t in self.tenants}

    @property
    def main_point_ids(self) -> list[int]:
        return [p.id for p in self.points if p.role == "main"]

    @property
    def tenant_refs(self) -> list[TenantRef]:
        return [t.ref for t in self.tenants]

    def point_meters(self, point_id: int) -> list[MeterSeries]:
        return [self.series[mid] for mid in self.points_by_id[point_id].meter_ids]

    def point_registers(self, point_id: int) -> list[str]:
        regs: set[str] = set()
        for mid in self.points_by_id[point_id].meter_ids:
            regs.update(self.meters[mid].registers)
        return sorted(regs)

    def columns(self) -> list[MeterColumn]:
        """Meter × register columns in walk order (point sort_order, then meter id)."""
        cols = []
        for p in self.points:
            for mid in p.meter_ids:
                m = self.meters[mid]
                for reg in m.registers:
                    cols.append(MeterColumn(mid, reg, m.serial, p.id, p.name))
        return cols

    def all_reading_times(self) -> list[datetime]:
        return sorted({r.taken_at for s in self.series.values() for rs in s.readings.values() for r in rs})


def load_network(db: Session, *, include_pending: bool = False) -> Network:
    tenants = [
        TenantInfo(t.id, t.name, t.is_remainder, t.color, t.color_dark, t.sort_order, t.active)
        for t in db.scalars(select(Tenant).order_by(Tenant.sort_order, Tenant.id))
    ]
    meters_rows = list(db.scalars(select(Meter).order_by(Meter.id)))
    meters = {
        m.id: MeterInfo(
            m.id,
            m.meter_point_id,
            m.serial,
            tuple(m.registers or ["T1"]),
            m.digits,
            m.decimals_shown,
            m.installed_on,
            m.removed_on,
            m.source_type,
            m.note,
            m.display_note,
        )
        for m in meters_rows
    }
    by_point: dict[int, list[int]] = {}
    for m in meters_rows:
        by_point.setdefault(m.meter_point_id, []).append(m.id)
    points = [
        PointInfo(
            p.id,
            p.name,
            p.description,
            p.role,
            p.sort_order,
            p.parent_point_id,
            p.active,
            tuple(by_point.get(p.id, [])),
        )
        for p in db.scalars(select(MeterPoint).order_by(MeterPoint.sort_order, MeterPoint.id))
    ]
    rules = [
        AllocationRule(a.tenant_id, a.meter_point_id, a.factor, a.valid_from, a.valid_to)
        for a in db.scalars(select(Allocation).order_by(Allocation.id))
    ]
    q = select(Reading.id, Reading.meter_id, Reading.register, Reading.taken_at, Reading.value, Reading.source)
    if not include_pending:
        q = q.where(Reading.status == "confirmed")
    readings: dict[int, dict[str, list[ReadingPoint]]] = {}
    for rid, mid, reg, t, v, src in db.execute(q.order_by(Reading.taken_at)):
        readings.setdefault(mid, {}).setdefault(reg, []).append(ReadingPoint(t, v, rid, src))
    series = {
        m.id: MeterSeries(
            meter_id=m.id,
            serial=m.serial,
            digits=m.digits,
            registers=m.registers,
            installed_on=m.installed_on,
            removed_on=m.removed_on,
            readings=readings.get(m.id, {}),
            meter_point_id=m.meter_point_id,
        )
        for m in meters.values()
    }
    return Network(tenants=tenants, points=points, meters=meters, rules=rules, series=series)
