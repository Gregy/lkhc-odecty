"""Live reading sources (interface only; SPEC §5 "Live zdroje").

A source fetches readings of a meter with ``source_type == "live"`` from an
external system (e.g. the distributor's portal for the main meter). No
implementation or scheduler exists in the MVP.

Adding an integration:

1. Implement the ``ReadingSource`` protocol in ``app/sources/<name>.py``::

       class DistributorPortalSource:
           kind = "distributor_portal"

           def fetch(self, meter: Meter, since: datetime | None) -> list[Reading]:
               cfg = meter.source_config or {}   # e.g. {"kind": "distributor_portal", "ean": "..."}
               ...  # call the API, map values to Reading(meter_id=meter.id, register="T1"/"T2",
                    #   taken_at=<real time, naive local>, value=Decimal(...), source="live",
                    #   status="confirmed")

2. Register it: ``register_source(DistributorPortalSource())``.
3. Configure the meter: ``source_type = "live"``, ``source_config = {"kind": "distributor_portal", ...}``.
4. A future scheduler calls ``get_source(meter).fetch(meter, since=<last reading time>)``
   and inserts the returned readings (unique on meter/register/taken_at). Live
   readings may have any timestamp; aligned calculations handle arbitrary times.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.models import Meter, Reading


@runtime_checkable
class ReadingSource(Protocol):
    kind: str  # matches meter.source_config["kind"]

    def fetch(self, meter: Meter, since: datetime | None) -> list[Reading]:
        """Return new (unsaved) readings of ``meter`` taken after ``since``."""
        ...


class SourceNotConfiguredError(LookupError):
    pass


_REGISTRY: dict[str, ReadingSource] = {}


def register_source(source: ReadingSource) -> None:
    _REGISTRY[source.kind] = source


def unregister_source(kind: str) -> None:
    _REGISTRY.pop(kind, None)


def available_sources() -> list[str]:
    return sorted(_REGISTRY)


def get_source(meter: Meter) -> ReadingSource:
    """Source for a live meter, chosen by ``meter.source_config["kind"]``."""
    if meter.source_type != "live":
        raise SourceNotConfiguredError(f"meter {meter.serial} is not a live meter")
    kind = (meter.source_config or {}).get("kind")
    if not kind or kind not in _REGISTRY:
        raise SourceNotConfiguredError(f"no reading source registered for kind {kind!r} (meter {meter.serial})")
    return _REGISTRY[kind]
