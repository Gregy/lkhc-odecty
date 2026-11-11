"""Interpolation strategies for distributing consumption between readings.

An interpolator answers two questions for one register of one meter:

- ``share(register, seg_start, seg_end, t)``: which fraction (0..1) of the
  consumption between two real readings at ``seg_start`` and ``seg_end`` happened
  in ``[seg_start, t]`` (used for *estimated* boundary values);
- ``extrapolate(register, ref_start, ref_end, ref_consumption, last_t, t)``: how
  much is consumed between the last reading ``last_t`` and ``t`` given that
  ``ref_consumption`` was consumed in the reference interval ``[ref_start, ref_end]``
  (the last known interval; used for *provisional* values at the open end).

Strategies:
- ``LinearInterpolator`` (default): proportional to elapsed time.
- ``ProfileInterpolator``: proportional to a reference meter's consumption in the
  same register (typically the main meter, which will become live); falls back to
  linear where the profile does not cover the span or is flat.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.domain.consumption import ZERO, ReadingPoint, register_delta, sort_series


def _secs(a: datetime, b: datetime) -> Decimal:
    return Decimal((b - a).total_seconds())


@runtime_checkable
class Interpolator(Protocol):
    name: str

    def share(self, register: str, seg_start: datetime, seg_end: datetime, t: datetime) -> Decimal: ...

    def extrapolate(
        self,
        register: str,
        ref_start: datetime,
        ref_end: datetime,
        ref_consumption: Decimal,
        last_t: datetime,
        t: datetime,
    ) -> Decimal: ...


class LinearInterpolator:
    """Consumption is spread evenly in time."""

    name = "linear"

    def share(self, register: str, seg_start: datetime, seg_end: datetime, t: datetime) -> Decimal:
        total = _secs(seg_start, seg_end)
        if total <= 0:
            return Decimal(1)
        return min(Decimal(1), max(ZERO, _secs(seg_start, t) / total))

    def extrapolate(
        self,
        register: str,
        ref_start: datetime,
        ref_end: datetime,
        ref_consumption: Decimal,
        last_t: datetime,
        t: datetime,
    ) -> Decimal:
        span = _secs(ref_start, ref_end)
        if span <= 0:
            return ZERO
        return ref_consumption * _secs(last_t, t) / span


LINEAR = LinearInterpolator()


class CumulativeProfile:
    """Unwrapped cumulative counter of a reference meter, linearly interpolated between its readings."""

    def __init__(self, readings: Sequence[ReadingPoint], digits: int = 5) -> None:
        s = sort_series(readings)
        self.times: list[datetime] = [r.taken_at for r in s]
        self.cum: list[Decimal] = []
        acc = ZERO
        for i, r in enumerate(s):
            if i == 0:
                acc = r.value
            else:
                acc += register_delta(s[i - 1].value, r.value, digits).value
            self.cum.append(acc)

    def covers(self, a: datetime, b: datetime) -> bool:
        return bool(self.times) and self.times[0] <= a and b <= self.times[-1]

    def at(self, t: datetime) -> Decimal | None:
        if not self.times or t < self.times[0] or t > self.times[-1]:
            return None
        for i in range(len(self.times)):
            if self.times[i] == t:
                return self.cum[i]
            if i + 1 < len(self.times) and self.times[i] < t < self.times[i + 1]:
                frac = _secs(self.times[i], t) / _secs(self.times[i], self.times[i + 1])
                return self.cum[i] + (self.cum[i + 1] - self.cum[i]) * frac
        return None

    def between(self, a: datetime, b: datetime) -> Decimal | None:
        va, vb = self.at(a), self.at(b)
        if va is None or vb is None:
            return None
        return vb - va


class ProfileInterpolator:
    """Spread consumption proportionally to a reference meter's usage (per register).

    ``profiles`` maps register -> CumulativeProfile of the reference meter. Where
    the profile does not cover the needed span or shows no consumption, the
    linear strategy is used.
    """

    name = "profile"

    def __init__(self, profiles: Mapping[str, CumulativeProfile], fallback: Interpolator = LINEAR) -> None:
        self.profiles = profiles
        self.fallback = fallback

    def share(self, register: str, seg_start: datetime, seg_end: datetime, t: datetime) -> Decimal:
        p = self.profiles.get(register)
        if p is not None and p.covers(seg_start, seg_end):
            total = p.between(seg_start, seg_end)
            part = p.between(seg_start, t)
            if total is not None and part is not None and total > 0:
                return min(Decimal(1), max(ZERO, part / total))
        return self.fallback.share(register, seg_start, seg_end, t)

    def extrapolate(
        self,
        register: str,
        ref_start: datetime,
        ref_end: datetime,
        ref_consumption: Decimal,
        last_t: datetime,
        t: datetime,
    ) -> Decimal:
        p = self.profiles.get(register)
        if p is not None and p.covers(ref_start, ref_end) and p.covers(last_t, t):
            ref_profile = p.between(ref_start, ref_end)
            future = p.between(last_t, t)
            if ref_profile is not None and future is not None and ref_profile > 0:
                return ref_consumption * future / ref_profile
        return self.fallback.extrapolate(register, ref_start, ref_end, ref_consumption, last_t, t)
