"""Anomaly detection, plausibility checks, expected range, staleness."""

from datetime import datetime, timedelta
from decimal import Decimal as D

from app.domain.anomalies import (
    ANOMALY_SPIKE,
    check_reading,
    detect_anomalies,
    expected_range,
    staleness,
    typical_daily_rate,
)
from app.domain.consumption import ReadingPoint


def monthly(values: list[int], start: datetime = datetime(2025, 1, 1), days: int = 30) -> list[ReadingPoint]:
    return [ReadingPoint(start + timedelta(days=days * i), D(v)) for i, v in enumerate(values)]


def test_detects_decrease_and_spike() -> None:
    rs = monthly([100, 130, 160, 190, 220, 900, 890])
    kinds = [a.kind for a in detect_anomalies(rs)]
    assert kinds == [ANOMALY_SPIKE, "decrease"]


def test_rollover_is_not_an_anomaly() -> None:
    assert detect_anomalies(monthly([99700, 99850, 0, 150, 300])) == []


def test_small_absolute_spike_ignored() -> None:
    assert detect_anomalies(monthly([0, 1, 2, 3, 12])) == []


def test_typical_rate_and_expected_range() -> None:
    rs = monthly([1000, 1300, 1600, 1900])  # 10 kWh/day
    assert typical_daily_rate(rs) == 10
    er = expected_range(rs, datetime(2025, 4, 10))  # 9 days after last reading
    assert er is not None and er.daily_rate == 10 and er.low == 1900 and er.expected == 1990
    assert er.high == 2170  # 3 × expected use
    assert expected_range([], datetime(2025, 1, 1)) is None


def test_check_reading() -> None:
    rs = monthly([1000, 1300, 1600, 1900])
    at = datetime(2025, 5, 1)  # 30 days after last
    assert check_reading(D(2200), at, rs) == []
    codes = [i.code for i in check_reading(D(1800), at, rs)]
    assert codes == ["decrease"]
    high = check_reading(D(3500), at, rs)
    assert [i.code for i in high] == ["high"] and high[0].severity == "warning"
    assert "magnitude" in [i.code for i in check_reading(D(19000), at, rs)]
    # rollover is fine
    assert check_reading(D(100), at, monthly([99000, 99300, 99600, 99900])) == []
    # inserting an older value that does not fit the next reading
    assert [i.code for i in check_reading(D(2000), datetime(2025, 3, 15), rs)] == ["high", "before_last"]


def test_staleness() -> None:
    rs = monthly([1, 2, 3, 4])  # every 30 days, last 2025-04-01
    fresh = staleness(rs, datetime(2025, 4, 20))
    assert not fresh.overdue and fresh.usual_interval_days == 30
    late = staleness(rs, datetime(2025, 6, 1))
    assert late.overdue and late.days_since == 61
    assert staleness([], datetime(2025, 1, 1)).overdue
