"""Review queue (``/odecty/kontrola``): cards of photos with proposed readings, confirmation.

A *card* is one photo series (or a single photo) – photos taken seconds apart are grouped for
*display* only: one switchboard often holds several meters, so neighbouring photos are frequently
of different meters. Each readout is matched to a meter on its own evidence (serial read on that
photo, the walk point it was taken at, a manual choice, or a value that fits exactly one active
meter); readouts tied to the same meter are merged into one *row*. A readout without its own
identification never inherits a neighbour's meter: it becomes an unassigned row with ranked
suggestions, and choosing one is a human action. Disagreeing values for one meter are a conflict
(the field stays empty and the user picks a value), never a silent vote. Nothing is written as
``confirmed`` except by ``confirm_card`` / ``bulk_confirm``, which are only called from
explicit user actions; every plausibility issue and doubtful extraction flag must be
acknowledged by code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.capture.plausibility import Issue, ack_note, check_value, missing_acks
from app.capture.readings import (
    InputError,
    existing_reading,
    parse_day,
    parse_value,
    write_reading,
)
from app.extraction.hints import meter_hint_list
from app.extraction.images import SuggestedTime, suggest_from_filename
from app.extraction.matching import apply_matching, assign_registers, value_fits
from app.extraction.types import ExtractionResult, MeterHintIn, MeterReadout, problem_label
from app.models import Meter, Photo, Reading, User
from app.services.network import Network, load_network
from app.templating import format_number

# Extraction flags that make a value doubtful: keeping such a value needs an acknowledgement.
DOUBT_FLAGS = ("same_as_hint", "low_confidence", "register_guess", "value_mismatch", "register_unknown")
DOUBT_MESSAGES = {
    "same_as_hint": "Rozpoznaná hodnota je přesně shodná s hodnotou z nápovědy modelu – ověřte na fotce.",
    "low_confidence": "Model si hodnotou není jistý.",
    "register_guess": "Model neuvedl, o který registr jde (T1/T2) – ověřte.",
    "value_mismatch": "Číslice a hodnota od modelu nesouhlasí.",
    "register_unknown": "Model uvedl registr, který elektroměr nemá.",
}
# Identification that ties a readout to a meter by itself (merging rows, bulk confirmation).
STRONG_METHODS = ("exact", "manual", "walk")
METHOD_LABELS = {
    "exact": "výr. číslo",
    "fuzzy": "podobné výr. číslo",
    "values": "podle očekávaného stavu",
    "walk": "z obchůzky",
    "walk_mismatch": "jiné výr. číslo než u místa z obchůzky",
    "manual": "ručně",
    "unknown_serial": "neznámé výr. číslo",
}


@dataclass
class ValueCandidate:
    value: Decimal
    photo_id: int
    confidence: float
    problems: list[str]
    label_seen: str = ""
    digits_seen: str = ""
    guessed_register: bool = False
    second_read: str | None = None  # EXTRACTOR_VERIFY: what the second read showed instead ("" = nothing)


@dataclass
class Row:
    """One proposed reading (meter × registers) inside a card; also used to re-render posted forms."""

    key: str
    meter: MeterHintIn | None
    method: str | None = None
    photo_ids: list[int] = field(default_factory=list)
    serial_seen: str | None = None
    unknown_serial: bool = False
    candidate_meter_ids: list[int] = field(default_factory=list)
    candidates: dict[str, list[ValueCandidate]] = field(default_factory=dict)  # register -> candidates
    values: dict[str, str] = field(default_factory=dict)  # register -> value shown in the input
    conflicts: set[str] = field(default_factory=set)
    issues: dict[str, list[Issue]] = field(default_factory=dict)  # register -> issues of the shown value
    existing: dict[str, Reading] = field(default_factory=dict)
    acks: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def registers(self) -> tuple[str, ...]:
        return self.meter.registers if self.meter else ("T1",)

    @property
    def method_label(self) -> str:
        return METHOD_LABELS.get(self.method or "", "")

    @property
    def strongly_identified(self) -> bool:
        """At least one photo of the row identifies the meter by itself (serial / walk point / manual)."""
        return self.meter is not None and self.method in STRONG_METHODS

    def all_issues(self) -> list[Issue]:
        seen: dict[str, Issue] = {}
        for reg in self.registers:
            for i in self.issues.get(reg, []):
                seen.setdefault(i.code, i)
        return list(seen.values())

    def distinct_values(self, reg: str) -> list[Decimal]:
        return sorted({c.value for c in self.candidates.get(reg, [])})


@dataclass
class Card:
    key: str
    photos: list[Photo]
    taken_at: datetime | None
    suggested: SuggestedTime | None
    reported_by: str
    rows: list[Row]
    errors: list[str] = field(default_factory=list)

    @property
    def photo_ids(self) -> list[int]:
        return [p.id for p in self.photos]

    @property
    def day(self) -> date | None:
        return self.taken_at.date() if self.taken_at else None

    @property
    def extraction_pending(self) -> bool:
        return any(p.extraction_status == "queued" for p in self.photos)

    @property
    def date_required(self) -> bool:
        return self.taken_at is None

    def blockers(self) -> list[str]:
        """Reasons the card cannot be bulk-confirmed (empty = clean)."""
        out: list[str] = []
        if self.extraction_pending:
            out.append("rozpoznávání ještě běží")
        if self.taken_at is None:
            out.append("chybí datum pořízení")
        if not self.rows:
            out.append("nic nerozpoznáno")
        for r in self.rows:
            if r.meter is None:
                if r.unknown_serial:
                    out.append(f"neznámé výr. číslo {r.serial_seen}")
                elif r.method == "walk_mismatch":
                    out.append(f"výr. číslo {r.serial_seen} nepatří k místu z obchůzky")
                else:
                    out.append("nepřiřazený elektroměr")
                continue
            if r.method == "fuzzy":
                out.append(f"{r.meter.serial}: výr. číslo jen podobné")
            elif not r.strongly_identified:
                out.append(f"{r.meter.serial}: elektroměr určen jen podle očekávaného stavu")
            for reg in r.registers:
                if reg in r.conflicts:
                    out.append(f"{r.meter.serial} {reg}: fotky se neshodují")
                elif not r.values.get(reg):
                    out.append(f"{r.meter.serial} {reg}: chybí hodnota")
                if r.issues.get(reg):
                    out.append(f"{r.meter.serial} {reg}: varování")
        return list(dict.fromkeys(out))

    @property
    def clean(self) -> bool:
        return not self.blockers()


def card_key(photo: Photo) -> str:
    return photo.series_id or f"p{photo.id}"


def _fmt_value(v: Decimal) -> str:
    """Plain machine value without trailing zeros ("485.98"), used in inputs."""
    s = format(v, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


plain_value = _fmt_value


def display_value(v: Decimal | None) -> str:
    """Czech display of a value with only its significant decimals ("19 212,9")."""
    if v is None:
        return "–"
    s = _fmt_value(v)
    return format_number(v, len(s.split(".")[1]) if "." in s else 0)


def _flag_issues(cands: Sequence[ValueCandidate]) -> list[Issue]:
    codes = []
    for c in cands:
        codes.extend(p for p in c.problems if p in DOUBT_FLAGS)
        if c.guessed_register:
            codes.append("register_guess")
    return [Issue(code, "warning", DOUBT_MESSAGES[code]) for code in dict.fromkeys(codes)]


class HintCache:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.net: Network = load_network(db)
        self._cache: dict[date | None, list[MeterHintIn]] = {}

    def at(self, t: datetime | None) -> list[MeterHintIn]:
        key = t.date() if t else None
        if key not in self._cache:
            self._cache[key] = meter_hint_list(self.db, t, net=self.net)
        return self._cache[key]

    def meter(self, meter_id: int, t: datetime | None) -> MeterHintIn | None:
        return next((h for h in self.at(t) if h.meter_id == meter_id), None)


def _walk_focus(hc: HintCache, photo: Photo) -> tuple[int, ...]:
    if photo.match_method not in ("walk", "manual") or photo.meter_id is None:
        return ()
    m = hc.net.meters.get(photo.meter_id)
    if m is None:
        return ()
    if photo.match_method == "manual":
        return (photo.meter_id,)
    point = hc.net.points_by_id.get(m.meter_point_id)
    ids = tuple(mid for mid in (point.meter_ids if point else ()) if hc.net.meters[mid].active)
    return ids or (photo.meter_id,)


def _photo_readouts(hc: HintCache, photo: Photo, hints: list[MeterHintIn], at: datetime | None) -> list[MeterReadout]:
    res = ExtractionResult.from_dict(photo.extraction_result)
    focus = _walk_focus(hc, photo)
    readouts = res.meters if res else []
    if res:
        apply_matching(res, hints, at, focus_meter_ids=focus)
        if photo.match_method == "walk" and focus:
            for r in readouts:
                if r.meter_id is not None and r.meter_id not in focus and r.match_method in ("exact", "fuzzy"):
                    # the serial on the photo belongs to another meter than the tapped walk point: neither
                    # side wins silently - the row is unassigned with both as suggestions
                    r.match_candidates = list(dict.fromkeys([r.meter_id, *focus]))
                    r.meter_id, r.match_method = None, "walk_mismatch"
    if photo.match_method == "manual" and photo.meter_id:
        hint = next((h for h in hints if h.meter_id == photo.meter_id), None)
        if len(readouts) == 1 and hint is not None:
            readouts[0].meter_id, readouts[0].match_method = hint.meter_id, "manual"
            assign_registers(readouts[0], hint)
        elif not readouts:
            readouts = [MeterReadout(serial=None, meter_id=photo.meter_id, match_method="manual")]
    elif photo.match_method == "walk" and photo.meter_id and not readouts:
        readouts = [MeterReadout(serial=None, meter_id=photo.meter_id, match_method="walk")]
    return readouts


def _suggest_unidentified(pairs: list[tuple[Photo, MeterReadout]], hints: list[MeterHintIn]) -> None:
    """Readouts without their own identification get *ranked suggestions* (never an assignment).

    Order: meters identified elsewhere in the same group whose plausible range the value fits (closest
    first), the other meters of the group, then other active meters the value fits."""
    by_id = {h.meter_id: h for h in hints}
    group = list(dict.fromkeys(r.meter_id for _, r in pairs if r.meter_id is not None))
    for _, r in pairs:
        if r.meter_id is not None or r.match_method == "unknown_serial":
            continue
        fits: list[tuple[Decimal, int]] = []
        for mid in dict.fromkeys([*group, *r.match_candidates]):
            h = by_id.get(mid)
            if h is None:
                continue
            dists = [d for rv in r.registers if (d := value_fits(rv, h, rv.register)) is not None]
            if dists:
                fits.append((min(dists), mid))
        ranked = [m for _, m in sorted(fits)]
        rest = [m for m in [*r.match_candidates, *group] if m not in ranked]
        r.match_candidates = list(dict.fromkeys([*ranked, *rest]))


def _rows_for(hc: HintCache, db: Session, pairs: list[tuple[Photo, MeterReadout]], at: datetime | None) -> list[Row]:
    hints = hc.at(at)
    by_id = {h.meter_id: h for h in hints}
    rows: dict[str, Row] = {}
    for photo, r in pairs:
        if r.meter_id is not None and r.meter_id in by_id:
            key = f"m{r.meter_id}"
            row = rows.get(key)
            if row is None:
                row = rows[key] = Row(key=key, meter=by_id[r.meter_id], method=r.match_method)
            elif r.match_method in STRONG_METHODS and row.method not in STRONG_METHODS:
                row.method = r.match_method
        else:
            if not r.registers and r.match_method != "unknown_serial":
                continue  # nothing recognised and nothing to assign (e.g. unreadable photo)
            key = f"u{photo.id}_{len(rows)}"
            row = rows[key] = Row(
                key=key,
                meter=None,
                method=r.match_method,
                serial_seen=r.serial,
                unknown_serial=r.match_method == "unknown_serial",
                candidate_meter_ids=list(r.match_candidates),
            )
        if photo.id not in row.photo_ids:
            row.photo_ids.append(photo.id)
        if row.serial_seen is None and r.serial:
            row.serial_seen = r.serial
        regs = row.registers
        labelled = {rv.register for rv in r.registers if rv.register}
        for rv in r.registers:
            if rv.value is None:
                continue
            targets = [rv.register] if rv.register else [g for g in regs if g not in labelled]
            for reg in targets:
                row.candidates.setdefault(reg, []).append(
                    ValueCandidate(
                        rv.value,
                        photo.id,
                        rv.confidence,
                        list(rv.problems),
                        rv.label_seen,
                        rv.digits_seen,
                        guessed_register=rv.register is None and row.meter is not None and len(regs) > 1,
                        second_read=rv.second_read,
                    )
                )
    out = list(rows.values())
    for row in out:
        for reg in row.registers:
            vals = row.distinct_values(reg)
            if len(vals) == 1:
                row.values[reg] = _fmt_value(vals[0])
            elif len(vals) > 1:
                row.conflicts.add(reg)
        evaluate_row(db, row, at.date() if at else None)
    return out


def evaluate_row(db: Session, row: Row, day: date | None) -> None:
    """(Re)compute issues for the values currently in the row."""
    row.issues = {}
    row.existing = {}
    if row.meter is None:
        return
    meter = db.get(Meter, row.meter.meter_id)
    if meter is None:
        return
    for reg in row.registers:
        raw = row.values.get(reg)
        if not raw:
            continue
        try:
            value = parse_value(raw)
        except InputError as exc:
            row.errors.append(f"{reg}: {exc}")
            continue
        if value is None:
            continue
        issues: list[Issue] = []
        cands = [c for c in row.candidates.get(reg, []) if c.value == value]
        issues.extend(_flag_issues(cands))
        if day is not None:
            cur = existing_reading(db, meter.id, reg, day)
            if cur is not None:
                row.existing[reg] = cur
            if cur is not None and cur.value == value and cur.status == "confirmed":
                issues = []  # already recorded exactly like this: confirming writes nothing
            else:
                issues.extend(check_value(db, meter, reg, value, day))
            if cur is not None and cur.value != value:
                issues.append(
                    Issue(
                        "duplicate",
                        "warning",
                        f"Pro {day.day}.{day.month}.{day.year} už je zapsáno {display_value(cur.value)}\xa0kWh – "
                        "potvrzením se přepíše.",
                    )
                )
        row.issues[reg] = issues


def build_cards(db: Session, *, keys: Iterable[str] | None = None, limit: int | None = None) -> list[Card]:
    """Cards for unreviewed photos, oldest first (photos without a date last)."""
    photos = list(
        db.scalars(
            select(Photo)
            .where(Photo.review_status == "new")
            .order_by(Photo.taken_at.is_(None), Photo.taken_at, Photo.uploaded_at, Photo.id)
        )
    )
    groups: dict[str, list[Photo]] = {}
    for p in photos:
        groups.setdefault(card_key(p), []).append(p)
    wanted = set(keys) if keys is not None else None
    hc = HintCache(db)
    cards: list[Card] = []
    for key, group in groups.items():
        if wanted is not None and key not in wanted:
            continue
        cards.append(_card(db, hc, key, group))
        if limit is not None and len(cards) >= limit:
            break
    return cards


def _card(db: Session, hc: HintCache, key: str, group: list[Photo]) -> Card:
    at = next((p.taken_at for p in group if p.taken_at), None)
    hints = hc.at(at)
    pairs: list[tuple[Photo, MeterReadout]] = []
    for p in group:
        pairs.extend((p, r) for r in _photo_readouts(hc, p, hints, at))
    _suggest_unidentified(pairs, hints)
    rows = _rows_for(hc, db, pairs, at)
    suggested = next((s for p in group if (s := suggest_from_filename(p.original_filename))), None)
    reported_by = next((p.reported_by for p in group if p.reported_by), "")
    return Card(key, group, at, suggested, reported_by, rows)


def photo_problems(photo: Photo) -> list[str]:
    res = ExtractionResult.from_dict(photo.extraction_result)
    if res is None:
        return []
    codes = list(res.problems)
    for m in res.meters:
        for r in m.registers:
            codes.extend(r.problems)
    return [problem_label(c) for c in dict.fromkeys(codes)]


# --- confirmation -------------------------------------------------------------------------


@dataclass
class ConfirmOutcome:
    ok: bool
    card: Card
    written: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def rows_from_form(db: Session, card: Card, form: Mapping[str, object], getlist) -> list[Row]:
    """Rebuild rows from a posted review form (values/acks as the user left them)."""
    proposed = {r.key: r for r in card.rows}
    hc_meters = {h.meter_id: h for h in meter_hint_list(db, card.taken_at)}
    out: list[Row] = []
    for key in getlist("row"):
        key = str(key)
        base = proposed.get(key)
        raw_meter = str(form.get(f"meter_{key}") or "").strip()
        meter = hc_meters.get(int(raw_meter)) if raw_meter.isdigit() else None
        row = Row(
            key=key,
            meter=meter,
            method=(
                base.method if base and base.meter and meter and base.meter.meter_id == meter.meter_id else "manual"
            ),
            photo_ids=list(base.photo_ids) if base else [],
            serial_seen=base.serial_seen if base else None,
            unknown_serial=bool(base and base.unknown_serial and meter is None),
            candidate_meter_ids=list(base.candidate_meter_ids) if base else [],
            candidates=dict(base.candidates)
            if base and base.meter and meter and base.meter.meter_id == meter.meter_id
            else {},
        )
        if raw_meter and meter is None:
            row.errors.append("Neznámý elektroměr.")
        for reg in ("T1", "T2"):
            v = form.get(f"v_{key}_{reg}")
            if isinstance(v, str) and v.strip():
                row.values[reg] = v.strip()
        row.acks = {str(a) for a in getlist(f"ack_{key}")}
        out.append(row)
    return out


def _merge_same_meter(rows: list[Row]) -> list[Row]:
    """Rows the user assigned to the same meter (e.g. a close-up without serial put on the meter whose
    serial shot is already a row) become one row when their values agree; differing values stay as
    separate rows and are refused below ("Stejný elektroměr je v kartě dvakrát")."""
    out: list[Row] = []
    by_meter: dict[int, Row] = {}
    for row in rows:
        mid = row.meter.meter_id if row.meter else None
        first = by_meter.get(mid) if mid is not None else None
        if first is None:
            if mid is not None:
                by_meter[mid] = row
            out.append(row)
            continue
        if any(row.values.get(g) and first.values.get(g) and row.values[g] != first.values[g] for g in ("T1", "T2")):
            out.append(row)
            continue
        for g in ("T1", "T2"):
            if row.values.get(g) and not first.values.get(g):
                first.values[g] = row.values[g]
        first.photo_ids.extend(pid for pid in row.photo_ids if pid not in first.photo_ids)
        first.acks |= row.acks
    return out


def confirm_card(
    db: Session,
    user: User,
    card: Card,
    rows: list[Row],
    *,
    day_raw: str | None,
    reported_by: str,
    note: str = "",
) -> ConfirmOutcome:
    """Write the rows of a card as confirmed readings (explicit user action). No commit on failure."""
    out = ConfirmOutcome(False, card)
    card.rows = rows
    try:
        day = parse_day(day_raw) if day_raw else card.day
    except InputError as exc:
        card.errors.append(str(exc))
        return out
    if day is None:
        card.errors.append("Zadejte datum pořízení odečtu (fotka nemá čas pořízení).")
        return out
    if day > date.today():
        card.errors.append("Datum nesmí být v budoucnosti.")
        return out
    rows = _merge_same_meter(rows)
    card.rows = rows
    to_write: list[tuple[Row, str, Decimal, list[Issue]]] = []
    meters_seen: set[int] = set()
    for row in rows:
        evaluate_row(db, row, day)
        has_values = any(row.values.get(g) for g in ("T1", "T2"))
        if not has_values:
            continue
        if row.meter is None:
            row.errors.append("Vyberte elektroměr.")
            continue
        if row.meter.meter_id in meters_seen:
            row.errors.append("Stejný elektroměr je v kartě dvakrát.")
            continue
        meters_seen.add(row.meter.meter_id)
        for reg in ("T1", "T2"):
            raw = row.values.get(reg)
            if not raw:
                continue
            if reg not in row.registers:
                row.errors.append(f"Elektroměr nemá registr {reg}.")
                continue
            try:
                value = parse_value(raw)
            except InputError as exc:
                row.errors.append(f"{reg}: {exc}")
                continue
            issues = row.issues.get(reg, [])
            missing = missing_acks(issues, row.acks)
            if missing:
                row.errors.append(f"{reg}: potvrďte varování ({', '.join(i.code for i in missing)}).")
                continue
            assert value is not None
            to_write.append((row, reg, value, issues))
    if any(r.errors for r in rows) or card.errors:
        return out
    if not to_write:
        card.errors.append("Není co zapsat – vyplňte hodnotu nebo fotky zahoďte.")
        return out
    for row, reg, value, issues in to_write:
        src_photo = row.photo_ids[0] if row.photo_ids else card.photo_ids[0]
        res = write_reading(
            db,
            user,
            meter_id=row.meter.meter_id,  # type: ignore[union-attr]
            register=reg,
            day=day,
            value=value,
            source="photo",
            photo_id=src_photo,
            reported_by=reported_by.strip()[:200],
            note=note.strip(),
            audit_note=ack_note(issues),
            overwrite="duplicate" in row.acks,
        )
        if res.status in ("created", "updated"):
            out.written += 1
        elif res.status == "unchanged":
            out.unchanged += 1
        else:
            row.errors.append(res.message)
    if any(r.errors for r in rows):
        db.rollback()
        return out
    for p in card.photos:
        changes: dict[str, object] = {"review_status": "confirmed"}
        if p.taken_at is None:
            changes["taken_at"] = datetime.combine(day, datetime.min.time())
        if reported_by.strip() and not p.reported_by:
            changes["reported_by"] = reported_by.strip()[:200]
        # only the photo's own row ties it to a meter - never a neighbour in the group
        own = next((r.meter.meter_id for r in rows if r.meter and p.id in r.photo_ids), None)
        if own:
            changes["meter_id"] = own
        ch = audit.apply_changes(p, changes)
        if ch:
            audit.record_update(db, user, p, ch, note="potvrzeno v kontrole")
    out.ok = True
    return out


def bulk_confirm(
    db: Session, user: User, keys: Sequence[str], photo_sets: Mapping[str, str]
) -> tuple[int, int, list[str]]:
    """Confirm the posted cards that are clean (no warnings at all). Returns (cards, readings, skipped)."""
    cards = {c.key: c for c in build_cards(db, keys=keys)}
    done = written = 0
    skipped: list[str] = []
    for key in keys:
        card = cards.get(key)
        if card is None:
            continue
        posted = photo_sets.get(key)
        if posted is not None and posted != ",".join(str(i) for i in card.photo_ids):
            skipped.append(f"{key}: karta se mezitím změnila")
            continue
        if not card.clean:
            skipped.append(f"{key}: {', '.join(card.blockers())}")
            continue
        out = confirm_card(db, user, card, card.rows, day_raw=None, reported_by=card.reported_by)
        if not out.ok:
            skipped.append(f"{key}: {'; '.join(card.errors + [e for r in card.rows for e in r.errors])}")
            db.rollback()
            continue
        db.commit()
        done += 1
        written += out.written
    return done, written, skipped


def discard_photos(db: Session, user: User, photo_ids: Sequence[int], note: str = "") -> int:
    n = 0
    for pid in photo_ids:
        p = db.get(Photo, pid)
        if p is None or p.review_status != "new":
            continue
        ch = audit.apply_changes(p, {"review_status": "discarded"})
        audit.record_update(db, user, p, ch, note=note or "zahozeno v kontrole")
        n += 1
    return n


# --- pending readings (walk entries that need a look) ---------------------------------------


@dataclass
class PendingView:
    reading: Reading
    meter: Meter
    issues: list[Issue]


def pending_readings(db: Session) -> list[PendingView]:
    out = []
    for r in db.scalars(select(Reading).where(Reading.status == "pending").order_by(Reading.taken_at, Reading.id)):
        out.append(
            PendingView(r, r.meter, check_value(db, r.meter, r.register, r.value, r.taken_at, exclude_reading_id=r.id))
        )
    return out
