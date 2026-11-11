"""Alerts for the dashboard: stale meters, anomalies, negative remainder, control balance, pending review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.aligned import add_months, month_start
from app.formatting import NBSP, format_date, format_number, is_marked
from app.models import Photo, Reading
from app.services.consumption import control_balances, tenant_consumption_table
from app.services.network import Network, load_network
from app.services.readings import meter_anomalies, meter_staleness

SEVERITIES = ("info", "warning", "error")


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str  # stale_meter | anomaly | negative_remainder | control_balance | pending_photos | pending_readings
    severity: str  # info | warning | error
    message: str  # Czech, user facing
    meter_id: int | None = None
    point_id: int | None = None
    tenant_id: int | None = None
    at: datetime | None = None
    link: str | None = None


def alerts(
    db: Session,
    now: datetime | None = None,
    *,
    months_back: int = 12,
    anomaly_days: int = 365,
    net: Network | None = None,
) -> list[Alert]:
    """All current alerts, most severe first.

    Negative remainders (on T1+T2 of the remainder tenant) and control balances are evaluated on aligned
    monthly periods of the last ``months_back`` complete months that have any data.
    """
    net = net or load_network(db)
    now = now or datetime.now()
    out: list[Alert] = []

    for s in meter_staleness(db, now, net=net):
        st = s.staleness
        if not st.overdue:
            continue
        if st.last is None:
            msg = f"{s.point_name} ({s.meter.serial}) {s.register}: zatím žádný odečet."
        else:
            msg = (
                f"{s.point_name} ({s.meter.serial}) {s.register}: {int(st.days_since or 0)} dní od posledního odečtu "
                f"(obvykle ~{int(st.usual_interval_days)} dní)."
            )
        out.append(
            Alert(
                "stale_meter",
                "warning",
                msg,
                s.meter.id,
                s.meter.meter_point_id,
                at=st.last.taken_at if st.last else None,
                link=f"/mista/{s.meter.meter_point_id}",
            )
        )

    for a in meter_anomalies(db, since=now - timedelta(days=anomaly_days), net=net):
        sev = "error" if a.anomaly.kind in ("decrease", "jump") else "warning"
        out.append(
            Alert(
                "anomaly",
                sev,
                f"{a.point_name} ({a.meter.serial}) {a.register}, "
                f"{format_date(a.anomaly.curr.taken_at)}: {a.anomaly.message}",
                a.meter.id,
                a.meter.meter_point_id,
                at=a.anomaly.curr.taken_at,
                link=f"/mista/{a.meter.meter_point_id}",
            )
        )

    last = db.scalar(select(func.max(Reading.taken_at)).where(Reading.status == "confirmed"))
    if last is not None:
        end = min(month_start(last.date()), month_start(now.date()))
        start = add_months(end, -months_back)
        if start < end:
            table = tenant_consumption_table(db, start=start, end=end, net=net)
            rem = next((t for t in table.tenants if t.is_remainder), None)
            # Evaluated on T1+T2: single-tariff sub-meters count fully into T1, so the remainder's T1 alone is
            # too low (even negative in winter) and its T2 too high - only the sum is meaningful.
            for i in range(len(table.periods)):
                if rem is None:
                    break
                v = table.total(rem.id, i)
                if v.value >= 0:
                    continue
                out.append(
                    Alert(
                        "negative_remainder",
                        "warning",
                        f"Záporný zbytek {rem.name} (T1+T2) v období {table.periods[i].label}: "
                        f"{format_number(v.value)}{NBSP}kWh"
                        f"{' (předběžně – extrapolace za posledním odečtem)' if is_marked(v.quality) else ''}.",
                        tenant_id=rem.id,
                        at=table.periods[i].start,
                        link="/spotreba",
                    )
                )
            names = {p.id: p.name for p in net.points}
            for row in control_balances(table.points):
                if row.difference.value < 0:
                    out.append(
                        Alert(
                            "control_balance",
                            "warning",
                            f"Bilance {names[row.control_point_id]} {row.register} za posledních {months_back} měs.: "
                            f"podružné ({row.children.value:.0f} kWh) převyšují "
                            f"kontrolní měřák ({row.control.value:.0f} kWh).",
                            point_id=row.control_point_id,
                            link=f"/mista/{row.control_point_id}",
                        )
                    )

    n_photos = db.scalar(select(func.count(Photo.id)).where(Photo.review_status == "new")) or 0
    if n_photos:
        out.append(Alert("pending_photos", "info", f"Fotky čekající na kontrolu: {n_photos}.", link="/odecty/kontrola"))
    n_pending = db.scalar(select(func.count(Reading.id)).where(Reading.status == "pending")) or 0
    if n_pending:
        out.append(Alert("pending_readings", "info", f"Nepotvrzené odečty: {n_pending}.", link="/odecty/kontrola"))

    rank = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(out, key=lambda a: (-rank[a.severity], -(a.at.toordinal() if a.at else 0)))


@dataclass(frozen=True, slots=True)
class LastReadingInfo:
    """For the dashboard header: newest confirmed reading (any meter) and newest main-meter reading."""

    newest: date | None
    newest_days: int | None
    main: date | None
    main_days: int | None


def last_reading_info(db: Session, today: date | None = None) -> LastReadingInfo:
    from app.services.periods import main_reading_dates

    today = today or date.today()
    newest = db.scalar(select(func.max(Reading.taken_at)).where(Reading.status == "confirmed"))
    mains = main_reading_dates(db)
    n = newest.date() if newest else None
    m = mains[-1] if mains else None
    return LastReadingInfo(n, (today - n).days if n else None, m, (today - m).days if m else None)
