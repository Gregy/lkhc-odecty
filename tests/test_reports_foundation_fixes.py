"""Regression tests for foundation bugs found by the reports phase."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as s
from app.models import Meter
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
    from datetime import datetime

    msgs = [a.message for a in s.alerts(imported_db, datetime(2026, 11, 10)) if a.kind == "negative_remainder"]
    assert msgs and all("000000" not in m and "provisional" not in m for m in msgs)
