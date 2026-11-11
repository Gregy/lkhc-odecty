"""Regression tests for foundation bugs found by the reports phase."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as s
from app.models import Meter, Reading
from tests import synthetic as syn


def test_import_sets_digits_from_data(imported_db: Session) -> None:
    """T2 of the old office / hangar meters has 6-digit values; with digits=5 the counter state shown
    in protocols was taken modulo 100000."""
    by_serial = {m.serial: m for m in imported_db.scalars(select(Meter))}
    assert by_serial[syn.OLD_OFFICE_SERIAL].digits == 6
    assert by_serial[syn.HANGAR_SERIAL].digits == 6
    assert by_serial[syn.ROLLOVER_SERIAL].digits == 5  # the one that really rolled over
    res = s.compute_settlement(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    row = next(r for r in res.interval.meters if r.serial == syn.HANGAR_SERIAL and r.register == "T2")
    assert row.start.display > 100000


def test_negative_remainder_alert_is_formatted(imported_db: Session) -> None:
    now = datetime(2026, 11, 10)
    # the remainder's T1 part is negative in several months (single-tariff sub-meters count fully into T1), but
    # its T1+T2 total is not -> no alert (the alert evaluates the total)
    table = s.tenant_consumption_table(imported_db, start=date(2025, 11, 1), end=date(2026, 6, 1))
    assert table.negative_remainders()
    assert not [a for a in s.alerts(imported_db, now) if a.kind == "negative_remainder"]
    # a sub-meter jump of 50 000 kWh makes the remainder's total negative in that month -> alert
    meter = imported_db.scalar(select(Meter).where(Meter.serial == syn.PARA_SERIAL))
    readings = imported_db.scalars(
        select(Reading).where(Reading.meter_id == meter.id, Reading.taken_at >= datetime(2026, 3, 1))
    ).all()
    assert readings
    for r in readings:
        r.value += 50000
    imported_db.commit()
    msgs = [a.message for a in s.alerts(imported_db, now) if a.kind == "negative_remainder"]
    assert msgs and all("(T1+T2)" in m and "000000" not in m and "provisional" not in m for m in msgs)
    assert all(" T1 " not in m for m in msgs)
