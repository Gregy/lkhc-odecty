"""`/mista` list and detail."""

from __future__ import annotations

import json
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as s
from app.models import Meter, MeterPoint, Photo, Reading
from app.templating import format_number
from tests import synthetic as syn


def _point(db: Session, name: str) -> MeterPoint:
    return db.scalar(select(MeterPoint).where(MeterPoint.name == name))


def test_mista_list(imported_reader_client) -> None:
    r = imported_reader_client.get("/mista")
    assert r.status_code == 200
    html = r.text
    for name in (syn.MAIN_POINT, syn.HANGAR_POINT, syn.CONTROL_POINT, syn.CORRIDOR2_POINT):
        assert name in html
    assert "nealokováno (→" in html  # the unallocated corridor point
    assert "Spravovat" not in html  # reader


def test_misto_detail_replacement_and_history(imported_reader_client, imported_db: Session) -> None:
    p = _point(imported_db, syn.OFFICE_POINT)
    r = imported_reader_client.get(f"/mista/{p.id}")
    assert r.status_code == 200
    html = r.text
    assert syn.OLD_OFFICE_SERIAL in html and syn.NEW_OFFICE_SERIAL in html
    assert "Výměna" in html and f"{syn.OLD_OFFICE_SERIAL} → <strong>{syn.NEW_OFFICE_SERIAL}</strong>" in html
    assert "Historie odečtů" in html and "chart-point-monthly-data" in html and "chart-point-states-data" in html
    n_readings = imported_db.scalar(select(Reading.id).where(Reading.meter_id.in_([m.id for m in p.meters])).limit(1))
    assert n_readings is not None


def test_misto_monthly_chart_matches_service(imported_reader_client, imported_db: Session) -> None:
    p = _point(imported_db, syn.HANGAR_POINT)
    html = imported_reader_client.get(f"/mista/{p.id}").text
    m = re.search(r'id="chart-point-monthly-data">(.*?)</script>', html, re.S)
    data = json.loads(m.group(1))
    net = s.load_network(imported_db)
    pt = s.point_consumption_table(imported_db, start=date(2023, 12, 1), end=date(2026, 6, 1), net=net)
    t1 = data["series"][0]
    assert t1["label"] == "T1" and len(t1["values"]) == len(pt.periods)
    for i, v in enumerate(t1["values"]):
        assert v["v"] == round(float(pt.value(p.id, "T1", i).value), 0)
    # after the last reading (2.8.2025) values are provisional and say so
    last = t1["values"][-1]
    assert last["q"] == "provisional" and "2. 8. 2025" in last["t"].replace("\xa0", " ")


def test_misto_rollover_and_photos(imported_reader_client, imported_db: Session) -> None:
    p = _point(imported_db, syn.ROLLOVER_POINT)
    meter = imported_db.scalar(select(Meter).where(Meter.meter_point_id == p.id))
    imported_db.add(Photo(uuid="ph-1", path="photos/a.jpg", meter_id=meter.id, review_status="new"))
    imported_db.commit()
    html = imported_reader_client.get(f"/mista/{p.id}").text
    assert "↻" in html  # the 5-digit counter rolled over in February 2024
    assert "/fotky/" in html and "ke kontrole" in html


def test_misto_404(imported_reader_client) -> None:
    assert imported_reader_client.get("/mista/9999").status_code == 404


def test_mista_last_month_values(imported_reader_client, imported_db: Session) -> None:
    net = s.load_network(imported_db)
    pt = s.point_consumption_table(imported_db, start=date(2026, 5, 1), end=date(2026, 6, 1), net=net)
    p = _point(imported_db, syn.E_POINT)
    html = imported_reader_client.get("/mista").text
    assert format_number(pt.total(p.id, 0).value) in html
