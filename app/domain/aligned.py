"""Aligned periods - the PRIMARY calculation mode (SPEC §3 "Zarovnaná období").

All meters share the same period boundaries (calendar months by default, or any
explicit dates, e.g. the main meter's reading days). The counter state at each boundary is computed by ``value_at``:

- ``exact``: a real reading on the boundary's day (same-day tolerance), or a
  value fixed by definition (before the meter's first reading / after removal:
  consumption 0, never extrapolated backwards);
- ``estimated``: interpolated between the nearest readings before and after
  (strategy is pluggable, see ``app.domain.interpolation``);
- ``provisional``: extrapolated at the open end (no reading after the boundary yet)
  from the average daily usage of the last known interval (at least
  ``MIN_EXTRAPOLATION_REF_DAYS`` long).

A meter removed without a final reading on its removal day is extrapolated up to
the removal and flat afterwards; that state is ``estimated`` (no later reading can
ever replace it), not ``exact``.

Values are computed on the *unwrapped cumulative* counter (rollovers resolved),
so consumption of adjacent periods always telescopes to the difference of the
real readings: interpolation only redistributes, it never adds or loses kWh.
Nothing derived is stored; results recompute when late readings arrive.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.consumption import (
    QUANT,
    ZERO,
    ReadingPoint,
    as_datetime,
    counter_range,
    register_delta,
    sort_series,
)
from app.domain.interpolation import LINEAR, Interpolator


class Quality(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    PROVISIONAL = "provisional"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {Quality.EXACT: 0, Quality.ESTIMATED: 1, Quality.PROVISIONAL: 2}


def worst(qualities: Iterable[Quality]) -> Quality:
    out = Quality.EXACT
    for q in qualities:
        if q.rank > out.rank:
            out = q
    return out


# How a boundary value was obtained
METHOD_READING = "reading"
METHOD_INTERPOLATED = "interpolated"
METHOD_EXTRAPOLATED = "extrapolated"
METHOD_BEFORE_FIRST = "before_first"  # before the first reading / installation: flat, consumption 0
METHOD_AFTER_REMOVAL = "after_removal"  # after the meter was removed: flat
METHOD_NO_DATA = "no_data"  # active meter without any reading

# The reference span for open-end extrapolation reaches back at least this many days.
MIN_EXTRAPOLATION_REF_DAYS = 28


def _days(a: datetime, b: datetime) -> Decimal:
    return (Decimal((b - a).total_seconds()) / Decimal(86400)).quantize(QUANT)


@dataclass(frozen=True, slots=True)
class BoundaryValue:
    """Counter state of one register at time ``t`` with quality and provenance."""

    t: datetime
    cumulative: Decimal  # unwrapped counter (comparable across rollovers within a series)
    display: Decimal | None  # value as shown on the meter (mod 10**digits); None without data
    quality: Quality
    method: str
    before: ReadingPoint | None = None  # reading used / reading before t
    after: ReadingPoint | None = None  # reading after t (interpolation)
    span_days: Decimal = ZERO  # length of the interpolated / extrapolated span in days

    @property
    def effective_t(self) -> datetime:
        """Time the value really refers to (a same-day reading's own time)."""
        if self.method == METHOD_READING and self.before is not None:
            return self.before.taken_at
        return self.t


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where one component of a computed value came from (for tooltips/protocols)."""

    register: str
    start: BoundaryValue
    end: BoundaryValue
    meter_id: int | None = None
    serial: str | None = None
    meter_point_id: int | None = None
    factor: Decimal = Decimal(1)
    value: Decimal = ZERO  # this component's contribution (after factor)


@dataclass(frozen=True, slots=True)
class QValue:
    """A computed kWh value with quality.

    ``estimated_kwh`` is the absolute amount of kWh derived from interpolation or
    extrapolation (``share_estimated`` relates it to the value).
    """

    value: Decimal = ZERO
    quality: Quality = Quality.EXACT
    estimated_kwh: Decimal = ZERO
    parts: tuple[Provenance, ...] = ()
    anomalies: tuple[str, ...] = ()

    @property
    def share_estimated(self) -> Decimal:
        if self.estimated_kwh == 0:
            return ZERO
        if self.value == 0:
            return Decimal(1)
        return min(Decimal(1), self.estimated_kwh / abs(self.value))

    def scaled(self, factor: Decimal, meter_point_id: int | None = None) -> QValue:
        if factor == 1 and meter_point_id is None:
            return self
        parts = tuple(
            replace(
                p,
                factor=p.factor * factor,
                value=p.value * factor,
                meter_point_id=meter_point_id if meter_point_id is not None else p.meter_point_id,
            )
            for p in self.parts
        )
        return QValue(self.value * factor, self.quality, self.estimated_kwh * abs(factor), parts, self.anomalies)

    def __add__(self, other: QValue) -> QValue:
        return QValue(
            self.value + other.value,
            worst((self.quality, other.quality)),
            self.estimated_kwh + other.estimated_kwh,
            self.parts + other.parts,
            self.anomalies + other.anomalies,
        )

    def __sub__(self, other: QValue) -> QValue:
        return self + other.scaled(Decimal(-1))


QZERO = QValue()


def qsum(values: Iterable[QValue]) -> QValue:
    out = QZERO
    for v in values:
        out = out + v
    return out


class RegisterSeries:
    """One register of one meter, prepared for aligned calculations."""

    def __init__(
        self,
        readings: Sequence[ReadingPoint],
        digits: int = 5,
        *,
        register: str = "T1",
        installed_on: date | None = None,
        removed_on: date | None = None,
        meter_id: int | None = None,
        serial: str | None = None,
    ) -> None:
        self.readings = sort_series(readings)
        self.digits = digits
        self.register = register
        self.installed_on = installed_on
        self.removed_on = removed_on
        self.meter_id = meter_id
        self.serial = serial
        self.range = counter_range(digits)
        self.cum: list[Decimal] = []
        self.anomalies: list[tuple[datetime, str]] = []  # (time of closing reading, kind)
        acc = ZERO
        for i, r in enumerate(self.readings):
            if i == 0:
                acc = r.value
            else:
                d = register_delta(self.readings[i - 1].value, r.value, digits)
                acc += d.value
                if d.anomaly:
                    self.anomalies.append((r.taken_at, d.anomaly))
            self.cum.append(acc)

    # -- boundary values --------------------------------------------------

    def _display(self, cum: Decimal) -> Decimal:
        return cum % self.range if cum >= 0 else cum

    def _bv(self, t: datetime, cum: Decimal, quality: Quality, method: str, before=None, after=None, span=ZERO):
        cum = cum.quantize(QUANT)
        return BoundaryValue(t, cum, self._display(cum), quality, method, before, after, span)

    def value_at(self, t: date | datetime, interpolator: Interpolator = LINEAR) -> BoundaryValue:
        t = as_datetime(t)
        rs = self.readings
        if not rs:
            if self.installed_on is not None and t.date() <= self.installed_on:
                return BoundaryValue(t, ZERO, None, Quality.EXACT, METHOD_BEFORE_FIRST)
            if self.removed_on is not None and t.date() >= self.removed_on:
                return BoundaryValue(t, ZERO, None, Quality.EXACT, METHOD_AFTER_REMOVAL)
            return BoundaryValue(t, ZERO, None, Quality.PROVISIONAL, METHOD_NO_DATA)
        # same-day tolerance: a reading on the boundary's day counts as exact
        same_day = [i for i, r in enumerate(rs) if r.taken_at.date() == t.date()]
        if same_day:
            i = min(same_day, key=lambda k: abs((rs[k].taken_at - t).total_seconds()))
            return self._bv(t, self.cum[i], Quality.EXACT, METHOD_READING, rs[i])
        if t < rs[0].taken_at:
            return self._bv(t, self.cum[0], Quality.EXACT, METHOD_BEFORE_FIRST, None, rs[0])
        if t > rs[-1].taken_at:
            last = rs[-1]
            removal = as_datetime(self.removed_on) if self.removed_on is not None else None
            if removal is not None and removal <= last.taken_at:
                # final reading on/after the removal day: flat from there on
                return self._bv(t, self.cum[-1], Quality.EXACT, METHOD_AFTER_REMOVAL, last)
            if removal is not None:
                # Removed without a final reading (e.g. replacement recorded from the photo review):
                # the meter ran until its removal, so extrapolate up to the removal and stay flat after.
                # No later reading can ever close this span, hence ESTIMATED (finalizable, listed in
                # the protocol) rather than PROVISIONAL - and never a negative month at the removal.
                upto = min(t, removal)
                extra = self._extrapolate(interpolator, upto)
                method = METHOD_AFTER_REMOVAL if t >= removal else METHOD_EXTRAPOLATED
                return self._bv(
                    t, self.cum[-1] + extra, Quality.ESTIMATED, method, last, None, _days(last.taken_at, upto)
                )
            extra = self._extrapolate(interpolator, t)
            return self._bv(
                t, self.cum[-1] + extra, Quality.PROVISIONAL, METHOD_EXTRAPOLATED, last, None, _days(last.taken_at, t)
            )
        for i in range(len(rs) - 1):
            a, b = rs[i], rs[i + 1]
            if a.taken_at < t < b.taken_at:
                share = interpolator.share(self.register, a.taken_at, b.taken_at, t)
                cum = self.cum[i] + (self.cum[i + 1] - self.cum[i]) * share
                return self._bv(t, cum, Quality.ESTIMATED, METHOD_INTERPOLATED, a, b, _days(a.taken_at, b.taken_at))
        raise AssertionError("unreachable")  # pragma: no cover

    def _extrapolate(self, interpolator: Interpolator, t: datetime) -> Decimal:
        """kWh consumed between the last reading and ``t`` (>= 0), from the average daily usage of
        the last known interval - extended back to at least ``MIN_EXTRAPOLATION_REF_DAYS`` so that a
        very short last interval (two readings a day apart, a date typo) cannot blow up the estimate."""
        rs = self.readings
        if len(rs) < 2:
            return ZERO
        last = rs[-1]
        j = len(rs) - 2
        while j > 0 and (last.taken_at - rs[j].taken_at).days < MIN_EXTRAPOLATION_REF_DAYS:
            j -= 1
        ref_consumption = self.cum[-1] - self.cum[j]
        extra = interpolator.extrapolate(
            self.register, rs[j].taken_at, last.taken_at, ref_consumption, last.taken_at, t
        )
        return max(ZERO, extra)

    # -- consumption ------------------------------------------------------

    def between(self, v0: BoundaryValue, v1: BoundaryValue) -> QValue:
        """Consumption between two boundary values of this series (with quality)."""
        e0, e1 = v0.effective_t, v1.effective_t
        if v0.method == METHOD_AFTER_REMOVAL and v1.method == METHOD_AFTER_REMOVAL:
            # the whole span lies after the meter's removal: nothing consumed, by definition
            prov = Provenance(self.register, v0, v1, self.meter_id, self.serial, value=ZERO)
            return QValue(ZERO, Quality.EXACT, ZERO, (prov,), ())
        knots: list[tuple[Decimal, Quality]] = [(v0.cumulative, v0.quality)]
        for r, c in zip(self.readings, self.cum, strict=True):
            if e0 < r.taken_at < e1:
                knots.append((c, Quality.EXACT))
        knots.append((v1.cumulative, v1.quality))
        est = ZERO
        for (c_a, q_a), (c_b, q_b) in itertools.pairwise(knots):
            if worst((q_a, q_b)) != Quality.EXACT:
                est += abs(c_b - c_a)
        value = v1.cumulative - v0.cumulative
        anomalies = tuple(kind for when, kind in self.anomalies if v0.t < when <= v1.t)
        prov = Provenance(self.register, v0, v1, self.meter_id, self.serial, value=value)
        return QValue(value, worst((v0.quality, v1.quality)), est, (prov,), anomalies)

    def consumption(self, t0: date | datetime, t1: date | datetime, interpolator: Interpolator = LINEAR) -> QValue:
        t0, t1 = as_datetime(t0), as_datetime(t1)
        if t1 < t0:
            raise ValueError("t1 must not be before t0")
        return self.between(self.value_at(t0, interpolator), self.value_at(t1, interpolator))

    def aligned(self, boundaries: Sequence[date | datetime], interpolator: Interpolator = LINEAR) -> list[QValue]:
        """Consumption for each period ``[b_i, b_{i+1}]`` (len = len(boundaries) - 1)."""
        bvs = [self.value_at(b, interpolator) for b in boundaries]
        return [self.between(a, b) for a, b in itertools.pairwise(bvs)]


def value_at(
    readings: Sequence[ReadingPoint],
    t: date | datetime,
    digits: int = 5,
    *,
    installed_on: date | None = None,
    removed_on: date | None = None,
    interpolator: Interpolator = LINEAR,
    register: str = "T1",
) -> BoundaryValue:
    """Counter state at ``t`` (exact / estimated / provisional), see module docstring."""
    rs = RegisterSeries(readings, digits, register=register, installed_on=installed_on, removed_on=removed_on)
    return rs.value_at(t, interpolator)


# --- meters and meter points ----------------------------------------------


@dataclass(frozen=True, slots=True)
class MeterSeries:
    """All readings of one physical meter, grouped by register."""

    meter_id: int
    serial: str
    digits: int = 5
    registers: tuple[str, ...] = ("T1",)
    installed_on: date | None = None
    removed_on: date | None = None
    readings: Mapping[str, Sequence[ReadingPoint]] = field(default_factory=dict)
    meter_point_id: int | None = None

    def register_series(self, register: str) -> RegisterSeries:
        return RegisterSeries(
            self.readings.get(register, ()),
            self.digits,
            register=register,
            installed_on=self.installed_on,
            removed_on=self.removed_on,
            meter_id=self.meter_id,
            serial=self.serial,
        )


def meter_aligned(
    meter: MeterSeries, boundaries: Sequence[date | datetime], interpolator: Interpolator = LINEAR
) -> dict[str, list[QValue]]:
    return {reg: meter.register_series(reg).aligned(boundaries, interpolator) for reg in meter.registers}


def point_aligned(
    meters: Sequence[MeterSeries],
    boundaries: Sequence[date | datetime],
    interpolator: Interpolator = LINEAR,
    registers: Sequence[str] | None = None,
) -> dict[str, list[QValue]]:
    """Per-register aligned consumption of a meter point, summed over its meters (replacements)."""
    n = max(0, len(boundaries) - 1)
    regs = list(registers) if registers is not None else sorted({r for m in meters for r in m.registers})
    out: dict[str, list[QValue]] = {r: [QZERO] * n for r in regs}
    for m in meters:
        for reg, vals in meter_aligned(m, boundaries, interpolator).items():
            if reg not in out:
                continue
            out[reg] = [a + b for a, b in zip(out[reg], vals, strict=True)]
    return out


def point_consumption(
    meters: Sequence[MeterSeries],
    t0: date | datetime,
    t1: date | datetime,
    interpolator: Interpolator = LINEAR,
    registers: Sequence[str] | None = None,
) -> dict[str, QValue]:
    """Consumption of a meter point over one arbitrary interval, per register."""
    return {reg: vals[0] for reg, vals in point_aligned(meters, [t0, t1], interpolator, registers).items()}


# --- boundaries -------------------------------------------------------------


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def month_boundaries(start: date, end: date) -> list[datetime]:
    """Midnights of the 1st of each month from ``start``'s month up to ``end``'s month (inclusive).

    E.g. (2025-01-15, 2025-03-01) -> [2025-01-01, 2025-02-01, 2025-03-01].
    """
    out = []
    m = month_start(start)
    last = month_start(end)
    while m <= last:
        out.append(as_datetime(m))
        m = add_months(m, 1)
    return out


def date_boundaries(dates: Iterable[date | datetime]) -> list[datetime]:
    return sorted({as_datetime(d) for d in dates})
