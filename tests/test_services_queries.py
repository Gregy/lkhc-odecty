"""DB-backed services on the imported data set."""

import json
from datetime import date, datetime
from decimal import Decimal as D

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as s
from app.domain.aligned import Quality
from app.models import Meter, Reading
from tests import synthetic as syn


def _tenant(net: s.Network, name: str) -> s.TenantInfo:
    return next(t for t in net.tenants if t.name == name)


def test_reading_days_and_spans(imported_db: Session) -> None:
    days = s.reading_dates(imported_db)
    assert len(days) == 32 and days[0] == date(2023, 12, 30) and days[1] == date(2024, 1, 1)
    spans = s.month_spans(date(2025, 1, 1), date(2026, 1, 1))
    assert len(spans) == 12 and spans[0].label == "leden 2025" and spans[0].key == "2025-01"
    mains = s.main_reading_dates(imported_db, date(2025, 1, 1), date(2025, 12, 31))
    assert len(s.main_reading_spans(imported_db, date(2025, 1, 1), date(2025, 12, 31))) == len(mains) - 1 == 10
    assert s.default_month_range(imported_db, today=date(2026, 11, 10)) == (date(2023, 12, 1), date(2026, 7, 1))


def test_suggest_settlement_bounds(imported_db: Session) -> None:
    b = s.suggest_settlement_bounds(imported_db, 2025)
    assert (b.start, b.end, len(b.boundaries)) == (date(2025, 1, 1), date(2026, 1, 1), 13)
    # alternative own dates: main-meter reading days nearest to 1.1. (like the old Final_2025 sheet)
    assert (b.main_near_start, b.main_near_end) == (date(2025, 1, 2), date(2026, 1, 4))


def test_tenant_monthly_sums_equal_interval(imported_db: Session) -> None:
    net = s.load_network(imported_db)
    table = s.tenant_consumption_table(imported_db, start=date(2025, 1, 1), end=date(2026, 1, 1), net=net)
    interval = s.consumption_over_interval(imported_db, date(2025, 1, 1), date(2026, 1, 1), net=net)
    assert len(table.periods) == 12
    for t in net.tenants:
        for reg in ("T1", "T2"):
            assert table.period_sum(t.id, reg).value == interval.tenants[t.id][reg].value, (t.name, reg)
    # remainder = main - others in every period
    rem = _tenant(net, syn.REMAINDER)
    for i in range(12):
        for reg in ("T1", "T2"):
            others = sum((table.value(t.id, reg, i).value for t in net.tenants if t.id != rem.id), D(0))
            assert table.value(rem.id, reg, i).value == table.main[reg][i].value - others


def test_aligned_vs_raw_view_on_reading_days(imported_db: Session) -> None:
    """On reading-day boundaries, tenants whose meters are read every time match the raw view exactly."""
    net = s.load_network(imported_db)
    res = s.consumption_over_interval(imported_db, date(2025, 1, 2), date(2026, 1, 4), net=net)
    raw = s.raw_allocation_table(imported_db, date(2025, 1, 3), date(2026, 1, 4), net=net)
    for name in (syn.A, syn.E, syn.C, syn.F, syn.D):
        t = _tenant(net, name)
        raw_sum = sum((raw.value(i, t.id, "T1") or D(0)) for i in range(len(raw.periods)))
        assert res.tenants[t.id]["T1"].value == raw_sum, name


def test_point_table_and_control_balance(imported_db: Session) -> None:
    pt = s.point_consumption_table(imported_db, start=date(2025, 11, 1), end=date(2026, 1, 1))
    rows = s.control_balances(pt)
    names = {p.id: p.name for p in pt.points}
    control = [r for r in rows if names[r.control_point_id] == syn.CONTROL_POINT]
    assert len(control) == 1 and control[0].register == "T1"
    assert control[0].difference.value == control[0].control.value - control[0].children.value
    assert any(names[r.control_point_id] == syn.MAIN_POINT for r in rows)


def test_raw_view_tables(imported_db: Session) -> None:
    t = s.raw_consumption_table(imported_db)
    assert len(t.periods) == 31 and len(t.columns) == 21
    alloc = s.raw_allocation_table(imported_db)
    assert alloc.negative_remainders()  # the sheet has negative remainder values too


def test_latest_readings_and_hints(imported_db: Session) -> None:
    latest = s.latest_readings(imported_db)
    main = imported_db.scalar(select(Meter).where(Meter.serial == syn.MAIN_SERIAL))
    last = syn.value("B", "3.6.2026")
    assert latest[(main.id, "T1")].value == last and latest[(main.id, "T1")].taken_at == datetime(2026, 6, 3)
    er = s.meter_expected_range(imported_db, main.id, "T1", date(2026, 7, 3))
    assert er is not None and er.low == last and er.expected > last and er.high > er.expected
    hints = s.meter_hints(imported_db, date(2026, 7, 3))
    assert [h.serial for h in hints][:2] == [syn.MAIN_SERIAL, syn.PARA_SERIAL]
    assert all(h.serial not in (syn.OLD_OFFICE_SERIAL, syn.KIOSK_SERIAL) for h in hints)  # removed meters excluded


def test_check_new_reading(imported_db: Session) -> None:
    main = imported_db.scalar(select(Meter).where(Meter.serial == syn.MAIN_SERIAL))
    last = D(int(syn.value("B", "3.6.2026")))
    lower = [i.code for i in s.check_new_reading(imported_db, main.id, "T1", last - 700, date(2026, 7, 3))]
    assert lower == ["decrease"]
    er = s.meter_expected_range(imported_db, main.id, "T1", date(2026, 7, 3))
    assert s.check_new_reading(imported_db, main.id, "T1", er.expected.quantize(D(1)), date(2026, 7, 3)) == []


def test_staleness_and_alerts(imported_db: Session) -> None:
    st = s.meter_staleness(imported_db, datetime(2026, 11, 10))
    assert st[0].staleness.overdue
    hangar = next(x for x in st if x.meter.serial == syn.HANGAR_SERIAL)
    assert hangar.staleness.last.taken_at == datetime(2025, 8, 2)
    alerts = s.alerts(imported_db, now=datetime(2026, 11, 10))
    kinds = {a.kind for a in alerts}
    assert {"stale_meter", "anomaly"} <= kinds
    # the remainder's T1 part is negative in some months, but negative-remainder alerts evaluate T1+T2 (positive)
    assert "negative_remainder" not in kinds
    # the synthetic heater month behind the corridor meter is reported as a usage spike
    assert any(a.kind == "anomaly" and syn.CORRIDOR_SERIAL in a.message for a in alerts)
    assert alerts[0].severity in ("error", "warning")


def test_settlement_check_and_snapshot(imported_db: Session) -> None:
    res = s.compute_settlement(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    # the tenant-B hangar meter has no reading after 2.8.2025 -> its end state is provisional
    assert not res.check.can_finalize
    assert any(i.serial == syn.HANGAR_SERIAL for i in res.check.provisional)
    assert res.check.estimated
    snap = s.settlement_snapshot(res)
    json.dumps(snap)  # JSON-serializable
    assert snap["version"] == 1 and len(snap["periods"]) == 12 and len(snap["tenants"]) == 7
    # 2024 with own dates (main-meter reading days): all relevant meters read on/after the boundaries
    r2024 = s.compute_settlement(imported_db, date(2024, 1, 1), date(2025, 1, 2))
    assert r2024.check.can_finalize, [i.message for i in r2024.check.provisional]


def test_late_reading_recomputes_automatically(imported_db: Session) -> None:
    hangar = imported_db.scalar(select(Meter).where(Meter.serial == syn.HANGAR_SERIAL))
    before = s.settlement_check(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    assert not before.can_finalize
    for reg, extra in (("T1", 1000), ("T2", 40)):
        v = syn.last_value("N", reg)[1] + extra
        imported_db.add(
            Reading(meter_id=hangar.id, register=reg, taken_at=datetime(2026, 1, 15), value=D(v), source="manual")
        )
    imported_db.commit()
    after = s.settlement_check(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    assert after.can_finalize
    q = s.consumption_over_interval(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    b_tenant = next(t for t in q.tenant_infos if t.name == syn.B)
    assert q.tenants[b_tenant.id]["T1"].quality == Quality.ESTIMATED


def test_profile_interpolator_service(imported_db: Session) -> None:
    net = s.load_network(imported_db)
    interp = s.profile_interpolator(net)
    kw = {"start": date(2025, 1, 1), "end": date(2025, 4, 1), "net": net}
    prof = s.tenant_consumption_table(imported_db, interpolator=interp, **kw)
    lin = s.tenant_consumption_table(imported_db, **kw)
    te, ta = _tenant(net, syn.E), _tenant(net, syn.A)
    # the main meter (profile) is read on the same days as tenant E's meter -> profile is linear there, same split
    assert [prof.value(te.id, "T1", i).value for i in range(3)] == [lin.value(te.id, "T1", i).value for i in range(3)]
    # tenant A's hall meter was unread 2.1.-30.4.2025 (spans several main readings) -> split follows the profile
    assert [prof.value(ta.id, "T1", i).value for i in range(3)] != [lin.value(ta.id, "T1", i).value for i in range(3)]
    assert prof.period_sum(ta.id).quality == Quality.ESTIMATED
    # ... but on boundaries that are real readings (E read on 2.1. and 2.3.2025) totals are identical
    a = s.consumption_over_interval(imported_db, date(2025, 1, 2), date(2025, 3, 2), interpolator=interp, net=net)
    b = s.consumption_over_interval(imported_db, date(2025, 1, 2), date(2025, 3, 2), net=net)
    read = syn.value("P", "2.3.2025") - syn.value("P", "2.1.2025")
    assert a.tenants[te.id]["T1"].value == b.tenants[te.id]["T1"].value == read
    assert a.tenants[te.id]["T1"].quality == Quality.EXACT
    # the hall meter was not read on 2.3.2025 -> estimated, strategy-dependent
    assert b.tenants[ta.id]["T1"].quality == Quality.ESTIMATED
