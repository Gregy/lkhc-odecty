"""Prompt construction and robust parsing of the model's answer.

Design notes (see SPEC §5 "Co ukázaly reálné fotky"):
- The model is told what each known meter is expected to show (type, digits, decimals,
  display note, last reading / expected range) so it can identify meters and avoid
  decimal slips - but it must report the *digits as seen* (``digits_seen``), with
  ``?`` for unreadable digits. The kWh value is computed from those digits in code
  (``interpret_value``) using the matched meter's ``decimals_shown``.
- A value that exactly equals a hint (last reading / expected value) is flagged
  ``same_as_hint`` by ``flag_hint_echo`` so an echoed hint can never slip through as
  a reading unnoticed (it needs an explicit acknowledgement in the review).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.extraction.types import (
    ExtractionHints,
    ExtractionResult,
    MeterHintIn,
    MeterReadout,
    RegisterValue,
)

SYSTEM_PROMPT = (
    "You read electricity meters from photos for a meter-reading app. A wrong number is much worse "
    "than admitting a digit is unreadable. Answer with one JSON object only, no prose, no markdown."
)

# Examples in this text use made-up numbers on purpose: measured on real photos, models copied a real
# example serial from the prompt (-> exact match to the wrong meter) and example readings into their answers.
INSTRUCTIONS = """Read every electricity meter visible in this photo.

What the photos look like:
- Mechanical drum counters (Křižík, ZPA, ...): black drums are whole kWh. The last drum(s) on a RED or
  differently coloured background are DECIMALS: write them after a '.', e.g. "34567.8".
- LCD modules on a DIN rail (Maneler, Schrack, ...): the display is often rotated by 90°; read it in its
  own orientation. If a decimal point is visible on the display, include it. If none is visible, write
  the digits exactly as shown, including leading zeros (e.g. "0012345") and do NOT insert a point.
- One device can have two displays (e.g. labelled T0/T1 or T1/T2): report each display as its own register.
- Cycling displays show one register at a time with a label such as "A+ T1", "T2", "1.8.1", "1.8.2",
  "1.8.0". Copy the label exactly into label_seen. OBIS 1.8.1 = T1, 1.8.2 = T2, 1.8.0 = total.
- "register" is the app's register of that meter (T1/T2 as listed below). The label on the display is not
  always the same thing: follow the meter's display note. Use null when you are not sure.
- Several meters can be in one photo: report each one separately.
- The serial number is printed on the faceplate or faintly on the side of the module ("No 12345678",
  "Nr. 87654321", "N 1234567"). Report only the characters you can actually read on THIS photo, without the
  "No"/"Nr." prefix; null if it is not visible. Never take a serial from this text.
- IGNORE handwritten numbers (old readings written on stickers or labels), stamps, type designations
  (e.g. "9907D", "ET 404"), voltages, currents, imp/kWh constants, years, barcodes.
- If the display is fogged, blurred, covered by glare or a flash reflection, too dark or cut off:
  set "readable": false or put "?" in place of every digit you cannot see clearly. NEVER guess a digit.
"""

FORMAT = """Answer with exactly this JSON structure:
{"readable": true|false,
 "problems": [zero or more of "blurry","glare","fog","dark","cropped","no_meter","no_serial","partial"],
 "meters": [
  {"serial": "<as printed>" or null, "serial_confidence": 0.0-1.0, "kind": "lcd"|"drum"|"other",
   "registers": [
     {"label_seen": "<as shown>" or "", "register": "T1"|"T2"|null, "digits_seen": "<as shown>",
      "value": <kWh number> or null, "confidence": 0.0-1.0}
   ]}
 ]}
"digits_seen" must contain only what is on the display: digits, an optional '.', and '?' for unreadable digits."""

# Numbers that appear in the prompt itself: a reading equal to one of them may be copied, not read.
PROMPT_EXAMPLE_NUMBERS = ("34567.8", "0012345", "12345678", "87654321", "1234567")


def _fmt(v: Decimal | None) -> str:
    if v is None:
        return "?"
    s = format(v.normalize(), "f") if v == v.to_integral_value() else format(v, "f")
    return s


def describe_meter(m: MeterHintIn, include_values: bool) -> str:
    regs = "/".join(m.registers)
    dec = (
        f"{m.decimals_shown} decimal place(s) on the display"
        if m.decimals_shown
        else "whole kWh (or a red decimal drum)"
    )
    parts = [f'- serial {m.serial}, point "{m.point_name}": registers {regs}, {m.digits} integer digits, {dec}.']
    if m.display_note:
        parts.append(f"  Display note: {m.display_note}")
    if include_values:
        for r in m.register_hints:
            if r.last_value is None:
                continue
            txt = f"  {r.register}: last reading {_fmt(r.last_value)} kWh"
            if r.last_date:
                txt += f" on {r.last_date.isoformat()}"
            if r.low is not None and r.high is not None:
                txt += f"; plausible now roughly {_fmt(r.low)}-{_fmt(r.high)}"
            parts.append(txt + ".")
    return "\n".join(parts)


def build_prompt(hints: ExtractionHints) -> str:
    out = [INSTRUCTIONS]
    meters = list(hints.meters)
    if hints.focus_meter_ids:
        focus = [m for m in meters if m.meter_id in hints.focus_meter_ids]
        if focus:
            out.append("The photo was taken at this meter point, so it most likely shows:")
            out.extend(describe_meter(m, hints.include_values) for m in focus)
            meters = [m for m in meters if m.meter_id not in hints.focus_meter_ids]
    if meters:
        out.append("Other known meters at this site (to identify the meter - never copy their numbers):")
        out.extend(describe_meter(m, hints.include_values) for m in meters)
    if hints.include_values:
        out.append(
            "The last readings above only help you notice misreadings such as a missed decimal place. "
            "Always report what the display shows now, even when it differs. Never copy a number from this text."
        )
    out.append(FORMAT)
    return "\n\n".join(out)


# --- parsing ----------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_REFUSAL_RE = re.compile(
    r"\b(sorry|i can(?:no|')t|cannot assist|unable to|not able to|i won't|as an ai)\b", re.IGNORECASE
)
_CONF_WORDS = {"high": 0.9, "medium": 0.6, "med": 0.6, "low": 0.3, "none": 0.0}


class ParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def extract_json_object(text: str) -> dict[str, Any]:
    """Find the first JSON object in a model answer (code fences, prose and trailing commas tolerated)."""
    if not isinstance(text, str) or not text.strip():
        raise ParseError("model_no_json", "empty answer")
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for cand in candidates:
        for variant in (cand, _TRAILING_COMMA_RE.sub(r"\1", cand)):
            idx = variant.find("{")
            while idx != -1:
                try:
                    obj, _ = decoder.raw_decode(variant, idx)
                except json.JSONDecodeError:
                    idx = variant.find("{", idx + 1)
                    continue
                # an inner object (one meter of a cut-off answer) must not pass for the whole answer
                if isinstance(obj, dict) and ("meters" in obj or "readable" in obj):
                    return obj
                idx = variant.find("{", idx + 1)
    if _REFUSAL_RE.search(text):
        raise ParseError("model_refused", text[:200])
    raise ParseError("model_no_json", text[:200])


def _conf(v: Any) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        f = float(v)
        if f > 1:  # percent
            f = f / 100
        return max(0.0, min(1.0, f))
    if isinstance(v, str):
        s = v.strip().lower().rstrip("%")
        if s in _CONF_WORDS:
            return _CONF_WORDS[s]
        try:
            return _conf(float(s))
        except ValueError:
            return 0.0
    return 0.0


def _to_decimal(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        v = repr(v)
    if not isinstance(v, str):
        return None
    s = v.strip().replace(" ", "").replace(" ", "").replace("kWh", "").replace("kwh", "")
    if not s or "?" in s:
        return None
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


def clean_digits(v: Any) -> str:
    """Normalise ``digits_seen``: keep digits, one '.', and '?'."""
    if v is None:
        return ""
    s = str(v).strip().replace(",", ".")
    s = re.sub(r"[^0-9.?|]", "", s).replace("|", ".")
    if s.count(".") > 1:
        first = s.index(".")
        s = s[: first + 1] + s[first + 1 :].replace(".", "")
    return s.strip(".")


def clean_serial(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    s = re.sub(r"^(?:no\.?|nr\.?|n[°º]?\.?|s/?n:?|serial:?|č\.?)\s*", "", s, flags=re.IGNORECASE)
    s = s.strip(" .:#")
    if not s or s.lower() in {"null", "none", "unknown", "n/a", "?"} or "?" in s:
        return None
    return s[:100]


def register_from_label(register: Any, label: str) -> str | None:
    """Map the model's register / label to T1/T2 (None when unknown)."""
    return _register_and_conflict(register, label)[0]


def _register_and_conflict(register: Any, label: str) -> tuple[str | None, bool]:
    """(T1/T2/None, conflict). OBIS codes decide; a plain "T1"/"T2" label and the model's register must agree.

    A display label is not always the app's register: the Maneler 9907D shows the reading on a display
    labelled "T0" and an unused second tariff labelled "T1" (always 0). With a display note the model
    can map that; when label and model disagree the register is left unknown (review decides).
    """
    lab = (label or "").upper().replace(" ", "")
    if "1.8.1" in lab:
        return "T1", False
    if "1.8.2" in lab:
        return "T2", False
    if "1.8.0" in lab:
        return None, False
    reg = str(register or "").upper().strip()
    model_reg = reg if reg in ("T1", "T2") else None
    label_reg = "T1" if lab.endswith("T1") else "T2" if lab.endswith("T2") else None
    if label_reg and model_reg and label_reg != model_reg:
        return None, True
    if lab.endswith("T0") and not model_reg:
        return None, False
    return model_reg or label_reg, False


def parse_answer(obj: dict[str, Any]) -> ExtractionResult:
    """Convert the model JSON into an ExtractionResult (values not yet scaled to a meter)."""
    problems = [str(p) for p in obj.get("problems") or [] if isinstance(p, (str, int))]
    meters_raw = obj.get("meters")
    if isinstance(meters_raw, dict):
        meters_raw = [meters_raw]
    meters: list[MeterReadout] = []
    for m in meters_raw if isinstance(meters_raw, list) else []:
        if not isinstance(m, dict):
            continue
        regs: list[RegisterValue] = []
        regs_raw = m.get("registers")
        if isinstance(regs_raw, dict):
            regs_raw = [regs_raw]
        for r in regs_raw if isinstance(regs_raw, list) else []:
            if not isinstance(r, dict):
                continue
            label = str(r.get("label_seen") or r.get("label") or "")
            digits = clean_digits(r.get("digits_seen") if r.get("digits_seen") is not None else r.get("digits"))
            register, conflict = _register_and_conflict(r.get("register"), label)
            rv = RegisterValue(
                register=register,
                value=_to_decimal(r.get("value")),
                confidence=_conf(r.get("confidence", 0.5)),
                label_seen=label[:40],
                digits_seen=digits[:24],
            )
            if "?" in digits:
                rv.problems.append("partial")
            if conflict:
                rv.problems.append("register_unknown")
            regs.append(rv)
        meters.append(
            MeterReadout(
                serial=clean_serial(m.get("serial")),
                serial_confidence=_conf(m.get("serial_confidence", 0.5 if m.get("serial") else 0)),
                registers=regs,
                kind=str(m.get("kind") or "")[:16],
            )
        )
    readable_flag = obj.get("readable")
    any_value = any(
        r.value is not None or (r.digits_seen and "?" not in r.digits_seen) for m in meters for r in m.registers
    )
    readable = bool(readable_flag) if isinstance(readable_flag, bool) else any_value
    if readable and not any_value:
        readable = False
    if not readable and "unreadable" not in problems:
        problems.append("unreadable")
    return ExtractionResult(meters=meters, readable=readable, problems=problems, raw=obj)


# --- value interpretation (after matching) -----------------------------------------


def value_from_digits(digits: str, decimals_shown: int | None) -> Decimal | None:
    """kWh from the digits as seen: explicit point wins, else scale by the meter's decimals_shown."""
    if not digits or "?" in digits:
        return None
    try:
        if "." in digits:
            return Decimal(digits)
        d = Decimal(int(digits))
    except InvalidOperation, ValueError:
        return None
    if decimals_shown:
        d = d.scaleb(-decimals_shown)
    return d


def interpret_value(rv: RegisterValue, hint: MeterHintIn | None) -> None:
    """Set ``rv.value`` from ``digits_seen`` (+ meter decimals); flag disagreement with the model's value."""
    if not rv.digits_seen:
        return
    derived = value_from_digits(rv.digits_seen, hint.decimals_shown if hint else 0)
    model_value = rv.value
    rv.value = derived
    if derived is None:
        if "partial" not in rv.problems:
            rv.problems.append("partial")
        return
    if model_value is not None and model_value != derived:
        plain = value_from_digits(rv.digits_seen, 0)
        if model_value != plain and "value_mismatch" not in rv.problems:
            rv.problems.append("value_mismatch")
    _fix_point_slip(rv, hint)


def _fix_point_slip(rv: RegisterValue, hint: MeterHintIn | None) -> None:
    """A point in the wrong place when the digit count matches the meter exactly: use the meter's layout.

    Drums have no printed point - the model infers it from the red drums and sometimes misplaces it (measured:
    4 black + 2 red drums read as "NNNNN.N"). Flagged ``value_mismatch`` so the review shows it.
    """
    if hint is None or not hint.decimals_shown or "." not in rv.digits_seen:
        return
    whole, frac = rv.digits_seen.split(".", 1)
    if len(whole) + len(frac) != hint.digits + hint.decimals_shown or len(frac) == hint.decimals_shown:
        return
    rv.value = value_from_digits(whole + frac, hint.decimals_shown)
    if "value_mismatch" not in rv.problems:
        rv.problems.append("value_mismatch")


ECHO_WHOLE_MIN_USE = 5  # kWh expected since the last reading
ECHO_WHOLE_MIN_RANGE = 20  # kWh width of the plausible range


def flag_hint_echo(rv: RegisterValue, hint: MeterHintIn | None, register: str | None) -> None:
    """Mark a value equal to a value given in the prompt (possible echo of the hint).

    Numbers shown in the prompt (last reading, upper bound of the range) are compared on whole kWh when
    the meter clearly consumes (>= 5 kWh expected since the last reading, range >= 20 kWh): the hints are
    whole kWh from the sheet and a model echoing one tends to add a decimal digit (measured on a real
    photo: the prompt gave last reading N, the drum showed a much higher value, the model "read" N.9).
    A nearly idle meter legitimately shows the same whole kWh again: exact comparison only.
    """
    if hint is None or rv.value is None or register is None:
        return
    h = hint.reg(register)
    if h is None:
        return
    echo = set()
    idle = h.last_value is not None and h.expected is not None and h.expected == h.last_value
    if not idle:  # a meter without consumption legitimately shows its last value again
        if h.last_value is not None and h.expected is not None:
            echo.add(h.last_value)  # consumption expected but the value did not move
        if h.expected is not None:
            echo.add(h.expected)
    if h.low is not None and h.high is not None and h.high != h.low:
        echo.add(h.high)
    moving = (
        h.last_value is not None
        and h.expected is not None
        and h.expected - h.last_value >= ECHO_WHOLE_MIN_USE
        and h.low is not None
        and h.high is not None
        and h.high - h.low >= ECHO_WHOLE_MIN_RANGE
    )
    # whole-kWh comparison only for numbers the prompt shows (last reading, range); the expected value is not
    # in the prompt and a steady meter really lands on it (measured: three walks in a row within 1 kWh)
    shown = {e for e in echo if e in (h.last_value, h.high)}
    hit = rv.value in echo or (moving and any(int(e) == int(rv.value) for e in shown))
    if hit and "same_as_hint" not in rv.problems:
        rv.problems.append("same_as_hint")


def prompt_numbers(hints: Sequence[MeterHintIn]) -> frozenset[str]:
    """Number literals (>= 3 digits) the model saw in the prompt text: examples and every display note."""
    texts = [*PROMPT_EXAMPLE_NUMBERS, *(h.display_note for h in hints if h.display_note)]
    out = set()
    for t in texts:
        for lit in re.findall(r"\d+(?:[.,]\d+)?", t):
            if sum(c.isdigit() for c in lit) >= 3:
                out.add(lit.replace(",", "."))
    return frozenset(out)


def flag_prompt_echo(rv: RegisterValue, numbers: frozenset[str], hint: MeterHintIn | None) -> None:
    """Mark a value equal to a number written in the prompt (a display note / an example): maybe copied."""
    if rv.value is None or not numbers or "same_as_hint" in rv.problems:
        return
    dec = hint.decimals_shown if hint else 0
    for lit in numbers:
        candidates = {value_from_digits(lit, dec), value_from_digits(lit, 0)}
        if rv.value in candidates or (rv.digits_seen and rv.digits_seen.lstrip("0") == lit.lstrip("0")):
            rv.problems.append("same_as_hint")
            return
