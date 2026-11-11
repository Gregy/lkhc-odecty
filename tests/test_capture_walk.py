"""Walk PWA endpoints: shell/SW/manifest, point data, idempotent photo + entry sync, share target."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.extraction import queue as q
from app.models import AuditLog, Meter, Photo, PointSkip, Reading
from tests import synthetic as syn
from tests.capture_helpers import answer, fake_extractor, make_jpeg, sha

BASE = 436
SER = syn.PARA_SERIAL
LAST = int(syn.value("D", syn.TYPO))  # last imported value (3.6.2026)
V1 = str(LAST + 57)  # a plausible value today
NEW = LAST + 23  # a plausible value on 1.7.2026
LOW = LAST - 143  # below the last reading


@pytest.fixture
def fapp(make_app: Callable[..., FastAPI], imported_db_url: str) -> FastAPI:
    return make_app(imported_db_url, extractor="fake")


@pytest.fixture
def admin(fapp: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(fapp, "admin")


def meter(app: FastAPI, serial: str) -> Meter:
    with app.state.sessionmaker() as db:
        return db.scalar(select(Meter).where(Meter.serial == serial))


def count(app: FastAPI, model=Reading) -> int:
    with app.state.sessionmaker() as db:
        return db.scalar(select(func.count(model.id))) or 0


def now_iso(days: int = 0) -> str:
    return (datetime.now().replace(microsecond=0) - timedelta(days=days)).isoformat()


def entry(
    m: Meter, value: str, *, uid: str | None = None, acks: list[str] | None = None, days_ago: int = 0, **kw: Any
) -> dict[str, Any]:
    return {
        "uuid": uid or str(uuid.uuid4()),
        "point_id": m.meter_point_id,
        "taken_at": now_iso(days_ago),
        "readings": [{"meter_id": m.id, "register": "T1", "value": value, "acks": acks or []}],
        "photos": [],
        **kw,
    }


def test_shell_and_pwa_plumbing(fapp: FastAPI, admin: TestClient) -> None:
    r = admin.get("/obchuzka", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/obchuzka/"
    r = admin.get("/obchuzka/")
    assert r.status_code == 200 and 'rel="manifest"' in r.text and "obchuzka/app.js" in r.text
    assert "no-cache" in r.headers["cache-control"]
    anon = TestClient(fapp)
    assert anon.get("/obchuzka/", follow_redirects=False).status_code == 303  # shell needs login
    sw = anon.get("/obchuzka/sw.js")  # public, no secrets
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert sw.headers["service-worker-allowed"] == "/obchuzka/"
    man = anon.get("/obchuzka/manifest.webmanifest")
    assert man.status_code == 200 and man.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(man.text)
    assert data["scope"] == "/obchuzka/" and data["start_url"] == "/obchuzka/" and data["display"] == "standalone"
    assert data["share_target"]["action"] == "/obchuzka/sdilet" and data["share_target"]["method"] == "POST"
    assert {"192x192", "512x512"} <= {i["sizes"] for i in data["icons"]}
    for icon in data["icons"]:
        assert anon.get(icon["src"]).status_code == 200
    assert anon.get("/obchuzka/api/body", follow_redirects=False).status_code in (303, 401)


def test_point_data(fapp: FastAPI, admin: TestClient) -> None:
    body = admin.get("/obchuzka/api/body?den=2026-07-01").json()
    assert body["is_admin"] and body["csrf_token"] and body["day"] == "2026-07-01"
    names = [p["name"] for p in body["points"]]
    assert names[0] == syn.MAIN_POINT and syn.OFFICE_POINT in names
    office = next(p for p in body["points"] if p["name"] == syn.OFFICE_POINT)
    assert [m["serial"] for m in office["meters"]] == [syn.NEW_OFFICE_SERIAL]  # removed meter not offered
    para = next(p for p in body["points"] if p["name"] == syn.PARA_POINT)["meters"][0]["regs"]["T1"]
    assert para["last"] == str(LAST) and para["last_date"] == "2026-06-03" and para["low"] == str(LAST)
    assert para["high"]
    assert para["today"] is None
    body = admin.get("/obchuzka/api/body?den=2026-06-03").json()
    para = next(p for p in body["points"] if p["name"] == syn.PARA_POINT)["meters"][0]["regs"]["T1"]
    assert para["today"] == {"value": str(LAST), "status": "confirmed", "source": "import"}


def test_entry_is_idempotent_and_editable(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    e = entry(m, V1, acks=["high"])
    r1 = admin.post("/obchuzka/api/zaznam", json=e)
    assert r1.status_code == 200 and r1.json()["status"] == "ok", r1.text
    assert r1.json()["results"][0]["status"] == "created"
    r2 = admin.post("/obchuzka/api/zaznam", json=e)  # retried sync
    assert r2.json()["results"][0]["status"] == "unchanged"
    assert count(fapp) == BASE + 1
    with fapp.state.sessionmaker() as db:
        rd = db.scalars(select(Reading).order_by(Reading.id.desc())).first()
        assert rd.value == D(V1) and rd.status == "confirmed" and rd.source == "manual"
        assert rd.taken_at == datetime.combine(date.today(), datetime.min.time())
        assert rd.client_ref == f"{e['uuid']}:{m.id}:T1"
    # the same entry edited on the phone and synced again -> update, not a duplicate
    e["readings"][0]["value"] = str(LAST + 58)
    r3 = admin.post("/obchuzka/api/zaznam", json=e)
    assert r3.json()["results"][0]["status"] == "updated" and count(fapp) == BASE + 1


def test_unacknowledged_warning_goes_to_review(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    r = admin.post("/obchuzka/api/zaznam", json=entry(m, str(LOW)))  # decrease, not acknowledged on the phone
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert r.json()["results"][0]["issues"][0]["code"] == "decrease"
    with fapp.state.sessionmaker() as db:
        rd = db.scalars(select(Reading).order_by(Reading.id.desc())).first()
        assert rd.status == "pending"
    page = admin.get("/odecty/kontrola")
    assert "Nepotvrzené odečty z obchůzky" in page.text
    # confirming it needs the acknowledgement too
    admin.post(f"/odecty/kontrola/cekajici/{rd.id}", data={"action": "confirm"})
    with fapp.state.sessionmaker() as db:
        assert db.get(Reading, rd.id).status == "pending"
    admin.post(f"/odecty/kontrola/cekajici/{rd.id}", data={"action": "confirm", "ack": "decrease"})
    with fapp.state.sessionmaker() as db:
        assert db.get(Reading, rd.id).status == "confirmed"


def test_acknowledged_warning_is_confirmed_and_audited(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    r = admin.post("/obchuzka/api/zaznam", json=entry(m, str(LOW), acks=["decrease"]))
    assert r.json()["status"] == "ok"
    with fapp.state.sessionmaker() as db:
        rd = db.scalars(select(Reading).order_by(Reading.id.desc())).first()
        assert rd.status == "confirmed"
        note = (
            db.scalars(select(AuditLog).where(AuditLog.entity_type == "readings", AuditLog.entity_id == rd.id))
            .one()
            .note
        )
    assert "[decrease]" in note


def test_conflict_with_other_reading_writes_nothing(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    admin.post("/obchuzka/api/zaznam", json=entry(m, V1, acks=["high"]))
    other = entry(m, str(LAST + 67), acks=["high"])  # a different submission for the same day (other phone / tab)
    r = admin.post("/obchuzka/api/zaznam", json=other)
    assert r.status_code == 409 and r.json()["status"] == "conflict"
    assert r.json()["results"][0]["existing"] == V1
    assert count(fapp) == BASE + 1
    other["overwrite"] = True
    r = admin.post("/obchuzka/api/zaznam", json=other)
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "updated"


def test_entry_validation(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    other_point = meter(fapp, syn.CORRIDOR_SERIAL)
    bad = entry(m, V1)
    bad["readings"][0]["meter_id"] = other_point.id  # meter of another point
    assert admin.post("/obchuzka/api/zaznam", json=bad).status_code == 400
    assert admin.post("/obchuzka/api/zaznam", json=entry(m, "abc")).status_code == 400
    assert admin.post("/obchuzka/api/zaznam", json={**entry(m, V1), "uuid": "nope"}).status_code == 400
    future = entry(m, V1)
    future["taken_at"] = (datetime.now() + timedelta(days=2)).isoformat()
    assert admin.post("/obchuzka/api/zaznam", json=future).status_code == 400
    assert count(fapp) == BASE


def test_skip_is_idempotent_and_replaced_by_entry(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    sk = {
        "uuid": str(uuid.uuid4()),
        "point_id": m.meter_point_id,
        "taken_at": now_iso(),
        "skip": True,
        "skip_note": "zamčeno",
    }
    assert admin.post("/obchuzka/api/zaznam", json=sk).json()["skipped"]
    assert admin.post("/obchuzka/api/zaznam", json=sk).json()["skipped"]
    assert count(fapp, PointSkip) == 1
    with fapp.state.sessionmaker() as db:
        assert db.scalar(select(PointSkip)).day == date.today()
    body = admin.get("/obchuzka/api/body").json()
    assert next(p for p in body["points"] if p["id"] == m.meter_point_id)["skipped"] == {"note": "zamčeno"}
    admin.post("/obchuzka/api/zaznam", json=entry(m, V1, acks=["high"]))
    assert count(fapp, PointSkip) == 0


def test_walk_photo_upload_idempotent_and_prefill(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    data = make_jpeg(color=(200, 1, 1))  # client re-encoded -> no EXIF
    fake_extractor(fapp).responses[sha(data)] = answer({"serial": None, "registers": [{"digits_seen": f"0{NEW}"}]})
    uid = str(uuid.uuid4())
    taken = now_iso()
    form = {"uuid": uid, "point_id": str(m.meter_point_id), "taken_at": taken, "source": "walk"}
    r1 = admin.post("/obchuzka/api/foto", data=form, files={"file": ("foto.jpg", data, "image/jpeg")})
    assert r1.status_code == 200 and r1.json()["created"]
    r2 = admin.post("/obchuzka/api/foto", data=form, files={"file": ("foto.jpg", data, "image/jpeg")})
    assert r2.status_code == 200 and not r2.json()["created"]
    assert count(fapp, Photo) == 1
    with fapp.state.sessionmaker() as db:
        p = db.scalars(select(Photo)).one()
        assert p.uuid == uid and p.source == "walk" and p.taken_at == datetime.fromisoformat(taken)
        assert p.meter_id == m.id and p.match_method == "walk"
    st = admin.get(f"/obchuzka/api/foto/{uid}").json()
    assert st["status"] == "queued" and st["values"] == {}
    q.process_pending(fapp)
    st = admin.get(f"/obchuzka/api/foto/{uid}").json()
    assert st["status"] == "done" and st["values"][str(m.id)]["T1"]["value"] == str(NEW)
    assert count(fapp) == BASE  # prefill only, nothing written
    # the entry referencing the photo links it and takes it out of the review queue
    e = entry(m, str(NEW), photos=[uid])
    assert admin.post("/obchuzka/api/zaznam", json=e).json()["status"] == "ok"
    with fapp.state.sessionmaker() as db:
        rd = db.scalars(select(Reading).order_by(Reading.id.desc())).first()
        assert rd.photo_id == p.id and rd.source == "photo"
        assert db.get(Photo, p.id).review_status == "confirmed"


def test_walk_photo_rejections(fapp: FastAPI, admin: TestClient) -> None:
    r = admin.post(
        "/obchuzka/api/foto", data={"uuid": str(uuid.uuid4())}, files={"file": ("x.jpg", b"nope", "image/jpeg")}
    )
    assert r.status_code == 422 and r.json()["permanent"]
    r = admin.post("/obchuzka/api/foto", data={"uuid": "../../x"}, files={"file": ("x.jpg", make_jpeg(), "image/jpeg")})
    assert r.status_code == 400
    assert count(fapp, Photo) == 0


def test_shared_photo_goes_to_review_with_reporter(fapp: FastAPI, admin: TestClient) -> None:
    data = make_jpeg(color=(1, 200, 1))
    uid = str(uuid.uuid4())
    r = admin.post(
        "/obchuzka/api/foto",
        data={
            "uuid": uid,
            "source": "share",
            "filename": "IMG-20261105-WA0012.jpg",
            "reported_by": "Nájemce A (WhatsApp)",
            "taken_at": now_iso(),
        },
        files={"file": ("IMG-20261105-WA0012.jpg", data, "image/jpeg")},
    )
    assert r.status_code == 200
    with fapp.state.sessionmaker() as db:
        p = db.scalars(select(Photo)).one()
        # a shared photo's time is unknown: client time is NOT used (only for fresh walk photos)
        assert p.source == "share" and p.taken_at is None and p.reported_by == "Nájemce A (WhatsApp)"
        assert p.review_status == "new" and p.original_filename == "IMG-20261105-WA0012.jpg"


def test_reader_cannot_write(fapp: FastAPI, login_client: Callable[..., TestClient]) -> None:
    reader = login_client(fapp, "reader")
    assert reader.get("/obchuzka/api/body").json()["is_admin"] is False
    m = meter(fapp, SER)
    assert reader.post("/obchuzka/api/zaznam", json=entry(m, V1)).status_code == 403
    r = reader.post(
        "/obchuzka/api/foto", data={"uuid": str(uuid.uuid4())}, files={"file": ("x.jpg", make_jpeg(), "image/jpeg")}
    )
    assert r.status_code == 403
    assert count(fapp) == BASE and count(fapp, Photo) == 0


def test_csrf_required_for_sync(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    token = admin.headers.pop("X-CSRF-Token")
    assert admin.post("/obchuzka/api/zaznam", json=entry(m, V1)).status_code == 403
    admin.headers["X-CSRF-Token"] = "wrong"
    assert admin.post("/obchuzka/api/zaznam", json=entry(m, V1)).status_code == 403
    admin.headers["X-CSRF-Token"] = token
    assert admin.get("/obchuzka/api/stav").json()["csrf_token"] == token


def test_share_fallback_is_csrf_exempt_but_changes_nothing(fapp: FastAPI, admin: TestClient) -> None:
    admin.headers.pop("X-CSRF-Token")
    for client in (admin, TestClient(fapp)):
        r = client.post(
            "/obchuzka/sdilet",
            files={"photos": ("IMG-20261105-WA0012.jpg", make_jpeg(), "image/jpeg")},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/obchuzka/?sdileni=nezpracovano"
    assert count(fapp, Photo) == 0
    # the exemption is exactly this one path
    assert admin.post("/obchuzka/sdilet/x").status_code in (403, 404, 405)
    assert admin.post("/obchuzka/api/zaznam", json={}).status_code == 403


def test_server_plausibility_preview(fapp: FastAPI, admin: TestClient) -> None:
    m = meter(fapp, SER)
    r = admin.get(f"/obchuzka/api/kontrola?meter_id={m.id}&register=T1&value={LOW}&den=2026-07-01").json()
    assert [i["code"] for i in r["issues"]] == ["decrease"]
    r = admin.get(f"/obchuzka/api/kontrola?meter_id={m.id}&register=T1&value={NEW}&den=2026-07-01").json()
    assert r["issues"] == []
