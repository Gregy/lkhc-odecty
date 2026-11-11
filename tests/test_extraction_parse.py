"""Prompt building, robust answer parsing, digit interpretation and hint-echo detection."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal as D

import pytest

from app.extraction.prompt import (
    PROMPT_EXAMPLE_NUMBERS,
    ParseError,
    build_prompt,
    extract_json_object,
    flag_hint_echo,
    flag_prompt_echo,
    interpret_value,
    parse_answer,
    prompt_numbers,
    register_from_label,
    value_from_digits,
)
from app.extraction.types import ExtractionHints, ExtractionResult, MeterHintIn, RegisterHint, RegisterValue

HINT = MeterHintIn(
    meter_id=10,
    serial="69253071",
    point_name="Nájemce A z chodby",
    decimals_shown=2,
    display_note="displej otočený o 90°",
    register_hints=(RegisterHint("T1", D("512"), date(2026, 10, 1), D("512"), D("517"), D("547")),),
)


def test_prompt_contains_hints_and_rules() -> None:
    p = build_prompt(ExtractionHints(meters=(HINT,)))
    assert "69253071" in p and "Nájemce A z chodby" in p and "displej otočený" in p
    assert "last reading was at least 511 kWh on 2026-10-01" in p and "roughly 511-547" in p
    assert "512" not in p  # the real last reading is never shown: a read equal to it cannot be an echo
    assert "digits_seen" in p and "NEVER guess" in p and "handwritten" in p.lower()
    no_values = build_prompt(ExtractionHints(meters=(HINT,), include_values=False))
    assert "last reading" not in no_values and "69253071" in no_values


def test_focus_meters_listed_first() -> None:
    other = MeterHintIn(meter_id=2, serial="3906127", point_name="Nájemce A věž")
    p = build_prompt(ExtractionHints(meters=(other, HINT), focus_meter_ids=(10,)))
    assert p.index("69253071") < p.index("3906127")
    assert "most likely shows" in p


@pytest.mark.parametrize(
    "text",
    [
        '{"readable": true, "meters": []}',
        'Sure! Here is the result:\n```json\n{"readable": true, "meters": []}\n```',
        'Result: {"readable": true, "meters": [],}  hope it helps',
        '```\n{"readable": true,\n "meters": []\n}\n```',
    ],
)
def test_extract_json_variants(text: str) -> None:
    assert extract_json_object(text)["readable"] is True


def test_extract_json_failures() -> None:
    with pytest.raises(ParseError) as e:
        extract_json_object("I'm sorry, but I can't help with reading this meter.")
    assert e.value.code == "model_refused"
    with pytest.raises(ParseError) as e:
        extract_json_object("the value is 12345")
    assert e.value.code == "model_no_json"
    with pytest.raises(ParseError):
        extract_json_object("")


def test_parse_answer_tolerates_odd_shapes() -> None:
    res = parse_answer(
        {
            "readable": "yes",  # not a bool -> derived from values
            "meters": {  # single object instead of a list
                "serial": "No 69253071",
                "serial_confidence": "high",
                "registers": {"label_seen": "A+ T1", "digits_seen": "0051 237", "value": "512,37", "confidence": 87},
            },
        }
    )
    assert res.readable
    m = res.meters[0]
    assert m.serial == "69253071" and m.serial_confidence == 0.9
    r = m.registers[0]
    assert r.register == "T1" and r.digits_seen == "0051237" and r.value == D("512.37") and r.confidence == 0.87


def test_unreadable_answer() -> None:
    res = parse_answer({"readable": False, "problems": ["fog", "glare"], "meters": []})
    assert not res.readable and "unreadable" in res.problems and "fog" in res.problems
    # claims readable but every digit is '?'
    res = parse_answer({"readable": True, "meters": [{"serial": None, "registers": [{"digits_seen": "4??12"}]}]})
    assert not res.readable
    assert res.meters[0].registers[0].problems == ["partial"]


@pytest.mark.parametrize(
    ("label", "reg", "expected"),
    [
        ("1.8.1", None, "T1"),
        ("1.8.2", None, "T2"),
        ("A+ T1", None, "T1"),
        ("T2", None, "T2"),
        ("1.8.0", "T1", None),
        ("T0", None, None),
        ("", "t2", "T2"),
        ("", "X", None),
        ("T0", "T1", "T1"),  # Maneler 9907D: the display labelled T0 is the meter's T1 (display note)
        ("T1", "T2", None),  # label and model disagree: unknown, review decides
        ("1.8.2", "T1", "T2"),  # OBIS codes are unambiguous
    ],
)
def test_register_from_label(label: str, reg: str | None, expected: str | None) -> None:
    assert register_from_label(reg, label) == expected


def test_label_register_conflict_is_flagged() -> None:
    res = parse_answer(
        {
            "readable": True,
            "meters": [
                {
                    "serial": "40000003",
                    "registers": [
                        {"label_seen": "T0", "register": "T1", "digits_seen": "00136.98", "confidence": 1},
                        {"label_seen": "T1", "register": "T2", "digits_seen": "00000.00", "confidence": 1},
                    ],
                }
            ],
        }
    )
    left, right = res.meters[0].registers
    assert left.register == "T1" and not left.problems
    assert right.register is None and right.problems == ["register_unknown"]


def test_value_from_digits_uses_meter_decimals() -> None:
    assert value_from_digits("0051237", 2) == D("512.37")  # LCD without a visible point
    assert value_from_digits("18734.6", 0) == D("18734.6")  # red drum written after '.'
    assert value_from_digits("18734.6", 2) == D("18734.6")  # explicit point wins
    assert value_from_digits("01916", 0) == D("1916")
    assert value_from_digits("18?6", 0) is None


def test_interpret_value_flags_disagreement() -> None:
    rv = RegisterValue("T1", D("51237"), 0.9, digits_seen="0051237")
    interpret_value(rv, HINT)  # model ignored decimals: the digits decide
    assert rv.value == D("512.37") and "value_mismatch" not in rv.problems
    rv = RegisterValue("T1", D("999"), 0.9, digits_seen="0051237")
    interpret_value(rv, HINT)
    assert rv.value == D("512.37") and "value_mismatch" in rv.problems


def test_echo_of_hint_is_flagged() -> None:
    for v in ("511", "547"):  # numbers the prompt shows: last reading - 1 kWh, upper bound
        rv = RegisterValue("T1", D(v), 0.9)
        flag_hint_echo(rv, HINT, "T1")
        assert "same_as_hint" in rv.problems, v
    for v in ("512", "517", "518.37"):  # real last reading (idle meter), expected value, any other read
        rv = RegisterValue("T1", D(v), 0.9)
        flag_hint_echo(rv, HINT, "T1")
        assert rv.problems == [], v


def test_result_roundtrip() -> None:
    res = parse_answer(
        {"readable": True, "meters": [{"serial": "1", "registers": [{"digits_seen": "12", "value": 12}]}]}
    )
    back = ExtractionResult.from_dict(res.to_dict())
    assert back is not None and back.meters[0].registers[0].value == D("12")


def test_static_prompt_text_has_only_made_up_numbers() -> None:
    # models copied example serials / readings from the prompt (measured): examples must be made up and known
    p = build_prompt(ExtractionHints())
    numbers = {n for n in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])", p) if sum(c.isdigit() for c in n) >= 4}
    assert numbers and numbers <= set(PROMPT_EXAMPLE_NUMBERS)


def test_value_equal_to_a_number_in_a_display_note_is_flagged() -> None:
    noted = MeterHintIn(meter_id=1, serial="40000001", decimals_shown=2, display_note="7 digits: 0045620 = 456.20")
    other = MeterHintIn(meter_id=2, serial="40000002", decimals_shown=2)
    numbers = prompt_numbers([noted, other])
    assert {"0045620", "456.20"} <= numbers and "7" not in numbers
    copied = RegisterValue("T1", D("456.20"), 0.95, digits_seen="0045620")
    flag_prompt_echo(copied, numbers, other)  # the note of another meter was copied
    assert copied.problems == ["same_as_hint"]
    read = RegisterValue("T1", D("3456.78"), 0.95, digits_seen="0345678")
    flag_prompt_echo(read, numbers, other)
    assert read.problems == []
    example = RegisterValue("T1", D("123.45"), 0.95, digits_seen="0012345")  # the prompt's own example
    flag_prompt_echo(example, numbers, other)
    assert example.problems == ["same_as_hint"]


def test_echo_with_an_added_decimal_is_flagged() -> None:
    h = MeterHintIn(
        meter_id=1,
        serial="40000009",
        decimals_shown=1,
        register_hints=(RegisterHint("T1", D("4321"), date(2026, 2, 2), D("4321"), D("4622"), D("5223")),),
    )
    echoed = RegisterValue("T1", D("4320.9"), 0.9, digits_seen="4320.9")  # prompt showed 4320
    flag_hint_echo(echoed, h, "T1")
    assert echoed.problems == ["same_as_hint"]
    read = RegisterValue("T1", D("4916.4"), 0.9, digits_seen="4916.4")
    flag_hint_echo(read, h, "T1")
    assert read.problems == []
    # a nearly idle meter shows the same whole kWh again: only an exact copy is suspicious
    slow = MeterHintIn(
        meter_id=2,
        serial="40000010",
        decimals_shown=1,
        register_hints=(RegisterHint("T1", D("5555"), date(2026, 7, 3), D("5555"), D("5556"), D("5576")),),
    )
    for v in ("5555.9", "5555", "5554.9"):
        same_kwh = RegisterValue("T1", D(v), 0.9, digits_seen=v)
        flag_hint_echo(same_kwh, slow, "T1")
        assert same_kwh.problems == [], v
    copied = RegisterValue("T1", D("5554"), 0.9, digits_seen="5554")  # exactly the number in the prompt
    flag_hint_echo(copied, slow, "T1")
    assert copied.problems == ["same_as_hint"]


def test_point_slip_on_a_drum_uses_the_meter_layout() -> None:
    drum = MeterHintIn(meter_id=1, serial="40000011", digits=4, decimals_shown=2)
    slipped = RegisterValue("T1", None, 0.9, digits_seen="12345.6")  # 4 black + 2 red drums
    interpret_value(slipped, drum)
    assert slipped.value == D("1234.56") and "value_mismatch" in slipped.problems
    right = RegisterValue("T1", None, 0.9, digits_seen="1234.56")
    interpret_value(right, drum)
    assert right.value == D("1234.56") and right.problems == []
    other = RegisterValue("T1", None, 0.9, digits_seen="0123.4")  # digit count does not match: left alone
    interpret_value(other, drum)
    assert other.value == D("123.4") and other.problems == []


def test_steady_meter_landing_on_the_unshown_expected_value_is_not_flagged() -> None:
    # the expected value is not in the prompt; a steady consumer really shows it (whole kWh)
    h = MeterHintIn(
        meter_id=3,
        serial="40000012",
        decimals_shown=2,
        register_hints=(RegisterHint("T1", D("6000"), date(2026, 7, 3), D("6000"), D("6777"), D("8000")),),
    )
    read = RegisterValue("T1", D("6777.57"), 0.95, digits_seen="0677757")
    flag_hint_echo(read, h, "T1")
    assert read.problems == []
    exact = RegisterValue("T1", D("6777"), 0.95, digits_seen="06777")  # never shown, so not an echo either
    flag_hint_echo(exact, h, "T1")
    assert exact.problems == []
