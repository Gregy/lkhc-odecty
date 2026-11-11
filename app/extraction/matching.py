"""Matching recognised meters to known meters (SPEC §5 "Párování").

Order: exact serial -> Levenshtein <= 2 -> expected-value proximity -> (series neighbours /
walk point, done by the review grouping) -> unassigned. A confidently read serial that is
unknown is *not* matched by values: it is reported as an unknown meter (new meter / replacement).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal

from app.extraction.prompt import (
    flag_hint_echo,
    flag_prompt_echo,
    interpret_value,
    prompt_numbers,
    value_from_digits,
)
from app.extraction.types import ExtractionResult, MeterHintIn, MeterReadout, RegisterValue

_STRENGTH = {"exact": 0, "fuzzy": 1, "walk": 2, "values": 3}
MAX_EDIT_DISTANCE = 2
MIN_FUZZY_LEN = 5
SERIAL_TRUST = 0.6  # serial confidence above which an unknown serial is not value-matched
LOW_CONFIDENCE = 0.6


def normalize_serial(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^0-9A-Z]", "", s.upper()).lstrip("0")


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _in_range(value: Decimal, lo: Decimal, hi: Decimal, digits: int) -> bool:
    if lo <= hi:
        return lo <= value <= hi
    return value >= lo or value <= hi  # range wraps over the counter rollover


def value_fits(rv: RegisterValue, hint: MeterHintIn, register: str | None) -> Decimal | None:
    """Relative distance to the expected value if the value fits the meter's plausible range, else None."""
    value = value_from_digits(rv.digits_seen, hint.decimals_shown) if rv.digits_seen else rv.value
    if value is None:
        return None
    regs = [register] if register else list(hint.registers)
    best: Decimal | None = None
    for reg in regs:
        h = hint.reg(reg)
        if h is None or h.low is None or h.high is None:
            continue
        if not _in_range(value, h.low, h.high, hint.digits):
            continue
        exp = h.expected if h.expected is not None else h.low
        span = max(abs(h.high - h.low), Decimal(1))
        dist = abs(value - exp) / span
        best = dist if best is None else min(best, dist)
    return best


def _values_contradict(readout: MeterReadout, hint: MeterHintIn) -> bool:
    """The readout has readable values and none of them fits the meter's plausible range."""
    readable = [
        rv for rv in readout.registers if rv.value is not None or (rv.digits_seen and "?" not in rv.digits_seen)
    ]
    has_range = any(h.low is not None and h.high is not None for h in hint.register_hints)
    if not readable or not has_range:
        return False
    return all(value_fits(rv, hint, rv.register if rv.register in hint.registers else None) is None for rv in readable)


def match_serial(
    serial: str | None, hints: Sequence[MeterHintIn], at: datetime | None
) -> tuple[MeterHintIn | None, str | None, list[int]]:
    ns = normalize_serial(serial)
    if not ns:
        return None, None, []
    active = [h for h in hints if h.active_at(at)]
    exact_active = [h for h in active if normalize_serial(h.serial) == ns]
    if len(exact_active) == 1:
        return exact_active[0], "exact", []
    if len(exact_active) > 1:
        return None, None, [h.meter_id for h in exact_active]
    if len(ns) >= MIN_FUZZY_LEN:
        scored = sorted(
            ((levenshtein(ns, normalize_serial(h.serial)), h) for h in active if normalize_serial(h.serial)),
            key=lambda x: x[0],
        )
        close = [(d, h) for d, h in scored if d <= MAX_EDIT_DISTANCE]
        if close:
            if len(close) == 1 or close[0][0] < close[1][0]:
                return close[0][1], "fuzzy", []
            return None, None, [h.meter_id for d, h in close if d == close[0][0]]
    exact_any = [h for h in hints if normalize_serial(h.serial) == ns]
    if len(exact_any) == 1:
        return exact_any[0], "exact", []  # meter not active at that date - review shows it
    return None, None, []


def match_by_values(readout: MeterReadout, candidates: Iterable[MeterHintIn]) -> tuple[MeterHintIn | None, list[int]]:
    scored: list[tuple[Decimal, MeterHintIn]] = []
    for h in candidates:
        dists = [d for rv in readout.registers if (d := value_fits(rv, h, rv.register)) is not None]
        if dists:
            scored.append((min(dists), h))
    if not scored:
        return None, []
    scored.sort(key=lambda x: x[0])
    if len(scored) == 1:
        return scored[0][1], []
    return None, [h.meter_id for _, h in scored[:5]]


def match_readout(
    readout: MeterReadout,
    hints: Sequence[MeterHintIn],
    at: datetime | None,
    *,
    focus_meter_ids: Sequence[int] = (),
) -> tuple[MeterHintIn | None, str | None, list[int]]:
    """Return (hint, method, ambiguous candidate ids)."""
    hint, method, cands = match_serial(readout.serial, hints, at)
    if hint is not None and method == "fuzzy" and _values_contradict(readout, hint):
        # A serial one or two characters off is weak evidence: with values that fit nowhere in this meter's
        # range it is more likely another meter (measured: a second meter of the same model, serial one digit
        # off, not in the DB, was assigned to the known one).
        return None, "unknown_serial" if readout.serial_confidence >= SERIAL_TRUST else None, [hint.meter_id]
    if hint is not None:
        return hint, method, []
    unknown_trusted_serial = (
        bool(normalize_serial(readout.serial)) and readout.serial_confidence >= SERIAL_TRUST and not cands
    )
    if unknown_trusted_serial:
        return None, "unknown_serial", []
    active = [h for h in hints if h.active_at(at)]
    if focus_meter_ids:
        focus = [h for h in active if h.meter_id in focus_meter_ids]
        if len(focus) == 1:
            return focus[0], "walk", []
        if focus:
            h, c = match_by_values(readout, focus)
            if h is not None:
                return h, "values", []
            return None, None, [x.meter_id for x in focus]
    pool = [h for h in active if h.meter_id in cands] if cands else active
    h, c = match_by_values(readout, pool)
    if h is not None:
        return h, "values", []
    return None, None, c or cands


def assign_registers(
    readout: MeterReadout, hint: MeterHintIn | None, numbers_in_prompt: frozenset[str] = frozenset()
) -> None:
    """Scale values by the meter's decimals, map unknown registers, flag echoes / low confidence."""
    for rv in readout.registers:
        interpret_value(rv, hint)
        if rv.confidence < LOW_CONFIDENCE and rv.value is not None and "low_confidence" not in rv.problems:
            rv.problems.append("low_confidence")
        flag_prompt_echo(rv, numbers_in_prompt, hint)
    if hint is None:
        return
    if len(hint.registers) == 1:
        only = hint.registers[0]
        unknown = [rv for rv in readout.registers if rv.register not in hint.registers]
        if len(readout.registers) == 1 and unknown:
            readout.registers[0].register = only
    for rv in readout.registers:
        if rv.register is not None and rv.register not in hint.registers:
            rv.problems.append("register_unknown")
            rv.register = None
        flag_hint_echo(rv, hint, rv.register)


def apply_matching(
    result: ExtractionResult,
    hints: Sequence[MeterHintIn],
    at: datetime | None,
    *,
    focus_meter_ids: Sequence[int] = (),
) -> ExtractionResult:
    """Match every readout of ``result`` in place (``meter_id``, ``match_method``, values).

    Two readouts of one photo cannot both be the same meter unless both carry its exact serial
    (a model may split one device's displays into two entries): a weaker claim (fuzzy serial, value,
    walk point) on a meter that another readout identifies more strongly is left unassigned with
    that meter as a suggestion. Real case: two meters of the same model side by side whose serials differ in one digit -
    the unknown one is one digit away from the known one.
    """
    numbers = prompt_numbers(hints)
    matches = [match_readout(r, hints, at, focus_meter_ids=focus_meter_ids) for r in result.meters]
    best: dict[int, int] = {}
    for hint, method, _ in matches:
        if hint is not None:
            best[hint.meter_id] = min(best.get(hint.meter_id, 9), _STRENGTH.get(method or "", 9))
    used: set[int] = set()
    for readout, (hint, method, cands) in zip(result.meters, matches, strict=True):
        if hint is not None and method != "exact":
            weaker = _STRENGTH.get(method or "", 9) > best[hint.meter_id]
            if weaker or hint.meter_id in used:
                hint, method, cands = None, None, [hint.meter_id]
        if hint is not None:
            used.add(hint.meter_id)
        readout.meter_id = hint.meter_id if hint else None
        readout.match_method = method
        readout.match_candidates = cands
        assign_registers(readout, hint, numbers)
    return result
