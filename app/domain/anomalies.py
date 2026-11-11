"""Anomaly detection and plausibility checks for readings (pure, no DB)."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from statistics import median

from app.domain.consumption import (
    ANOMALY_DECREASE,
    ANOMALY_JUMP,
    QUANT,
    ZERO,
    ReadingPoint,
    as_datetime,
    counter_range,
    register_delta,
    segments,
    sort_series,
)
from app.formatting import NBSP, format_number, format_reading

ANOMALY_SPIKE = "spike"  # daily usage much higher than usual

DEFAULT_SPIKE_FACTOR = Decimal(3)
DEFAULT_MIN_SPIKE_KWH = Decimal(20)  # ignore spikes smaller than this (absolute excess)
DEFAULT_LOOKBACK_DAYS = 365
MIN_SEGMENTS_FOR_RATE = 3
# Daily rates of very short segments are noise (two readings a day apart, or a date typo like
# 1.6. / 3.6. instead of 3.7.): they neither raise a spike nor enter the "usual rate" history.
MIN_SPIKE_SEGMENT_DAYS = 7


@dataclass(frozen=True, slots=True)
class Anomaly:
    kind: str  # decrease | jump | spike
    prev: ReadingPoint
    curr: ReadingPoint
    delta: Decimal
    daily_rate: Decimal | None = None  # rate of this segment (kWh/day)
    typical_rate: Decimal | None = None  # median rate of earlier segments

    @property
    def message(self) -> str:
        if self.kind == ANOMALY_DECREASE:
            return f"Stav klesl o {_kwh(-self.delta)} (nejde o přetečení počitadla)."
        if self.kind == ANOMALY_JUMP:
            return f"Nárůst o {_kwh(self.delta)} je větší než polovina rozsahu počitadla."
        return (
            f"Neobvykle vysoká spotřeba: {format_number(self.daily_rate, 1)}{NBSP}kWh/den, "
            f"obvykle {format_number(self.typical_rate, 1)}{NBSP}kWh/den."
        )


def _kwh(v: Decimal) -> str:
    """A counter value / kWh amount in a Czech message (cs-CZ, decimals only when present)."""
    return f"{format_reading(v)}{NBSP}kWh"


def _date(t: datetime) -> str:
    return f"{t.day}.{t.month}.{t.year}"


def _days(v: Decimal) -> str:
    return format_number(v, 0 if v >= 10 else 1)


def daily_rates(readings: Sequence[ReadingPoint], digits: int = 5) -> list[tuple[ReadingPoint, ReadingPoint, Decimal]]:
    """(prev, curr, kWh/day) for non-anomalous segments with positive duration."""
    out = []
    for g in segments(readings, digits):
        if g.delta.anomaly or g.days <= 0:
            continue
        out.append((g.prev, g.curr, g.delta.value / g.days))
    return out


def typical_daily_rate(
    readings: Sequence[ReadingPoint],
    digits: int = 5,
    at: date | datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Decimal | None:
    """Duration-weighted mean kWh/day over segments ending within ``lookback_days`` before ``at``.

    Falls back to all history when the lookback window has fewer than
    ``MIN_SEGMENTS_FOR_RATE`` segments. None if there is no usable segment.
    """
    s = sort_series(readings)
    if at is not None:
        at_dt = as_datetime(at)
        s = [r for r in s if r.taken_at <= at_dt]
    segs = [g for g in segments(s, digits) if not g.delta.anomaly and g.days > 0]
    if not segs:
        return None
    end = s[-1].taken_at
    recent = [g for g in segs if (end - g.curr.taken_at).days <= lookback_days]
    use = recent if len(recent) >= MIN_SEGMENTS_FOR_RATE else segs
    days = sum((g.days for g in use), ZERO)
    if days <= 0:
        return None
    return sum((g.delta.value for g in use), ZERO) / days


def detect_anomalies(
    readings: Sequence[ReadingPoint],
    digits: int = 5,
    spike_factor: Decimal = DEFAULT_SPIKE_FACTOR,
    min_spike_kwh: Decimal = DEFAULT_MIN_SPIKE_KWH,
) -> list[Anomaly]:
    """Find decreases, implausible jumps and usage spikes in one register series.

    A spike is a segment whose daily rate exceeds ``spike_factor`` × the median
    rate of up to 12 preceding normal segments (at least 3 needed), and whose
    consumption exceeds the expected amount by more than ``min_spike_kwh``.
    """
    out: list[Anomaly] = []
    history: list[Decimal] = []
    for g in segments(readings, digits):
        if g.delta.anomaly:
            out.append(Anomaly(g.delta.anomaly, g.prev, g.curr, g.delta.value))
            continue
        if g.days < MIN_SPIKE_SEGMENT_DAYS:
            continue
        rate = g.delta.value / g.days
        if len(history) >= MIN_SEGMENTS_FOR_RATE:
            typical = Decimal(median(history[-12:]))
            expected = typical * g.days
            if rate > typical * spike_factor and g.delta.value - expected > min_spike_kwh:
                out.append(Anomaly(ANOMALY_SPIKE, g.prev, g.curr, g.delta.value, rate, typical))
        history.append(rate)
    return out


# --- plausibility of a new reading ----------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedRange:
    """Hint for a new reading at ``at`` based on the last reading and usual usage."""

    last: ReadingPoint
    days: Decimal  # days since last reading
    daily_rate: Decimal | None  # typical kWh/day (None = unknown)
    low: Decimal  # minimum plausible value (= last value; counter must not go down)
    expected: Decimal | None  # last + rate × days
    high: Decimal | None  # upper plausible bound (rate × days × factor, at least +min slack)
    digits: int = 5


def expected_range(
    readings: Sequence[ReadingPoint],
    at: date | datetime,
    digits: int = 5,
    factor: Decimal = DEFAULT_SPIKE_FACTOR,
    min_slack: Decimal = DEFAULT_MIN_SPIKE_KWH,
) -> ExpectedRange | None:
    """Expected value range of a register at ``at``; None without any previous reading."""
    at_dt = as_datetime(at)
    prior = [r for r in sort_series(readings) if r.taken_at <= at_dt]
    if not prior:
        return None
    last = prior[-1]
    days = Decimal((at_dt - last.taken_at).total_seconds()) / Decimal(86400)
    rate = typical_daily_rate(prior, digits)
    rng = counter_range(digits)
    if rate is None:
        return ExpectedRange(last, days.quantize(QUANT), None, last.value, None, None, digits)
    exp_use = rate * days
    expected = (last.value + exp_use) % rng
    high = (last.value + max(exp_use * factor, exp_use + min_slack)) % rng
    return ExpectedRange(
        last=last,
        days=days.quantize(QUANT),
        daily_rate=rate.quantize(QUANT),
        low=last.value,
        expected=expected.quantize(Decimal(1)),
        high=high.quantize(Decimal(1)),
        digits=digits,
    )


@dataclass(frozen=True, slots=True)
class PlausibilityIssue:
    code: str  # decrease | jump | high | magnitude | before_last
    severity: str  # "error" (should not be saved without override) | "warning" (confirmable)
    message: str  # Czech, user facing


def check_reading(
    value: Decimal,
    at: date | datetime,
    readings: Sequence[ReadingPoint],
    digits: int = 5,
    factor: Decimal = DEFAULT_SPIKE_FACTOR,
    min_slack: Decimal = DEFAULT_MIN_SPIKE_KWH,
) -> list[PlausibilityIssue]:
    """Validate a new value against the previous reading and usual daily usage (SPEC §5)."""
    at_dt = as_datetime(at)
    value = Decimal(value)
    issues: list[PlausibilityIssue] = []
    s = sort_series(readings)
    prior = [r for r in s if r.taken_at < at_dt]
    later = [r for r in s if r.taken_at > at_dt]
    if prior:
        prev = prior[-1]
        d = register_delta(prev.value, value, digits)
        if d.anomaly == ANOMALY_DECREASE:
            issues.append(
                PlausibilityIssue("decrease", "error", f"Stav je nižší než poslední odečet ({_kwh(prev.value)}).")
            )
        elif d.anomaly == ANOMALY_JUMP:
            issues.append(
                PlausibilityIssue(
                    "jump", "error", f"Nárůst o {_kwh(d.value)} je nepravděpodobný (polovina rozsahu počitadla)."
                )
            )
        else:
            rate = typical_daily_rate(prior, digits)
            days = Decimal((at_dt - prev.taken_at).total_seconds()) / Decimal(86400)
            if rate is not None and days > 0:
                exp_use = rate * days
                if d.value > max(exp_use * factor, exp_use + min_slack):
                    issues.append(
                        PlausibilityIssue(
                            "high",
                            "warning",
                            f"Spotřeba {_kwh(d.value)} za {_days(days)} dní je výrazně vyšší "
                            f"než obvykle (cca {format_number(exp_use)}{NBSP}kWh).",
                        )
                    )
            more_digits = len(str(int(value))) - len(str(int(prev.value))) >= 1
            if not d.rollover and prev.value >= 10 and value > 0 and more_digits and d.value > prev.value:
                issues.append(
                    PlausibilityIssue("magnitude", "warning", "Hodnota má o řád více číslic než poslední odečet.")
                )
    if later:
        nxt = later[0]
        d = register_delta(value, nxt.value, digits)
        if d.anomaly:
            issues.append(
                PlausibilityIssue(
                    "before_last",
                    "warning",
                    f"Hodnota nenavazuje na pozdější odečet {_kwh(nxt.value)} ({_date(nxt.taken_at)}).",
                )
            )
    return issues


# --- staleness (per-meter "missing reading") --------------------------------

DEFAULT_STALE_FACTOR = Decimal("1.5")
DEFAULT_STALE_GRACE_DAYS = Decimal(7)
DEFAULT_INTERVAL_DAYS = Decimal(31)  # assumed usual interval when history is too short


@dataclass(frozen=True, slots=True)
class Staleness:
    last: ReadingPoint | None
    days_since: Decimal | None  # days from last reading to ``now``
    usual_interval_days: Decimal  # median interval between readings (or the default)
    overdue: bool  # days_since > max(usual × factor, usual + grace)


def staleness(
    readings: Sequence[ReadingPoint],
    now: date | datetime,
    factor: Decimal = DEFAULT_STALE_FACTOR,
    grace_days: Decimal = DEFAULT_STALE_GRACE_DAYS,
    default_interval: Decimal = DEFAULT_INTERVAL_DAYS,
) -> Staleness:
    """How overdue the next reading of a register is, relative to its own usual interval."""
    now_dt = as_datetime(now)
    s = [r for r in sort_series(readings) if r.taken_at <= now_dt]
    days = sorted({r.taken_at.date() for r in s})
    intervals = [Decimal((b - a).days) for a, b in itertools.pairwise(days)][-12:]
    usual = Decimal(median(intervals)) if len(intervals) >= 2 else default_interval
    if not s:
        return Staleness(None, None, usual, True)
    last = s[-1]
    since = (Decimal((now_dt - last.taken_at).total_seconds()) / Decimal(86400)).quantize(Decimal("0.1"))
    return Staleness(last, since, usual, since > max(usual * factor, usual + grace_days))
