"""Move all readings (and point skips) of one day to another day – e.g. a confirmed date typo.

    python -m app.tools.move_reading_date --from 2026-06-03 --to 2026-07-03 [--dry-run] [--no-backup]
    docker compose exec app python -m app.tools.move_reading_date --from ... --to ...

- Moves every reading taken on ``--from`` (confirmed and pending, any source; the time of day is kept)
  and the "skipped" marks of meter points on that day. Photos keep their real EXIF time.
- Refuses (nothing changes) when the target day already has a reading of the same meter/register or
  a skip of the same point – the unique constraints would clash and it has to be merged by hand.
- Before writing: a consistent snapshot ``DATA_DIR/backups/pre-move-<from>-<to>-<timestamp>.db``.
- Every change is audited (per reading/skip + one summary entry, user ``cli:move_reading_date``).
- Finalized settlements are not touched (they keep their snapshot); drafts recompute.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.backup import snapshot_db, sqlite_path
from app.config import get_settings
from app.db import make_engine, make_sessionmaker
from app.models import PointSkip, Reading, Settlement

AUDIT_USER = "cli:move_reading_date"


class MoveRefusedError(RuntimeError):
    pass


@dataclass
class MoveResult:
    readings: int = 0
    skips: int = 0
    finalized_settlements: list[str] = field(default_factory=list)


def _day_range(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min)
    return start, start + timedelta(days=1)


def move_reading_date(db: Session, src: date, dst: date, *, note: str = "") -> MoveResult:
    """Move readings + point skips of ``src`` to ``dst`` (no commit). Raises MoveRefusedError on conflicts."""
    if src == dst:
        raise MoveRefusedError("zdrojové a cílové datum jsou stejné")
    shift = datetime.combine(dst, time.min) - datetime.combine(src, time.min)
    a, b = _day_range(src)
    readings = list(db.scalars(select(Reading).where(Reading.taken_at >= a, Reading.taken_at < b)))
    skips = list(db.scalars(select(PointSkip).where(PointSkip.day == src)))
    if not readings and not skips:
        raise MoveRefusedError(f"k {src.isoformat()} nejsou žádné odečty ani přeskočená místa")
    conflicts = []
    for r in readings:
        target = r.taken_at + shift
        other = db.scalar(
            select(Reading).where(
                Reading.meter_id == r.meter_id, Reading.register == r.register, Reading.taken_at == target
            )
        )
        if other is not None:
            conflicts.append(f"elektroměr #{r.meter_id} {r.register}: k {target:%d.%m.%Y} už je odečet {other.value}")
    for sk in skips:
        if db.scalar(select(PointSkip).where(PointSkip.day == dst, PointSkip.meter_point_id == sk.meter_point_id)):
            conflicts.append(f"místo #{sk.meter_point_id}: k {dst.isoformat()} už je označené jako přeskočené")
    if conflicts:
        raise MoveRefusedError("cílové datum koliduje:\n  " + "\n  ".join(conflicts))

    why = note or f"přesun data {src.isoformat()} → {dst.isoformat()}"
    res = MoveResult()
    for r in readings:
        changes = audit.apply_changes(r, {"taken_at": r.taken_at + shift})
        audit.record_update(db, None, r, changes, note=why, user_email=AUDIT_USER)
        res.readings += 1
    for sk in skips:
        changes = audit.apply_changes(sk, {"day": dst})
        audit.record_update(db, None, sk, changes, note=why, user_email=AUDIT_USER)
        res.skips += 1
    lo, hi = min(src, dst), max(src, dst)
    for s in db.scalars(select(Settlement).where(Settlement.status == "final")):
        if s.period_start <= hi and s.period_end >= lo:
            res.finalized_settlements.append(f"{s.name} ({s.period_start}–{s.period_end})")
    audit.record(
        db,
        None,
        "move_date",
        "readings",
        None,
        {"from": src, "to": dst, "readings": res.readings, "skips": res.skips},
        why,
        user_email=AUDIT_USER,
    )
    db.flush()
    return res


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.tools.move_reading_date", description=__doc__.split("\n")[0])
    p.add_argument("--from", dest="src", required=True, type=date.fromisoformat, help="původní datum (YYYY-MM-DD)")
    p.add_argument("--to", dest="dst", required=True, type=date.fromisoformat, help="správné datum (YYYY-MM-DD)")
    p.add_argument("--note", default="", help="důvod (uloží se do auditu)")
    p.add_argument("--database-url", help="přepíše DATABASE_URL / DATA_DIR")
    p.add_argument("--dry-run", action="store_true", help="jen zkontrolovat a vypsat, nic nezapsat")
    p.add_argument("--no-backup", action="store_true", help="bez zálohy před zápisem (nedoporučeno)")
    args = p.parse_args(argv)

    url = args.database_url or get_settings().sqlalchemy_url
    engine = make_engine(url)
    try:
        with make_sessionmaker(engine)() as db:
            try:
                res = move_reading_date(db, args.src, args.dst, note=args.note)
            except MoveRefusedError as exc:
                print(f"Odmítnuto: {exc}", file=sys.stderr)
                return 1
            if args.dry_run:
                db.rollback()
                print(f"Nanečisto: přesunulo by se {res.readings} odečtů a {res.skips} přeskočených míst.")
                return 0
            db.rollback()  # take the backup before writing anything
            path = sqlite_path(url)
            if path is not None and not args.no_backup:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                snap = snapshot_db(
                    Path(path), Path(path).parent / "backups" / f"pre-move-{args.src}-{args.dst}-{stamp}.db"
                )
                print(f"Záloha: {snap}")
            res = move_reading_date(db, args.src, args.dst, note=args.note)
            db.commit()
    finally:
        engine.dispose()
    print(f"Přesunuto odečtů: {res.readings}, přeskočených míst: {res.skips} ({args.src} → {args.dst}).")
    for s in res.finalized_settlements:
        print(f"Pozor: finalizované vyúčtování {s} se nemění (snapshot); zvažte znovuotevření.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
