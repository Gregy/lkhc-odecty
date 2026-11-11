"""`/spotreba`: aligned / raw views, filters and XLSX/CSV exports."""

from __future__ import annotations

import csv
import io
from datetime import date

import openpyxl
from sqlalchemy.orm import Session

from app import services as s
from app.templating import format_number
from tests import synthetic as syn


def _tenant(net: s.Network, name: str) -> s.TenantInfo:
    return next(t for t in net.tenants if t.name == name)


def test_spotreba_aligned_default(imported_reader_client, imported_db: Session) -> None:
    r = imported_reader_client.get("/spotreba?rok=2025")
    assert r.status_code == 200
    html = r.text
    assert "Zarovnaná období" in html and "leden 2025" in html and "prosinec 2025" in html
    net = s.load_network(imported_db)
    tab = s.tenant_consumption_table(imported_db, start=date(2025, 1, 1), end=date(2026, 1, 1), net=net)
    tenant_a = _tenant(net, syn.A)
    v = tab.period_sum(tenant_a.id, "T1")
    assert format_number(v.value) in html
    # tenant B is provisional from August 2025 on (hangar meter unread) -> marked with "≈"
    tenant_b = _tenant(net, syn.B)
    assert tab.value(tenant_b.id, "T1", 11).quality.value == "provisional"
    assert f"≈{format_number(tab.value(tenant_b.id, 'T1', 11).value)}" in html and "q-provisional" in html
    assert "≈?" not in html
    # interpolated (estimated) values carry no mark, no italics class and no legend entry
    assert v.quality.value == "estimated" and f"≈{format_number(v.value)}" not in html
    assert "q-estimated" not in html and "odhad – interpolace" not in html
    assert "q-legend" in html and "extrapolovaný odhad za posledním odečtem" in html


def test_spotreba_year_capped_at_coverage(imported_reader_client) -> None:
    html = imported_reader_client.get("/spotreba?rok=2026").text
    assert "květen 2026" in html and "červen 2026" not in html
    assert "nejsou zobrazeny" in html
    html = imported_reader_client.get("/spotreba?rok=2026&predbezne=1").text
    assert "červen 2026" in html


def test_spotreba_raw_view_matches_service(imported_reader_client, imported_db: Session) -> None:
    r = imported_reader_client.get("/spotreba?pohled=dny&rok=2024&sazba=1")
    assert r.status_code == 200
    html = r.text
    assert "Surový pohled po dnech odečtů" in html
    raw = s.raw_allocation_table(imported_db, date(2024, 1, 1), date(2024, 12, 31))
    assert raw.periods[0].label in html
    rem = next(t for t in raw.tenants if t.is_remainder)
    neg = [raw.value(i, rem.id, "T1") for i in range(len(raw.periods)) if (raw.value(i, rem.id, "T1") or 0) < 0]
    # negative remainder from the spreadsheet logic is visible and highlighted
    assert neg and f'negative">{format_number(neg[0])}' in html


def test_spotreba_main_meter_granularity(imported_reader_client) -> None:
    html = imported_reader_client.get("/spotreba?rok=2025&obdobi=hlavni").text
    assert "2.1.2025–" in html  # aligned spans between main-meter reading days (nearest to 1.1.)
    # old links (?obdobi=obchuzky / ?pohled=obchuzky) keep working
    assert "2.1.2025–" in imported_reader_client.get("/spotreba?rok=2025&obdobi=obchuzky").text
    assert "Surový pohled po dnech" in imported_reader_client.get("/spotreba?rok=2025&pohled=obchuzky").text


def test_spotreba_xlsx_export(imported_reader_client, imported_db: Session) -> None:
    r = imported_reader_client.get("/spotreba/export.xlsx?rok=2025&sazba=1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "spotreba-2025" in r.headers["content-disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Nájemci", "Měřicí místa", "Data"]
    ws = wb["Nájemci"]
    values = [[c.value for c in row] for row in ws.iter_rows()]
    flat = [v for row in values for v in row if v is not None]
    assert "Kvalita hodnot:" in flat and "předběžně" in flat and "odhad" not in flat  # legend: provisional only
    header_idx = next(i for i, row in enumerate(values) if row[0] == "Období")
    header = values[header_idx]
    assert f"{syn.A} T1" in header and "Kvalita (nejhorší v řádku)" in header
    net = s.load_network(imported_db)
    tab = s.tenant_consumption_table(imported_db, start=date(2025, 1, 1), end=date(2026, 1, 1), net=net)
    tenant_a = _tenant(net, syn.A)
    jan = values[header_idx + 1]
    assert jan[0] == "leden 2025"
    assert (
        jan[header.index(f"{syn.A} T1")] == round(float(tab.value(tenant_a.id, "T1", 0).value), 3)
        or abs(jan[header.index(f"{syn.A} T1")] - float(tab.value(tenant_a.id, "T1", 0).value)) < 1e-6
    )
    dec = values[header_idx + 12]
    assert dec[0] == "prosinec 2025" and dec[header.index("Kvalita (nejhorší v řádku)")] == "předběžně"
    # provisional cells carry a fill and a comment with provenance; interpolated ones look like exact ones
    b_col = header.index(f"{syn.B} T1") + 1
    cell = ws.cell(row=header_idx + 13, column=b_col)
    assert cell.comment is not None and "předběžné" in cell.comment.text and cell.fill.fgColor.rgb.endswith("FBE4E2")
    assert tab.value(tenant_a.id, "T1", 0).quality.value == "estimated"
    a_cell = ws.cell(row=header_idx + 2, column=header.index(f"{syn.A} T1") + 1)
    assert a_cell.comment is None and a_cell.fill.fill_type is None and not a_cell.font.italic
    assert jan[header.index("Kvalita (nejhorší v řádku)")] in (None, "")
    data = wb["Data"]
    rows = list(data.iter_rows(values_only=True))
    assert rows[0][:8] == ("Typ", "Nájemce / místo", "Tarif", "Období", "Od", "Do", "kWh", "Kvalita")
    qualities = {r[7] for r in rows[1:]}
    assert qualities - {None, ""} == {"předběžně"}  # only provisional values are labelled


def test_spotreba_csv_export(imported_reader_client) -> None:
    r = imported_reader_client.get("/spotreba/export.csv?rok=2025&sazba=0")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    assert rows[0][7] == "Kvalita"
    tenant_b = [x for x in rows[1:] if x[0] == "nájemce" and x[1] == syn.B and x[3] == "prosinec 2025"]
    assert tenant_b and tenant_b[0][2] == "T1+T2" and tenant_b[0][7] == "předběžně" and tenant_b[0][9]
    raw = imported_reader_client.get("/spotreba/export.csv?pohled=dny&rok=2025").content.decode("utf-8-sig")
    assert "surově (po dnech odečtů)" in raw


def test_spotreba_requires_login(anon_client) -> None:
    r = anon_client.get("/spotreba", follow_redirects=False, headers={"accept": "text/html"})
    assert r.status_code == 303
