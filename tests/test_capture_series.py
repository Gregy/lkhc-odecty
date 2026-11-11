"""Review cards are grouped by what extraction recognised, never by time proximity (real bursts from walks).

One switchboard holds several meters and the walker photographs them seconds apart, so photos close in
time are often of *different* meters. A card holds the photos of the same meter(s) on the same day: a
strongly identified readout (serial, walk point, manual choice) links a photo to its meter, a photo of
several meters joins their cards, a close-up identified only by its value joins the meter's card only when
that is unambiguous, and a photo without an identified meter is its own card with ranked suggestions from
photos taken shortly before / after. Values and identities are never inherited from a neighbouring photo.

Bursts (timing modelled on real walks; meters and values fit the synthetic corrected import):
- October 2026-10-01 12:35:51–12:36:07: tenant E's LCD meter ×2, the new office meter, a frame with
  both corridor meters ×2 – 5 photos, 4 meters.
- November 2026-11-08 16:54:18–16:54:58: a new meter not in the DB, the office meter ×2, cabinet overview,
  tenant E's meter ×2, both corridor meters – 7 photos, 5 meters.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
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


def shoot(app: FastAPI, client: TestClient, ans: dict[str, Any] | None, at: datetime | None) -> int:
    _n[0] += 1
    data = make_jpeg(taken=at, color=(_n[0] * 13 % 255, _n[0] * 7 % 255, 90))
    if ans is not None:
        fake_extractor(app).responses[sha(data)] = ans
    res = upload(client, data, name=f"{at:%Y%m%d_%H%M%S}.jpg" if at else "IMG-20261001-WA0001.jpg")
    assert res["ok"], res
    return res["id"]


def ids(app: FastAPI) -> dict[str, int]:
    with app.state.sessionmaker() as db:
        return {m.serial: m.id for m in db.scalars(select(Meter))}


def all_cards(app: FastAPI, *, process: bool = True) -> list[Card]:
    if process:
        q.process_pending(app)
    with app.state.sessionmaker() as db:
        return build_cards(db)


def card_of(cards: list[Card], pid: int) -> Card:
    return next(c for c in cards if pid in c.photo_ids)


def m(serial: str | None, *values: str) -> dict[str, Any]:
    return {"serial": serial, "registers": [{"digits_seen": v} for v in values]}


def rows_by_meter(card: Card) -> dict[int | None, list]:
    out: dict[int | None, list] = {}
    for r in card.rows:
        out.setdefault(r.meter.meter_id if r.meter else None, []).append(r)
    return out


def bulk(client: TestClient, cards: list[Card]):
    data: dict[str, Any] = {"card": [c.key for c in cards]}
    data.update({f"photos_{c.key}": ",".join(map(str, c.photo_ids)) for c in cards})
    return client.post("/odecty/kontrola/hromadne", data=data)


OCT = [datetime(2026, 10, 1, 12, 35, s) for s in (51, 54, 58)] + [datetime(2026, 10, 1, 12, 36, s) for s in (2, 7)]
NOV = [datetime(2026, 11, 8, 16, 54, s) for s in (18, 24, 28, 35, 49, 53, 58)]


def test_six_photos_of_four_meters_within_two_minutes_are_four_cards(fapp: FastAPI, admin: TestClient) -> None:
    """The reported complaint: a fast walk past one switchboard must not become one big card."""
    mid = ids(fapp)
    t0 = datetime(2026, 10, 1, 12, 35, 0)
    p = [
        shoot(fapp, admin, answer(m(E, E_OCT)), t0),
        shoot(fapp, admin, answer(m(OFFICE, OFFICE_OCT)), t0 + timedelta(seconds=15)),
        shoot(fapp, admin, answer(m(E, E_OCT)), t0 + timedelta(seconds=30)),
        shoot(fapp, admin, answer(m(CORR, CORR_V)), t0 + timedelta(seconds=50)),
        shoot(fapp, admin, answer(m(CORR2, CORR2_V)), t0 + timedelta(seconds=70)),
        shoot(fapp, admin, answer(m(OFFICE, OFFICE_OCT)), t0 + timedelta(seconds=110)),
    ]
    cards = all_cards(fapp)
    # walk order = time of each card's first photo
    assert [c.photo_ids for c in cards] == [[p[0], p[2]], [p[1], p[5]], [p[3]], [p[4]]]
    assert [c.key for c in cards] == [f"m{mid[s]}_20261001" for s in (E, OFFICE, CORR, CORR2)]
    assert [[r.meter.serial for r in c.rows] for c in cards] == [[E], [OFFICE], [CORR], [CORR2]]
    assert all(c.clean for c in cards), [c.blockers() for c in cards]
    assert cards[1].taken_at == t0 + timedelta(seconds=15) and cards[1].last_taken_at == t0 + timedelta(seconds=110)
    page = admin.get("/odecty/kontrola").text
    assert page.count('class="card review-card') == 4 and "série" not in page


def test_october_burst_with_serials_is_bulk_confirmable(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    p = [
        shoot(fapp, admin, answer(m(E, E_OCT)), OCT[0]),
        shoot(fapp, admin, answer(m(E, E_OCT)), OCT[1]),
        shoot(fapp, admin, answer(m(OFFICE, OFFICE_OCT)), OCT[2]),
        shoot(fapp, admin, answer(m(CORR, CORR_V), m(CORR2, CORR2_V)), OCT[3]),
        shoot(fapp, admin, answer(m(CORR, CORR_V), m(CORR2, CORR2_V)), OCT[4]),
    ]
    cards = all_cards(fapp)
    assert [c.photo_ids for c in cards] == [p[:2], [p[2]], p[3:]]
    frame = rows_by_meter(cards[2])
    assert set(frame) == {mid[CORR], mid[CORR2]}  # one photo of two meters: one card, a row per meter
    assert frame[mid[CORR]][0].photo_ids == p[3:] and frame[mid[CORR2]][0].photo_ids == p[3:]
    assert all(r.method == "exact" for c in cards for r in c.rows) and all(c.clean for c in cards)
    assert bulk(admin, cards).status_code == 200
    with fapp.state.sessionmaker() as db:
        new = list(db.scalars(select(Reading).where(Reading.photo_id.in_(p))))
        assert {x.meter_id for x in new} == {mid[E], mid[OFFICE], mid[CORR], mid[CORR2]}
        assert db.get(Photo, p[2]).meter_id == mid[OFFICE]  # each photo keeps its own meter


def test_multi_meter_photo_and_close_ups_are_one_card(fapp: FastAPI, admin: TestClient) -> None:
    """One overview photo of three meters links them; close-ups of each (shot in between other meters, or much
    later the same day) join that card. Another meter photographed in between stays a card of its own."""
    mid = ids(fapp)
    t0 = datetime(2026, 10, 1, 12, 0)
    overview = shoot(fapp, admin, answer(m(E, E_OCT), m(CORR, CORR_V), m(CORR2, CORR2_V)), t0)
    office = shoot(fapp, admin, answer(m(OFFICE, OFFICE_OCT)), t0 + timedelta(seconds=20))
    close_e = shoot(fapp, admin, answer(m(E, E_OCT)), t0 + timedelta(seconds=40))
    close_corr = shoot(fapp, admin, answer(m(CORR, CORR_V)), t0 + timedelta(seconds=55))
    close_corr2 = shoot(fapp, admin, answer(m(CORR2, CORR2_V)), t0 + timedelta(hours=3))
    cards = all_cards(fapp)
    assert [c.photo_ids for c in cards] == [[overview, close_e, close_corr, close_corr2], [office]]
    card = cards[0]
    rows = rows_by_meter(card)
    assert set(rows) == {mid[E], mid[CORR], mid[CORR2]} and all(len(v) == 1 for v in rows.values())
    assert rows[mid[E]][0].photo_ids == [overview, close_e]
    assert rows[mid[CORR2]][0].photo_ids == [overview, close_corr2]
    assert card.key == f"m{min(mid[E], mid[CORR], mid[CORR2])}_20261001" and card.clean


def test_october_burst_without_serials_is_not_glued(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    p = [
        shoot(fapp, admin, answer(m(E, E_OCT)), OCT[0]),  # serial read
        shoot(fapp, admin, answer(m(None, E_OCT)), OCT[1]),  # close-up, fits E AND ROLL
        shoot(fapp, admin, answer(m(None, OFFICE_OCT)), OCT[2]),  # fits OFFICE AND CONTROL
        shoot(fapp, admin, answer(m(None, CORR_V), m(None, CORR2_V)), OCT[3]),  # each fits one meter only
        shoot(fapp, admin, answer(readable=False, problems=["unreadable"]), OCT[4]),
    ]
    cards = all_cards(fapp)
    # nothing is glued by time: every photo without its own (unambiguous) meter is a card of its own
    assert [c.photo_ids for c in cards] == [[x] for x in p]
    assert [r.meter.meter_id for r in cards[0].rows] == [mid[E]]
    (r1,) = cards[1].rows
    (r2,) = cards[2].rows
    assert r1.meter is None and r2.meter is None
    # suggestions: the meter identified just before (and fitting the value) first - a suggestion, not a value
    assert r1.candidate_meter_ids[:2] == [mid[E], mid[ROLL]]
    assert r1.values == {"T1": E_OCT} and [c.photo_id for c in r1.candidates["T1"]] == [p[1]]  # its own value only
    assert set(r2.candidate_meter_ids[:2]) == {mid[OFFICE], mid[CONTROL]}
    # a frame whose two values each fit exactly one meter: proposed, but only as weak ("podle očekávaného stavu")
    frame = rows_by_meter(cards[3])
    assert frame[mid[CORR]][0].method == "values" and frame[mid[CORR2]][0].method == "values"
    assert cards[4].rows == [] and "nic nerozpoznáno" in cards[4].blockers()
    blockers = " | ".join(b for c in cards for b in c.blockers())
    assert "nepřiřazený elektroměr" in blockers and f"{CORR}: elektroměr určen jen podle očekávaného stavu" in blockers
    # bulk confirm writes nothing for the cards without a strongly identified meter
    with fapp.state.sessionmaker() as db:
        before = db.scalar(select(Reading.id).order_by(Reading.id.desc()))
    bulk(admin, cards[1:])
    with fapp.state.sessionmaker() as db:
        assert db.scalar(select(Reading.id).order_by(Reading.id.desc())) == before


def test_weak_links_join_one_card_but_never_merge_two(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    t0 = datetime(2026, 10, 1, 12, 0)
    corr = shoot(fapp, admin, answer(m(CORR, CORR_V)), t0)
    corr2 = shoot(fapp, admin, answer(m(CORR2, CORR2_V)), t0 + timedelta(seconds=10))
    # no serial, both values fit exactly one meter each - which belong to two different cards
    frame = shoot(fapp, admin, answer(m(None, CORR_V), m(None, CORR2_V)), t0 + timedelta(seconds=20))
    # no serial, the value fits only the corridor meter: joins its card (and merges into its row)
    close = shoot(fapp, admin, answer(m(None, CORR_V)), t0 + timedelta(seconds=30))
    cards = all_cards(fapp)
    assert [c.photo_ids for c in cards] == [[corr, close], [corr2], [frame]]
    (row,) = cards[0].rows
    assert row.meter.meter_id == mid[CORR] and row.photo_ids == [corr, close] and row.method == "exact"
    assert cards[0].clean  # the close-up agrees with the serial shot
    assert {r.method for r in cards[2].rows} == {"values"} and not cards[2].clean


def test_suggestions_come_from_photos_taken_shortly_before_or_after(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    t0 = datetime(2026, 10, 1, 12, 0)
    control = shoot(fapp, admin, answer(m(CONTROL, OFFICE_OCT)), t0)
    near_control = shoot(fapp, admin, answer(m(None, OFFICE_OCT)), t0 + timedelta(seconds=30))
    office = shoot(fapp, admin, answer(m(OFFICE, OFFICE_OCT)), t0 + timedelta(minutes=10))
    near_office = shoot(fapp, admin, answer(m(None, OFFICE_OCT)), t0 + timedelta(minutes=10, seconds=-40))
    cards = all_cards(fapp)
    # the same value fits both meters; the meter photographed next to the close-up is suggested first
    assert card_of(cards, near_control).rows[0].candidate_meter_ids[:2] == [mid[CONTROL], mid[OFFICE]]
    assert card_of(cards, near_office).rows[0].candidate_meter_ids[:2] == [mid[OFFICE], mid[CONTROL]]
    assert all(len(c.photo_ids) == 1 for c in cards)  # suggestions never group
    # the neighbour is confirmed (left the queue) - it still counts for suggestions
    (c_card,) = [c for c in cards if c.photo_ids == [control]]
    row = c_card.rows[0]
    from tests.test_capture_review import post_card

    res = post_card(
        admin,
        c_card,
        {row.key: {"meter": mid[CONTROL], "T1": str(int(OFFICE_OCT)), "acks": [i.code for i in row.all_issues()]}},
    )
    assert res.status_code == 200, res.text
    cards = all_cards(fapp)
    assert card_of(cards, near_control).rows[0].candidate_meter_ids[0] == mid[CONTROL]
    assert card_of(cards, office).rows[0].meter.meter_id == mid[OFFICE]


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
    cards = all_cards(fapp)
    assert [c.photo_ids for c in cards] == [[x] for x in p[:6]] + [[p[6]]]
    (unknown,) = cards[0].rows
    assert unknown.unknown_serial and unknown.serial_seen == NEW_SERIAL and cards[0].key == f"p{p[0]}"
    assert [r.meter.meter_id for r in cards[1].rows] == [mid[OFFICE]]
    assert cards[2].rows[0].meter is None and cards[2].rows[0].candidate_meter_ids[0] == mid[OFFICE]
    assert cards[5].rows[0].meter is None and cards[5].rows[0].candidate_meter_ids[0] == mid[E]
    assert {r.meter.meter_id for r in cards[6].rows} == {mid[CORR], mid[CORR2]}
    # a human picks the suggestion for the close-up: written from that photo
    from tests.test_capture_review import post_card

    loose = cards[2].rows[0]
    res = post_card(admin, cards[2], {loose.key: {"meter": mid[OFFICE], "T1": str(int(OFFICE_NOV))}}, den="2026-11-08")
    assert res.status_code == 200, res.text
    with fapp.state.sessionmaker() as db:
        new = db.scalar(select(Reading).where(Reading.meter_id == mid[OFFICE], Reading.value == int(OFFICE_NOV)))
        assert new is not None and new.photo_id == p[2]
        assert db.get(Photo, p[3]).meter_id is None  # the unreadable overview is not tied to any meter
    # the office card now sees the value as already recorded (confirming it writes nothing new)
    office_card = card_of(all_cards(fapp), p[1])
    assert office_card.rows[0].existing["T1"].value == int(OFFICE_NOV)


def test_same_meter_on_two_days_is_two_cards(fapp: FastAPI, admin: TestClient) -> None:
    """A tenant's late photo and the next walk uploaded in one batch: a card (and a reading) per day."""
    mid = ids(fapp)
    day1 = shoot(fapp, admin, answer(m(E, "10400")), datetime(2026, 9, 28, 18, 0))
    day2 = shoot(fapp, admin, answer(m(E, E_OCT)), datetime(2026, 10, 1, 12, 0))
    undated = shoot(fapp, admin, answer(m(E, "10450")), None)  # WhatsApp: no EXIF time - asks for a date
    cards = all_cards(fapp)
    assert [c.photo_ids for c in cards] == [[day1], [day2], [undated]]
    assert [c.key for c in cards] == [f"m{mid[E]}_20260928", f"m{mid[E]}_20261001", f"p{undated}"]
    assert cards[2].date_required
    from tests.test_capture_review import post_card

    for pid in (day1, day2):
        c = card_of(all_cards(fapp), pid)  # fresh: the first reading changes the second one's plausibility
        row = c.rows[0]
        acks = [i.code for i in row.all_issues()]
        res = post_card(admin, c, {row.key: {"meter": mid[E], "T1": row.values["T1"], "acks": acks}})
        assert res.status_code == 200, res.text
    with fapp.state.sessionmaker() as db:
        days = {
            r.taken_at.date().isoformat() for r in db.scalars(select(Reading).where(Reading.photo_id.in_([day1, day2])))
        }
    assert days == {"2026-09-28", "2026-10-01"}


def test_pending_photo_is_own_card_then_joins_its_meter(fapp: FastAPI, admin: TestClient) -> None:
    mid = ids(fapp)
    t0 = datetime(2026, 10, 1, 12, 0)
    first = shoot(fapp, admin, answer(m(E, E_OCT)), t0)
    q.process_photo(fapp, first)
    second = shoot(fapp, admin, answer(m(E, E_OCT)), t0 + timedelta(minutes=30))
    cards = all_cards(fapp, process=False)
    assert [c.photo_ids for c in cards] == [[first], [second]]
    assert cards[1].key == f"p{second}" and cards[1].extraction_pending and not cards[0].extraction_pending
    frag = admin.get(f"/odecty/kontrola/karta/p{second}").text
    assert 'hx-trigger="every 5s"' in frag and "rozpoznávám" in frag
    q.process_pending(fapp)
    (card,) = all_cards(fapp)
    assert card.photo_ids == [first, second] and card.key == f"m{mid[E]}_20261001"
    # the polling fragment of the pending card now returns the meter's card (capture.js drops the stale copy)
    frag = admin.get(f"/odecty/kontrola/karta/p{second}").text
    assert f'id="karta-{card.key}"' in frag and "every 5s" not in frag
    # the old meter card on the page has a stale photo set: confirming it is refused
    from tests.test_capture_review import post_card

    stale = card.__class__(**{**card.__dict__, "photos": card.photos[:1]})
    row = card.rows[0]
    res = post_card(admin, stale, {row.key: {"meter": mid[E], "T1": E_OCT}})
    assert res.status_code == 409


def test_photo_detail_shows_same_meter_photos_of_the_day(fapp: FastAPI, admin: TestClient) -> None:
    t0 = datetime(2026, 10, 1, 12, 0)
    a = shoot(fapp, admin, answer(m(E, E_OCT)), t0)
    other = shoot(fapp, admin, answer(m(OFFICE, OFFICE_OCT)), t0 + timedelta(seconds=5))
    b = shoot(fapp, admin, answer(m(E, E_OCT)), t0 + timedelta(hours=2))
    next_day = shoot(fapp, admin, answer(m(E, E_OCT)), t0 + timedelta(days=1))
    cards = all_cards(fapp)
    page = admin.get(f"/fotky/{a}").text
    assert "Další fotky téhož elektroměru z tohoto dne" in page
    assert f'href="/fotky/{b}"' in page and f'href="/fotky/{other}"' not in page
    assert f'href="/fotky/{next_day}"' not in page
    assert f"/odecty/kontrola#karta-{card_of(cards, a).key}" in page


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
    card = card_of(all_cards(fapp), photo.id)
    assert card.key == f"p{photo.id}"  # no meter of its own -> its own card
    (row,) = card.rows
    assert row.meter is None and row.method == "walk_mismatch"
    assert row.candidate_meter_ids[:2] == [mid[CORR], mid[E]]
    assert "nepatří k místu z obchůzky" in " ".join(card.blockers())
