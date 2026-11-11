"""The offline evaluation CLI (``python -m app.extraction.eval``) with the fake extractor."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from app.config import get_settings
from app.extraction import eval as ev
from tests.capture_helpers import answer, make_jpeg, sha


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    a = make_jpeg(taken=datetime(2026, 11, 8, 16, 48, 51), color=(1, 2, 3))
    b = make_jpeg(color=(4, 5, 6))
    (tmp_path / "imgs").mkdir()
    (tmp_path / "imgs" / "a.jpg").write_bytes(a)
    (tmp_path / "imgs" / "b.jpg").write_bytes(b)
    (tmp_path / "imgs" / "notes.txt").write_text("ignored")
    responses = {sha(a): answer({"serial": "3906127", "registers": [{"digits_seen": "09187.4", "register": "T1"}]})}
    (tmp_path / "fake.json").write_text(json.dumps(responses))
    monkeypatch.setenv("EXTRACTOR", "fake")
    monkeypatch.setenv("EXTRACTOR_FAKE_RESPONSES", str(tmp_path / "fake.json"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_eval_dir_with_expected(fake_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    truth = fake_env / "truth.json"
    truth.write_text(
        json.dumps({"a.jpg": {"serial": "3906127", "T1": "9187.4"}, "b.jpg": {"serial": "69253071", "T1": "512.37"}})
    )
    out = fake_env / "report.json"
    assert ev.main([str(fake_env / "imgs"), "--no-hints", "--out", str(out), "--expected", str(truth)]) == 0
    text = capsys.readouterr().out
    assert "a.jpg" in text and "3906127" in text and "T1=9187.4" in text and "unreadable" in text
    rep = json.loads(out.read_text())
    assert rep["summary"]["photos"] == 2 and rep["summary"]["readable"] == 1
    acc = rep["summary"]["accuracy"]
    assert acc["serial_ok"] == 1 and acc["serial_total"] == 2 and acc["value_ok"] == 1 and acc["false_values"] == 0
    assert rep["photos"][0]["taken_at"] == "2026-11-08T16:48:51"


def test_eval_zip(fake_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    z = fake_env / "walk.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(fake_env / "imgs" / "a.jpg", "deep/../a.jpg")
        zf.writestr("readme.md", "x")
    assert ev.main([str(z), "--no-hints"]) == 0
    assert "a.jpg" in capsys.readouterr().out
    assert not (fake_env / "a.jpg").exists()  # nothing extracted to disk
