"""Aligned (PRIMARY) consumption services: per point and per tenant, with quality."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.aligned import QZERO, BoundaryValue, Quality, QValue, point_aligned, qsum, worst
from app.domain.allocation import allocate_aligned, validity_window
from app.domain.consumption import as_datetime
from app.domain.interpolation import LINEAR, CumulativeProfile, Interpolator, ProfileInterpolator
from app.services.network import Network, PointInfo, TenantInfo, load_network
from app.services.periods import PeriodSpan, default_month_range, main_reading_spans, month_spans, spans_from_boundaries

REGISTERS = ("T1", "T2")


def profile_interpolator(net: Network, reference_point_id: int | None = None) -> ProfileInterpolator:
    """Interpolator spreading consumption like the reference point's meter (default: main point).

    Uses the reference point's active (last) meter. Off by default; pass it as
    ``interpolator=`` to the services below.
    """
    pid = reference_point_id or (net.main_point_ids[0] if net.main_point_ids else None)
    profiles: dict[str, CumulativeProfile] = {}
    if pid is not None:
        meters = net.point_meters(pid)
        if meters:
            m = meters[-1]
            profiles = {reg: CumulativeProfile(m.readings.get(reg, ()), m.digits) for reg in m.registers}
    return ProfileInterpolator(profiles, LINEAR)


def resolve_spans(
    db: Session,
    *,
    granularity: str = "month",
    start: date | None = None,
    end: date | None = None,
    boundaries: Sequence[date | datetime] | None = None,
) -> list[PeriodSpan]:
    """Period spans: explicit ``boundaries`` > main-meter reading days (``granularity="main"``) >
    calendar months (default range = all data)."""
    if boundaries is not None:
        return spans_from_boundaries(sorted({as_datetime(b) for b in boundaries}), "custom")
    if granularity == "main":
        return main_reading_spans(db, start, end)
    if granularity != "month":
        raise ValueError(f"unknown granularity {granularity!r}")
    if start is None or end is None:
        rng = default_month_range(db)
        if rng is None:
            return []
        start = start or rng[0]
        end = end or rng[1]
    return month_spans(start, end)


def _point_value_fn(
    net: Network, register: str, interpolator: Interpolator, known: dict[tuple[int, datetime, datetime], QValue]
) -> Callable[[int, datetime, datetime], QValue]:
    """Point consumption lookup for allocation; computes (and caches) spans not in ``known``."""
    cache = dict(known)

    def fn(point_id: int, t0: datetime, t1: datetime) -> QValue:
        key = (point_id, t0, t1)
        if key not in cache:
            if register not in net.point_registers(point_id):
                cache[key] = QZERO
            else:
                vals = point_aligned(net.point_meters(point_id), [t0, t1], interpolator, [register])[register]
                cache[key] = vals[0].scaled(Decimal(1), meter_point_id=point_id)
        return cache[key]

    return fn


def _boundaries(spans: Sequence[PeriodSpan]) -> list[datetime]:
    if not spans:
        return []
    return [spans[0].start, *[s.end for s in spans]]


# --- per point ----------------------------------------------------------------


@dataclass
class PointPeriodTable:
    """Aligned consumption per meter point × register × period."""

    periods: list[PeriodSpan]
    points: list[PointInfo]
    # point_id -> register -> [QValue per period]
    values: dict[int, dict[str, list[QValue]]]

    def value(self, point_id: int, register: str, idx: int) -> QValue:
        return self.values.get(point_id, {}).get(register, [QZERO] * len(self.periods))[idx]

    def total(self, point_id: int, idx: int) -> QValue:
        return qsum(v[idx] for v in self.values.get(point_id, {}).values())


def point_consumption_table(
    db: Session,
    *,
    granularity: str = "month",
    start: date | None = None,
    end: date | None = None,
    boundaries: Sequence[date | datetime] | None = None,
    interpolator: Interpolator | None = None,
    net: Network | None = None,
) -> PointPeriodTable:
    net = net or load_network(db)
    spans = resolve_spans(db, granularity=granularity, start=start, end=end, boundaries=boundaries)
    b = _boundaries(spans)
    interp = interpolator or LINEAR
    values = {}
    for p in net.points:
        if len(b) < 2:
            values[p.id] = {reg: [] for reg in net.point_registers(p.id)}
            continue
        pv = point_aligned(net.point_meters(p.id), b, interp, net.point_registers(p.id))
        values[p.id] = {reg: [v.scaled(Decimal(1), meter_point_id=p.id) for v in vals] for reg, vals in pv.items()}
    return PointPeriodTable(spans, list(net.points), values)


# --- per tenant ------------------------------------------------------------------


@dataclass
class TenantPeriodTable:
    """Aligned consumption per tenant × register × period (remainder tenant = main − others)."""

    periods: list[PeriodSpan]
    tenants: list[TenantInfo]
    # tenant_id -> register -> [QValue per period]
    values: dict[int, dict[str, list[QValue]]]
    main: dict[str, list[QValue]]  # main meter(s) per register
    points: PointPeriodTable

    def value(self, tenant_id: int, register: str, idx: int) -> QValue:
        return self.values[tenant_id][register][idx]

    def total(self, tenant_id: int, idx: int) -> QValue:
        """T1 + T2 of one tenant in period ``idx``."""
        return qsum(self.values[tenant_id][reg][idx] for reg in REGISTERS)

    def period_sum(self, tenant_id: int, register: str | None = None) -> QValue:
        """Sum over all periods (one register, or T1+T2 when ``register`` is None)."""
        regs = [register] if register else list(REGISTERS)
        return qsum(self.values[tenant_id][r][i] for r in regs for i in range(len(self.periods)))

    def negative_remainders(self) -> list[tuple[int, str]]:
        """(period index, register) where the remainder tenant is negative."""
        rem = next((t for t in self.tenants if t.is_remainder), None)
        if rem is None:
            return []
        return [(i, reg) for reg in REGISTERS for i, v in enumerate(self.values[rem.id][reg]) if v.value < 0]


def tenant_consumption_table(
    db: Session,
    *,
    granularity: str = "month",
    start: date | None = None,
    end: date | None = None,
    boundaries: Sequence[date | datetime] | None = None,
    interpolator: Interpolator | None = None,
    net: Network | None = None,
) -> TenantPeriodTable:
    net = net or load_network(db)
    pt = point_consumption_table(
        db, granularity=granularity, start=start, end=end, boundaries=boundaries, interpolator=interpolator, net=net
    )
    values: dict[int, dict[str, list[QValue]]] = {t.id: {r: [] for r in REGISTERS} for t in net.tenants}
    main: dict[str, list[QValue]] = {r: [] for r in REGISTERS}
    for i, span in enumerate(pt.periods):
        for reg in REGISTERS:
            fn = _point_value_fn(
                net,
                reg,
                interpolator or LINEAR,
                {(p.id, span.start, span.end): pt.values[p.id][reg][i] for p in net.points if reg in pt.values[p.id]},
            )
            res = allocate_aligned(fn, net.rules, net.tenant_refs, net.main_point_ids, span.start, span.end)
            for t in net.tenants:
                values[t.id][reg].append(res.values.get(t.id, QZERO))
            main[reg].append(res.main)
    return TenantPeriodTable(pt.periods, list(net.tenants), values, main, pt)


# --- arbitrary interval (settlements) ----------------------------------------------


@dataclass(frozen=True, slots=True)
class MeterIntervalRow:
    """One meter × register in an interval: start/end state and consumption (for protocols)."""

    meter_id: int
    serial: str
    meter_point_id: int
    register: str
    start: BoundaryValue
    end: BoundaryValue
    consumption: QValue
    installed_on: date | None
    removed_on: date | None
    note: str


@dataclass
class IntervalResult:
    start: datetime
    end: datetime
    points: dict[int, dict[str, QValue]]  # point_id -> register -> value
    tenants: dict[int, dict[str, QValue]]  # tenant_id -> register -> value
    main: dict[str, QValue]
    meters: list[MeterIntervalRow]
    tenant_infos: list[TenantInfo]
    point_infos: list[PointInfo]
    tenant_points: dict[int, list[int]] = field(default_factory=dict)  # tenant_id -> allocated point ids

    def tenant_total(self, tenant_id: int) -> QValue:
        return qsum(self.tenants[tenant_id].values())

    @property
    def quality(self) -> Quality:
        return worst(v.quality for regs in self.points.values() for v in regs.values())


def consumption_over_interval(
    db: Session,
    start: date | datetime,
    end: date | datetime,
    *,
    interpolator: Interpolator | None = None,
    net: Network | None = None,
) -> IntervalResult:
    """Consumption per meter, point and tenant over ``[start, end]`` (aligned, with quality)."""
    net = net or load_network(db)
    t0, t1 = as_datetime(start), as_datetime(end)
    interp = interpolator or LINEAR
    rows: list[MeterIntervalRow] = []
    points: dict[int, dict[str, QValue]] = {}
    for p in net.points:
        per_reg: dict[str, QValue] = dict.fromkeys(net.point_registers(p.id), QZERO)
        for ms in net.point_meters(p.id):
            info = net.meters[ms.meter_id]
            for reg in ms.registers:
                rs = ms.register_series(reg)
                v0, v1 = rs.value_at(t0, interp), rs.value_at(t1, interp)
                c = rs.between(v0, v1).scaled(Decimal(1), meter_point_id=p.id)
                per_reg[reg] = per_reg[reg] + c
                rows.append(
                    MeterIntervalRow(
                        ms.meter_id, ms.serial, p.id, reg, v0, v1, c, info.installed_on, info.removed_on, info.note
                    )
                )
        points[p.id] = per_reg
    tenants: dict[int, dict[str, QValue]] = {t.id: {} for t in net.tenants}
    main: dict[str, QValue] = {}
    for reg in REGISTERS:
        known = {(pid, t0, t1): regs[reg] for pid, regs in points.items() if reg in regs}
        res = allocate_aligned(
            _point_value_fn(net, reg, interp, known), net.rules, net.tenant_refs, net.main_point_ids, t0, t1
        )
        for t in net.tenants:
            tenants[t.id][reg] = res.values.get(t.id, QZERO)
        main[reg] = res.main
    tenant_points: dict[int, list[int]] = {}
    for r in net.rules:
        if validity_window(r, t0, t1) is not None:
            tenant_points.setdefault(r.tenant_id, []).append(r.meter_point_id)
    return IntervalResult(t0, t1, points, tenants, main, rows, list(net.tenants), list(net.points), tenant_points)


# --- control balances ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlBalanceRow:
    control_point_id: int
    child_point_ids: tuple[int, ...]
    register: str
    control: QValue
    children: QValue
    difference: QValue  # control − Σ children (unmetered consumption)


def control_balances(table: PointPeriodTable, idx: int | None = None) -> list[ControlBalanceRow]:
    """Control-meter balances for period ``idx`` (or summed over all periods) from a point table.

    Also includes the main point(s) vs. all sub points that have no parent ("bilance hlavní vs. podružné").
    """
    out: list[ControlBalanceRow] = []
    n = len(table.periods)
    idxs = range(n) if idx is None else [idx]

    def val(pid: int, reg: str) -> QValue:
        vals = table.values.get(pid, {}).get(reg)
        return qsum(vals[i] for i in idxs) if vals else QZERO

    for p in table.points:
        if p.role == "control":
            children = tuple(c.id for c in table.points if c.parent_point_id == p.id)
        elif p.role == "main":
            children = tuple(c.id for c in table.points if c.role == "sub" and c.parent_point_id is None) + tuple(
                c.id for c in table.points if c.role == "control" and c.parent_point_id is None
            )
        else:
            continue
        for reg in REGISTERS:
            control = val(p.id, reg)
            ch = qsum(val(c, reg) for c in children)
            if not table.values.get(p.id, {}).get(reg) and ch.value == 0:
                continue
            out.append(ControlBalanceRow(p.id, children, reg, control, ch, control - ch))
    return out
