"""Photo series must not glue different meters together (real bursts from the example walks).

One switchboard holds several meters and the walker photographs them seconds apart, so photos close in
time are often of *different* meters. Time proximity only groups photos for display. A readout is tied to
a meter by its own evidence: a serial read on that photo, the tapped walk point, a manual choice, or a
value that fits exactly one active meter. Otherwise it is unassigned with ranked suggestions.

Bursts (timing modelled on real walks; meters and values fit the synthetic corrected import):
- October 2026-10-01 12:35:51–12:36:07: tenant E's LCD meter ×2, the new office meter, a frame with
  both corridor meters ×2 – one series of 5 photos, 4 meters.
- November 2026-11-08 16:54:18–16:54:58: a new meter not in the DB, the office meter ×2, cabinet overview,
  tenant E's meter ×2, both corridor meters – a chain of 7 photos (60 s gap rule), 5 meters.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.capture.review import Card, build_cards
from app.extraction import queue as q
from app.models import Meter, Photo, Reading
from tests import synthetic as syn
from tests.capture_helpers import answer, fake_extractor, make_jpeg, sha, upload

E = syn.E_SERIAL  # LCD
OFFICE = syn.NEW_OFFICE_SERIAL
CORR, CORR2 = syn.CORRIDOR_SERIAL, syn.CORRIDOR2_SERIAL  # one frame; CORR2 is idle
ROLL, CONTROL = syn.ROLLOVER_SERIAL, syn.CONTROL_SERIAL
NEW_SERIAL = "91827364"  # not in the DB
# expected ranges in October: E 10061–11446 and ROLL 8264–10650 overlap; OFFICE 274–622 and CONTROL 406–769
E_OCT, OFFICE_OCT, CORR2_V = "10500", "00600", "00012"
CORR_V = f"{int(syn.value('T', syn.TYPO)) + 60:05d}"  # just above the corridor meter's last value: fits it only
OFFICE_NOV, E_NOV = "00450", "11000"  # both fit several meters


@pytest.fixture
def fapp(make_app: Callable[..., FastAPI], imported_corrected_db_url: str) -> FastAPI:
    return make_app(imported_corrected_db_url, extractor="fake")


@pytest.fixture
def admin(fapp: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(fapp, "admin")


_n = [0]


def shoot(app: FastAPI, client: TestClient, ans: dict[str, Any] | None, at: datetime) -> int:
    _n[0] += 1
    data = make_jpeg(taken=at, color=(_n[0] * 13 % 255, _n[0] * 7 % 255, 90))
    if ans is not None:
        fake_extractor(app).responses[sha(data)] = ans
    res = upload(client, data, name=f"{at:%Y%m%d_%H%M%S}.jpg")
    assert res["ok"], res
    return res["id"]


def ids(app: FastAPI) -> dict[str, int]:
    with app.state.sessionmaker() as db:
        return {m.serial: m.id for m in db.scalars(select(Meter))}


def one_card(app: FastAPI) -> Card:
    q.process_pending(app)
    with app.state.sessionmaker() as db:
        cards = build_cards(db)
    assert len(cards) == 1, [c.key for c in cards]
    return cards[0]


def m(serial: str | None, *values: str) -> dict[str, Any]:
    return {"serial": serial, "registers": [{"digits_seen": v} for v in values]}


def rows_by_meter(card: Card) -> dict[int | None, list]:
    out: dict[int | None, list] = {}
    for r in card.rows:
        out.setdefault(r.meter.meter_id if r.meter else None, []).append(r)
    return out


OCT = [datetime(2026, 10, 1, 12, 35, s) for s in (51, 54, 58)] + [datetime(2026, 10, 1, 12, 36, s) for s in (2, 7)]
NOV = [datetime(2026, 11, 8, 16, 54, s) for s in (18, 24, 28, 35, 49, 53, 58)]


def test_october_burst_with_serials_is_four_rows_and_bulk_confirmable(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    p = [
        shoot(fapp, admin, answer(m(E, E_OCT)), OCT[0]),
        shoot(fapp, admin, answer(m(E, E_OCT)), OCT[1]),
        shoot(fapp, admin, answer(m(OFFICE, OFFICE_OCT)), OCT[2]),
        shoot(fapp, admin, answer(m(CORR, CORR_V), m(CORR2, CORR2_V)), OCT[3]),
        shoot(fapp, admin, answer(m(CORR, CORR_V), m(CORR2, CORR2_V)), OCT[4]),
    ]
    card = one_card(fapp)
    rows = rows_by_meter(card)
    assert set(rows) == {mid[E], mid[OFFICE], mid[CORR], mid[CORR2]}
    assert rows[mid[E]][0].photo_ids == p[:2]  # the two shots of E merge (serial on each)
    assert rows[mid[OFFICE]][0].photo_ids == [p[2]]
    assert rows[mid[CORR]][0].photo_ids == p[3:] and rows[mid[CORR2]][0].photo_ids == p[3:]
    assert all(r.method == "exact" for r in card.rows) and card.clean
    r = admin.post("/odecty/kontrola/hromadne", data={"card": card.key, f"photos_{card.key}": ",".join(map(str, p))})
    assert r.status_code == 200
    with fapp.state.sessionmaker() as db:
        new = list(db.scalars(select(Reading).where(Reading.photo_id.in_(p))))
        assert {x.meter_id for x in new} == set(rows)
        assert db.get(Photo, p[2]).meter_id == mid[OFFICE]  # each photo keeps its own meter


def test_october_burst_without_serials_is_not_glued(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    p = [
        shoot(fapp, admin, answer(m(E, E_OCT)), OCT[0]),  # serial read
        shoot(fapp, admin, answer(m(None, E_OCT)), OCT[1]),  # close-up, fits E AND ROLL
        shoot(fapp, admin, answer(m(None, OFFICE_OCT)), OCT[2]),  # fits OFFICE AND CONTROL
        shoot(fapp, admin, answer(m(None, CORR_V), m(None, CORR2_V)), OCT[3]),  # each fits one meter only
        shoot(fapp, admin, answer(readable=False, problems=["unreadable"]), OCT[4]),
    ]
    card = one_card(fapp)
    assert all(r.method != "series" for r in card.rows)
    rows = rows_by_meter(card)
    # the serial photo is alone in its row: the neighbour without a serial did not inherit E
    assert rows[mid[E]][0].photo_ids == [p[0]]
    unassigned = {r.photo_ids[0]: r for r in rows[None]}
    assert set(unassigned) == {p[1], p[2]}
    assert unassigned[p[1]].candidate_meter_ids[:2] == [mid[E], mid[ROLL]]  # group meter first
    assert set(unassigned[p[2]].candidate_meter_ids[:2]) == {mid[OFFICE], mid[CONTROL]}
    # values that fit exactly one active meter are proposed, but only as weak ("podle očekávaného stavu")
    assert rows[mid[CORR]][0].method == "values" and rows[mid[CORR2]][0].method == "values"
    blockers = " | ".join(card.blockers())
    assert "nepřiřazený elektroměr" in blockers and f"{CORR}: elektroměr určen jen podle očekávaného stavu" in blockers
    # bulk confirm writes nothing for such a card
    with fapp.state.sessionmaker() as db:
        before = db.scalar(select(Reading.id).order_by(Reading.id.desc()))
    admin.post(
        "/odecty/kontrola/hromadne", data={"card": card.key, f"photos_{card.key}": ",".join(map(str, card.photo_ids))}
    )
    with fapp.state.sessionmaker() as db:
        assert db.scalar(select(Reading.id).order_by(Reading.id.desc())) == before


def test_november_chain_of_seven_photos(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    p = [
        shoot(fapp, admin, answer(m(NEW_SERIAL, "0000004")), NOV[0]),  # new meter, unknown serial
        shoot(fapp, admin, answer(m(OFFICE, OFFICE_NOV)), NOV[1]),
        shoot(fapp, admin, answer(m(None, OFFICE_NOV)), NOV[2]),  # same meter, serial not readable
        shoot(fapp, admin, answer(readable=False, problems=["unreadable"]), NOV[3]),  # cabinet overview
        shoot(fapp, admin, answer(m(E, E_NOV)), NOV[4]),
        shoot(fapp, admin, answer(m(None, E_NOV)), NOV[5]),
        shoot(fapp, admin, answer(m(CORR, CORR_V), m(CORR2, CORR2_V)), NOV[6]),
    ]
    card = one_card(fapp)
    assert card.photo_ids == p  # one display group (40 s chain)
    rows = rows_by_meter(card)
    unknown = [r for r in rows[None] if r.unknown_serial]
    assert len(unknown) == 1 and unknown[0].serial_seen == NEW_SERIAL and unknown[0].photo_ids == [p[0]]
    assert rows[mid[OFFICE]][0].photo_ids == [p[1]]
    assert rows[mid[E]][0].photo_ids == [p[4]]
    loose = {r.photo_ids[0]: r for r in rows[None] if not r.unknown_serial}
    assert set(loose) == {p[2], p[5]}
    assert loose[p[2]].candidate_meter_ids[0] == mid[OFFICE]  # suggestion, not assignment
    assert loose[p[5]].candidate_meter_ids[0] == mid[E]
    assert not card.clean
    # a human picks the suggestion: one row per meter, values agree -> written
    from tests.test_capture_review import post_card

    rows_post = {
        r.key: {"meter": r.meter.meter_id if r.meter else "", "T1": r.values.get("T1", "")}
        for r in card.rows
        if r.meter is not None and r.method == "exact"
    }
    rows_post[loose[p[2]].key] = {"meter": mid[OFFICE], "T1": str(int(OFFICE_NOV))}
    res = post_card(admin, card, rows_post, den="2026-11-08")
    assert res.status_code == 200, res.text
    with fapp.state.sessionmaker() as db:
        new = db.scalar(select(Reading).where(Reading.meter_id == mid[OFFICE], Reading.value == int(OFFICE_NOV)))
        assert new is not None and new.photo_id in (p[1], p[2])
        assert db.get(Photo, p[3]).meter_id is None  # the unreadable overview is not tied to any meter


def test_walk_photo_with_another_meters_serial_warns(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    with fapp.state.sessionmaker() as db:
        point_id = db.get(Meter, mid[E]).meter_point_id
    data = make_jpeg(color=(5, 200, 5))
    fake_extractor(fapp).responses[sha(data)] = answer(m(CORR, CORR_V))
    uid = str(uuid.uuid4())
    form = {"uuid": uid, "point_id": str(point_id), "taken_at": "2026-10-01T12:35:51", "source": "walk"}
    assert admin.post("/obchuzka/api/foto", data=form, files={"file": ("f.jpg", data, "image/jpeg")}).json()["ok"]
    q.process_pending(fapp)
    with fapp.state.sessionmaker() as db:
        photo = db.scalar(select(Photo).where(Photo.uuid == uid))
        assert photo.meter_id == mid[E] and photo.match_method == "walk"  # stored with the tapped point
    st = admin.get(f"/obchuzka/api/foto/{uid}").json()
    assert st["values"] == {} and st["mismatch"] and CORR in st["mismatch"][0]
    # not confirmed in the walk -> in the review queue it is neither the tapped meter nor silently the corridor meter
    with fapp.state.sessionmaker() as db:
        card = next(c for c in build_cards(db) if photo.id in c.photo_ids)
    (row,) = card.rows
    assert row.meter is None and row.method == "walk_mismatch"
    assert row.candidate_meter_ids[:2] == [mid[CORR], mid[E]]
    assert "nepatří k místu z obchůzky" in " ".join(card.blockers())
