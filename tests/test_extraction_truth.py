"""Ground truth from the DB (one day) and meter-level scoring of the extraction eval."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.extraction import eval as ev
from app.extraction.hints import meter_hint_list
from app.extraction.truth import build_truth
from tests import synthetic as syn
from tests.capture_helpers import answer, make_jpeg, sha

DAY = date(2026, 3, 1)
AT = datetime(2026, 3, 1, 18, 18, 5)
MAIN_T1, MAIN_T2 = (str(int(syn.value("B", DAY, reg))) for reg in ("T1", "T2"))
OFFICE = int(syn.value("L", DAY))
E_VALUE = int(syn.value("P", DAY))
WORKSHOP = int(syn.value("X", DAY))


def test_truth_from_db_and_no_leak_into_hints(imported_corrected_db: Session) -> None:
    truth = build_truth(imported_corrected_db, DAY)
    assert truth["kind"] == "meter_truth" and len(truth["meters"]) == 14
    assert truth["meters"][syn.MAIN_SERIAL]["T1"] == MAIN_T1 and truth["meters"][syn.MAIN_SERIAL]["T2"] == MAIN_T2
    assert "5820734" not in truth["meters"]  # the hangar meter of tenant B was not read that day
    # hints for photos of that day are built only from earlier days: the truth never reaches the model
    for h in meter_hint_list(imported_corrected_db, AT):
        for rh in h.register_hints:
            assert rh.last_date is None or rh.last_date < DAY


def test_eval_scores_against_meter_truth(
    imported_corrected_db: Session, imported_corrected_db_url: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    truth = build_truth(imported_corrected_db, DAY)
    (tmp_path / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    photos = tmp_path / "fotky"
    photos.mkdir()
    answers = {
        "a_main.jpg": answer(
            {
                "serial": "52814903",
                "registers": [{"register": "T1", "digits_seen": MAIN_T1}, {"register": "T2", "digits_seen": MAIN_T2}],
            }
        ),
        "b_office.jpg": answer(
            {"serial": "64918203", "registers": [{"digits_seen": f"{OFFICE:05d}45"}]}
        ),  # x.45, decimals 0
        "c_tenant_e.jpg": answer(
            {"serial": None, "registers": [{"digits_seen": f"{E_VALUE:05d}"}]}
        ),  # identified by value
        "d_tenant_c.jpg": answer({"serial": "84726051", "registers": [{"digits_seen": "00900"}]}),  # confidently wrong
        "e_workshop.jpg": answer({"serial": "15937402", "registers": [{"digits_seen": f"{WORKSHOP}.4"}]}),  # truncates
        "f_hangar.jpg": answer({"serial": "5820734", "registers": [{"digits_seen": "30000"}]}),  # not read that day
        "g_blur.jpg": answer(readable=False, problems=["unreadable"]),
    }
    fake = {}
    for i, (name, ans) in enumerate(answers.items()):
        data = make_jpeg(taken=AT, color=(i * 30, 10, 10))
        (photos / name).write_bytes(data)
        fake[sha(data)] = ans
    (tmp_path / "fake.json").write_text(json.dumps(fake), encoding="utf-8")
    monkeypatch.setenv("EXTRACTOR", "fake")
    monkeypatch.setenv("EXTRACTOR_FAKE_RESPONSES", str(tmp_path / "fake.json"))
    monkeypatch.setenv("DATABASE_URL", imported_corrected_db_url)
    get_settings.cache_clear()
    try:
        out = tmp_path / "report.json"
        assert ev.main([str(photos), "--expected", str(tmp_path / "truth.json"), "--out", str(out)]) == 0
    finally:
        get_settings.cache_clear()
    text = capsys.readouterr().out
    report = json.loads(out.read_text(encoding="utf-8"))
    mt = report["summary"]["meter_truth"]
    assert mt["values"] == {
        "exact": 3,
        "truncated": 1,
        "scaled": 1,
        "wrong": 1,
        "no_value": 0,
        "not_in_truth": 1,
        "unassigned": 0,
        "unassigned_fits_truth": 0,
        "confident_wrong": 1,
        "plausible_wrong": 0,
    }
    status = {k: v["status"] for k, v in mt["meters"].items()}
    assert status["52814903"] == "correct" and status["15937402"] == "correct" and status["73601548"] == "correct"
    assert status["64918203"] == "wrong" and status["84726051"] == "wrong" and status["3906127"] == "not_found"
    tenant_e = next(n for p in report["photos"] if p["file"] == "c_tenant_e.jpg" for n in p["truth"])
    assert tenant_e["identified_by"] == "match" and tenant_e["outcome"] == "exact"
    assert "JISTĚ ŠPATNĚ" in text and "jistě špatně: 1" in text
