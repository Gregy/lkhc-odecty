"""Meter matching: exact -> fuzzy -> values -> walk focus -> unassigned; also on imported meters."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D

from sqlalchemy.orm import Session

from app.extraction.hints import build_hints, meter_hint_list
from app.extraction.matching import apply_matching, levenshtein, match_readout, normalize_serial
from app.extraction.types import ExtractionResult, MeterHintIn, MeterReadout, RegisterHint, RegisterValue


def hint(mid: int, serial: str, low: str, high: str, **kw) -> MeterHintIn:
    return MeterHintIn(
        meter_id=mid,
        serial=serial,
        register_hints=(RegisterHint("T1", D(low), date(2026, 6, 3), D(low), None, D(high)),),
        **kw,
    )


HINTS = [
    hint(1, "69253071", "3000", "3100"),
    hint(2, "38170625", "2", "25"),
    hint(3, "3906127", "1889", "1950"),
    hint(4, "26481357", "100", "200", removed_on=date(2025, 4, 30)),
    hint(5, "26481357-B", "265", "300"),
]
AT = datetime(2026, 7, 1, 12, 0)


def ro(serial: str | None, *values: str, conf: float = 0.95) -> MeterReadout:
    return MeterReadout(
        serial=serial, serial_confidence=conf, registers=[RegisterValue("T1", D(v), 0.9, digits_seen=v) for v in values]
    )


def test_helpers() -> None:
    assert normalize_serial("0069-253 071") == "69253071"
    assert levenshtein("69253071", "69253079") == 1 and levenshtein("abc", "abc") == 0


def test_exact_and_fuzzy() -> None:
    assert match_readout(ro("69253071", "3050"), HINTS, AT)[0].meter_id == 1
    h, method, _ = match_readout(ro("6925307", "3050"), HINTS, AT)  # missed a digit
    assert h.meter_id == 1 and method == "fuzzy"


def test_removed_meter_loses_to_active_replacement() -> None:
    # the imported "26481357" was removed in 2025; the active "-B" meter wins by edit distance
    h, method, _ = match_readout(ro("26481357", "270"), HINTS, AT)
    assert h.meter_id == 5 and method == "fuzzy"
    # before removal the exact one is active
    h, method, _ = match_readout(ro("26481357", "150"), HINTS, datetime(2025, 3, 1))
    assert h.meter_id == 4 and method == "exact"


def test_ambiguous_fuzzy_is_not_guessed() -> None:
    hints = [hint(1, "69253071", "0", "10"), hint(2, "69253072", "0", "10")]
    h, _, cands = match_readout(ro("69253073", "5"), hints, AT)
    assert h is None and sorted(cands) == [1, 2]


def test_values_without_serial() -> None:
    h, method, _ = match_readout(ro(None, "1910"), HINTS, AT)
    assert h.meter_id == 3 and method == "values"
    h, method, _ = match_readout(ro(None, "99999"), HINTS, AT)
    assert h is None and method is None


def test_unknown_confident_serial_is_not_value_matched() -> None:
    h, method, _ = match_readout(ro("91827364", "1910"), HINTS, AT)  # new meter showing a plausible value
    assert h is None and method == "unknown_serial"
    h, method, _ = match_readout(ro("91827364", "1910", conf=0.2), HINTS, AT)  # serial unsure -> values
    assert h.meter_id == 3 and method == "values"


def test_walk_focus() -> None:
    h, method, _ = match_readout(ro(None, "123456"), HINTS, AT, focus_meter_ids=[2])
    assert h.meter_id == 2 and method == "walk"


def test_two_meters_in_one_photo() -> None:
    res = ExtractionResult(meters=[ro("69253071", "3050"), ro("38170625", "3")], readable=True)
    apply_matching(res, HINTS, AT)
    assert [m.meter_id for m in res.meters] == [1, 2]


def test_hints_from_db(imported_db: Session) -> None:
    hints = meter_hint_list(imported_db, datetime(2026, 7, 1, 10))
    para = next(h for h in hints if h.serial == "3906127")  # synthetic: last 1889 on 3.6.2026
    t1 = para.reg("T1")
    assert t1.last_value == D("1889") and t1.last_date == date(2026, 6, 3)
    assert t1.low == D("1889") and t1.expected is not None and t1.high > t1.expected
    # readings of the same day are not used as hints (a photo must never be hinted with its own value)
    hints_0603 = meter_hint_list(imported_db, datetime(2026, 6, 3, 15))
    assert next(h for h in hints_0603 if h.serial == "3906127").reg("T1").last_value == D("1861")
    built = build_hints(imported_db, datetime(2026, 7, 1))
    assert "27350" not in {h.serial for h in built.meters}  # removed meter not offered to the model


def test_fuzzy_cannot_claim_a_meter_identified_exactly_on_the_same_photo() -> None:
    # two meters of one model side by side: 40000215 (known) and 40000225 (not in the DB, one digit away)
    hints = [hint(7, "40000215", "198", "294")]
    res = ExtractionResult(meters=[ro("40000215", "214.51"), ro("40000225", "1.86")], readable=True)
    apply_matching(res, hints, AT)
    known, other = res.meters
    assert known.meter_id == 7 and known.match_method == "exact"
    assert other.meter_id is None and other.match_candidates == [7]
    # order does not matter
    res = ExtractionResult(meters=[ro("40000225", "1.86"), ro("40000215", "214.51")], readable=True)
    apply_matching(res, hints, AT)
    assert [m.meter_id for m in res.meters] == [None, 7]
    # alone on a photo with a value that fits nowhere in the known meter's range: another meter
    res = ExtractionResult(meters=[ro("40000225", "1.86")], readable=True)
    apply_matching(res, hints, AT)
    assert res.meters[0].meter_id is None and res.meters[0].match_method == "unknown_serial"
    assert res.meters[0].match_candidates == [7]
    # alone on a photo the fuzzy match still works (a misread digit)
    res = ExtractionResult(meters=[ro("40000225", "214.6")], readable=True)
    apply_matching(res, hints, AT)
    assert res.meters[0].meter_id == 7 and res.meters[0].match_method == "fuzzy"


def test_two_exact_readouts_of_one_device_both_kept() -> None:
    res = ExtractionResult(meters=[ro("69253071", "3050"), ro("69253071", "3051")], readable=True)
    apply_matching(res, HINTS, AT)
    assert [m.meter_id for m in res.meters] == [1, 1]
