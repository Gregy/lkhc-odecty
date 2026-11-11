"""Second read (EXTRACTOR_VERIFY): an independent re-read of the same photo; unreproduced values are doubtful.

The model endpoint is an ``httpx.MockTransport`` (no network); serials and readings are fictional.
"""

from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.extraction.eval import ReplayExtractor, usage_summary
from app.extraction.extractors import OpenAICompatExtractor, make_extractor, same_reading
from app.extraction.matching import apply_matching
from app.extraction.types import ExtractionHints, ExtractionResult, MeterHintIn, RegisterValue
from tests.capture_helpers import make_jpeg

IMG = make_jpeg()
HINTS = ExtractionHints(
    meters=(
        MeterHintIn(meter_id=1, serial="40000001", point_name="Místo A", decimals_shown=2),
        MeterHintIn(meter_id=2, serial="40000002", point_name="Místo B", decimals_shown=2),
    )
)
U1 = {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.001}
U2 = {"prompt_tokens": 100, "completion_tokens": 12, "cost": 0.002}


def meter(serial: str | None, *regs: tuple[str | None, str]) -> dict[str, Any]:
    return {
        "serial": serial,
        "serial_confidence": 0.9 if serial else 0.0,
        "registers": [{"register": g, "digits_seen": d, "confidence": 0.95} for g, d in regs],
    }


def answer(*meters: dict[str, Any], readable: bool = True) -> dict[str, Any]:
    return {"readable": readable, "problems": [], "meters": list(meters)}


def chat(obj: dict[str, Any], usage: dict[str, Any] | None = None, finish: str = "stop") -> dict[str, Any]:
    return {
        "model": "stub-vision",
        "choices": [{"message": {"role": "assistant", "content": json.dumps(obj)}, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 1, "completion_tokens": 1},
    }


class Script:
    """Scripted endpoint: each item is a chat body (200), an ``httpx.Response`` or an exception to raise."""

    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        item = self.items.pop(0) if self.items else httpx.Response(500, text="no script")
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


def ex(script: Script, *, verify: bool = True) -> OpenAICompatExtractor:
    return OpenAICompatExtractor(
        "http://model.invalid/v1",
        "stub-vision",
        "test-key",
        transport=httpx.MockTransport(script),
        sleep=lambda s: None,
        max_retries=0,
        verify=verify,
    )


A = meter("40000001", ("T1", "0048598"))


def test_agreeing_second_read_keeps_the_value_confident_and_sums_usage() -> None:
    s = Script(chat(answer(A), U1), chat(answer(meter("40000001", ("T1", "485.98"))), U2))
    res = ex(s).extract(IMG, HINTS)
    assert res.ok and len(s.requests) == 2
    # an independent request: the same image and prompt, the first answer is not shown
    assert s.requests[0] == s.requests[1] and len(s.requests[1]["messages"]) == 2
    r = res.meters[0].registers[0]
    assert r.problems == [] and r.confidence == 0.95 and r.second_read is None
    assert "second_read" not in r.to_dict()
    raw = res.raw
    assert raw["calls"] == 2 and raw["verify"]["agreed"] == 1 and raw["verify"]["doubtful"] == 0
    assert raw["usage"]["prompt_tokens"] == 200 and raw["usage"]["completion_tokens"] == 22
    assert raw["usage"]["cost"] == pytest.approx(0.003) and raw["verify"]["usage"] == U2
    apply_matching(res, HINTS.meters, None)
    assert r.value == D("485.98") and r.problems == []


def test_disagreeing_second_read_makes_the_value_doubtful_and_keeps_both() -> None:
    s = Script(chat(answer(A)), chat(answer(meter("40000001", ("T1", "0048596")))))
    res = ex(s).extract(IMG, HINTS)
    assert res.ok and res.readable and len(s.requests) == 2
    r = res.meters[0].registers[0]
    assert r.digits_seen == "0048598"  # the first read stays the proposed value
    assert r.problems == ["low_confidence"] and r.confidence == 0.5 and r.second_read == "0048596"
    (cmp,) = res.raw["verify"]["registers"]
    assert cmp == {
        "serial": "40000001",
        "register": "T1",
        "first": "0048598",
        "second": ["0048596"],
        "paired_by": "serial",
        "agreed": False,
    }
    # persisted (Photo.extraction_result) and re-matched like the review does
    back = ExtractionResult.from_dict(res.to_dict())
    assert back is not None
    apply_matching(back, HINTS.meters, None)
    rb = back.meters[0].registers[0]
    assert rb.value == D("485.98") and rb.second_read == "0048596" and "low_confidence" in rb.problems


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(401, json={"error": "bad key"}),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
        chat({"partial": True}, U2, finish="length"),  # answer cut off at max_tokens
    ],
    ids=["http-error", "timeout", "connection", "truncated"],
)
def test_failed_second_read_leaves_the_photo_ok_but_doubtful(failure: Any) -> None:
    s = Script(chat(answer(A, meter("40000002", ("T1", "0001234"))), U1), failure)
    res = ex(s).extract(IMG, HINTS)
    assert res.ok and res.error is None and res.readable and len(s.requests) == 2
    regs = [m.registers[0] for m in res.meters]
    assert all(r.problems == ["low_confidence"] and r.confidence == 0.5 and r.second_read == "" for r in regs)
    assert res.raw["verify"]["error"] and res.raw["verify"]["doubtful"] == 2
    if isinstance(failure, dict):  # the truncated answer was billed: counted in the photo's usage
        assert res.raw["usage"]["completion_tokens"] == 22


def test_verify_off_makes_a_single_call() -> None:
    s = Script(chat(answer(A)))
    res = ex(s, verify=False).extract(IMG, HINTS)
    assert res.ok and len(s.requests) == 1 and "verify" not in res.raw
    assert res.meters[0].registers[0].problems == []


@pytest.mark.parametrize(
    "first",
    [
        chat(answer(readable=False)),
        chat(answer(meter("40000001", ("T1", "00??598")), readable=False)),
        httpx.Response(500, text="boom"),  # a failed first read is an error, never re-read
    ],
    ids=["nothing", "only-unreadable-digits", "error"],
)
def test_no_second_call_without_digits_in_the_first_read(first: Any) -> None:
    s = Script(first, chat(answer(A)))
    ex(s).extract(IMG, HINTS)
    assert len(s.requests) == 1


def test_meters_are_paired_by_serial_then_the_only_one_left() -> None:
    first = answer(A, meter("40000002", ("T1", "0001234")))
    # other order; the first meter's serial misread by one digit in the second read
    second = answer(meter("40000002", ("T1", "0001234")), meter("40000007", ("T1", "0048598")))
    res = ex(Script(chat(first), chat(second))).extract(IMG, HINTS)
    assert all(m.registers[0].problems == [] for m in res.meters)
    assert [c["paired_by"] for c in res.raw["verify"]["registers"]] == ["only", "serial"]


def test_meters_without_serial_are_paired_by_position_only_with_equal_counts() -> None:
    first = answer(meter(None, ("T1", "0048598")), meter(None, ("T1", "0001234")))
    same = answer(meter(None, ("T1", "0048598")), meter(None, ("T1", "0001234")))
    res = ex(Script(chat(first), chat(same))).extract(IMG, HINTS)
    assert all(m.registers[0].problems == [] for m in res.meters)
    assert {c["paired_by"] for c in res.raw["verify"]["registers"]} == {"position"}
    # the second read saw one meter only: which one is unknown -> neither value counts as reproduced
    fewer = answer(meter(None, ("T1", "0048598")))
    res = ex(Script(chat(first), chat(fewer))).extract(IMG, HINTS)
    assert all(m.registers[0].problems == ["low_confidence"] for m in res.meters)


def test_registers_must_agree_per_register() -> None:
    first = answer(meter("40000001", ("T1", "0010000"), ("T2", "0020000")))
    swapped = answer(meter("40000001", ("T1", "0020000"), ("T2", "0010000")))
    res = ex(Script(chat(first), chat(swapped))).extract(IMG, HINTS)
    t1, t2 = res.meters[0].registers
    assert t1.problems == t2.problems == ["low_confidence"]
    assert t1.second_read == "0020000" and t2.second_read == "0010000"
    # a register left unlabelled by one read is compared with any register of the paired meter
    unlabelled = answer(meter("40000001", (None, "0020000"), (None, "0010000")))
    res = ex(Script(chat(first), chat(unlabelled))).extract(IMG, HINTS)
    assert all(r.problems == [] for r in res.meters[0].registers)


def test_same_reading() -> None:
    def rv(digits: str = "", value: str | None = None) -> RegisterValue:
        return RegisterValue("T1", D(value) if value else None, 0.9, "", digits)

    assert same_reading(rv("0048598"), rv("485.98"))  # point placed by the meter's decimals later
    assert same_reading(rv("0012"), rv("12"))
    assert not same_reading(rv("19212.9"), rv("1921.29"))  # both put a point: it must match
    assert not same_reading(rv("0048598"), rv("0048596"))
    assert not same_reading(rv("12?4"), rv("1234"))
    assert same_reading(rv(value="512.5"), rv(value="512.50"))  # no digits on either side: model values
    assert not same_reading(rv("5125"), rv(value="512.5"))


def test_setting_reaches_the_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTOR_VERIFY", "true")
    settings = Settings(_env_file=None, app_env="test", extractor="openai_compat")  # type: ignore[call-arg]
    assert settings.extractor_verify is True
    extractor = make_extractor(settings)
    assert isinstance(extractor, OpenAICompatExtractor) and extractor.verify is True
    monkeypatch.delenv("EXTRACTOR_VERIFY")
    default = Settings(_env_file=None, app_env="test", extractor="openai_compat")  # type: ignore[call-arg]
    assert default.extractor_verify is False
    assert make_extractor(default).verify is False  # type: ignore[attr-defined]


def test_eval_counts_usage_once_and_replays_the_verdicts(tmp_path: Path) -> None:
    s = Script(chat(answer(A), U1), chat(answer(meter("40000001", ("T1", "0048596"))), U2))
    res = ex(s).extract(IMG, HINTS)
    legacy = {"content": "{}", "usage": U1, "verify": {"usage": U2}}  # reports made before "calls"
    summary = usage_summary([{"raw": res.raw}, {"raw": legacy}])
    assert summary["prompt_tokens"] == 400 and summary["completion_tokens"] == 44
    assert summary["cost_usd"] == pytest.approx(0.006)

    report = tmp_path / "report.json"
    report.write_text(json.dumps({"photos": [{"file": "a.jpg", "raw": res.raw, "model": "stub-vision"}]}))
    replayed = ReplayExtractor(report).extract(b"", ExtractionHints(image_sha256="a.jpg"))
    r = replayed.meters[0].registers[0]
    assert r.problems == ["low_confidence"] and r.second_read == "0048596"
