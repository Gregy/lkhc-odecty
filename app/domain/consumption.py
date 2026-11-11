"""Pure consumption basics (no DB access): register deltas and the *raw per-day* view.

The per-day table here (``period_consumption``) reproduces the spreadsheet's
"carry to next reading" semantics and is only the secondary/raw view and the XLSX
oracle. The primary calculation mode (aligned periods, value_at with quality) is
in ``app.domain.aligned``.

Terminology:
- A *series* is the ordered list of readings of one register of one meter.
- A *period* is the half-open time span between two consecutive reading days,
  ``(start, end]``; a reading belongs to the period that contains its date.
- Consumption of a register between two readings is ``(curr - prev) mod 10**digits``
  (counter rollover); a decrease or a jump larger than half the counter range is
  *not* treated as rollover but reported as an anomaly with the raw difference.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

ZERO = Decimal(0)
QUANT = Decimal("0.001")

# Anomaly kinds produced by register_delta
ANOMALY_DECREASE = "decrease"  # counter went down (not a plausible rollover)
ANOMALY_JUMP = "jump"  # increase larger than half the counter range


@dataclass(frozen=True, slots=True)
class ReadingPoint:
    """One reading of one register (minimal, DB-independent)."""

    taken_at: datetime
    value: Decimal
    reading_id: int | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class Delta:
    value: Decimal  # consumption; raw (possibly negative) difference when anomalous
    rollover: bool = False
    anomaly: str | None = None


def as_datetime(t: date | datetime) -> datetime:
    """Dates are interpreted as 00:00 local time."""
    if isinstance(t, datetime):
        return t
    return datetime.combine(t, time.min)


def counter_range(digits: int) -> Decimal:
    return Decimal(10) ** digits


def register_delta(prev: Decimal, curr: Decimal, digits: int = 5) -> Delta:
    """Consumption between two consecutive readings of one register."""
    rng = counter_range(digits)
    half = rng / 2
    raw = Decimal(curr) - Decimal(prev)
    if raw >= 0:
        if raw > half:
            return Delta(raw, anomaly=ANOMALY_JUMP)
        return Delta(raw)
    wrapped = raw + rng
    if wrapped <= half:
        return Delta(wrapped, rollover=True)
    return Delta(raw, anomaly=ANOMALY_DECREASE)


def sort_series(readings: Iterable[ReadingPoint]) -> list[ReadingPoint]:
    return sorted(readings, key=lambda r: r.taken_at)


@dataclass(frozen=True, slots=True)
class Segment:
    """Consumption between two consecutive readings."""

    prev: ReadingPoint
    curr: ReadingPoint
    delta: Delta

    @property
    def days(self) -> Decimal:
        secs = (self.curr.taken_at - self.prev.taken_at).total_seconds()
        return Decimal(secs) / Decimal(86400)


def segments(readings: Sequence[ReadingPoint], digits: int = 5) -> list[Segment]:
    s = sort_series(readings)
    return [Segment(a, b, register_delta(a.value, b.value, digits)) for a, b in itertools.pairwise(s)]


# --- per-day table -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Period:
    """Period between two consecutive reading days: ``(start, end]``."""

    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start.day}.{self.start.month}.{self.start.year}-{self.end.day}.{self.end.month}.{self.end.year}"

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def periods_from_dates(dates: Iterable[date]) -> list[Period]:
    ds = sorted(set(dates))
    return [Period(a, b) for a, b in itertools.pairwise(ds)]


@dataclass(frozen=True, slots=True)
class PeriodCell:
    """Consumption of one register in one period.

    ``value is None`` means no reading closed a segment in this period (meter not
    read, or only its very first reading lies here). Consumption of a meter that
    was not read on some day is attributed entirely to the period of its next
    reading (``carried=True``), exactly like the original spreadsheet.
    """

    value: Decimal | None
    prev: ReadingPoint | None = None  # reading the consumption is measured from
    curr: ReadingPoint | None = None  # last reading inside the period
    carried: bool = False  # prev reading lies before period.start
    rollover: bool = False
    anomalies: tuple[str, ...] = ()
    first_reading: bool = False  # only the very first reading of the series is in the period

    @property
    def has_value(self) -> bool:
        return self.value is not None


EMPTY_CELL = PeriodCell(None)


def period_consumption(
    readings: Sequence[ReadingPoint], periods: Sequence[Period], digits: int = 5
) -> list[PeriodCell]:
    """Per-period consumption of one register series (carry-to-next-reading semantics).

    Each segment (prev -> curr) is attributed to the period containing ``curr``'s
    date. Readings dated on or before the first period's start only serve as bases.
    """
    segs = segments(readings, digits)
    s = sort_series(readings)
    first = s[0] if s else None
    out: list[PeriodCell] = []
    for p in periods:
        in_p = [g for g in segs if p.start < g.curr.taken_at.date() <= p.end]
        if not in_p:
            is_first = first is not None and p.start < first.taken_at.date() <= p.end
            out.append(PeriodCell(None, curr=first if is_first else None, first_reading=is_first))
            continue
        total = sum((g.delta.value for g in in_p), ZERO)
        anomalies = tuple(g.delta.anomaly for g in in_p if g.delta.anomaly)
        out.append(
            PeriodCell(
                value=total,
                prev=in_p[0].prev,
                curr=in_p[-1].curr,
                carried=in_p[0].prev.taken_at.date() < p.start,
                rollover=any(g.delta.rollover for g in in_p),
                anomalies=anomalies,
            )
        )
    return out


def sum_optional(values: Iterable[Decimal | None]) -> Decimal | None:
    """Sum ignoring None; None when every value is None."""
    total: Decimal | None = None
    for v in values:
        if v is not None:
            total = v if total is None else total + v
    return total
