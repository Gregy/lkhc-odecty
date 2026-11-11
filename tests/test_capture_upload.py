"""Batch upload, photo storage/serving, roles/CSRF, and the persistent extraction queue."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.capture.photos import resolve_path
from app.extraction import queue as q
from app.extraction.types import ExtractionResult
from app.models import AuditLog, Photo, Reading
from tests import synthetic as syn
from tests.capture_helpers import answer, fake_extractor, make_jpeg, sha, upload

T0 = datetime(2026, 7, 1, 13, 45, 10)


@pytest.fixture
def fapp(make_app: Callable[..., FastAPI], imported_db_url: str) -> FastAPI:
    return make_app(imported_db_url, extractor="fake")


@pytest.fixture
def admin(fapp: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(fapp, "admin")


def photos(app: FastAPI) -> list[Photo]:
    with app.state.sessionmaker() as db:
        return list(db.scalars(select(Photo).order_by(Photo.id)))


def n_readings(app: FastAPI) -> int:
    with app.state.sessionmaker() as db:
        return db.scalar(select(func.count(Reading.id))) or 0


def test_upload_stores_with_server_name_and_exif_time(fapp: FastAPI, admin: TestClient) -> None:
    res = upload(admin, make_jpeg(taken=T0, offset="+02:00"), name="../../etc/passwd.jpg")
    assert res["ok"] and res["created"] and res["exif"] and res["taken_at"] == T0.isoformat()
    (p,) = photos(fapp)
    assert p.original_filename == "passwd.jpg"  # client path stripped, kept only as metadata
    assert p.path.startswith("photos/2026/07/") and p.path.endswith(".jpg") and "passwd" not in p.path
    assert p.extraction_status == "queued" and p.review_status == "new" and p.source == "upload"
    settings = fapp.state.settings
    assert resolve_path(settings, p.path).is_file() and resolve_path(settings, p.thumb_path).is_file()
    assert resolve_path(settings, "../app.db") is None and resolve_path(settings, "/etc/passwd") is None


def test_upload_without_exif_has_no_time(fapp: FastAPI, admin: TestClient) -> None:
    res = upload(admin, make_jpeg(), name="IMG-20261105-WA0012.jpg", reported_by="Nájemce A (WhatsApp)")
    assert res["ok"] and res["taken_at"] is None  # never a silent "now"
    (p,) = photos(fapp)
    assert p.taken_at is None and p.reported_by == "Nájemce A (WhatsApp)"


def test_duplicate_upload_is_idempotent(fapp: FastAPI, admin: TestClient) -> None:
    data = make_jpeg(taken=T0)
    assert upload(admin, data)["created"]
    again = upload(admin, data)
    assert again["ok"] and not again["created"] and again["duplicate"]
    assert len(photos(fapp)) == 1


def test_invalid_uploads_rejected(fapp: FastAPI, admin: TestClient) -> None:
    res = upload(admin, b"%PDF-1.4 not an image", name="x.jpg")
    assert not res["ok"] and "obrázek" in res["error"]
    r = admin.post(
        "/odecty/davka",
        files=[
            ("files", ("a.jpg", make_jpeg(color=(1, 1, 1)), "image/jpeg")),
            ("files", ("b.svg", b"<svg/>", "image/svg+xml")),
        ],
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 207
    assert [x["ok"] for x in r.json()["results"]] == [True, False]
    assert len(photos(fapp)) == 1


def test_upload_requires_admin_and_csrf(fapp: FastAPI, login_client: Callable[..., TestClient]) -> None:
    reader = login_client(fapp, "reader")
    r = reader.post(
        "/odecty/davka", files={"files": ("a.jpg", make_jpeg(), "image/jpeg")}, headers={"Accept": "application/json"}
    )
    assert r.status_code == 403
    admin = login_client(fapp, "admin", "a2@example.org")
    del admin.headers["X-CSRF-Token"]
    r = admin.post("/odecty/davka", files={"files": ("a.jpg", make_jpeg(), "image/jpeg")})
    assert r.status_code == 403 and "CSRF" in r.text
    anon = TestClient(fapp)
    assert anon.post("/odecty/davka", files={"files": ("a.jpg", make_jpeg(), "image/jpeg")}).status_code == 403
    assert photos(fapp) == []


def test_photo_files_served_only_to_logged_in(fapp: FastAPI, admin: TestClient, login_client) -> None:
    data = make_jpeg(taken=T0)
    pid = upload(admin, data)["id"]
    reader = login_client(fapp, "reader")
    r = reader.get(f"/fotky/{pid}/soubor")
    assert r.status_code == 200 and r.content == data and r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert reader.get(f"/fotky/{pid}/nahled").headers["content-type"] == "image/jpeg"
    anon = TestClient(fapp)
    r = anon.get(f"/fotky/{pid}/soubor", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert anon.get("/static/../data/photos").status_code == 404
    assert reader.get("/fotky/9999/soubor").status_code == 404
    # pages render
    assert reader.get("/fotky").status_code == 200
    assert reader.get(f"/fotky/{pid}").status_code == 200


def test_queue_processes_and_matches(fapp: FastAPI, admin: TestClient) -> None:
    data = make_jpeg(taken=T0)
    fake = fake_extractor(fapp)
    fake.responses[sha(data)] = answer(
        {"serial": syn.PARA_SERIAL, "registers": [{"digits_seen": "01916", "value": 1916}]}
    )
    upload(admin, data)
    assert n_readings(fapp) == 436
    assert q.process_pending(fapp) == 1
    (p,) = photos(fapp)
    assert p.extraction_status == "done" and p.extraction_model == "fake"
    assert p.meter_id is not None and p.match_method == "exact"
    res = ExtractionResult.from_dict(p.extraction_result)
    assert res is not None and res.meters[0].serial == syn.PARA_SERIAL
    # extraction never writes readings and never confirms anything
    assert n_readings(fapp) == 436 and p.review_status == "new"


def test_extraction_failure_leaves_photo_usable(fapp: FastAPI, admin: TestClient) -> None:
    data = make_jpeg(taken=T0, color=(9, 9, 9))
    fake_extractor(fapp).responses[sha(data)] = ExtractionResult(error="HTTP 500: boom", problems=["model_error"])
    upload(admin, data)
    q.process_pending(fapp)
    (p,) = photos(fapp)
    assert p.extraction_status == "failed" and "500" in p.extraction_error
    page = admin.get("/odecty/kontrola")
    assert page.status_code == 200 and f"/fotky/{p.id}/nahled" in page.text  # still reviewable by hand
    # retry from the photo page re-queues it
    r = admin.post(f"/fotky/{p.id}/znovu", follow_redirects=False)
    assert r.status_code == 303 and photos(fapp)[0].extraction_status == "queued"


def test_extractor_crash_is_contained(fapp: FastAPI, admin: TestClient) -> None:
    def boom(data: bytes, hints: object) -> None:
        raise RuntimeError("model exploded")

    fake_extractor(fapp).hook = boom
    upload(admin, make_jpeg(taken=T0, color=(5, 6, 7)))
    q.process_pending(fapp)
    (p,) = photos(fapp)
    assert p.extraction_status == "failed" and "exploded" in p.extraction_error


def test_queue_resumes_after_restart(make_app: Callable[..., FastAPI], imported_db_url: str, login_client) -> None:
    app1 = make_app(imported_db_url, extractor="fake")
    c = login_client(app1, "admin")
    upload(c, make_jpeg(taken=T0, color=(3, 3, 3)))
    upload(c, make_jpeg(taken=T0, color=(4, 4, 4)))
    # simulate a crash in the middle of processing the first photo: it was claimed but never finished
    with app1.state.sessionmaker() as db:
        first = db.scalars(select(Photo).order_by(Photo.id)).first()
        assert q._claim(db, first.id, 0)
    # "restart": a new app instance on the same DB; the lifespan starts the worker thread
    app2 = make_app(imported_db_url, extractor="fake")
    with TestClient(app2):
        w = app2.state.extraction_worker
        assert w is not None and w.thread.is_alive()
        deadline = datetime.now().timestamp() + 10
        while datetime.now().timestamp() < deadline:
            if all(p.extraction_status != "queued" for p in photos(app2)):
                break
            q.notify(app2)
            import time

            time.sleep(0.05)
    assert [p.extraction_status for p in photos(app2)] == ["done", "done"]
    assert app2.state.extraction_worker is None  # stopped with the lifespan


def test_attempts_cap_marks_failed(fapp: FastAPI, admin: TestClient) -> None:
    upload(admin, make_jpeg(taken=T0, color=(8, 8, 8)))
    with fapp.state.sessionmaker() as db:
        p = db.scalars(select(Photo)).one()
        p.extraction_attempts = q.MAX_ATTEMPTS
        db.commit()
    q.process_pending(fapp)
    assert photos(fapp)[0].extraction_status == "failed"


def test_extractor_none_skips_queue(make_app: Callable[..., FastAPI], imported_db_url: str, login_client) -> None:
    app = make_app(imported_db_url)  # EXTRACTOR=none
    c = login_client(app, "admin")
    upload(c, make_jpeg(taken=T0))
    assert photos(app)[0].extraction_status == "skipped"
    assert q.process_pending(app) == 0


def test_upload_is_audited(fapp: FastAPI, admin: TestClient) -> None:
    pid = upload(admin, make_jpeg(taken=T0))["id"]
    with fapp.state.sessionmaker() as db:
        entry = db.scalars(select(AuditLog).where(AuditLog.entity_type == "photos", AuditLog.entity_id == pid)).one()
        assert entry.action == "upload" and entry.user_email == "admin@example.org"
