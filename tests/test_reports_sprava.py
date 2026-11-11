"""`/sprava` (admin only): CRUD with audit, walk order, meter replacement, users, audit log."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as s
from app.models import Allocation, AuditLog, Meter, MeterPoint, Reading, Tenant, User
from tests import synthetic as syn

PAGES = [
    "/sprava",
    "/sprava/najemci",
    "/sprava/najemci/2",
    "/sprava/mista",
    "/sprava/mista/5",
    "/sprava/elektromery/5",
    "/sprava/alokace",
    "/sprava/alokace/1",
    "/sprava/uzivatele",
    "/sprava/audit",
]


@pytest.mark.parametrize("url", PAGES)
def test_pages_admin_ok_reader_forbidden(url, imported_admin_client, imported_reader_client) -> None:
    assert imported_admin_client.get(url).status_code == 200, url
    assert imported_reader_client.get(url).status_code == 403, url


def test_nav_hides_sprava_for_reader(imported_reader_client, imported_admin_client) -> None:
    assert 'href="/sprava"' not in imported_reader_client.get("/").text
    assert 'href="/sprava"' in imported_admin_client.get("/").text


def _audit(db: Session, entity: str, action: str) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.entity_type == entity, AuditLog.action == action)))


def test_reader_cannot_post(imported_reader_client, imported_db: Session) -> None:
    for url in ("/sprava/najemci", "/sprava/mista", "/sprava/alokace", "/sprava/uzivatele", "/sprava/mista/poradi"):
        assert imported_reader_client.post(url, data={"name": "x"}).status_code == 403, url
    assert imported_db.scalar(select(Tenant).where(Tenant.name == "x")) is None


def test_csrf_enforced(imported_admin_client) -> None:
    r = imported_admin_client.post("/sprava/najemci", data={"name": "X"}, headers={"X-CSRF-Token": "nope"})
    assert r.status_code == 403
    # form field token works too
    del imported_admin_client.headers["X-CSRF-Token"]
    r = imported_admin_client.post(
        "/sprava/najemci",
        data={"name": "Formulář", "active": "1", "csrf_token": imported_admin_client.csrf_token},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_tenant_crud(imported_admin_client, imported_db: Session) -> None:
    r = imported_admin_client.post(
        "/sprava/najemci",
        data={"name": "Hangár 3", "color": "#e34948", "color_dark": "#e66767", "sort_order": "70", "active": "1"},
        follow_redirects=True,
    )
    assert "Nájemce Hangár 3 vytvořen" in r.text
    t = imported_db.scalar(select(Tenant).where(Tenant.name == "Hangár 3"))
    assert t is not None and t.color == "#e34948" and t.active
    assert _audit(imported_db, "tenants", "create")
    # only one remainder tenant
    r = imported_admin_client.post(
        f"/sprava/najemci/{t.id}", data={"name": "Hangár 3", "is_remainder": "1", "active": "1"}, follow_redirects=True
    )
    assert "Zbytkový nájemce už existuje" in r.text
    # invalid colour
    r = imported_admin_client.post(
        f"/sprava/najemci/{t.id}", data={"name": "Hangár 3", "color": "red"}, follow_redirects=True
    )
    assert "#rrggbb" in r.text
    r = imported_admin_client.post(f"/sprava/najemci/{t.id}", data={"name": "Hangár III"}, follow_redirects=True)
    imported_db.expire_all()
    assert imported_db.get(Tenant, t.id).name == "Hangár III" and not imported_db.get(Tenant, t.id).active
    upd = _audit(imported_db, "tenants", "update")
    assert upd and upd[-1].changes["name"] == ["Hangár 3", "Hangár III"]
    # tenant with allocations cannot be deleted; without can
    tenant_a = imported_db.scalar(select(Tenant).where(Tenant.name == syn.A))
    r = imported_admin_client.post(f"/sprava/najemci/{tenant_a.id}/smazat", follow_redirects=True)
    assert "má alokace" in r.text
    tid = t.id
    imported_admin_client.post(f"/sprava/najemci/{tid}/smazat", follow_redirects=True)
    imported_db.expire_all()
    assert imported_db.get(Tenant, tid) is None and _audit(imported_db, "tenants", "delete")


def test_point_create_order_and_meter(imported_admin_client, imported_db: Session) -> None:
    r = imported_admin_client.post(
        "/sprava/mista",
        data={"name": "Nová dílna", "role": "sub", "sort_order": "155", "active": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    p = imported_db.scalar(select(MeterPoint).where(MeterPoint.name == "Nová dílna"))
    assert r.headers["location"] == f"/sprava/mista/{p.id}"
    # duplicate name -> error, not 500
    r = imported_admin_client.post("/sprava/mista", data={"name": "Nová dílna", "role": "sub"}, follow_redirects=True)
    assert r.status_code == 200 and "koliduje" in r.text
    # add meter with digits / decimals / display note / live source
    r = imported_admin_client.post(
        f"/sprava/mista/{p.id}/elektromery",
        data={
            "serial": "90000001",
            "reg_T1": "1",
            "reg_T2": "1",
            "digits": "6",
            "decimals_shown": "2",
            "display_note": "LCD otočený o 90°",
            "source_type": "live",
            "source_config": '{"kind": "demo"}',
        },
        follow_redirects=True,
    )
    assert "Elektroměr 90000001 přidán" in r.text
    m = imported_db.scalar(select(Meter).where(Meter.serial == "90000001"))
    assert (m.registers, m.digits, m.decimals_shown, m.source_type, m.source_config) == (
        ["T1", "T2"],
        6,
        2,
        "live",
        {"kind": "demo"},
    )
    assert m.display_note == "LCD otočený o 90°"
    # invalid JSON / digits
    r = imported_admin_client.post(
        f"/sprava/mista/{p.id}/elektromery", data={"serial": "X1", "reg_T1": "1", "digits": "20"}, follow_redirects=True
    )
    assert "rozsahu 1–12" in r.text
    # walk order bulk update
    main_point = imported_db.scalar(select(MeterPoint).where(MeterPoint.name == syn.MAIN_POINT))
    r = imported_admin_client.post(
        "/sprava/mista/poradi", data={f"order_{p.id}": "5", f"order_{main_point.id}": "10"}, follow_redirects=True
    )
    assert "Pořadí obchůzky uloženo (1 změn)" in r.text
    imported_db.expire_all()
    assert imported_db.get(MeterPoint, p.id).sort_order == 5
    net = s.load_network(imported_db)
    assert net.points[0].name == "Nová dílna"
    assert any(a.note == "pořadí obchůzky" for a in _audit(imported_db, "meter_points", "update"))
    # new point shows on the public pages
    assert imported_admin_client.get(f"/mista/{p.id}").status_code == 200


def test_meter_replacement(imported_admin_client, imported_db: Session) -> None:
    p = imported_db.scalar(select(MeterPoint).where(MeterPoint.name == syn.E_POINT))
    old = imported_db.scalar(select(Meter).where(Meter.meter_point_id == p.id))
    before = s.consumption_over_interval(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    r = imported_admin_client.post(
        f"/sprava/mista/{p.id}/vymena",
        data={
            "old_meter_id": str(old.id),
            "date": "2026-07-01",
            "old_final_T1": "12345",
            "new_serial": "90000002",
            "new_reg_T1": "1",
            "new_digits": "6",
            "new_decimals_shown": "1",
            "new_source_type": "manual",
            "new_initial_T1": "0",
        },
        follow_redirects=True,
    )
    assert "Výměna zaznamenána" in r.text, r.text[-2000:]
    imported_db.expire_all()
    old = imported_db.get(Meter, old.id)
    new = imported_db.scalar(select(Meter).where(Meter.serial == "90000002"))
    assert old.removed_on == date(2026, 7, 1) and new.installed_on == date(2026, 7, 1)
    assert new.meter_point_id == p.id and new.digits == 6 and new.decimals_shown == 1
    rds = {
        (r.meter_id, r.register): r.value
        for r in imported_db.scalars(select(Reading).where(Reading.taken_at == datetime(2026, 7, 1)))
    }
    assert rds[(old.id, "T1")] == D("12345") and rds[(new.id, "T1")] == D("0")
    assert len(_audit(imported_db, "readings", "create")) == 2
    assert any("výměna" in a.note for a in _audit(imported_db, "meters", "update"))
    # history before the replacement is unchanged
    after = s.consumption_over_interval(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    assert after.points[p.id]["T1"].value == before.points[p.id]["T1"].value
    # cannot replace an already removed meter; date before last reading refused
    r = imported_admin_client.post(
        f"/sprava/mista/{p.id}/vymena",
        data={"old_meter_id": str(old.id), "date": "2026-08-01", "new_serial": "X2", "new_reg_T1": "1"},
        follow_redirects=True,
    )
    assert "už je odebrán" in r.text
    r = imported_admin_client.post(
        f"/sprava/mista/{p.id}/vymena",
        data={"old_meter_id": str(new.id), "date": "2020-01-01", "new_serial": "X3", "new_reg_T1": "1"},
        follow_redirects=True,
    )
    assert "odečet po datu výměny" in r.text
    assert imported_db.scalar(select(Meter).where(Meter.serial == "X3")) is None


def test_meter_edit_and_delete_rules(imported_admin_client, imported_db: Session) -> None:
    m = imported_db.scalar(select(Meter).where(Meter.serial == syn.ACCU_SERIAL))  # T1+T2 with readings
    r = imported_admin_client.post(
        f"/sprava/elektromery/{m.id}",
        data={"serial": m.serial, "reg_T1": "1", "digits": "5", "decimals_shown": "0", "source_type": "manual"},
        follow_redirects=True,
    )
    assert "má odečty" in r.text  # T2 cannot be removed
    r = imported_admin_client.post(
        f"/sprava/elektromery/{m.id}",
        data={
            "serial": m.serial,
            "reg_T1": "1",
            "reg_T2": "1",
            "digits": "6",
            "decimals_shown": "0",
            "source_type": "manual",
            "display_note": "Křižík, bubínky",
        },
        follow_redirects=True,
    )
    imported_db.expire_all()
    assert imported_db.get(Meter, m.id).digits == 6
    assert imported_admin_client.post(f"/sprava/elektromery/{m.id}/smazat", follow_redirects=True).status_code == 200
    imported_db.expire_all()
    assert imported_db.get(Meter, m.id) is not None  # has readings


def test_allocation_crud(imported_admin_client, imported_db: Session) -> None:
    corridor2 = imported_db.scalar(select(MeterPoint).where(MeterPoint.name == syn.CORRIDOR2_POINT))
    tenant_a = imported_db.scalar(select(Tenant).where(Tenant.name == syn.A))
    before = s.consumption_over_interval(imported_db, date(2026, 1, 1), date(2026, 6, 1))
    r = imported_admin_client.post(
        "/sprava/alokace",
        data={
            "tenant_id": str(tenant_a.id),
            "meter_point_id": str(corridor2.id),
            "factor": "0,5",
            "valid_from": "2026-01-01",
        },
        follow_redirects=True,
    )
    assert "Alokace přidána" in r.text
    a = imported_db.scalar(select(Allocation).where(Allocation.meter_point_id == corridor2.id))
    assert a.factor == D("0.5") and a.valid_from == date(2026, 1, 1)
    after = s.consumption_over_interval(imported_db, date(2026, 1, 1), date(2026, 6, 1))
    half = before.points[corridor2.id]["T1"].value * D("0.5")
    assert after.tenants[tenant_a.id]["T1"].value == before.tenants[tenant_a.id]["T1"].value + half
    # invalid validity
    r = imported_admin_client.post(
        f"/sprava/alokace/{a.id}",
        data={
            "tenant_id": str(tenant_a.id),
            "meter_point_id": str(corridor2.id),
            "valid_from": "2026-02-01",
            "valid_to": "2026-01-01",
        },
        follow_redirects=True,
    )
    assert "musí být po" in r.text
    aid = a.id
    imported_admin_client.post(f"/sprava/alokace/{aid}/smazat", follow_redirects=True)
    imported_db.expire_all()
    assert imported_db.get(Allocation, aid) is None
    assert _audit(imported_db, "allocations", "create") and _audit(imported_db, "allocations", "delete")


def test_users(imported_admin_client, imported_db: Session) -> None:
    r = imported_admin_client.post(
        "/sprava/uzivatele",
        data={"email": "pilot@example.org", "name": "Pilot", "role": "reader"},
        follow_redirects=True,
    )
    assert "pilot@example.org přidán" in r.text
    r = imported_admin_client.post("/sprava/uzivatele", data={"email": "x@gmail.com"}, follow_redirects=True)
    assert "v doméně example.org" in r.text
    u = imported_db.scalar(select(User).where(User.email == "pilot@example.org"))
    imported_admin_client.post(f"/sprava/uzivatele/{u.id}", data={"role": "admin", "active": "1", "name": "Pilot"})
    imported_db.expire_all()
    assert imported_db.get(User, u.id).role == "admin"
    assert _audit(imported_db, "users", "update")[-1].changes["role"] == ["reader", "admin"]
    me = imported_db.scalar(select(User).where(User.email == "admin@example.org"))
    r = imported_admin_client.post(
        f"/sprava/uzivatele/{me.id}", data={"role": "reader", "active": "1"}, follow_redirects=True
    )
    assert "sami sebe" in r.text
    imported_db.expire_all()
    assert imported_db.get(User, me.id).role == "admin"


def test_audit_log_page(imported_admin_client) -> None:
    imported_admin_client.post("/sprava/najemci", data={"name": "Audit test", "active": "1"})
    html = imported_admin_client.get("/sprava/audit?typ=tenants").text
    assert "Audit test" in html and "vytvoření" in html and "admin@example.org" in html
    assert "Audit test" in imported_admin_client.get("/sprava").text


def test_flash_survives_pending_flash(imported_admin_client) -> None:
    """Regression: a second flash queued before the first was displayed must not be lost."""
    imported_admin_client.post("/sprava/najemci", data={"name": "A1", "active": "1"}, follow_redirects=False)
    imported_admin_client.post("/sprava/najemci", data={"name": "A2", "active": "1"}, follow_redirects=False)
    html = imported_admin_client.get("/sprava/najemci").text
    assert "Nájemce A1 vytvořen" in html and "Nájemce A2 vytvořen" in html
