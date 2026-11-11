"""Pure allocation of meter-point consumption to tenants (no DB access).

Two flavours: ``allocate_aligned`` (primary; QValue with quality) and
``allocate`` (raw per-day view; Decimal | None like the spreadsheet).

``tenant = Σ factor × consumption(allocated points)``;
the remainder tenant gets ``main − Σ other tenants`` (may be negative).
Explicit allocations of the remainder tenant are ignored: everything not
allocated elsewhere (unallocated points, control points) ends up in the remainder.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.domain.aligned import QZERO, QValue, qsum
from app.domain.consumption import ZERO, as_datetime, sum_optional


@dataclass(frozen=True, slots=True)
class AllocationRule:
    tenant_id: int
    meter_point_id: int
    factor: Decimal = Decimal(1)
    valid_from: date | None = None  # inclusive
    valid_to: date | None = None  # exclusive


@dataclass(frozen=True, slots=True)
class TenantRef:
    id: int
    name: str
    is_remainder: bool = False


def validity_fraction(rule: AllocationRule, start: date | datetime | None, end: date | datetime) -> Decimal:
    """Fraction of the period ``[start, end]`` covered by the rule's validity ``[valid_from, valid_to)``.

    With ``start is None`` (unknown start) the rule must be valid at ``end``.
    Partial overlaps are pro-rated by time.
    """
    end_dt = as_datetime(end)
    vf = as_datetime(rule.valid_from) if rule.valid_from else None
    vt = as_datetime(rule.valid_to) if rule.valid_to else None
    if start is None or end_dt <= as_datetime(start):
        ok = (vf is None or vf <= end_dt) and (vt is None or end_dt < vt)
        return Decimal(1) if ok else ZERO
    start_dt = as_datetime(start)
    lo = max(start_dt, vf) if vf else start_dt
    hi = min(end_dt, vt) if vt else end_dt
    if hi <= lo:
        return ZERO
    if lo == start_dt and hi == end_dt:
        return Decimal(1)
    return Decimal((hi - lo).total_seconds()) / Decimal((end_dt - start_dt).total_seconds())


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """Per-tenant values for one register and one period."""

    values: dict[int, Decimal | None]  # tenant_id -> kWh (None = no data at all)
    main: Decimal | None
    allocated: Decimal | None  # Σ non-remainder tenants
    remainder_tenant_id: int | None

    @property
    def remainder(self) -> Decimal | None:
        return self.values.get(self.remainder_tenant_id) if self.remainder_tenant_id is not None else None

    @property
    def negative_remainder(self) -> bool:
        r = self.remainder
        return r is not None and r < 0


def allocate(
    point_values: Mapping[int, Decimal | None],
    rules: Sequence[AllocationRule],
    tenants: Sequence[TenantRef],
    main_point_ids: Sequence[int],
    start: date | None,
    end: date,
) -> AllocationResult:
    """Allocate one register's per-point consumption in one period to tenants.

    ``point_values`` maps meter_point_id -> consumption (None = no data in period).
    A tenant's value is None when none of its active allocated points has data;
    the remainder is None only when neither the main point(s) nor any tenant has data
    (a missing main reading counts as 0 in that period - its consumption is carried
    to the period of its next reading, which keeps yearly totals right).
    """
    remainder_id = next((t.id for t in tenants if t.is_remainder), None)
    values: dict[int, Decimal | None] = {}
    for t in tenants:
        if t.is_remainder:
            continue
        contribs: list[Decimal | None] = []
        for r in rules:
            if r.tenant_id != t.id:
                continue
            frac = validity_fraction(r, start, end)
            if frac == 0:
                continue
            v = point_values.get(r.meter_point_id)
            contribs.append(None if v is None else v * r.factor * frac)
        values[t.id] = sum_optional(contribs)
    allocated = sum_optional(values.values())
    main = sum_optional(point_values.get(pid) for pid in main_point_ids)
    if remainder_id is not None:
        if main is None and allocated is None:
            values[remainder_id] = None
        else:
            values[remainder_id] = (main or ZERO) - (allocated or ZERO)
    return AllocationResult(values=values, main=main, allocated=allocated, remainder_tenant_id=remainder_id)


@dataclass(frozen=True, slots=True)
class ControlBalance:
    control: Decimal | None
    children: Decimal | None
    difference: Decimal | None  # control - Σ children (unmetered consumption / losses)

    @property
    def negative(self) -> bool:
        return self.difference is not None and self.difference < 0


def control_balance(control_value: Decimal | None, children_values: Sequence[Decimal | None]) -> ControlBalance:
    children = sum_optional(children_values)
    diff = None if control_value is None else control_value - (children or ZERO)
    return ControlBalance(control_value, children, diff)


# --- aligned (primary) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlignedAllocation:
    """Per-tenant values (with quality) for one register and one period."""

    values: dict[int, QValue]  # tenant_id -> value
    main: QValue
    allocated: QValue  # Σ non-remainder tenants
    remainder_tenant_id: int | None

    @property
    def remainder(self) -> QValue | None:
        return self.values.get(self.remainder_tenant_id) if self.remainder_tenant_id is not None else None

    @property
    def negative_remainder(self) -> bool:
        r = self.remainder
        return r is not None and r.value < 0


PointValueFn = Callable[[int, datetime, datetime], QValue]
"""(meter_point_id, t0, t1) -> consumption of that point in [t0, t1] (one register)."""


def validity_window(
    rule: AllocationRule, start: date | datetime, end: date | datetime
) -> tuple[datetime, datetime] | None:
    """The part of ``[start, end]`` where the rule is valid, or None."""
    lo = as_datetime(start)
    hi = as_datetime(end)
    if rule.valid_from is not None:
        lo = max(lo, as_datetime(rule.valid_from))
    if rule.valid_to is not None:
        hi = min(hi, as_datetime(rule.valid_to))
    if hi <= lo:
        return None
    return lo, hi


def allocate_aligned(
    point_value: PointValueFn,
    rules: Sequence[AllocationRule],
    tenants: Sequence[TenantRef],
    main_point_ids: Sequence[int],
    start: date | datetime,
    end: date | datetime,
) -> AlignedAllocation:
    """Allocate one register's aligned consumption in ``[start, end]`` to tenants.

    ``point_value(point_id, t0, t1)`` supplies the point's consumption; a rule valid
    only for part of the interval is evaluated on exactly that part (no time
    pro-rating, so e.g. a meter installed mid-interval is attributed fully).
    Each tenant value carries the worst quality of its components and the sum of
    their estimated kWh; the remainder (main − Σ others) combines the main meter's
    and all others' quality.
    """
    t0, t1 = as_datetime(start), as_datetime(end)
    remainder_id = next((t.id for t in tenants if t.is_remainder), None)
    values: dict[int, QValue] = {}
    for t in tenants:
        if t.is_remainder:
            continue
        acc = QZERO
        for r in rules:
            if r.tenant_id != t.id:
                continue
            win = validity_window(r, t0, t1)
            if win is None:
                continue
            acc = acc + point_value(r.meter_point_id, *win).scaled(r.factor, meter_point_id=r.meter_point_id)
        values[t.id] = acc
    allocated = qsum(values.values())
    main = qsum(point_value(pid, t0, t1).scaled(Decimal(1), meter_point_id=pid) for pid in main_point_ids)
    if remainder_id is not None:
        values[remainder_id] = main - allocated
    return AlignedAllocation(values=values, main=main, allocated=allocated, remainder_tenant_id=remainder_id)


def control_balance_aligned(control: QValue, children: Sequence[QValue]) -> tuple[QValue, QValue, QValue]:
    """(control, Σ children, control − Σ children) with quality."""
    ch = qsum(children)
    return control, ch, control - ch
