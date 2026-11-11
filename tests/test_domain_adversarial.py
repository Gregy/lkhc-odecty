"""Adversarial / property-style checks of the aligned calculation (written by an independent reviewer).

Invariants:
- Σ consumption over adjacent periods == difference of the boundary states (telescoping), and
  == Σ real deltas when the outer boundaries are real readings - with rollovers, readings at odd
  times of day and boundaries at arbitrary dates;
- a late-arriving reading (entered with its real date) only changes periods that were not ``exact``;
- open-end extrapolation is robust against a very short last interval (e.g. a 2-day date typo);
- a meter removed without a final reading never produces a negative period, and periods after
  the removal are exactly 0.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from itertools import pairwise

import pytest

from app.domain.aligned import (
    METHOD_AFTER_REMOVAL,
    MeterSeries,
    Quality,
    RegisterSeries,
    month_boundaries,
    point_aligned,
)
from app.domain.consumption import ReadingPoint, register_delta


def _series(rnd: random.Random, n: int = 30, digits: int = 5) -> list[ReadingPoint]:
    t = datetime(2023, 12, 30) + timedelta(hours=rnd.choice([0, 0, 0, 9, 17]))
    v = D(rnd.randint(0, 10**digits - 1))
    out = [ReadingPoint(t, v)]
    for _ in range(n):
        t += timedelta(days=rnd.choice([1, 2, 14, 28, 30, 31, 33, 60, 92]), hours=rnd.choice([0, 0, 5, 13]))
        v = (v + D(rnd.randint(0, 3000)) + D(rnd.randint(0, 999)) / 1000) % (10**digits)
        out.append(ReadingPoint(t, v))
    return out


def _real_delta(readings: list[ReadingPoint], digits: int = 5) -> D:
    return sum((register_delta(a.value, b.value, digits).value for a, b in pairwise(readings)), D(0))


@pytest.mark.parametrize("seed", range(25))
def test_telescoping_with_arbitrary_boundaries(seed: int) -> None:
    rnd = random.Random(seed)
    readings = _series(rnd)
    rs = RegisterSeries(readings, 5)
    first, last = readings[0].taken_at, readings[-1].taken_at
    inner = {first + timedelta(days=rnd.randint(1, (last - first).days - 1)) for _ in range(12)}
    inner |= {datetime.combine(r.taken_at.date(), datetime.min.time()) for r in rnd.sample(readings, 5)}
    inner |= set(month_boundaries(first.date(), last.date()))
    bounds = sorted(b for b in inner if first < b < last)
    # outer boundaries = real reading times -> Σ == Σ of real deltas
    vals = rs.aligned([first, *bounds, last])
    assert sum((v.value for v in vals), D(0)) == _real_delta(readings)
    # beyond the data (before first / open end) it still telescopes on the cumulative scale
    wide = [first - timedelta(days=40), *bounds, last + timedelta(days=50)]
    vals = rs.aligned(wide)
    assert sum((v.value for v in vals), D(0)) == rs.value_at(wide[-1]).cumulative - rs.value_at(wide[0]).cumulative
    for v in vals:
        assert D(0) <= v.estimated_kwh <= abs(v.value) + D("0.01")
        if v.quality == Quality.EXACT:
            assert v.estimated_kwh == 0


@pytest.mark.parametrize("seed", range(25))
def test_late_reading_never_changes_exact_periods(seed: int) -> None:
    rnd = random.Random(1000 + seed)
    # manual readings are stored at 00:00 of the day
    readings = [ReadingPoint(datetime.combine(r.taken_at.date(), datetime.min.time()), r.value) for r in _series(rnd)]
    readings = list({r.taken_at: r for r in readings}.values())
    first, last = readings[0].taken_at, readings[-1].taken_at
    bounds = month_boundaries(first.date(), (last + timedelta(days=90)).date())
    before = RegisterSeries(readings, 5).aligned(bounds)
    # a tenant reports a reading weeks later, entered with its real date (a day without a reading)
    days = {r.taken_at.date() for r in readings}
    for _ in range(5):
        i = rnd.randrange(len(readings))  # after reading i (possibly after the last one)
        a = readings[i]
        b = readings[i + 1] if i + 1 < len(readings) else None
        span = (b.taken_at - a.taken_at).days if b else 60
        if span < 2:
            continue
        t = a.taken_at + timedelta(days=rnd.randint(1, span - 1))
        if t.date() in days:
            continue
        if b is not None:
            step = register_delta(a.value, b.value).value
            v = (a.value + step * D(rnd.randint(0, 100)) / 100) % 100000
        else:
            v = (a.value + D(rnd.randint(0, 2000))) % 100000
        late = ReadingPoint(t, v.quantize(D("0.001")))
        after = RegisterSeries([*readings, late], 5).aligned(bounds)
        for (start, end), x, y in zip(pairwise(bounds), before, after, strict=True):
            if x.quality == Quality.EXACT:
                assert y.value == x.value and y.quality == Quality.EXACT, (start, end, late)
        # a late reading inside the data only redistributes: first..last reading total is unchanged
        if b is not None:
            span = [first, *[x for x in bounds if first < x < last], last]
            assert sum(v.value for v in RegisterSeries([*readings, late], 5).aligned(span)) == _real_delta(readings)


def test_short_last_interval_does_not_blow_up_extrapolation() -> None:
    """XLSX has readings 1.6.2026 and 3.6.2026 (probably a typo for 3.7.); the 2-day span must not
    be used alone as the usage rate for the open end."""
    readings = [
        ReadingPoint(datetime(2026, 3, 1), D(1000)),
        ReadingPoint(datetime(2026, 4, 1), D(1310)),
        ReadingPoint(datetime(2026, 5, 3), D(1630)),
        ReadingPoint(datetime(2026, 6, 1), D(1920)),
        ReadingPoint(datetime(2026, 6, 3), D(2220)),  # a month's usage "in two days"
    ]
    rs = RegisterSeries(readings, 5)
    july = rs.consumption(datetime(2026, 7, 1), datetime(2026, 8, 1))
    assert july.quality == Quality.PROVISIONAL
    # reference = at least 28 days back (3.5.-3.6.: 590 kWh / 31 days ≈ 19/day), not 150/day
    assert D(400) < july.value < D(700)


def test_meter_removed_without_final_reading_has_no_negative_month() -> None:
    # replacement recorded from the photo review: old meter removed 10.11.2026, last read 3.6.2026
    old = MeterSeries(
        1,
        "OLD",
        removed_on=date(2026, 11, 10),
        readings={"T1": [ReadingPoint(datetime(2026, 5, 1), D(1000)), ReadingPoint(datetime(2026, 6, 3), D(1330))]},
    )
    new = MeterSeries(
        2,
        "NEW",
        installed_on=date(2026, 11, 10),
        readings={"T1": [ReadingPoint(datetime(2026, 11, 10), D(52))]},
    )
    bounds = month_boundaries(date(2026, 6, 1), date(2027, 3, 1))
    vals = point_aligned([old, new], bounds)["T1"]
    assert all(v.value >= 0 for v in vals), [v.value for v in vals]
    old_vals = old.register_series("T1").aligned(bounds)
    # it ran until the removal (estimated, finalizable), then exactly nothing
    assert all(v.quality == Quality.ESTIMATED for v in old_vals[:6])  # June..November
    assert [v.value for v in old_vals[6:]] == [0, 0, 0]  # December..February
    assert all(v.quality == Quality.EXACT for v in old_vals[6:])
    end = old.register_series("T1").value_at(datetime(2027, 1, 1))
    assert end.method == METHOD_AFTER_REMOVAL and end.quality == Quality.ESTIMATED
    # telescoping still holds across the removal
    total = sum((v.value for v in old_vals), D(0))
    rs = old.register_series("T1")
    assert total == rs.value_at(bounds[-1]).cumulative - rs.value_at(bounds[0]).cumulative


def test_removed_with_final_reading_stays_exact() -> None:
    rs = RegisterSeries(
        [ReadingPoint(datetime(2025, 11, 1), D(100)), ReadingPoint(datetime(2025, 12, 1), D(160))],
        5,
        removed_on=date(2025, 12, 1),
    )
    v = rs.value_at(datetime(2026, 1, 1))
    assert v.quality == Quality.EXACT and v.method == METHOD_AFTER_REMOVAL and v.display == 160
