"""Result and hint types of the reading extractor (SPEC §5 "Extrakce z fotek").

Results are stored as JSON in ``Photo.extraction_result`` (``to_dict``/``from_dict``),
so the shapes here are a persistence contract too - only add optional fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

# Problem codes (machine) -> Czech label shown in the UI.
PROBLEM_LABELS: dict[str, str] = {
    "unreadable": "nečitelné",
    "blurry": "rozmazané",
    "glare": "odlesk",
    "fog": "zamlžené sklo",
    "dark": "tmavé",
    "cropped": "displej oříznutý",
    "partial": "některé číslice nejisté",
    "no_meter": "na fotce není elektroměr",
    "no_serial": "výrobní číslo nečitelné",
    "value_mismatch": "číslice a hodnota modelu nesouhlasí",
    "same_as_hint": "hodnota je shodná s nápovědou – ověřte",
    "low_confidence": "model si není jistý",
    "model_error": "chyba modelu",
    "model_no_json": "model nevrátil platnou odpověď",
    "model_refused": "model odmítl odpovědět",
    "timeout": "vypršel čas",
}


def problem_label(code: str) -> str:
    return PROBLEM_LABELS.get(code, code)


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation, ValueError:
        return None
    return d if d.is_finite() else None


@dataclass
class RegisterValue:
    """One register value as seen on a photo."""

    register: str | None  # "T1" | "T2" | None (unknown which tariff)
    value: Decimal | None  # kWh incl. decimals; None = not readable
    confidence: float = 0.0  # 0..1
    label_seen: str = ""  # e.g. "A+ T1", "1.8.1", "T0" - exactly as printed/shown
    digits_seen: str = ""  # digits as seen, e.g. "0048598" or "19212.9"; '?' = unreadable digit
    problems: list[str] = field(default_factory=list)
    # Second (verification) read, EXTRACTOR_VERIFY: set only when it did NOT reproduce this value - what it
    # showed for the register instead ("" = nothing / the second request failed); the value is then flagged
    # ``low_confidence``. None = verified and reproduced, or not verified. Full comparison: ``raw["verify"]``.
    second_read: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "register": self.register,
            "value": None if self.value is None else str(self.value),
            "confidence": round(float(self.confidence), 3),
            "label_seen": self.label_seen,
            "digits_seen": self.digits_seen,
            "problems": list(self.problems),
        }
        if self.second_read is not None:
            d["second_read"] = self.second_read
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RegisterValue:
        sr = d.get("second_read")
        return cls(
            register=d.get("register"),
            value=_dec(d.get("value")),
            confidence=float(d.get("confidence") or 0.0),
            label_seen=str(d.get("label_seen") or ""),
            digits_seen=str(d.get("digits_seen") or ""),
            problems=[str(p) for p in d.get("problems") or []],
            second_read=None if sr is None else str(sr),
        )


@dataclass
class MeterReadout:
    """One meter recognised on a photo (a photo may show several)."""

    serial: str | None
    serial_confidence: float = 0.0
    registers: list[RegisterValue] = field(default_factory=list)
    kind: str = ""  # lcd | drum | other (as reported by the model)
    # Filled in by matching (not by the model):
    meter_id: int | None = None
    match_method: str | None = None  # exact | fuzzy | values | walk | manual | None
    match_candidates: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "serial_confidence": round(float(self.serial_confidence), 3),
            "kind": self.kind,
            "registers": [r.to_dict() for r in self.registers],
            "meter_id": self.meter_id,
            "match_method": self.match_method,
            "match_candidates": list(self.match_candidates),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MeterReadout:
        return cls(
            serial=d.get("serial"),
            serial_confidence=float(d.get("serial_confidence") or 0.0),
            registers=[RegisterValue.from_dict(r) for r in d.get("registers") or []],
            kind=str(d.get("kind") or ""),
            meter_id=d.get("meter_id"),
            match_method=d.get("match_method"),
            match_candidates=[int(x) for x in d.get("match_candidates") or []],
        )


@dataclass
class ExtractionResult:
    meters: list[MeterReadout] = field(default_factory=list)
    readable: bool = False
    problems: list[str] = field(default_factory=list)
    raw: Any = None  # raw model output (text / parsed JSON) for debugging
    model: str = ""
    latency_ms: int | None = None
    error: str | None = None  # set when the extractor failed (photo stays usable for manual entry)

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "readable": self.readable,
            "problems": list(self.problems),
            "meters": [m.to_dict() for m in self.meters],
            "raw": self.raw,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ExtractionResult | None:
        if not d:
            return None
        return cls(
            meters=[MeterReadout.from_dict(m) for m in d.get("meters") or []],
            readable=bool(d.get("readable")),
            problems=[str(p) for p in d.get("problems") or []],
            raw=d.get("raw"),
            model=str(d.get("model") or ""),
            latency_ms=d.get("latency_ms"),
            error=d.get("error"),
        )


@dataclass(frozen=True)
class RegisterHint:
    register: str
    last_value: Decimal | None = None
    last_date: date | None = None
    low: Decimal | None = None
    expected: Decimal | None = None
    high: Decimal | None = None


@dataclass(frozen=True)
class MeterHintIn:
    """What the extractor (and matching) may know about a meter."""

    meter_id: int
    serial: str
    point_name: str = ""
    registers: tuple[str, ...] = ("T1",)
    digits: int = 5
    decimals_shown: int = 0
    display_note: str = ""
    note: str = ""
    sort_order: int = 0
    meter_point_id: int | None = None
    installed_on: date | None = None
    removed_on: date | None = None
    register_hints: tuple[RegisterHint, ...] = ()

    def reg(self, register: str) -> RegisterHint | None:
        for r in self.register_hints:
            if r.register == register:
                return r
        return None

    def active_at(self, at: date | datetime | None) -> bool:
        if at is None:
            return self.removed_on is None
        d = at.date() if isinstance(at, datetime) else at
        if self.installed_on and d < self.installed_on:
            return False
        return not (self.removed_on and d > self.removed_on)


@dataclass(frozen=True)
class ExtractionHints:
    meters: tuple[MeterHintIn, ...] = ()
    taken_at: datetime | None = None
    # When the photo was taken at a known point (walk), extraction may focus on these meters.
    focus_meter_ids: tuple[int, ...] = ()
    include_values: bool = True  # include last/expected values in the prompt
    # sha256 of the stored original (the extractor gets a downscaled copy); used by logs / the fake
    image_sha256: str | None = None

    def by_id(self) -> dict[int, MeterHintIn]:
        return {m.meter_id: m for m in self.meters}


class ReadingExtractor(Protocol):
    """``extract`` must never raise: failures are reported via ``ExtractionResult.error``."""

    name: str

    def extract(self, image_bytes: bytes, hints: ExtractionHints) -> ExtractionResult: ...
