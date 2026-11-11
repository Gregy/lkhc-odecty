"""Aligned periods (primary mode): value_at quality, invariants, edge cases."""

import random
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from itertools import pairwise

import pytest

from app.domain.aligned import (
    METHOD_AFTER_REMOVAL,
    METHOD_BEFORE_FIRST,
    METHOD_EXTRAPOLATED,
    METHOD_INTERPOLATED,
    METHOD_NO_DATA,
    METHOD_READING,
    MeterSeries,
    Quality,
    QValue,
    RegisterSeries,
    month_boundaries,
    point_aligned,
    point_consumption,
    qsum,
    value_at,
    worst,
)
from app.domain.consumption import ReadingPoint, register_delta
from app.domain.interpolation import LINEAR, CumulativeProfile, ProfileInterpolator


def rp(d: str, v: float | int | str) -> ReadingPoint:
    return ReadingPoint(datetime.fromisoformat(d), D(str(v)))


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


SERIES = [rp("2025-01-02", 1000), rp("2025-02-01", 1300), rp("2025-03-03", 1600)]


def test_value_at_exact_and_same_day_tolerance() -> None:
    v = value_at(SERIES, date(2025, 2, 1))
    assert v.quality == Quality.EXACT and v.method == METHOD_READING and v.display == 1300
    # a reading taken later the same day counts as exact for a midnight boundary
    later = [rp("2025-01-01", 0), rp("2025-02-01T15:30", 310), rp("2025-03-01", 600)]
    v = value_at(later, date(2025, 2, 1))
    assert v.quality == Quality.EXACT and v.display == 310


def test_value_at_interpolated() -> None:
    v = value_at(SERIES, date(2025, 1, 17))  # 15 of 30 days
    assert v.quality == Quality.ESTIMATED and v.method == METHOD_INTERPOLATED
    assert v.display == D("1150.000")
    assert v.before.value == 1000 and v.after.value == 1300 and v.span_days == 30


def test_value_at_provisional_extrapolation_from_last_interval() -> None:
    v = value_at(SERIES, date(2025, 3, 13))  # last interval: 300 kWh / 30 days = 10/day
    assert v.quality == Quality.PROVISIONAL and v.method == METHOD_EXTRAPOLATED
    assert v.display == D("1700.000")


def test_value_at_before_first_reading_is_flat_not_backward_extrapolated() -> None:
    v = value_at(SERIES, date(2024, 12, 1))
    assert v.quality == Quality.EXACT and v.method == METHOD_BEFORE_FIRST and v.display == 1000


def test_value_at_after_removal_is_flat() -> None:
    v = value_at(SERIES, date(2025, 4, 1), removed_on=date(2025, 3, 3))
    assert v.quality == Quality.EXACT and v.method == METHOD_AFTER_REMOVAL and v.display == 1600


def test_value_at_no_data() -> None:
    assert value_at([], date(2025, 1, 1)).method == METHOD_NO_DATA
    assert value_at([], date(2025, 1, 1)).quality == Quality.PROVISIONAL
    assert value_at([], date(2025, 1, 1), installed_on=date(2025, 2, 1)).quality == Quality.EXACT


def _random_series(seed: int) -> list[ReadingPoint]:
    rnd = random.Random(seed)
    t = datetime(2024, 1, 3, 9, 0)
    v = D(rnd.randint(90000, 99990))
    out = [ReadingPoint(t, v)]
    for _ in range(25):
        t += timedelta(days=rnd.randint(3, 75), hours=rnd.randint(0, 10))
        v = (v + D(rnd.randint(0, 1500)) + D(rnd.randint(0, 99)) / 100) % 100000
        out.append(ReadingPoint(t, v))
    return out


@pytest.mark.parametrize("seed", range(8))
def test_invariant_sum_of_periods_equals_reading_difference(seed: int) -> None:
    """Interpolation only redistributes: Σ periods == Σ real deltas (incl. rollovers)."""
    readings = _random_series(seed)
    rs = RegisterSeries(readings, 5)
    first, last = readings[0].taken_at, readings[-1].taken_at
    # boundaries: the real first/last reading times + every month start in between
    bounds = [first, *[b for b in month_boundaries(first.date(), last.date()) if first < b < last], last]
    vals = rs.aligned(bounds)
    real = sum((register_delta(a.value, b.value, 5).value for a, b in pairwise(readings)), D(0))
    assert sum((v.value for v in vals), D(0)) == real
    # every estimated kWh is accounted for and never exceeds the value
    assert all(D(0) <= v.estimated_kwh <= abs(v.value) for v in vals)


def test_invariant_with_open_end_months() -> None:
    rs = RegisterSeries(SERIES, 5)
    b = month_boundaries(date(2025, 1, 1), date(2025, 6, 1))
    vals = rs.aligned(b)
    # telescoping: Σ periods == value(end) - value(start) on the cumulative scale
    assert sum((v.value for v in vals), D(0)) == rs.value_at(b[-1]).cumulative - rs.value_at(b[0]).cumulative
    assert [v.quality for v in vals] == [
        Quality.EXACT,  # Jan: 1.1. before first reading (flat), 1.2. real reading
        Quality.ESTIMATED,  # Feb: 1.3. interpolated between 1.2. and 3.3.
        Quality.PROVISIONAL,  # Mar..May: open end, extrapolated
        Quality.PROVISIONAL,
        Quality.PROVISIONAL,
    ]
    assert vals[0].value == 300 and vals[0].estimated_kwh == 0


def test_late_arriving_reading_turns_provisional_into_estimated() -> None:
    b = [dt("2025-03-01"), dt("2025-04-01")]
    before = RegisterSeries(SERIES[:2], 5).aligned(b)[0]  # last reading 1.2.
    assert before.quality == Quality.PROVISIONAL
    # tenant reports the reading taken on 3.3. only weeks later -> entered with its real date
    after = RegisterSeries(SERIES, 5).aligned(b)[0]
    assert after.quality == Quality.PROVISIONAL  # still beyond 3.3. -> open end
    after2 = RegisterSeries([*SERIES, rp("2025-04-20", 1900)], 5).aligned(b)[0]
    assert after2.quality == Quality.ESTIMATED
    # 1.3. = 1300 + 300·28/30 = 1580; 1.4. = 1600 + 300·29/48 = 1781.25
    assert after2.value == D("201.250")


def test_meter_missing_several_months_spreads_consumption() -> None:
    readings = [rp("2025-01-01", 0), rp("2025-05-01", 1200)]  # read in Jan, next in May
    vals = RegisterSeries(readings, 5).aligned(month_boundaries(date(2025, 1, 1), date(2025, 5, 1)))
    assert sum((v.value for v in vals), D(0)) == 1200
    assert all(v.quality == Quality.ESTIMATED for v in vals)
    assert vals[0].value == D("310.000")  # 31 of 120 days


def test_new_meter_without_earlier_reading() -> None:
    m = MeterSeries(
        1, "NEW", installed_on=date(2025, 3, 10), readings={"T1": [rp("2025-03-10", 0), rp("2025-04-10", 62)]}
    )
    vals = point_aligned([m], month_boundaries(date(2025, 1, 1), date(2025, 5, 1)))["T1"]
    assert [v.value for v in vals[:2]] == [0, 0]
    assert vals[0].quality == Quality.EXACT and vals[1].quality == Quality.EXACT
    assert vals[2].value == D("44.000")  # 10.3.-1.4. = 22 of 31 days
    assert vals[3].quality == Quality.PROVISIONAL


def test_meter_replacement_inside_period() -> None:
    old = MeterSeries(
        1, "OLD", removed_on=date(2025, 3, 15), readings={"T1": [rp("2025-02-01", 9000), rp("2025-03-15", 9420)]}
    )
    new = MeterSeries(
        2, "NEW", installed_on=date(2025, 3, 15), readings={"T1": [rp("2025-03-15", 0), rp("2025-04-01", 170)]}
    )
    vals = point_aligned([old, new], [dt("2025-03-01"), dt("2025-04-01")])["T1"]
    # old: 1.3.(interp 9000+420*28/42=9280) -> 9420 = 140; new: 0 -> 170
    assert vals[0].value == D("310.000")
    assert vals[0].quality == Quality.ESTIMATED
    assert len(vals[0].parts) == 2
    # after the replacement the old meter is flat (exact), the new one carries on
    apr = point_aligned([old, new], [dt("2025-04-01"), dt("2025-05-01")])["T1"][0]
    assert apr.quality == Quality.PROVISIONAL and apr.value == D("300.000")


def test_rollover_inside_interpolated_span() -> None:
    readings = [rp("2025-01-01", 99900), rp("2025-01-31", 200)]  # 300 kWh across rollover
    rs = RegisterSeries(readings, 5)
    mid = rs.value_at(dt("2025-01-16"))
    assert mid.quality == Quality.ESTIMATED and mid.display == D("50.000") and mid.cumulative == D("100050.000")
    vals = rs.aligned([dt("2025-01-01"), dt("2025-01-16"), dt("2025-01-31")])
    assert [v.value for v in vals] == [D("150.000"), D("150.000")]


def test_estimated_share_counts_only_interpolated_parts() -> None:
    readings = [rp("2025-01-01", 0), rp("2025-01-11", 100), rp("2025-01-21", 200), rp("2025-02-10", 400)]
    v = RegisterSeries(readings, 5).consumption(dt("2025-01-06"), dt("2025-01-31"))
    # 6.1.->11.1. est 50, 11.1.->21.1. exact 100, 21.1.->31.1. est 100
    assert v.value == 250 and v.estimated_kwh == 150 and v.quality == Quality.ESTIMATED
    assert v.share_estimated == D("0.6")


def test_qvalue_aggregation_keeps_worst_quality() -> None:
    a = QValue(D(10), Quality.EXACT)
    b = QValue(D(5), Quality.ESTIMATED, D(5))
    c = QValue(D(1), Quality.PROVISIONAL, D(1))
    s = qsum([a, b])
    assert s.value == 15 and s.quality == Quality.ESTIMATED and s.estimated_kwh == 5
    assert (s - c).quality == Quality.PROVISIONAL and (s - c).value == 14 and (s - c).estimated_kwh == 6
    assert worst([Quality.EXACT, Quality.PROVISIONAL, Quality.ESTIMATED]) == Quality.PROVISIONAL


def test_point_consumption_interval_api() -> None:
    m = MeterSeries(
        1, "A", registers=("T1", "T2"), readings={"T1": SERIES, "T2": [rp("2025-01-02", 0), rp("2025-03-03", 60)]}
    )
    res = point_consumption([m], date(2025, 1, 2), date(2025, 3, 3))
    assert res["T1"].value == 600 and res["T1"].quality == Quality.EXACT
    assert res["T2"].value == 60


def test_month_boundaries() -> None:
    b = month_boundaries(date(2024, 11, 15), date(2025, 2, 1))
    assert b == [dt("2024-11-01"), dt("2024-12-01"), dt("2025-01-01"), dt("2025-02-01")]


def test_profile_interpolator_follows_reference_meter() -> None:
    # reference (main) meter: 100 kWh in first half of January, 300 in the second half
    ref = CumulativeProfile([rp("2025-01-01", 0), rp("2025-01-16", 100), rp("2025-01-31", 400)])
    interp = ProfileInterpolator({"T1": ref})
    sub = RegisterSeries([rp("2025-01-01", 0), rp("2025-01-31", 40)], 5)
    mid = sub.value_at(dt("2025-01-16"), interp)
    assert mid.quality == Quality.ESTIMATED and mid.display == D("10.000")  # 40 × 100/400
    assert sub.value_at(dt("2025-01-16"), LINEAR).display == D("20.000")
    # outside profile coverage -> falls back to linear
    assert interp.share("T1", dt("2024-12-01"), dt("2024-12-31"), dt("2024-12-16")) == D(15) / D(30)
    # extrapolation proportional to profile
    ext = ProfileInterpolator({"T1": CumulativeProfile([rp("2025-01-01", 0), rp("2025-03-01", 600)])})
    v = RegisterSeries([rp("2025-01-01", 0), rp("2025-01-31", 300)], 5).value_at(dt("2025-02-15"), ext)
    assert v.quality == Quality.PROVISIONAL and v.display > 300
