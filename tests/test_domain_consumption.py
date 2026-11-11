"""Register deltas (rollover, anomalies) and the raw per-day table."""

from datetime import date, datetime
from decimal import Decimal as D

from app.domain.consumption import (
    ANOMALY_DECREASE,
    ANOMALY_JUMP,
    Period,
    ReadingPoint,
    period_consumption,
    periods_from_dates,
    register_delta,
    sum_optional,
)


def rp(d: str, v: float | int | str) -> ReadingPoint:
    return ReadingPoint(datetime.fromisoformat(d), D(str(v)))


def test_delta_normal() -> None:
    d = register_delta(D(100), D(150))
    assert d.value == 50 and not d.rollover and d.anomaly is None


def test_delta_rollover() -> None:
    # a 5-digit counter passing 99999 (happened in the original data)
    d = register_delta(D(99812), D(377), 5)
    assert d.value == 565 and d.rollover and d.anomaly is None


def test_delta_decrease_is_anomaly_not_rollover() -> None:
    d = register_delta(D(428), D(420), 5)
    assert d.value == -8 and not d.rollover and d.anomaly == ANOMALY_DECREASE


def test_delta_huge_jump_is_anomaly() -> None:
    d = register_delta(D(100), D(60000), 5)
    assert d.anomaly == ANOMALY_JUMP and d.value == 59900


def test_delta_other_digits_and_decimals() -> None:
    assert register_delta(D("999998.5"), D("1.25"), 6).value == D("2.75")
    assert register_delta(D("485.98"), D("486.10"), 7).value == D("0.12")


def test_periods_from_dates_sorted_unique() -> None:
    ps = periods_from_dates([date(2024, 3, 1), date(2024, 1, 1), date(2024, 2, 1), date(2024, 2, 1)])
    assert [(p.start, p.end) for p in ps] == [
        (date(2024, 1, 1), date(2024, 2, 1)),
        (date(2024, 2, 1), date(2024, 3, 1)),
    ]
    assert ps[0].label == "1.1.2024-1.2.2024"


def test_period_consumption_carries_to_next_reading() -> None:
    periods = periods_from_dates([date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1), date(2024, 4, 1)])
    readings = [rp("2024-01-01", 100), rp("2024-03-01", 160), rp("2024-04-01", 170)]
    cells = period_consumption(readings, periods)
    assert [c.value for c in cells] == [None, D(60), D(10)]
    assert cells[1].carried and cells[1].prev.value == 100
    assert not cells[2].carried


def test_period_consumption_first_reading_and_base_before_first_period() -> None:
    periods = periods_from_dates([date(2024, 1, 1), date(2024, 1, 13)])
    # base reading before the first period start (like Odečty row 30.12.2023)
    readings = [rp("2023-12-30", 4810), rp("2024-01-13", 4882)]
    assert period_consumption(readings, periods)[0].value == 72
    only_first = period_consumption([rp("2024-01-05", 10)], periods)[0]
    assert only_first.value is None and only_first.first_reading


def test_period_consumption_sums_multiple_readings_and_rollover() -> None:
    periods = [Period(date(2024, 1, 1), date(2024, 2, 1))]
    readings = [rp("2024-01-01", 99990), rp("2024-01-10", 99999), rp("2024-01-20", 5), rp("2024-02-01", 20)]
    c = period_consumption(readings, periods)[0]
    assert c.value == 30 and c.rollover


def test_sum_optional() -> None:
    assert sum_optional([None, None]) is None
    assert sum_optional([None, D(1), D(2)]) == 3
