"""Raw per-day view (secondary): equivalents of sheets `Spotřeba` and `Rozpočítání`.

Rows are the days with at least one confirmed reading (for imported data exactly the sheet's
row dates); a period is ``(previous day, day]``. A meter not read on a day has its whole
consumption attributed to the period of its next reading, like in the sheet. ``None`` = no data.
A late tenant reading on its own date therefore makes its own (thin) row: the raw view shows
what was read when, it does not interpret. Overviews and settlements use the aligned services
in ``app.services.consumption``; this view is oracle-tested against the original spreadsheet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.allocation import AllocationResult, allocate
from app.domain.consumption import Period, PeriodCell, period_consumption, periods_from_dates, sum_optional
from app.services.network import MeterColumn, Network, TenantInfo, load_network
from app.services.periods import reading_dates

REGISTERS = ("T1", "T2")


@dataclass
class RawConsumptionTable:
    periods: list[Period]
    columns: list[MeterColumn]
    cells: list[list[PeriodCell]]  # [period index][column index]

    def cell(self, idx: int, meter_id: int, register: str) -> PeriodCell:
        for j, c in enumerate(self.columns):
            if c.meter_id == meter_id and c.register == register:
                return self.cells[idx][j]
        raise KeyError((meter_id, register))

    def point_value(self, idx: int, point_id: int, register: str) -> Decimal | None:
        """Sum over the point's meters (None when none of them has data)."""
        return sum_optional(
            self.cells[idx][j].value
            for j, c in enumerate(self.columns)
            if c.meter_point_id == point_id and c.register == register
        )


def _filter(periods: list[Period], start: date | None, end: date | None) -> list[int]:
    return [i for i, p in enumerate(periods) if (start is None or p.end >= start) and (end is None or p.end <= end)]


def raw_consumption_table(
    db: Session, start: date | None = None, end: date | None = None, *, net: Network | None = None
) -> RawConsumptionTable:
    """Sheet `Spotřeba`: meters × registers per reading-day period.

    ``start``/``end`` filter periods by their *end* date; calculation always uses
    all readings (so carried consumption stays correct).
    """
    net = net or load_network(db)
    periods = periods_from_dates(reading_dates(db))
    cols = net.columns()
    per_col = [
        period_consumption(net.series[c.meter_id].readings.get(c.register, ()), periods, net.meters[c.meter_id].digits)
        for c in cols
    ]
    keep = _filter(periods, start, end)
    return RawConsumptionTable(
        periods=[periods[i] for i in keep],
        columns=cols,
        cells=[[per_col[j][i] for j in range(len(cols))] for i in keep],
    )


@dataclass
class RawAllocationTable:
    """Sheet `Rozpočítání se sazbou` (split_tariff) / `bez sazby`."""

    periods: list[Period]
    tenants: list[TenantInfo]
    # [period index][tenant_id][register] -> kWh | None; register "total" = T1 + T2
    values: list[dict[int, dict[str, Decimal | None]]]
    results: list[dict[str, AllocationResult]]  # [period index][register]

    def value(self, idx: int, tenant_id: int, register: str = "total") -> Decimal | None:
        return self.values[idx][tenant_id][register]

    def negative_remainders(self) -> list[tuple[int, str]]:
        return [(i, reg) for i, res in enumerate(self.results) for reg, r in res.items() if r.negative_remainder]


def raw_allocation_table(
    db: Session, start: date | None = None, end: date | None = None, *, net: Network | None = None
) -> RawAllocationTable:
    """Tenant allocation per reading-day period, per register and total (T1+T2)."""
    net = net or load_network(db)
    table = raw_consumption_table(db, start, end, net=net)
    values: list[dict[int, dict[str, Decimal | None]]] = []
    results: list[dict[str, AllocationResult]] = []
    for i, p in enumerate(table.periods):
        per_reg: dict[str, AllocationResult] = {}
        for reg in REGISTERS:
            pv = {pt.id: table.point_value(i, pt.id, reg) for pt in net.points}
            per_reg[reg] = allocate(pv, net.rules, net.tenant_refs, net.main_point_ids, p.start, p.end)
        results.append(per_reg)
        row: dict[int, dict[str, Decimal | None]] = {}
        for t in net.tenants:
            t1 = per_reg["T1"].values.get(t.id)
            t2 = per_reg["T2"].values.get(t.id)
            row[t.id] = {"T1": t1, "T2": t2, "total": sum_optional([t1, t2])}
        values.append(row)
    return RawAllocationTable(table.periods, list(net.tenants), values, results)
