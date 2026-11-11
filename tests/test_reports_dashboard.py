"""Dashboard (`/`) on the imported data set."""

from __future__ import annotations

import json
import re
from datetime import date

from sqlalchemy.orm import Session

from app import services as s
from app.models import Photo
from app.services.reports import data_coverage
from app.templating import format_date, format_number
from tests import synthetic as syn


def _chart(html: str, el_id: str) -> dict:
    m = re.search(rf'<script type="application/json" id="{el_id}">(.*?)</script>', html, re.S)
    assert m, el_id
    return json.loads(m.group(1))


def test_dashboard_requires_login(anon_client) -> None:
    r = anon_client.get("/", follow_redirects=False, headers={"accept": "text/html"})
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_dashboard_empty_db(reader_client) -> None:
    r = reader_client.get("/")
    assert r.status_code == 200
    assert "Zatím nejsou k dispozici žádné odečty" in r.text


def test_dashboard_numbers_match_services(imported_reader_client, imported_db: Session) -> None:
    r = imported_reader_client.get("/")
    assert r.status_code == 200
    html = r.text
    net = s.load_network(imported_db)
    cov = data_coverage(net)
    # main meter last read 3.6.2026 -> last covered month = May 2026
    assert cov.last_month == (date(2026, 5, 1), date(2026, 6, 1))
    assert "Poslední období s daty" in html and format_date(date(2026, 5, 1)) in html
    ytd = s.consumption_over_interval(imported_db, date(2026, 1, 1), date(2026, 6, 1), net=net)
    tenant_b = next(t for t in net.tenants if t.name == syn.B)
    v = ytd.tenant_total(tenant_b.id)
    # the hangar meter of tenant B has no reading since 2.8.2025 -> B's values are provisional and marked so
    assert v.quality.value == "provisional"
    assert f"≈?{format_number(v.value)}" in html
    # chart data per tenant equals the aligned monthly table
    chart = _chart(html, "chart-monthly-data")
    assert chart["year"] == 2026 and len(chart["labels"]) == 12
    tab = s.tenant_consumption_table(imported_db, start=date(2026, 1, 1), end=date(2026, 6, 1), net=net)
    series = {x["id"]: x["values"] for x in chart["series"]}
    for t in net.tenants:
        for i in range(5):
            assert series[t.id][i]["v"] == round(float(tab.total(t.id, i).value), 0)
        assert series[t.id][5] is None  # June not covered yet
    assert any(p for p in chart["prev_total"])  # last-year comparison present
    # alerts: stale meters listed, balance section present
    assert "Chybějící odečty" in html and syn.HANGAR_POINT in html
    assert "Bilance hlavní a kontrolní" in html


def test_dashboard_pending_photos_count(imported_reader_client, imported_db: Session) -> None:
    for i in range(3):
        imported_db.add(Photo(uuid=f"u-{i}", path=f"photos/x{i}.jpg", review_status="new"))
    imported_db.add(Photo(uuid="u-done", path="photos/y.jpg", review_status="confirmed"))
    imported_db.commit()
    html = imported_reader_client.get("/").text
    assert "Fotky čekající na kontrolu: 3." in html
    assert 'href="/odecty/kontrola"' in html
