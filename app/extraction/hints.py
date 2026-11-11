"""Extractor/matching hints built from the DB (known meters, last readings, expected ranges)."""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy.orm import Session

from app.domain.anomalies import expected_range
from app.extraction.types import ExtractionHints, MeterHintIn, RegisterHint
from app.services.network import Network, load_network


def meter_hint_list(db: Session, at: datetime | None = None, *, net: Network | None = None) -> list[MeterHintIn]:
    """All meters (incl. removed ones, with their validity) with register hints at ``at``.

    Only confirmed readings from days *before* ``at``'s day are used, so a photo
    processed later never gets a hint derived from its own (or a later) reading.
    """
    net = net or load_network(db)
    at = at or datetime.now()
    day_start = datetime.combine(at.date(), time.min)
    out: list[MeterHintIn] = []
    for p in net.points:
        for mid in p.meter_ids:
            m = net.meters[mid]
            regs: list[RegisterHint] = []
            for reg in m.registers:
                series = [r for r in net.series[mid].readings.get(reg, ()) if r.taken_at < day_start]
                if not series:
                    regs.append(RegisterHint(reg))
                    continue
                last = series[-1]
                er = expected_range(series, at, m.digits)
                regs.append(
                    RegisterHint(
                        register=reg,
                        last_value=last.value,
                        last_date=last.taken_at.date(),
                        low=er.low if er else last.value,
                        expected=er.expected if er else None,
                        high=er.high if er else None,
                    )
                )
            out.append(
                MeterHintIn(
                    meter_id=mid,
                    serial=m.serial,
                    point_name=p.name,
                    registers=tuple(m.registers),
                    digits=m.digits,
                    decimals_shown=m.decimals_shown,
                    display_note=m.display_note,
                    note=m.note,
                    sort_order=p.sort_order,
                    meter_point_id=p.id,
                    installed_on=m.installed_on,
                    removed_on=m.removed_on,
                    register_hints=tuple(regs),
                )
            )
    return out


def build_hints(
    db: Session,
    at: datetime | None = None,
    *,
    focus_meter_ids: tuple[int, ...] = (),
    include_values: bool = True,
    net: Network | None = None,
) -> ExtractionHints:
    """Hints for the extractor: meters active at ``at`` (point active) in walk order."""
    net = net or load_network(db)
    active_points = {p.id for p in net.points if p.active}
    meters = [
        m
        for m in meter_hint_list(db, at, net=net)
        if m.active_at(at) and (m.meter_point_id in active_points or m.meter_id in focus_meter_ids)
    ]
    return ExtractionHints(
        meters=tuple(meters), taken_at=at, focus_meter_ids=focus_meter_ids, include_values=include_values
    )
