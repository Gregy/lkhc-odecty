"""Readings grid, inline cell editing with audit, manual (late) readings, history, roles."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal as D

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditLog, Meter, Reading
from tests import synthetic as syn

PARA = syn.PARA_SERIAL  # read on every recent reading day (T1 only)
LAST = int(syn.value("D", syn.TYPO))  # its last value (3.6.2026 in the sheet)
PREV = int(syn.value("D", "1.6.2026"))


@pytest.fixture
def admin(imported_app: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(imported_app, "admin")


def mid(app: FastAPI, serial: str) -> int:
    with app.state.sessionmaker() as db:
        return db.scalar(select(Meter.id).where(Meter.serial == serial))


def reading(app: FastAPI, meter_id: int, day: str) -> Reading | None:
    with app.state.sessionmaker() as db:
        return db.scalar(
            select(Reading).where(Reading.meter_id == meter_id, Reading.taken_at == datetime.fromisoformat(day))
        )


def audit(app: FastAPI, rid: int) -> list[AuditLog]:
    with app.state.sessionmaker() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.entity_type == "readings", AuditLog.entity_id == rid)))


def test_grid_renders_like_the_sheet(imported_app: FastAPI, admin: TestClient) -> None:
    r = admin.get("/odecty?rok=2026")
    assert r.status_code == 200
    t = r.text
    assert syn.MAIN_POINT in t and syn.MAIN_SERIAL in t and 'id="d-2026-06-03"' in t
    m = mid(imported_app, PARA)
    assert f'id="c-{m}-T1-2026-06-03"' in t and f"{LAST // 1000}\u00a0{LAST % 1000:03d}" in t
    assert 'hx-get="/odecty/bunka' in t  # admin can edit
    assert admin.get("/odecty?rok=vse").status_code == 200
    assert admin.get("/odecty").status_code == 200
    removed = f'title="{syn.KIOSK_SERIAL}"'
    assert removed not in admin.get("/odecty").text  # removed meter without recent data hidden ...
    assert removed in admin.get("/odecty?rok=2025").text  # ... but shown where it has data
    assert removed in admin.get("/odecty?odebrane=1").text


def test_reader_view_only(imported_app: FastAPI, login_client: Callable[..., TestClient]) -> None:
    reader = login_client(imported_app, "reader")
    r = reader.get("/odecty?rok=2026")
    assert r.status_code == 200 and 'hx-get="/odecty/bunka' not in r.text
    m = mid(imported_app, PARA)
    assert reader.get(f"/odecty/bunka?meter_id={m}&register=T1&den=2026-06-03").status_code == 403
    assert (
        reader.post(
            "/odecty/bunka", data={"meter_id": m, "register": "T1", "den": "2026-06-03", "value": "1"}
        ).status_code
        == 403
    )
    assert reader.get("/odecty/novy").status_code == 403
    assert (
        reader.post("/odecty/novy", data={"meter_id": m, "den": "2026-06-10", "T1": str(LAST + 7)}).status_code == 403
    )
    assert reader.get("/odecty/historie").status_code == 200


def test_cell_edit_with_ack_and_audit(imported_app: FastAPI, admin: TestClient) -> None:
    m = mid(imported_app, PARA)
    before = reading(imported_app, m, "2026-06-03")
    ed = admin.get(f"/odecty/bunka?meter_id={m}&register=T1&den=2026-06-03")
    assert ed.status_code == 200 and f'value="{LAST}"' in ed.text
    # a value below the previous reading (1.6.2026) -> needs acknowledgement
    low = str(PREV - 20)
    r = admin.post(
        "/odecty/bunka", data={"meter_id": m, "register": "T1", "den": "2026-06-03", "value": low, "action": "save"}
    )
    assert "decrease" in r.text and reading(imported_app, m, "2026-06-03").value == D(LAST)
    r = admin.post(
        "/odecty/bunka",
        data={
            "meter_id": m,
            "register": "T1",
            "den": "2026-06-03",
            "value": f"{LAST + 1},5",
            "action": "save",
            "note": "oprava",
            "ack": "high",
        },  # the imported 1.6.->3.6. step is itself unusually high
    )
    assert r.status_code == 200 and "Uloženo" in r.text and 'hx-swap-oob="true"' in r.text
    after = reading(imported_app, m, "2026-06-03")
    assert after.id == before.id and after.value == D(f"{LAST + 1}.5") and after.note == "oprava"
    upd = [a for a in audit(imported_app, after.id) if a.action == "update"]
    assert upd and upd[-1].changes["value"] == [f"{LAST}.000", f"{LAST + 1}.500"]
    assert upd[-1].user_email == "admin@example.org"
    # delete
    r = admin.post("/odecty/bunka", data={"meter_id": m, "register": "T1", "den": "2026-06-03", "action": "delete"})
    assert r.status_code == 200 and reading(imported_app, m, "2026-06-03") is None
    assert [a.action for a in audit(imported_app, after.id)][-1] == "delete"


def test_cell_add_on_new_day_and_bad_input(imported_app: FastAPI, admin: TestClient) -> None:
    m = mid(imported_app, PARA)
    r = admin.post("/odecty/bunka", data={"meter_id": m, "register": "T1", "den": "2026-06-20", "value": "12abc"})
    assert "není číslo" in r.text and reading(imported_app, m, "2026-06-20") is None
    new = str(LAST + 13)
    r = admin.post("/odecty/bunka", data={"meter_id": m, "register": "T1", "den": "2026-06-20", "value": new})
    assert "Uloženo" in r.text
    rd = reading(imported_app, m, "2026-06-20")
    assert rd.value == D(new) and rd.source == "manual" and rd.status == "confirmed"
    assert [a.action for a in audit(imported_app, rd.id)] == ["create"]


def test_manual_late_reading_from_tenant(imported_app: FastAPI, admin: TestClient) -> None:
    m = mid(imported_app, syn.ACCU_SERIAL)  # T1+T2
    assert admin.get("/odecty/novy").status_code == 200
    r = admin.post("/odecty/novy", data={"meter_id": m, "den": "", "T1": "1"})
    assert r.status_code == 422 and "datum" in r.text
    r = admin.post("/odecty/novy", data={"meter_id": m, "den": "2026-06-15", "T1": "", "T2": ""})
    assert r.status_code == 422
    with imported_app.state.sessionmaker() as db:
        last = {
            reg: db.scalar(
                select(Reading.value)
                .where(Reading.meter_id == m, Reading.register == reg)
                .order_by(Reading.taken_at.desc())
            )
            for reg in ("T1", "T2")
        }
    data = {
        "meter_id": m,
        "den": "2026-06-15",
        "T1": str(last["T1"] + 5),
        "T2": str(last["T2"] + 3),
        "reported_by": "Nájemce A (SMS)",
        "note": "poslal pozdě",
    }
    r = admin.post("/odecty/novy", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    t1 = reading(imported_app, m, "2026-06-15")
    assert t1.reported_by == "Nájemce A (SMS)" and t1.source == "manual"
    # an implausible value asks for acknowledgement first
    r = admin.post("/odecty/novy", data={"meter_id": m, "den": "2026-06-16", "T1": "1"})
    assert r.status_code == 422 and 'name="ack" value="decrease"' in r.text
    r = admin.post(
        "/odecty/novy", data={"meter_id": m, "den": "2026-06-16", "T1": "1", "ack": "decrease"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_history_page(imported_app: FastAPI, admin: TestClient) -> None:
    m = mid(imported_app, PARA)
    admin.post("/odecty/bunka", data={"meter_id": m, "register": "T1", "den": "2026-06-20", "value": str(LAST + 13)})
    r = admin.get("/odecty/historie")
    assert r.status_code == 200 and PARA in r.text and "přidání" in r.text
