"""Migration 0003 (rounds removed) on data shapes the real DB does not have yet."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from alembic.config import Config
from sqlalchemy.orm import Session

from alembic import command
from app import services as s
from app.services.reports import settlement_document
from tests import synthetic as syn
from tests.conftest import ROOT, sqlite_url


def _cfg(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def test_upgrade_keeps_photos_skips_and_settlements(tmp_path: Path, imported_db: Session, make_app, login_client):
    # a finalized snapshot made by the current code, labelled as an old "rounds" settlement
    doc = settlement_document(
        s.compute_settlement(imported_db, date(2025, 1, 2), date(2026, 1, 4)), s.load_network(imported_db)
    )
    doc["mode"] = "rounds"
    e_total = next(t for t in doc["tenants"] if t["name"] == syn.E)["total"]["value"]

    path = tmp_path / "old.db"
    url = sqlite_url(path)
    command.upgrade(_cfg(url), "0002")
    c = sqlite3.connect(path)
    c.executescript(
        """
        INSERT INTO users (id, email, name, role, active) VALUES (1, 'admin@example.org', 'A', 'admin', 1);
        INSERT INTO meter_points (id, name, description, sort_order, role, active, note)
            VALUES (1, 'Distribuce', '', 10, 'main', 1, ''), (2, 'Dílna', '', 20, 'sub', 1, '');
        INSERT INTO meters (id, meter_point_id, serial, digits, decimals_shown, display_note, registers,
                            source_type, note)
            VALUES (1, 1, 'M1', 5, 0, '', '["T1"]', 'manual', ''), (2, 2, 'M2', 5, 0, '', '["T1"]', 'manual', '');
        INSERT INTO rounds (id, date, performed_by, status, note) VALUES
            (1, '2026-10-01', 'Pepa', 'closed', 'říjen'), (2, '2026-11-08', 'Pepa', 'open', '');
        INSERT INTO photos (id, uuid, path, original_filename, content_type, extraction_status, extraction_attempts,
                            review_status, note, round_id, source, reported_by)
            VALUES (1, 'u1', 'photos/2026/11/u1.jpg', 'a.jpg', 'image/jpeg', 'done', 1, 'confirmed', '', 2, 'walk', '');
        INSERT INTO readings (meter_id, register, taken_at, value, source, status, round_id, photo_id,
                              reported_by, note)
            VALUES (1, 'T1', '2026-10-01 00:00:00.000000', 100, 'manual', 'confirmed', 1, NULL, '', ''),
                   (1, 'T1', '2026-11-08 00:00:00.000000', 160, 'photo', 'confirmed', 2, 1, '', ''),
                   (2, 'T1', '2026-10-01 00:00:00.000000', 10, 'manual', 'confirmed', 1, NULL, '', '');
        INSERT INTO round_skips (round_id, meter_point_id, note, created_by_id) VALUES (2, 2, 'zamčeno', 1);
        """
    )
    c.execute(
        "INSERT INTO settlements (id, year, name, period_start, period_end, boundary_mode, status, snapshot, "
        "snapshot_version, finalize_forced, finalize_note, note) VALUES "
        "(1, 2025, 'Vyúčtování 2025', '2025-01-02', '2026-01-04', 'rounds', 'final', ?, 1, 0, '', ''),"
        "(2, 2026, 'Návrh 2026', '2026-01-01', '2027-01-01', 'rounds', 'draft', NULL, NULL, 0, '', '')",
        (json.dumps(doc),),
    )
    c.commit()
    c.close()

    command.upgrade(_cfg(url), "head")

    c = sqlite3.connect(path)
    assert c.execute("select count(*), count(photo_id) from readings").fetchone() == (3, 1)
    assert c.execute("select photo_id from readings where value = 160").fetchone() == (1,)
    assert c.execute("select meter_point_id, day, note from point_skips").fetchall() == [(2, "2026-11-08", "zamčeno")]
    assert [r[1] for r in c.execute("pragma table_info(readings)")].count("round_id") == 0
    assert c.execute("select count(*) from sqlite_master where name in ('rounds', 'round_skips')").fetchone() == (0,)
    assert c.execute("select id, status from settlements order by id").fetchall() == [(1, "final"), (2, "draft")]
    archive = json.loads(c.execute("select changes from audit_log where action = 'migrate_0003'").fetchone()[0])
    assert [r["note"] for r in archive["rounds"]] == ["říjen", ""]
    assert {x["boundary_mode"] for x in archive["settlements"]} == {"rounds"}
    assert c.execute("pragma foreign_key_check").fetchall() == [] and c.execute(
        "pragma integrity_check"
    ).fetchone() == ("ok",)
    assert json.loads(c.execute("select snapshot from settlements where id = 1").fetchone()[0]) == json.loads(
        json.dumps(doc)
    )
    c.close()

    # the finalized settlement still renders from its snapshot; the draft recomputes on aligned months
    app = make_app(url)
    admin = login_client(app, "admin")
    from app.formatting import format_number

    html = admin.get("/vyuctovani/1").text
    assert "hranice podle obchůzek (starší vyúčtování)" in html and format_number(e_total) in html
    assert admin.get("/vyuctovani/2").status_code == 200
    assert admin.get("/vyuctovani/1/tisk").status_code == 200
    assert admin.get("/vyuctovani/1/export.xlsx").status_code == 200
    body = admin.get("/obchuzka/api/body?den=2026-11-08").json()
    assert next(p for p in body["points"] if p["id"] == 2)["skipped"] == {"note": "zamčeno"}
