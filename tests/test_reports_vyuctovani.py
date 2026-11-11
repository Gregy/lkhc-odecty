"""`/vyuctovani`: create, draft, protocols, XLSX, print, finalize/snapshot/reopen, forced finalize."""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal as D

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as s
from app.models import AuditLog, Meter, Reading, Settlement
from app.services.reports import settlement_document
from app.templating import format_number
from tests import synthetic as syn


def _create(client, year: int = 2025, **extra) -> int:
    r = client.post("/vyuctovani", data={"year": str(year), **extra}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def _tenant(net: s.Network, name: str) -> s.TenantInfo:
    return next(t for t in net.tenants if t.name == name)


def test_list_and_bounds_preview(imported_reader_client) -> None:
    r = imported_reader_client.get("/vyuctovani")
    assert r.status_code == 200 and "Zatím žádné vyúčtování" in r.text
    assert "Nové vyúčtování" not in r.text  # reader cannot create
    frag = imported_reader_client.get("/vyuctovani/hranice?year=2025").text
    assert "odečtů hlavního elektroměru" in frag and "2.1.2025" in frag and "4.1.2026" in frag


def test_reader_cannot_mutate(imported_reader_client, imported_admin_client) -> None:
    r = imported_reader_client.post("/vyuctovani", data={"year": "2025"}, follow_redirects=False)
    assert r.status_code == 403
    sid = _create(imported_admin_client)
    for action in ("finalizovat", "znovu-otevrit", "smazat", "upravit"):
        r = imported_reader_client.post(f"/vyuctovani/{sid}/{action}", data={}, follow_redirects=False)
        assert r.status_code in (403, 422), action
    assert imported_reader_client.get(f"/vyuctovani/{sid}").status_code == 200


def test_csrf_required(imported_admin_client) -> None:
    r = imported_admin_client.post(
        "/vyuctovani", data={"year": "2025"}, headers={"X-CSRF-Token": "bad"}, follow_redirects=False
    )
    assert r.status_code == 403


def test_draft_matches_services(imported_admin_client, imported_db: Session) -> None:
    sid = _create(imported_admin_client, 2025)
    st = imported_db.get(Settlement, sid)
    assert (st.period_start, st.period_end, st.status) == (date(2025, 1, 1), date(2026, 1, 1), "draft")
    html = imported_admin_client.get(f"/vyuctovani/{sid}").text
    res = s.compute_settlement(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    net = s.load_network(imported_db)
    for name in (syn.A, syn.E, syn.REMAINDER):
        t = _tenant(net, name)
        assert format_number(res.interval.tenant_total(t.id).value) in html, name
    # the hangar meter of tenant B is provisional at 1.1.2026 -> finalize blocked, shown
    assert not res.check.can_finalize
    assert f"{syn.HANGAR_POINT} ({syn.HANGAR_SERIAL}) T1: stav k 1.1.2026 je předběžný" in html
    assert "Protokol o spotřebě elektřiny" in html and "NÁVRH" in html
    # replacement of tenant B's office meter in December 2025 is visible in B's protocol
    assert "Nový elektroměr" in html and syn.NEW_OFFICE_SERIAL in html


def test_own_dates_like_final_sheet(imported_admin_client, imported_db: Session) -> None:
    sid = _create(imported_admin_client, 2025, period_start="2025-01-02", period_end="2026-01-04")
    st = imported_db.get(Settlement, sid)
    assert (st.period_start, st.period_end) == (date(2025, 1, 2), date(2026, 1, 4))
    html = imported_admin_client.get(f"/vyuctovani/{sid}").text
    # periods: 2.1.-1.2., then calendar months, then 1.1.-4.1.
    assert "2.1.2025–1.2.2025" in html and "1.1.2026–4.1.2026" in html
    # the bounds helper offers the main-meter reading days nearest to 1.1.
    frag = imported_admin_client.get("/vyuctovani/hranice?year=2025").text
    assert "2.1.2025" in frag and "4.1.2026" in frag


def test_finalize_blocked_then_forced(imported_admin_client, imported_db: Session) -> None:
    sid = _create(imported_admin_client, 2025)
    r = imported_admin_client.post(f"/vyuctovani/{sid}/finalizovat", data={}, follow_redirects=True)
    assert "Nelze finalizovat" in r.text
    imported_db.expire_all()
    assert imported_db.get(Settlement, sid).status == "draft"
    # force without note -> refused
    r = imported_admin_client.post(f"/vyuctovani/{sid}/finalizovat", data={"force": "1"}, follow_redirects=True)
    assert "vyžaduje poznámku" in r.text
    imported_db.expire_all()
    assert imported_db.get(Settlement, sid).status == "draft"
    # force with note -> final, flagged, audited
    r = imported_admin_client.post(
        f"/vyuctovani/{sid}/finalizovat", data={"force": "1", "note": "hangár nepřístupný"}, follow_redirects=True
    )
    assert r.status_code == 200
    imported_db.expire_all()
    st = imported_db.get(Settlement, sid)
    assert st.status == "final" and st.finalize_forced and st.finalize_note == "hangár nepřístupný"
    assert st.snapshot["version"] == 1 and st.snapshot["reports"] == 1 and st.snapshot_version == 1
    log = imported_db.scalar(select(AuditLog).where(AuditLog.action == "finalize", AuditLog.entity_id == sid))
    assert log is not None and log.changes["forced"] is True and log.changes["provisional"]
    assert "vynuceně" in r.text


def _make_finalizable(db: Session) -> None:
    """Add a reading for the hangar meter after 1.1.2026 so no boundary is provisional."""
    meter = db.scalar(select(Meter).where(Meter.serial == syn.HANGAR_SERIAL))
    last = {
        reg: db.scalar(
            select(Reading.value)
            .where(Reading.meter_id == meter.id, Reading.register == reg)
            .order_by(Reading.taken_at.desc())
        )
        for reg in ("T1", "T2")
    }
    for reg in ("T1", "T2"):
        db.add(
            Reading(
                meter_id=meter.id,
                register=reg,
                taken_at=datetime(2026, 2, 2),
                value=last[reg] + 500,
                source="manual",
                status="confirmed",
            )
        )
    db.commit()


def test_finalize_snapshot_is_frozen_and_reopen(imported_admin_client, imported_db: Session) -> None:
    _make_finalizable(imported_db)
    sid = _create(imported_admin_client, 2025)
    res = s.compute_settlement(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    assert res.check.can_finalize
    r = imported_admin_client.post(f"/vyuctovani/{sid}/finalizovat", data={"note": "ok"}, follow_redirects=True)
    assert "Vyúčtování finalizováno." in r.text
    imported_db.expire_all()
    st = imported_db.get(Settlement, sid)
    assert st.status == "final" and not st.finalize_forced
    net = s.load_network(imported_db)
    tenant_e = _tenant(net, syn.E)
    snap_total = D(next(t for t in st.snapshot["tenants"] if t["id"] == tenant_e.id)["total"]["value"])
    assert snap_total == res.interval.tenant_total(tenant_e.id).value

    # a later correction of the first reading after the end boundary must not change the finalized settlement
    meter = imported_db.scalar(select(Meter).where(Meter.serial == syn.E_SERIAL))
    rd = imported_db.scalar(
        select(Reading)
        .where(Reading.meter_id == meter.id, Reading.taken_at > datetime(2026, 1, 1))
        .order_by(Reading.taken_at)
    )
    rd.value = rd.value + 1000
    imported_db.commit()
    live_res = s.compute_settlement(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    live = live_res.interval.tenant_total(tenant_e.id).value
    assert live != snap_total
    html = imported_admin_client.get(f"/vyuctovani/{sid}").text
    assert format_number(snap_total) in html
    assert "Aktuální data se od snapshotu liší" in html
    xlsx = openpyxl.load_workbook(io.BytesIO(imported_admin_client.get(f"/vyuctovani/{sid}/export.xlsx").content))
    ws = xlsx[syn.E]
    vals = [c.value for row in ws.iter_rows() for c in row]
    assert float(snap_total) in vals or int(snap_total) in vals

    # finalized cannot be edited or deleted
    imported_admin_client.post(f"/vyuctovani/{sid}/smazat", follow_redirects=True)
    imported_db.expire_all()
    assert imported_db.get(Settlement, sid) is not None

    # reopen needs a reason, is audited, and returns to live data
    r = imported_admin_client.post(f"/vyuctovani/{sid}/znovu-otevrit", data={"note": ""}, follow_redirects=True)
    assert "Uveďte důvod" in r.text
    r = imported_admin_client.post(
        f"/vyuctovani/{sid}/znovu-otevrit", data={"note": "oprava odečtu E"}, follow_redirects=True
    )
    imported_db.expire_all()
    st = imported_db.get(Settlement, sid)
    assert st.status == "draft" and st.snapshot is None and st.finalized_at is None
    log = imported_db.scalar(select(AuditLog).where(AuditLog.action == "reopen", AuditLog.entity_id == sid))
    assert log is not None and log.note == "oprava odečtu E" and syn.E in log.changes["snapshot_totals"]
    assert format_number(live) in r.text


def test_xlsx_export_structure(imported_admin_client, imported_db: Session) -> None:
    sid = _create(imported_admin_client, 2025)
    r = imported_admin_client.get(f"/vyuctovani/{sid}/export.xlsx")
    assert r.status_code == 200 and "vyuctovani-2025-navrh.xlsx" in r.headers["content-disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    net = s.load_network(imported_db)
    assert wb.sheetnames == ["Souhrn", *[t.name for t in net.tenants]]
    summary = [[c.value for c in row] for row in wb["Souhrn"].iter_rows()]
    flat = [v for row in summary for v in row if v is not None]
    assert "Kvalita hodnot:" in flat and "předběžně" in flat
    assert "odhad" not in flat and not any("interpolace):" in str(v) for v in flat)  # interpolation is not flagged
    hdr = next(row for row in summary if row[0] == "Období")
    assert f"{syn.B} kvalita" in hdr and f"{syn.A} T1" in hdr
    total = next(row for row in summary if row[0] == "Celkem")
    res = s.compute_settlement(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    tenant_a = _tenant(net, syn.A)
    assert abs(total[hdr.index(f"{syn.A} celkem")] - float(res.interval.tenant_total(tenant_a.id).value)) < 1e-6
    assert total[hdr.index(f"{syn.B} kvalita")] == "předběžně"
    tenant_b = [[c.value for c in row] for row in wb[syn.B].iter_rows()]
    meter_hdr = next(row for row in tenant_b if row[0] == "Místo")
    assert meter_hdr[:5] == ["Místo", "Výr. číslo", "Tarif", "Počáteční stav", "Kvalita"]
    hangar = [row for row in tenant_b if row[0] == syn.HANGAR_POINT]
    assert hangar and hangar[0][7] == "předběžně" and "extrapolace" in hangar[0][8]
    assert any(row[0] == "Po obdobích" for row in tenant_b)


def test_print_view(imported_reader_client, imported_admin_client, imported_db: Session) -> None:
    sid = _create(imported_admin_client, 2025)
    html = imported_reader_client.get(f"/vyuctovani/{sid}/tisk").text
    net = s.load_network(imported_db)
    assert html.count('class="protocol"') == len(net.tenants) + 1  # summary + one per tenant
    assert "window.print()" in html and "Výpočet zbytku" in html
    t = _tenant(net, syn.E)
    one = imported_reader_client.get(f"/vyuctovani/{sid}/tisk?najemce={t.id}").text
    assert one.count('class="protocol"') == 1 and syn.E in one
    assert imported_reader_client.get(f"/vyuctovani/{sid}/tisk?najemce=999").status_code == 404


def test_edit_and_delete_draft(imported_admin_client, imported_db: Session) -> None:
    sid = _create(imported_admin_client, 2025)
    r = imported_admin_client.post(
        f"/vyuctovani/{sid}/upravit",
        data={"name": "Pololetí", "mode": "months", "period_start": "2025-01-01", "period_end": "2025-07-01"},
        follow_redirects=True,
    )
    assert r.status_code == 200 and "Pololetí" in r.text and "leden 2025" in r.text and "srpen 2025" not in r.text
    assert imported_db.scalar(
        select(AuditLog).where(AuditLog.action == "update", AuditLog.entity_type == "settlements")
    )
    imported_admin_client.post(f"/vyuctovani/{sid}/smazat", follow_redirects=True)
    imported_db.expire_all()
    assert imported_db.get(Settlement, sid) is None


def test_document_allocations_sum_to_totals(imported_db: Session) -> None:
    res = s.compute_settlement(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    doc = settlement_document(res, s.load_network(imported_db))
    for td in doc["tenants"]:
        if td["is_remainder"]:
            rem = td["remainder"]
            for reg in ("T1", "T2"):
                v = D(rem["main"][reg]["value"]) - sum(D(o["values"][reg]["value"]) for o in rem["others"])
                assert v == D(td["totals"][reg]["value"])
            continue
        for reg in ("T1", "T2"):
            parts = sum((D(a["values"][reg]["value"]) for a in td["allocations"]), D(0))
            assert parts == D(td["totals"][reg]["value"]), (td["name"], reg)
