"""Plausibility of a reading being written: domain checks + decimal-slip detection.

Every issue (warning *or* error) must be acknowledged explicitly by the user before the
value is written as ``confirmed`` (acknowledged codes are recorded in the audit note).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.anomalies import PlausibilityIssue
from app.formatting import format_date
from app.models import Meter
from app.services.readings import check_new_reading, meter_expected_range
from app.templating import format_number


@dataclass(frozen=True)
class Issue:
    code: str  # decrease | jump | high | magnitude | before_last | decimal_slip | duplicate | inactive | ...
    severity: str  # error | warning
    message: str
    link: str = ""  # where the cause can be fixed (admin page), optional
    link_label: str = ""

    @classmethod
    def of(cls, p: PlausibilityIssue) -> Issue:
        return cls(p.code, p.severity, p.message)

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "link": self.link,
            "link_label": self.link_label,
        }


def decimal_slip(value: Decimal, decimals_shown: int, low: Decimal | None, high: Decimal | None) -> Issue | None:
    """Value outside the plausible range while value × 10^±k (k = 1..3) is inside it."""
    if low is None or high is None or high < low:
        return None
    if low <= value <= high:
        return None
    for k in (1, 2, 3):
        for cand, how in ((value.scaleb(-k), "menší"), (value.scaleb(k), "větší")):
            if low <= cand <= high:
                dec = max(decimals_shown, k if how == "menší" else 0)
                hint = f" Displej ukazuje {decimals_shown} desetinná místa." if decimals_shown else ""
                return Issue(
                    "decimal_slip",
                    "warning",
                    f"Vypadá to na posunutou desetinnou čárku: hodnota {10**k}× {how} "
                    f"({format_number(cand, dec)}) by odpovídala očekávání "
                    f"{format_number(low)}–{format_number(high)} kWh.{hint}",
                )
    return None


def check_value(
    db: Session,
    meter: Meter,
    register: str,
    value: Decimal,
    at: date | datetime,
    *,
    exclude_reading_id: int | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    at_d = at.date() if isinstance(at, datetime) else at
    if register not in (meter.registers or ["T1"]):
        issues.append(Issue("register", "error", f"Elektroměr {meter.serial} nemá registr {register}."))
    if meter.installed_on and at_d < meter.installed_on:
        issues.append(
            Issue("inactive", "warning", f"Datum je před instalací elektroměru ({format_date(meter.installed_on)}).")
        )
    if meter.removed_on and at_d > meter.removed_on:
        issues.append(Issue("inactive", "warning", f"Elektroměr byl odebrán {format_date(meter.removed_on)}."))
    domain = check_new_reading(db, meter.id, register, value, at, exclude_reading_id=exclude_reading_id)
    issues.extend(Issue.of(p) for p in domain)
    er = meter_expected_range(db, meter.id, register, at_d)
    if er is not None and er.high is not None and er.last.taken_at.date() < at_d:
        slip = decimal_slip(value, meter.decimals_shown, er.low, er.high)
        if slip is not None:
            # imported meters all start with decimals_shown = 0: point the admin to the meter settings
            issues.append(
                replace(
                    slip,
                    message=slip.message
                    + (
                        " Pokud displej tohoto elektroměru ukazuje desetinná místa, nastavte je ve Správě"
                        " (pole „Desetinná místa na displeji“)."
                        if not meter.decimals_shown
                        else ""
                    ),
                    link=f"/sprava/elektromery/{meter.id}",
                    link_label=f"Nastavení elektroměru {meter.serial}",
                )
            )
    return issues


def missing_acks(issues: list[Issue], acks: set[str]) -> list[Issue]:
    return [i for i in issues if i.code not in acks]


def ack_note(issues: list[Issue]) -> str:
    if not issues:
        return ""
    return "Potvrzená varování: " + "; ".join(f"[{i.code}] {i.message}" for i in issues)
