"""UI presentation rules: Czech dates without spaces, quality marks (only provisional values are marked),
main navigation (order, Správa hub for readers) and the remainder tenant shown only as T1+T2."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path

import openpyxl
import pytest

from app import services as s
from app.domain.aligned import Quality, QValue
from app.formatting import format_date, format_datetime, format_month, is_marked, quality_mark
from app.services.reports import QUALITY_LEGEND, quality_label
from app.templating import NAV, REMAINDER_TARIFF_NOTE, qvalue_html, templates
from tests import synthetic as syn
from tests.test_reports_vyuctovani import _create

APP = Path(__file__).resolve().parent.parent / "app"

# --- dates -----------------------------------------------------------------------------------------


def test_czech_dates_have_no_spaces() -> None:
    assert format_date(date(2026, 7, 3)) == "3.7.2026"
    assert format_date(datetime(2026, 12, 31, 23, 59)) == "31.12.2026"
    assert format_date(date(2026, 7, 3), with_year=False) == "3.7."
    assert format_month(date(2026, 7, 1)) == "7.2026"
    assert format_datetime(datetime(2026, 7, 3, 14, 5, 59)) == "3.7.2026 14:05"
    assert format_date(None) == format_month(None) == format_datetime(None) == "–"
    env = templates.env
    assert env.from_string("{{ d|date_cs }} {{ d|date_cs(false) }} {{ d|month_cs }}").render(d=date(2026, 1, 2)) == (
        "2.1.2026 2.1. 1.2026"
    )


def test_no_locale_date_formatting_in_static_js() -> None:
    """toLocale*String("cs-CZ") / Intl.DateTimeFormat give "3. 7. 2026" - the JS uses explicit formatters."""
    for path in [*(APP / "static" / "js").glob("*.js"), *(APP / "static" / "obchuzka").glob("*.js")]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"toLocale(Date|Time)?String|DateTimeFormat", text), path.name
        assert not re.search(r'"\. "', text), path.name  # hand-built "d. m. yyyy"


def test_no_spaced_dates_in_templates() -> None:
    for path in (APP / "templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d{1,2}\. \d{1,2}\.", text), path.name


# --- quality marks --------------------------------------------------------------------------------


def test_only_provisional_values_are_marked() -> None:
    assert [is_marked(q) for q in Quality] == [False, False, True]
    assert [quality_mark(q) for q in Quality] == ["", "", "≈"]
    assert quality_mark("provisional") == "≈" and quality_mark("estimated") == ""
    assert [quality_label(q) for q in ("exact", "estimated", "provisional")] == ["", "", "předběžně"]
    assert len(QUALITY_LEGEND) == 1 and "extrapolovaný" in QUALITY_LEGEND[0][1]

    exact, estimated, provisional = (QValue(D("1234"), q) for q in Quality)
    assert str(qvalue_html(exact)) == str(qvalue_html(estimated)).replace(
        ' title="odhad (interpolace)"', ' title="přesná hodnota"'
    )
    est = str(qvalue_html(estimated))
    assert "≈" not in est and "q-estimated" not in est and "tabindex" not in est and "1\xa0234" in est
    prov = str(qvalue_html(provisional))
    assert "≈1\xa0234" in prov and "≈?" not in prov and "q-provisional" in prov and 'tabindex="0"' in prov


def test_snapshot_macros_follow_the_mark_rule() -> None:
    env = templates.env
    tpl = env.from_string(
        '{% import "reports/_macros.html" as rm %}{{ rm.qd(q) }}|{{ rm.qbadge(q.quality) }}|{{ rm.state(q) }}'
    )

    def render(quality: str) -> str:
        return tpl.render(q={"value": D("50"), "quality": quality, "title": "t", "method": "x"})

    for quality in ("exact", "estimated"):
        html = render(quality)
        assert "≈" not in html and "badge" not in html and "q-" not in html, quality
    html = render("provisional")
    assert html.count("≈50") == 2 and "≈ předběžně" in html and "≈?" not in html
    legend = env.from_string('{% import "reports/_macros.html" as rm %}{{ rm.quality_legend() }}').render()
    assert "≈123" in legend and "extrapolovaný odhad za posledním odečtem" in legend
    assert "interpolace" not in legend and "≈?" not in legend


# --- navigation -------------------------------------------------------------------------------------


def _nav(html: str) -> list[tuple[str, str, bool]]:
    nav = re.search(r'<nav id="main-nav".*?</ul>', html, re.S)
    assert nav
    return [
        (label, url, bool(cur))
        for url, cur, label in re.findall(
            r'<li><a href="([^"]+)" (aria-current="page")?>([^<]+)</a></li>', nav.group(0)
        )
    ]


MAIN_TABS = ["Přehled", "Odečty", "Spotřeba", "Obchůzka", "Dávka", "Kontrola", "Vyúčtování", "Správa"]


def test_nav_order_and_content(admin_client, reader_client) -> None:
    assert [label for label, _url, _admin in NAV] == MAIN_TABS
    admin = _nav(admin_client.get("/").text)
    assert [label for label, _u, _c in admin] == MAIN_TABS
    assert not {"Místa", "Nájemci", "Fotky"} & {label for label, _u, _c in admin}
    # readers: no upload (admin only), but Správa with the read-only pages
    reader = _nav(reader_client.get("/").text)
    assert [label for label, _u, _c in reader] == [t for t in MAIN_TABS if t != "Dávka"]
    assert admin[0] == ("Přehled", "/", True)


@pytest.mark.parametrize("url", ["/mista", "/najemci", "/fotky"])
def test_moved_pages_highlight_sprava(imported_reader_client, url: str) -> None:
    r = imported_reader_client.get(url)
    assert r.status_code == 200
    current = [label for label, _u, cur in _nav(r.text) if cur]
    assert current == ["Správa"]


def test_reader_reaches_moved_pages_from_sprava(imported_reader_client) -> None:
    page = imported_reader_client.get("/sprava")
    assert page.status_code == 200
    links = set(re.findall(r'href="([^"]+)"', page.text))
    assert {"/mista", "/najemci", "/fotky"} <= links
    assert not {u for u in links if u.startswith("/sprava/")}
    for url in ("/sprava/mista", "/sprava/najemci", "/sprava/uzivatele"):
        assert imported_reader_client.get(url).status_code == 403


# --- remainder tenant: only T1+T2 -------------------------------------------------------------------


def test_remainder_shown_only_as_total_on_pages(imported_reader_client, imported_db) -> None:
    net = s.load_network(imported_db)
    rem = next(t for t in net.tenants if t.is_remainder)
    tenant_a = next(t for t in net.tenants if t.name == syn.A)
    dash = imported_reader_client.get("/").text
    assert dash.count('class="num muted remainder-split"') == 4 and REMAINDER_TARIFF_NOTE in dash
    lst = imported_reader_client.get("/najemci").text
    assert lst.count("remainder-split") == 2 and "Zbytek je uveden jen v součtu T1+T2" in lst
    detail = imported_reader_client.get(f"/najemci/{rem.id}?rok=2025").text
    assert detail.count("remainder-split") == 2 * 13  # 12 months + sum, T1 and T2
    assert "remainder-split" not in imported_reader_client.get(f"/najemci/{tenant_a.id}?rok=2025").text


def test_remainder_total_only_in_spotreba_and_exports(imported_reader_client, imported_db) -> None:
    net = s.load_network(imported_db)
    rem = next(t for t in net.tenants if t.is_remainder)
    html = imported_reader_client.get("/spotreba?rok=2025&sazba=1").text
    assert f'title="{REMAINDER_TARIFF_NOTE}">T1+T2</th>' in html and "Zbytek je uveden jen v součtu T1+T2" in html
    # XLSX: one T1+T2 column for the remainder, T1/T2 for the others
    wb = openpyxl.load_workbook(
        io.BytesIO(imported_reader_client.get("/spotreba/export.xlsx?rok=2025&sazba=1").content)
    )
    rows = [[c.value for c in row] for row in wb["Nájemci"].iter_rows()]
    header = next(r for r in rows if r[0] == "Období")
    assert f"{rem.name} T1+T2" in header and f"{rem.name} T1" not in header and f"{syn.A} T1" in header
    tab = s.tenant_consumption_table(imported_db, start=date(2025, 1, 1), end=date(2026, 1, 1), net=net)
    jan = rows[rows.index(header) + 1]
    assert abs(jan[header.index(f"{rem.name} T1+T2")] - float(tab.total(rem.id, 0).value)) < 1e-6
    # CSV (aligned): the remainder only as T1+T2
    text = imported_reader_client.get("/spotreba/export.csv?rok=2025&sazba=1").content.decode("utf-8-sig")
    regs = {r[2] for r in csv.reader(io.StringIO(text), delimiter=";") if r[0] == "nájemce" and r[1] == rem.name}
    assert regs == {"T1+T2"}
    # the raw "like the sheet" view keeps the remainder per tariff (oracle view)
    raw = imported_reader_client.get("/spotreba/export.csv?pohled=dny&rok=2025&sazba=1").content.decode("utf-8-sig")
    regs = {r[2] for r in csv.reader(io.StringIO(raw), delimiter=";") if r[0] == "nájemce" and r[1] == rem.name}
    assert regs == {"T1", "T2"}
    assert "remainder-split" not in imported_reader_client.get("/spotreba?pohled=dny&rok=2025&sazba=1").text


def test_remainder_total_only_in_settlement(imported_admin_client, imported_db) -> None:
    sid = _create(imported_admin_client, 2025)
    net = s.load_network(imported_db)
    rem = next(t for t in net.tenants if t.is_remainder)
    html = imported_admin_client.get(f"/vyuctovani/{sid}").text
    # summary per period (12 months + total, T1 and T2) and the protocol totals (T1, T2)
    assert html.count("remainder-split") == 2 * 13 + 2
    assert "kWh (T1+T2)" in html and "Zbytek je uveden jen v součtu T1+T2" in html
    assert imported_admin_client.get(f"/vyuctovani/{sid}/tisk").text.count("remainder-split") == 2 + 2
    # the stored document keeps its per-register structure (presentation only)
    res = s.compute_settlement(imported_db, date(2025, 1, 1), date(2026, 1, 1))
    doc = s.settlement_snapshot(res)
    rd = next(t for t in doc["tenants"] if t["id"] == rem.id)
    assert set(rd["totals"]) >= {"T1", "T2"}

    wb = openpyxl.load_workbook(io.BytesIO(imported_admin_client.get(f"/vyuctovani/{sid}/export.xlsx").content))
    summary = [[c.value for c in row] for row in wb["Souhrn"].iter_rows()]
    hdr = next(r for r in summary if r[0] == "Období")
    total = next(r for r in summary if r[0] == "Celkem")
    assert total[hdr.index(f"{rem.name} T1")] is None and total[hdr.index(f"{rem.name} T2")] is None
    rem_total = float(res.interval.tenant_total(rem.id).value)
    assert abs(total[hdr.index(f"{rem.name} celkem")] - rem_total) < 1e-6
    assert total[hdr.index(f"{syn.A} T1")] is not None
    sheet = [[c.value for c in row] for row in wb[rem.name].iter_rows()]
    calc = next(i for i, r in enumerate(sheet) if r[0] == "Výpočet zbytku")
    assert sheet[calc + 1][:3] == ["Složka", "kWh (T1+T2)", "Kvalita"]
    lines = []
    for r in sheet[calc + 2 :]:
        if not r[0]:
            break
        lines.append(r)
    assert lines[-1][0] == f"= {rem.name}" and abs(lines[-1][1] - rem_total) < 1e-6
    assert abs(sum(r[1] for r in lines[:-1]) - rem_total) < 1e-3  # main − others = remainder
