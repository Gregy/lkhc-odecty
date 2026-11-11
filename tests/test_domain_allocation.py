"""Tenant allocation (aligned + raw), validity windows, control balance."""

from datetime import date, datetime
from decimal import Decimal as D

from app.domain.aligned import QZERO, MeterSeries, Quality, QValue, point_consumption
from app.domain.allocation import (
    AllocationRule,
    TenantRef,
    allocate,
    allocate_aligned,
    control_balance,
    control_balance_aligned,
    validity_fraction,
    validity_window,
)
from app.domain.consumption import ReadingPoint

TENANTS = [TenantRef(1, "Provozovatel", True), TenantRef(2, "Nájemce A"), TenantRef(3, "Nájemce B")]
MAIN = 10
RULES = [AllocationRule(2, 20), AllocationRule(2, 21), AllocationRule(3, 30)]


def test_raw_allocation_remainder_and_none_semantics() -> None:
    res = allocate(
        {MAIN: D(1000), 20: D(300), 21: None, 30: None, 99: D(50)},
        RULES,
        TENANTS,
        [MAIN],
        date(2025, 1, 1),
        date(2025, 2, 1),
    )
    assert res.values[2] == 300
    assert res.values[3] is None  # no data
    assert res.values[1] == 700  # unallocated point 99 stays in the remainder
    assert not res.negative_remainder


def test_raw_allocation_missing_main_gives_negative_remainder() -> None:
    res = allocate({MAIN: None, 20: D(100)}, RULES, TENANTS, [MAIN], date(2025, 1, 1), date(2025, 2, 1))
    assert res.values[1] == -100 and res.negative_remainder
    empty = allocate({MAIN: None}, RULES, TENANTS, [MAIN], date(2025, 1, 1), date(2025, 2, 1))
    assert empty.values[1] is None


def test_factor_and_validity() -> None:
    rules = [AllocationRule(2, 20, D("0.5")), AllocationRule(3, 20, D("0.5"), valid_from=date(2025, 1, 16))]
    res = allocate({MAIN: D(100), 20: D(62)}, rules, TENANTS, [MAIN], date(2025, 1, 1), date(2025, 2, 1))
    assert res.values[2] == 31
    assert res.values[3] == 16  # raw view pro-rates by days: 62 × 0.5 × 16/31
    assert validity_fraction(rules[1], date(2025, 1, 1), date(2025, 2, 1)) == D(16) / D(31)
    assert validity_window(rules[1], date(2025, 1, 1), date(2025, 2, 1)) == (
        datetime(2025, 1, 16),
        datetime(2025, 2, 1),
    )
    assert validity_window(rules[1], date(2024, 1, 1), date(2025, 1, 1)) is None


def _fn(values: dict[int, QValue]):
    return lambda pid, t0, t1: values.get(pid, QZERO)


def test_aligned_allocation_quality_propagates_to_remainder() -> None:
    vals = {
        MAIN: QValue(D(1000), Quality.EXACT),
        20: QValue(D(300), Quality.ESTIMATED, D(100)),
        30: QValue(D(50), Quality.PROVISIONAL, D(50)),
    }
    res = allocate_aligned(_fn(vals), RULES, TENANTS, [MAIN], date(2025, 1, 1), date(2025, 2, 1))
    assert res.values[2].value == 300 and res.values[2].quality == Quality.ESTIMATED
    assert res.values[3].quality == Quality.PROVISIONAL
    rem = res.values[1]
    assert rem.value == 650 and rem.quality == Quality.PROVISIONAL and rem.estimated_kwh == 150
    assert res.main.value == 1000 and res.allocated.value == 350


def test_aligned_allocation_uses_clipped_window_not_time_share() -> None:
    """A point allocated from mid-interval gets its consumption in that window (kiosk case)."""
    readings = [ReadingPoint(datetime(2025, 4, 30), D(0)), ReadingPoint(datetime(2025, 12, 1), D(287))]
    meter = MeterSeries(1, "S", installed_on=date(2025, 4, 30), readings={"T1": readings})
    rules = [AllocationRule(2, 20, valid_from=date(2025, 4, 30))]

    def fn(pid: int, t0: datetime, t1: datetime) -> QValue:
        return point_consumption([meter], t0, t1)["T1"] if pid == 20 else QZERO

    res = allocate_aligned(fn, rules, TENANTS, [], date(2025, 1, 1), date(2025, 12, 1))
    assert res.values[2].value == 287


def test_control_balance() -> None:
    b = control_balance(D(50), [D(30), None])
    assert b.difference == 20 and not b.negative
    assert control_balance(None, [D(1)]).difference is None
    _c, _ch, diff = control_balance_aligned(QValue(D(10)), [QValue(D(12), Quality.ESTIMATED, D(2))])
    assert diff.value == -2 and diff.quality == Quality.ESTIMATED
