"""Reading extraction from meter photos (SPEC §5 "Extrakce z fotek").

- ``types``: ``ReadingExtractor`` protocol, ``ExtractionResult`` / ``MeterReadout`` / ``RegisterValue``, hints
- ``extractors``: ``none``, ``fake``, ``openai_compat`` (+ ``make_extractor(settings)``)
- ``prompt``: prompt text and robust parsing of model answers
- ``images``: upload validation, EXIF time, orientation, downscaling, file-name date suggestions
- ``hints``: hints from the DB; ``matching``: serial / fuzzy / value matching
- ``queue``: persistent background queue (Photo.extraction_status)
- ``eval``: offline CLI ``python -m app.extraction.eval <dir|zip>``
"""

from app.extraction.extractors import FakeExtractor, NoneExtractor, OpenAICompatExtractor, make_extractor
from app.extraction.types import (
    ExtractionHints,
    ExtractionResult,
    MeterHintIn,
    MeterReadout,
    ReadingExtractor,
    RegisterHint,
    RegisterValue,
)

__all__ = [
    "ExtractionHints",
    "ExtractionResult",
    "FakeExtractor",
    "MeterHintIn",
    "MeterReadout",
    "NoneExtractor",
    "OpenAICompatExtractor",
    "ReadingExtractor",
    "RegisterHint",
    "RegisterValue",
    "make_extractor",
]
