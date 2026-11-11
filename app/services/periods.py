"""Period boundaries: calendar months, reading days, main-meter reading days; settlement bounds."""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.aligned import add_months, date_boundaries, month_boundaries, month_start
from app.domain.consumption import as_datetime
from app.models import Meter, MeterPoint, Reading

MONTHS_CS = (
    "leden",
    "únor",
    "březen",
    "duben",
    "květen",
    "červen",
    "červenec",
    "srpen",
    "září",
    "říjen",
    "listopad",
    "prosinec",
)


def fmt_date_cs(d: date | datetime) -> str:
    return f"{d.day}.{d.month}.{d.year}"


@dataclass(frozen=True, slots=True)
class PeriodSpan:
    """Aligned period ``[start, end]`` (boundaries at midnight)."""

    start: datetime
    end: datetime
    label: str
    kind: str  # month | main (between main-meter reading days) | custom

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        return self.end.date()

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def key(self) -> str:
        """Stable identifier, e.g. '2025-01' for months or '2025-01-02_2025-02-01'."""
        if self.kind == "month":
            return f"{self.start.year:04d}-{self.start.month:02d}"
        return f"{self.start_date.isoformat()}_{self.end_date.isoformat()}"


def reading_dates(db: Session, start: date | None = None, end: date | None = None) -> list[date]:
    """Distinct days with at least one confirmed reading (optionally ``start <= day <= end``).

    These are the rows of the raw per-day view (like the rows of the old sheet; for imported data
    they are exactly the sheet's row dates)."""
    day = func.date(Reading.taken_at)
    q = select(day).where(Reading.status == "confirmed").group_by(day).order_by(day)
    if start is not None:
        q = q.where(Reading.taken_at >= as_datetime(start))
    if end is not None:
        q = q.where(Reading.taken_at < as_datetime(add_days(end, 1)))
    return [date.fromisoformat(d) for (d,) in db.execute(q)]


def main_reading_dates(db: Session, start: date | None = None, end: date | None = None) -> list[date]:
    """Days with a confirmed reading of a main meter (``role == "main"``) – natural period boundaries:
    the main meter (and so the remainder tenant) is exact there."""
    day = func.date(Reading.taken_at)
    q = (
        select(day)
        .join(Meter, Meter.id == Reading.meter_id)
        .join(MeterPoint, MeterPoint.id == Meter.meter_point_id)
        .where(Reading.status == "confirmed", MeterPoint.role == "main")
        .group_by(day)
        .order_by(day)
    )
    if start is not None:
        q = q.where(Reading.taken_at >= as_datetime(start))
    if end is not None:
        q = q.where(Reading.taken_at < as_datetime(add_days(end, 1)))
    return [date.fromisoformat(d) for (d,) in db.execute(q)]


def add_days(d: date, n: int) -> date:
    return date.fromordinal(d.toordinal() + n)


def spans_from_boundaries(boundaries: Sequence[datetime], kind: str = "custom") -> list[PeriodSpan]:
    out = []
    for a, b in itertools.pairwise(boundaries):
        if kind == "month":
            label = f"{MONTHS_CS[a.month - 1]} {a.year}"
        else:
            label = f"{fmt_date_cs(a)}–{fmt_date_cs(b)}"
        out.append(PeriodSpan(a, b, label, kind))
    return out


def month_spans(start: date, end: date) -> list[PeriodSpan]:
    """Calendar months from ``start``'s month up to (excluding) ``end``'s month start.

    ``month_spans(date(2025,1,1), date(2026,1,1))`` -> 12 spans Jan..Dec 2025.
    """
    return spans_from_boundaries(month_boundaries(start, end), "month")


def main_reading_spans(db: Session, start: date | None = None, end: date | None = None) -> list[PeriodSpan]:
    """Aligned periods between consecutive main-meter reading days (within [start, end] when given)."""
    return spans_from_boundaries(date_boundaries(main_reading_dates(db, start, end)), "main")


def reading_date_range(db: Session) -> tuple[date, date] | None:
    lo, hi = db.execute(
        select(func.min(Reading.taken_at), func.max(Reading.taken_at)).where(Reading.status == "confirmed")
    ).one()
    if lo is None:
        return None
    return lo.date(), hi.date()


def default_month_range(db: Session, today: date | None = None) -> tuple[date, date] | None:
    """(first month start, end boundary) covering all readings: end = 1st day of the month after
    the latest reading, but never later than the start of next month relative to ``today``."""
    rng = reading_date_range(db)
    if rng is None:
        return None
    today = today or date.today()
    end = min(add_months(rng[1], 1), add_months(today, 1))
    return month_start(rng[0]), end


@dataclass(frozen=True, slots=True)
class SettlementBounds:
    start: date
    end: date
    boundaries: list[datetime]  # aligned month boundaries inside [start, end]
    # helper for the "own dates" variant: main-meter reading days nearest to 1.1. of the year / next year
    main_near_start: date | None = None
    main_near_end: date | None = None


def nearest_date(dates: Iterable[date], target: date) -> date | None:
    ds = list(dates)
    if not ds:
        return None
    return min(ds, key=lambda d: (abs((d - target).days), d))


def settlement_month_boundaries(start: date, end: date) -> list[datetime]:
    """``start``, every 1st of a month strictly inside, ``end`` (start/end may be any dates)."""
    pts = {start, end}
    m = add_months(month_start(start), 1)
    while m < end:
        pts.add(m)
        m = add_months(m, 1)
    return sorted(as_datetime(d) for d in pts)


def suggest_settlement_bounds(db: Session, year: int) -> SettlementBounds:
    """Default settlement bounds for ``year``: exactly 1.1.year – 1.1.(year+1) on aligned months,
    plus the main-meter reading days nearest to those dates (an alternative the user may pick)."""
    s, e = date(year, 1, 1), date(year + 1, 1, 1)
    main = main_reading_dates(db)
    return SettlementBounds(s, e, settlement_month_boundaries(s, e), nearest_date(main, s), nearest_date(main, e))
