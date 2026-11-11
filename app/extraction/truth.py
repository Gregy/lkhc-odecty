"""Ground truth for the extraction evaluation, built from confirmed readings of one day.

    uv run python -m app.extraction.truth 2026-03-01 --out private/truth-2026-03-01.json

Writes ``{"kind": "meter_truth", "date": ..., "meters": {"<DB serial>": {"point": ..., "T1": "12345", ...}}}``.
The sheet holds whole kWh truncated from displays that show decimals, so the eval treats
"equal after truncating to whole kWh" as correct (reported separately from exact matches).
Keep the file in the private dir (``private/``, git/docker-ignored; see ``app.private``) – it holds real data.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Meter, Reading

KIND = "meter_truth"


def build_truth(db: Session, day: date) -> dict[str, Any]:
    start = datetime.combine(day, time.min)
    q = (
        select(Reading, Meter)
        .join(Meter, Meter.id == Reading.meter_id)
        .where(Reading.status == "confirmed", Reading.taken_at >= start, Reading.taken_at < start + timedelta(days=1))
        .order_by(Meter.id, Reading.register)
    )
    meters: dict[str, dict[str, Any]] = {}
    for r, m in db.execute(q):
        e = meters.setdefault(
            m.serial, {"point": m.meter_point.name, "decimals_shown": m.decimals_shown, "sources": []}
        )
        e[r.register] = format(r.value.normalize(), "f")
        if r.source not in e["sources"]:
            e["sources"].append(r.source)
    return {
        "kind": KIND,
        "date": day.isoformat(),
        "note": "Confirmed readings of this day from the app DB. Imported (sheet) values are whole kWh "
        "truncated from the display: a read value that truncates to them counts as correct.",
        "meters": meters,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.extraction.truth", description=__doc__.split("\n\n")[0])
    ap.add_argument("day", type=date.fromisoformat, help="YYYY-MM-DD")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--database-url", help="default: DATABASE_URL / DATA_DIR")
    args = ap.parse_args(argv)
    from app.config import get_settings
    from app.db import make_engine, make_sessionmaker

    engine = make_engine(args.database_url or get_settings().sqlalchemy_url)
    with make_sessionmaker(engine)() as db:
        truth = build_truth(db, args.day)
    engine.dispose()
    if not truth["meters"]:
        print(f"Pro {args.day} nejsou v databázi žádné potvrzené odečty.", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.out}: {len(truth['meters'])} elektroměrů k {args.day}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
