"""`/najemci` list and detail."""

from __future__ import annotations

import json
import re
from datetime import date

from sqlalchemy.orm import Session

from app import services as s
from app.templating import format_number
from tests import synthetic as syn


def _tenant(net: s.Network, name: str) -> s.TenantInfo:
    return next(t for t in net.tenants if t.name == name)


def test_najemci_list(imported_reader_client, imported_db: Session) -> None:
    r = imported_reader_client.get("/najemci")
    assert r.status_code == 200
    net = s.load_network(imported_db)
    ytd = s.consumption_over_interval(imported_db, date(2026, 1, 1), date(2026, 6, 1), net=net)
    for name in (syn.REMAINDER, syn.A, syn.E):
        t = _tenant(net, name)
        assert format_number(ytd.tenant_total(t.id).value) in r.text, name


def test_najemce_detail_year(imported_reader_client, imported_db: Session) -> None:
    net = s.load_network(imported_db)
    tenant_a = _tenant(net, syn.A)
    r = imported_reader_client.get(f"/najemci/{tenant_a.id}?rok=2025")
    assert r.status_code == 200
    html = r.text
    tab = s.tenant_consumption_table(imported_db, start=date(2025, 1, 1), end=date(2026, 1, 1), net=net)
    assert format_number(tab.period_sum(tenant_a.id).value) in html
    for name in (syn.PARA_POINT, syn.ACCU_POINT, syn.ROLLOVER_POINT):
        assert name in html
    m = re.search(r'id="chart-tenant-data">(.*?)</script>', html, re.S)
    chart = json.loads(m.group(1))
    assert chart["tenant_id"] == tenant_a.id and len(chart["cur"]) == 12
    assert chart["cur"][0]["v"] == round(float(tab.total(tenant_a.id, 0).value), 0)
    assert any(chart["prev"])  # 2024 comparison


def test_najemce_breakdown_sums_to_tenant(imported_reader_client, imported_db: Session) -> None:
    """Per-point breakdown of an allocated tenant adds up to the tenant total (all months, both registers)."""
    net = s.load_network(imported_db)
    tenant_b = _tenant(net, syn.B)
    tab = s.tenant_consumption_table(imported_db, start=date(2025, 1, 1), end=date(2026, 1, 1), net=net)
    for i in range(12):
        parts = sum(
            (tab.points.total(r.meter_point_id, i).value * r.factor for r in net.rules if r.tenant_id == tenant_b.id),
            start=0,
        )
        assert parts == tab.total(tenant_b.id, i).value
    assert imported_reader_client.get(f"/najemci/{tenant_b.id}?rok=2025").status_code == 200


def test_remainder_detail(imported_reader_client, imported_db: Session) -> None:
    net = s.load_network(imported_db)
    rem = _tenant(net, syn.REMAINDER)
    html = imported_reader_client.get(f"/najemci/{rem.id}?rok=2025").text
    assert "Skladba zbytku" in html and "Hlavní elektroměr" in html and "− " in html


def test_najemce_future_year_and_404(imported_reader_client, imported_db: Session) -> None:
    net = s.load_network(imported_db)
    t = _tenant(net, syn.E)
    html = imported_reader_client.get(f"/najemci/{t.id}?rok=2030").text
    assert "zatím nejsou data" in html
    assert imported_reader_client.get("/najemci/999").status_code == 404
