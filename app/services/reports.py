"""Read-only helpers for the overview / settlement pages (reports phase).

Built on top of the foundation services; nothing here writes to the DB.
See docs/SERVICES.md ("Reports helpers").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.audit import jsonable
from app.domain.aligned import QZERO, BoundaryValue, Quality, QValue, add_months, month_start, point_consumption
from app.domain.allocation import validity_window
from app.services.consumption import REGISTERS
from app.services.network import Network
from app.services.periods import MONTHS_CS
from app.services.settlement import SettlementResult, settlement_snapshot
from app.templating import format_date, format_number, quality_title

QUALITY_SHORT = {
    Quality.EXACT.value: "přesně",
    Quality.ESTIMATED.value: "odhad",
    Quality.PROVISIONAL.value: "předběžně",
}
QUALITY_LEGEND = (
    ("přesně", "hodnota z odečtů na hranicích období"),
    ("≈ odhad", "stav na hranici dopočten interpolací mezi dvěma skutečnými odečty"),
    ("≈? předběžně", "stav na hranici extrapolován – pozdější odečet zatím chybí, hodnota se ještě změní"),
)
SNAPSHOT_EXT_VERSION = 1  # version of the reports-side extension keys (doc["reports"])


# --- data coverage --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Coverage:
    """How far the data reaches.

    ``end`` is the last calendar-month boundary covered by the main meter's readings
    (the month start on or before its latest reading); periods ending after it would be
    extrapolated (``provisional``) for the main meter and thus for the remainder tenant.
    """

    first: date | None  # month start of the first reading
    last_reading: datetime | None  # latest confirmed reading of any meter
    last_main_reading: datetime | None  # latest confirmed reading of the main point(s)
    end: date | None

    @property
    def last_month(self) -> tuple[date, date] | None:
        """(start, end) of the last fully covered calendar month."""
        if self.end is None or self.first is None or self.end <= self.first:
            return None
        return add_months(self.end, -1), self.end


def data_coverage(net: Network) -> Coverage:
    def latest(meter_ids: Sequence[int]) -> datetime | None:
        ts = [rs[-1].taken_at for mid in meter_ids for rs in net.series[mid].readings.values() if rs]
        return max(ts) if ts else None

    all_ids = list(net.series)
    firsts = [rs[0].taken_at for s in net.series.values() for rs in s.readings.values() if rs]
    last = latest(all_ids)
    main_ids = [mid for pid in net.main_point_ids for mid in net.points_by_id[pid].meter_ids]
    last_main = latest(main_ids) if main_ids else None
    ref = last_main or last
    return Coverage(
        first=month_start(min(firsts).date()) if firsts else None,
        last_reading=last,
        last_main_reading=last_main,
        end=month_start(ref.date()) if ref else None,
    )


def year_range(
    year: int, cov: Coverage, *, include_provisional: bool = False, today: date | None = None
) -> tuple[date, date] | None:
    """Aligned monthly range for ``year``: Jan 1 up to the coverage end (or up to the current month
    start when ``include_provisional``); None when the year has no covered month."""
    if cov.first is None:
        return None
    start = max(date(year, 1, 1), cov.first)
    cap = month_start(today or date.today()) if include_provisional else cov.end
    if cap is None:
        return None
    end = min(date(year + 1, 1, 1), max(cap, cov.end or cap))
    if end <= start:
        return None
    return start, end


def data_years(cov: Coverage, today: date | None = None) -> list[int]:
    if cov.first is None or cov.last_reading is None:
        return []
    last = max(cov.last_reading.year, (today or date.today()).year)
    return list(range(cov.first.year, last + 1))


# --- value helpers -------------------------------------------------------------------


def qjson(q: QValue | None, decimals: int = 1) -> dict[str, Any] | None:
    """Compact JSON for charts: value, quality and the provenance tooltip."""
    if q is None:
        return None
    return {"v": float(round(q.value, decimals)), "q": q.quality.value, "t": quality_title(q)}


def qdict(q: QValue) -> dict[str, Any]:
    """Serializable QValue incl. human provenance (stored in settlement snapshots)."""
    return {
        "value": q.value,
        "quality": q.quality.value,
        "estimated_kwh": q.estimated_kwh,
        "title": quality_title(q),
    }


def boundary_title(b: BoundaryValue) -> str:
    """Czech description of how a counter state at a boundary was obtained."""
    when = format_date(b.t)
    if b.method == "reading" and b.before is not None:
        return f"odečet {format_date(b.before.taken_at)}"
    if b.method == "interpolated" and b.before is not None and b.after is not None:
        return (
            f"odhad k {when}: interpolace mezi odečty {format_date(b.before.taken_at)} "
            f"a {format_date(b.after.taken_at)} ({format_number(b.span_days)} dní)"
        )
    if b.method == "extrapolated" and b.before is not None:
        return (
            f"předběžné k {when}: extrapolace z průměrné spotřeby, poslední odečet "
            f"{format_date(b.before.taken_at)} ({format_number(b.span_days)} dní před hranicí)"
        )
    if b.method == "before_first":
        if b.after is not None:
            return f"před prvním odečtem ({format_date(b.after.taken_at)}) – spotřeba 0"
        return "před instalací – spotřeba 0"
    if b.method == "after_removal":
        if b.quality == Quality.ESTIMATED and b.before is not None:
            return (
                f"odhad: elektroměr odebrán bez konečného odečtu – spotřeba od posledního odečtu "
                f"{format_date(b.before.taken_at)} do demontáže dopočtena ({format_number(b.span_days)} dní)"
            )
        return "po demontáži elektroměru – beze změny"
    if b.method == "no_data":
        return "bez odečtů"
    return QUALITY_SHORT.get(b.quality.value, "")


# --- settlement document ----------------------------------------------------------------


def _tenant_allocation_rows(result: SettlementResult, net: Network, tenant_id: int) -> list[dict[str, Any]]:
    t0, t1 = result.interval.start, result.interval.end
    rows = []
    for r in net.rules:
        if r.tenant_id != tenant_id:
            continue
        win = validity_window(r, t0, t1)
        if win is None:
            continue
        regs = net.point_registers(r.meter_point_id)
        if win == (t0, t1):
            vals = {reg: result.interval.points[r.meter_point_id].get(reg, QZERO) for reg in regs}
        else:
            vals = point_consumption(net.point_meters(r.meter_point_id), win[0], win[1], registers=regs)
        vals = {reg: vals.get(reg, QZERO).scaled(r.factor) for reg in REGISTERS}
        total = vals["T1"] + vals["T2"]
        rows.append(
            {
                "point_id": r.meter_point_id,
                "point_name": net.points_by_id[r.meter_point_id].name,
                "factor": r.factor,
                "valid_from": r.valid_from,
                "valid_to": r.valid_to,
                "window": [win[0], win[1]],
                "partial": win != (t0, t1),
                "values": {reg: qdict(v) for reg, v in vals.items()},
                "total": qdict(total),
            }
        )
    return rows


def settlement_document(result: SettlementResult, net: Network) -> dict[str, Any]:
    """Everything the settlement pages / exports render, as JSON-safe data.

    It is the foundation's ``settlement_snapshot`` (version 1) extended with display data
    (provenance texts, tenant colours, per-tenant allocation rows, remainder breakdown,
    meter decimals). Draft settlements render a freshly computed document, finalized ones
    render the stored snapshot (= this document at finalization time).
    """
    doc = settlement_snapshot(result)
    t = result.table
    iv = result.interval
    n = len(result.periods)
    remainder = next((tn for tn in t.tenants if tn.is_remainder), None)
    doc["reports"] = SNAPSHOT_EXT_VERSION
    doc["generated_at"] = datetime.now().replace(microsecond=0)
    for p, span in zip(doc["periods"], result.periods, strict=True):
        p["days"] = span.days
        if span.start.day == 1 and span.end.date() == add_months(span.start_date, 1):
            p["label"] = f"{MONTHS_CS[span.start.month - 1]} {span.start.year}"  # whole calendar month
    for td, tn in zip(doc["tenants"], t.tenants, strict=True):
        td["color"] = tn.color
        td["color_dark"] = tn.color_dark
        td["periods"] = {reg: [qdict(v) for v in t.values[tn.id][reg]] for reg in REGISTERS}
        td["period_totals"] = [qdict(t.total(tn.id, i)) for i in range(n)]
        td["totals"] = {reg: qdict(iv.tenants[tn.id][reg]) for reg in REGISTERS}
        td["total"] = qdict(iv.tenant_total(tn.id))
        td["allocations"] = [] if tn.is_remainder else _tenant_allocation_rows(result, net, tn.id)
    if remainder is not None:
        allocated_points = {
            r.meter_point_id
            for r in net.rules
            if r.tenant_id != remainder.id and validity_window(r, iv.start, iv.end) is not None
        }
        rd = next(td for td in doc["tenants"] if td["id"] == remainder.id)
        rd["remainder"] = {
            "main_points": [{"point_id": pid, "point_name": net.points_by_id[pid].name} for pid in net.main_point_ids],
            "main": {reg: qdict(iv.main.get(reg, QZERO)) for reg in REGISTERS},
            "others": [
                {
                    "tenant_id": tn.id,
                    "name": tn.name,
                    "values": {reg: qdict(iv.tenants[tn.id][reg]) for reg in REGISTERS},
                }
                for tn in t.tenants
                if not tn.is_remainder
            ],
            "unallocated": [
                {
                    "point_id": p.id,
                    "point_name": p.name,
                    "values": {reg: qdict(iv.points[p.id].get(reg, QZERO)) for reg in REGISTERS},
                }
                for p in net.points
                if p.role == "sub" and p.id not in allocated_points and p.meter_ids
            ],
        }
    doc["main"] = {reg: qdict(iv.main.get(reg, QZERO)) for reg in REGISTERS}
    doc["main_total"] = qdict(iv.main.get("T1", QZERO) + iv.main.get("T2", QZERO))
    doc["period_main"] = {reg: [qdict(v) for v in t.main[reg]] for reg in REGISTERS}
    for md, row in zip(doc["meters"], iv.meters, strict=True):
        info = net.meters[row.meter_id]
        md["decimals_shown"] = info.decimals_shown
        md["point_role"] = net.points_by_id[row.meter_point_id].role
        md["source_type"] = info.source_type
        for key, b in (("start", row.start), ("end", row.end)):
            md[key]["span_days"] = b.span_days
            md[key]["title"] = boundary_title(b)
        md["consumption"] = qdict(row.consumption)
    doc["quality"] = iv.quality.value
    return jsonable(doc)


def document_tenant(doc: dict[str, Any], tenant_id: int) -> dict[str, Any] | None:
    return next((td for td in doc.get("tenants", []) if td["id"] == tenant_id), None)


def document_tenant_meters(doc: dict[str, Any], tenant: dict[str, Any]) -> list[dict[str, Any]]:
    """Meter rows of a tenant's protocol: its allocated points, or the main point(s) for the remainder."""
    if tenant.get("is_remainder"):
        pids = [p["point_id"] for p in tenant.get("remainder", {}).get("main_points", [])]
        if not pids:
            pids = [m["point_id"] for m in doc.get("meters", []) if m.get("point_role") == "main"]
    else:
        pids = list(dict.fromkeys(tenant.get("points", [])))
    order = {pid: i for i, pid in enumerate(pids)}
    rows = [m for m in doc.get("meters", []) if m["point_id"] in order]
    return sorted(rows, key=lambda m: (order[m["point_id"]], m["meter_id"], m["register"]))


def d(value: Any) -> Decimal:
    """Decimal from a snapshot value (str / number / None)."""
    if value is None or value == "":
        return Decimal(0)
    return value if isinstance(value, Decimal) else Decimal(str(value))
