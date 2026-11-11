"""Photo series: photos taken within ~60 s of each other form one group (SPEC §5 "Série").

A series is only a *grouping* for the review; one series may contain several meters
(the walker moves between adjacent meters in a few seconds). Proposals inside a series
are split by matched meter in ``app.capture.review``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Photo

SERIES_GAP = timedelta(seconds=60)
WINDOW = timedelta(hours=2)


def group_by_gap(times: Sequence[tuple[int, datetime]], gap: timedelta = SERIES_GAP) -> list[list[int]]:
    """Chain items whose consecutive times differ by <= ``gap``; returns lists of ids in time order."""
    groups: list[list[int]] = []
    last: datetime | None = None
    for pid, t in sorted(times, key=lambda x: (x[1], x[0])):
        if last is None or t - last > gap:
            groups.append([pid])
        else:
            groups[-1].append(pid)
        last = t
    return groups


def regroup_series(db: Session, around: datetime | None) -> None:
    """Recompute ``series_id`` of unreviewed photos near ``around`` (no commit).

    Series ids are deterministic (``s<first photo id>``), so re-running is idempotent.
    Photos without a time form no series.
    """
    if around is None:
        return
    rows = list(
        db.scalars(
            select(Photo).where(
                Photo.review_status == "new",
                Photo.taken_at.is_not(None),
                Photo.taken_at >= around - WINDOW,
                Photo.taken_at <= around + WINDOW,
            )
        )
    )
    by_id = {p.id: p for p in rows}
    for group in group_by_gap([(p.id, p.taken_at) for p in rows if p.taken_at is not None]):
        sid = f"s{group[0]}" if len(group) > 1 else None
        for pid in group:
            if by_id[pid].series_id != sid:
                by_id[pid].series_id = sid
