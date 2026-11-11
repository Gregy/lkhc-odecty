"""Reading-level queries: latest values, plausibility hints, staleness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.anomalies import (
    Anomaly,
    ExpectedRange,
    PlausibilityIssue,
    Staleness,
    check_reading,
    detect_anomalies,
    expected_range,
    staleness,
)
from app.domain.consumption import ReadingPoint, as_datetime
from app.models import Meter, Reading
from app.services.network import MeterInfo, Network, load_network


@dataclass(frozen=True, slots=True)
class ReadingInfo:
    id: int
    meter_id: int
    register: str
    taken_at: datetime
    value: Decimal
    source: str
    status: str
    photo_id: int | None
    reported_by: str
    note: str


def _info(r: Reading) -> ReadingInfo:
    return ReadingInfo(
        r.id,
        r.meter_id,
        r.register,
        r.taken_at,
        r.value,
        r.source,
        r.status,
        r.photo_id,
        r.reported_by,
        r.note,
    )


def latest_readings(
    db: Session, meter_ids: list[int] | None = None, *, before: datetime | None = None
) -> dict[tuple[int, str], ReadingInfo]:
    """Latest confirmed reading per (meter_id, register) (optionally strictly before ``before``)."""
    sub = select(Reading.meter_id, Reading.register, func.max(Reading.taken_at).label("t")).where(
        Reading.status == "confirmed"
    )
    if meter_ids is not None:
        sub = sub.where(Reading.meter_id.in_(meter_ids))
    if before is not None:
        sub = sub.where(Reading.taken_at < before)
    sub = sub.group_by(Reading.meter_id, Reading.register).subquery()
    q = select(Reading).join(
        sub,
        (Reading.meter_id == sub.c.meter_id) & (Reading.register == sub.c.register) & (Reading.taken_at == sub.c.t),
    )
    return {(r.meter_id, r.register): _info(r) for r in db.scalars(q)}


def _series(db: Session, meter_id: int, register: str) -> list[ReadingPoint]:
    q = (
        select(Reading.id, Reading.taken_at, Reading.value, Reading.source)
        .where(Reading.meter_id == meter_id, Reading.register == register, Reading.status == "confirmed")
        .order_by(Reading.taken_at)
    )
    return [ReadingPoint(t, v, rid, src) for rid, t, v, src in db.execute(q)]


def meter_expected_range(db: Session, meter_id: int, register: str, at: date | datetime) -> ExpectedRange | None:
    """Plausibility hint for a new reading at ``at`` (last value, usual daily usage, low/expected/high)."""
    meter = db.get(Meter, meter_id)
    if meter is None:
        raise LookupError(meter_id)
    return expected_range(_series(db, meter_id, register), at, meter.digits)


def check_new_reading(
    db: Session,
    meter_id: int,
    register: str,
    value: Decimal,
    at: date | datetime,
    *,
    exclude_reading_id: int | None = None,
) -> list[PlausibilityIssue]:
    """Validate a new/edited value against neighbours (decrease = error, unusual usage = warning)."""
    meter = db.get(Meter, meter_id)
    if meter is None:
        raise LookupError(meter_id)
    series = [r for r in _series(db, meter_id, register) if r.reading_id != exclude_reading_id]
    at_dt = as_datetime(at)
    series = [r for r in series if r.taken_at != at_dt]
    return check_reading(Decimal(value), at_dt, series, meter.digits)


@dataclass(frozen=True, slots=True)
class MeterStaleness:
    meter: MeterInfo
    point_name: str
    register: str
    staleness: Staleness


def meter_staleness(db: Session, now: datetime | None = None, *, net: Network | None = None) -> list[MeterStaleness]:
    """Per active manual meter and register: days since last reading vs. its usual interval.

    Live meters are included too (their usual interval will simply be short).
    Sorted by most overdue first.
    """
    net = net or load_network(db)
    now = now or datetime.now()
    out: list[MeterStaleness] = []
    for p in net.points:
        if not p.active:
            continue
        for mid in p.meter_ids:
            m = net.meters[mid]
            if not m.active:
                continue
            for reg in m.registers:
                st = staleness(net.series[mid].readings.get(reg, ()), now)
                out.append(MeterStaleness(m, p.name, reg, st))
    return sorted(out, key=lambda x: (not x.staleness.overdue, -(x.staleness.days_since or Decimal(10**6))))


@dataclass(frozen=True, slots=True)
class MeterAnomaly:
    meter: MeterInfo
    point_name: str
    register: str
    anomaly: Anomaly


def meter_anomalies(db: Session, *, since: datetime | None = None, net: Network | None = None) -> list[MeterAnomaly]:
    """Anomalies (decrease / jump / spike) of all meters; ``since`` filters by the closing reading time."""
    net = net or load_network(db)
    out: list[MeterAnomaly] = []
    for p in net.points:
        for mid in p.meter_ids:
            m = net.meters[mid]
            for reg in m.registers:
                for a in detect_anomalies(net.series[mid].readings.get(reg, ()), m.digits):
                    if since is None or a.curr.taken_at >= since:
                        out.append(MeterAnomaly(m, p.name, reg, a))
    return sorted(out, key=lambda x: x.anomaly.curr.taken_at, reverse=True)


@dataclass(frozen=True, slots=True)
class MeterHint:
    """Everything the capture UI / extractor needs to know about a meter."""

    meter_id: int
    meter_point_id: int
    point_name: str
    serial: str
    registers: tuple[str, ...]
    digits: int
    decimals_shown: int
    display_note: str
    sort_order: int
    last: dict[str, ReadingInfo]  # register -> latest reading
    expected: dict[str, ExpectedRange | None]  # register -> expected range at ``at``


def meter_hints(db: Session, at: date | datetime | None = None, *, net: Network | None = None) -> list[MeterHint]:
    """Active meters in walk order with last readings and expected ranges at ``at`` (default now)."""
    net = net or load_network(db)
    at_dt = as_datetime(at) if at is not None else datetime.now()
    latest = latest_readings(db)
    out: list[MeterHint] = []
    for p in net.points:
        if not p.active:
            continue
        for mid in p.meter_ids:
            m = net.meters[mid]
            if not m.active:
                continue
            out.append(
                MeterHint(
                    meter_id=mid,
                    meter_point_id=p.id,
                    point_name=p.name,
                    serial=m.serial,
                    registers=m.registers,
                    digits=m.digits,
                    decimals_shown=m.decimals_shown,
                    display_note=m.display_note,
                    sort_order=p.sort_order,
                    last={reg: latest[(mid, reg)] for reg in m.registers if (mid, reg) in latest},
                    expected={
                        reg: expected_range(net.series[mid].readings.get(reg, ()), at_dt, m.digits)
                        for reg in m.registers
                    },
                )
            )
    return out
