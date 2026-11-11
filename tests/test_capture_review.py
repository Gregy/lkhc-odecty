"""Review queue: cards by recognised meter, conflicts, required dates, acknowledgements, bulk confirm, new meters."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.capture.review import Card, build_cards
from app.extraction import queue as q
from app.models import AuditLog, Meter, Photo, Reading
from tests import synthetic as syn
from tests.capture_helpers import answer, fake_extractor, make_jpeg, sha, upload

T0 = datetime(2026, 7, 1, 13, 45, 10)
BASE = 436  # imported readings


@pytest.fixture
def fapp(make_app: Callable[..., FastAPI], imported_db_url: str) -> FastAPI:
    return make_app(imported_db_url, extractor="fake")


@pytest.fixture
def admin(fapp: FastAPI, login_client: Callable[..., TestClient]) -> TestClient:
    return login_client(fapp, "admin")


_color = [10]


def shoot(
    app: FastAPI,
    client: TestClient,
    ans: dict[str, Any] | None,
    *,
    at: datetime | None,
    name: str = "f.jpg",
    reported_by: str = "",
) -> int:
    _color[0] += 7
    data = make_jpeg(taken=at, color=(_color[0] % 255, 50, 60))
    if ans is not None:
        fake_extractor(app).responses[sha(data)] = ans
    res = upload(client, data, name=name, reported_by=reported_by)
    assert res["ok"], res
    return res["id"]


def cards(app: FastAPI) -> list[Card]:
    with app.state.sessionmaker() as db:
        return build_cards(db)


def readings(app: FastAPI, meter_serial: str | None = None) -> list[Reading]:
    with app.state.sessionmaker() as db:
        qy = select(Reading).where(Reading.id > 0)
        if meter_serial:
            qy = qy.join(Meter).where(Meter.serial == meter_serial)
        return list(db.scalars(qy.order_by(Reading.id)))


def count(app: FastAPI) -> int:
    with app.state.sessionmaker() as db:
        return db.scalar(select(func.count(Reading.id))) or 0


def meter_id(app: FastAPI, serial: str) -> int:
    with app.state.sessionmaker() as db:
        return db.scalar(select(Meter.id).where(Meter.serial == serial))


def post_card(client: TestClient, card: Card, rows: dict[str, dict[str, Any]], **extra: Any):
    data: list[tuple[str, str]] = [("card", card.key), ("photo_ids", ",".join(map(str, card.photo_ids)))]
    for k, v in extra.items():
        data.append((k, str(v)))
    for key, row in rows.items():
        data.append(("row", key))
        data.append((f"meter_{key}", str(row.get("meter", ""))))
        for reg in ("T1", "T2"):
            if reg in row:
                data.append((f"v_{key}_{reg}", str(row[reg])))
        for a in row.get("acks", []):
            data.append((f"ack_{key}", a))
    return client.post(
        "/odecty/kontrola/potvrdit",
        content=urlencode(data),
        headers={"HX-Request": "true", "Content-Type": "application/x-www-form-urlencoded"},
    )


SER = syn.PARA_SERIAL
LAST = int(syn.value("D", syn.TYPO))  # last imported value (3.6.2026)
NEW = LAST + 27  # a plausible value on 1.7.2026
NEW_SERIAL = "91827364"  # not in the DB
PARA = {"serial": SER, "registers": [{"digits_seen": f"0{NEW}", "value": NEW}]}


def test_photos_of_one_meter_form_one_card_and_bulk_confirmed(fapp: FastAPI, admin: TestClient) -> None:
    a = shoot(fapp, admin, answer(PARA), at=T0)
    b = shoot(
        fapp, admin, answer({"serial": None, "registers": [{"digits_seen": f"0{NEW}"}]}), at=T0 + timedelta(seconds=5)
    )
    c = shoot(fapp, admin, answer(readable=False, problems=["glare"]), at=T0 + timedelta(seconds=9))
    other = shoot(fapp, admin, None, at=T0 + timedelta(minutes=5))
    q.process_pending(fapp)
    cs = cards(fapp)
    # the serial shot and the close-up whose value fits only this meter share a card; the unreadable
    # photo taken between them is not glued to it by time - it is its own card
    assert [x.photo_ids for x in cs] == [[a, b], [c], [other]]
    card = cs[0]
    assert card.key == f"m{meter_id(fapp, SER)}_20260701" and [x.key for x in cs[1:]] == [f"p{c}", f"p{other}"]
    assert len(card.rows) == 1
    row = card.rows[0]
    assert row.meter.serial == SER and row.values == {"T1": str(NEW)} and not row.conflicts
    assert card.clean, card.blockers()
    assert count(fapp) == BASE  # nothing confirmed by extraction or by viewing the queue
    assert admin.get("/odecty/kontrola").status_code == 200 and count(fapp) == BASE

    r = admin.post(
        "/odecty/kontrola/hromadne",
        data={
            "card": [card.key, f"p{other}"],
            f"photos_{card.key}": ",".join(map(str, card.photo_ids)),
            f"photos_p{other}": str(other),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    (new,) = readings(fapp, SER)[-1:]
    assert (
        new.value == D(NEW)
        and new.status == "confirmed"
        and new.source == "photo"
        and new.taken_at == datetime(2026, 7, 1)
    )
    assert new.photo_id == a and new.created_by_id is not None
    assert count(fapp) == BASE + 1  # the empty card ("other") was skipped
    with fapp.state.sessionmaker() as db:
        assert {p.review_status for p in db.scalars(select(Photo).where(Photo.id.in_([a, b])))} == {"confirmed"}
        assert db.get(Photo, other).review_status == db.get(Photo, c).review_status == "new"
        audit = db.scalars(
            select(AuditLog).where(AuditLog.entity_type == "readings", AuditLog.entity_id == new.id)
        ).one()
        assert audit.action == "create"


def test_conflict_in_meter_card_is_flagged_and_needs_a_choice(fapp: FastAPI, admin: TestClient) -> None:
    a = shoot(fapp, admin, answer(PARA), at=T0)
    shoot(
        fapp,
        admin,
        answer({"serial": None, "registers": [{"digits_seen": f"0{NEW + 2}"}]}),
        at=T0 + timedelta(seconds=4),
    )
    q.process_pending(fapp)
    (card,) = cards(fapp)
    row = card.rows[0]
    assert row.conflicts == {"T1"} and "T1" not in row.values  # no silent vote
    assert row.distinct_values("T1") == [D(NEW), D(NEW + 2)]
    assert not card.clean
    # bulk refuses it
    admin.post(
        "/odecty/kontrola/hromadne", data={"card": card.key, f"photos_{card.key}": ",".join(map(str, card.photo_ids))}
    )
    assert count(fapp) == BASE
    # confirming without picking a value writes nothing
    r = post_card(admin, card, {row.key: {"meter": row.meter.meter_id}})
    assert r.status_code == 422 and count(fapp) == BASE
    r = post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(NEW + 2)}}, den="2026-07-01")
    assert r.status_code == 200, r.text
    assert readings(fapp, SER)[-1].value == D(NEW + 2)
    assert a in card.photo_ids


def test_photo_without_time_requires_a_date(fapp: FastAPI, admin: TestClient) -> None:
    pid = shoot(fapp, admin, answer(PARA), at=None, name="IMG-20260701-WA0003.jpg", reported_by="Nájemce A (WhatsApp)")
    q.process_pending(fapp)
    (card,) = cards(fapp)
    assert card.date_required and card.suggested is not None and card.suggested.value == datetime(2026, 7, 1)
    assert not card.clean and "chybí datum pořízení" in card.blockers()
    page = admin.get("/odecty/kontrola").text
    assert "Použít 1." in page and "WhatsApp" in page
    row = card.rows[0]
    r = post_card(
        admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(NEW)}}, reported_by="Nájemce A (WhatsApp)"
    )
    assert r.status_code == 422 and "datum" in r.text and count(fapp) == BASE
    r = post_card(
        admin,
        card,
        {row.key: {"meter": row.meter.meter_id, "T1": str(NEW)}},
        den="2026-07-01",
        reported_by="Nájemce A (WhatsApp)",
    )
    assert r.status_code == 200
    new = readings(fapp, SER)[-1]
    assert new.taken_at == datetime(2026, 7, 1) and new.reported_by == "Nájemce A (WhatsApp)"
    with fapp.state.sessionmaker() as db:
        assert db.get(Photo, pid).taken_at == datetime(2026, 7, 1)
    # future dates are refused
    pid2 = shoot(fapp, admin, None, at=None)
    card2 = next(c for c in cards(fapp) if pid2 in c.photo_ids)
    r = post_card(
        admin,
        card2,
        {"n1": {"meter": meter_id(fapp, SER), "T1": str(NEW + 30)}},
        den=(date.today() + timedelta(days=2)).isoformat(),
    )
    assert r.status_code == 422


def test_plausibility_issue_must_be_acknowledged(fapp: FastAPI, admin: TestClient) -> None:
    low = LAST - 40
    shoot(fapp, admin, answer({"serial": SER, "registers": [{"digits_seen": f"0{low}"}]}), at=T0)
    q.process_pending(fapp)
    (card,) = cards(fapp)
    row = card.rows[0]
    assert [i.code for i in row.issues["T1"]] == ["decrease"]
    r = post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(low)}})
    assert r.status_code == 422 and "decrease" in r.text and count(fapp) == BASE
    r = post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(low), "acks": ["decrease"]}})
    assert r.status_code == 200
    new = readings(fapp, SER)[-1]
    assert new.value == D(low)
    with fapp.state.sessionmaker() as db:
        note = (
            db.scalars(select(AuditLog).where(AuditLog.entity_type == "readings", AuditLog.entity_id == new.id))
            .one()
            .note
        )
    assert "[decrease]" in note


def test_decimal_slip_and_echo_need_acknowledgement(fapp: FastAPI, admin: TestClient) -> None:
    # NEW * 10 instead of NEW: a missed decimal / extra digit
    shoot(fapp, admin, answer({"serial": SER, "registers": [{"digits_seen": f"{NEW}0"}]}), at=T0)
    q.process_pending(fapp)
    (card,) = cards(fapp)
    codes = {i.code for i in card.rows[0].issues["T1"]}
    assert {"decimal_slip", "high"} <= codes
    admin.post("/odecty/kontrola/zahodit", data={"photo_ids": ",".join(map(str, card.photo_ids))})
    # model returns exactly the last reading as shown in the prompt (1 kWh low) -> possible echo
    with fapp.state.sessionmaker() as db:
        from app.extraction.hints import meter_hint_list

        exp = next(h for h in meter_hint_list(db, T0) if h.serial == SER).reg("T1").last_value - 1
    shoot(fapp, admin, answer({"serial": SER, "registers": [{"digits_seen": str(exp)}]}), at=T0)
    q.process_pending(fapp)
    (card,) = cards(fapp)
    assert "same_as_hint" in [i.code for i in card.rows[0].issues["T1"]] and not card.clean
    row = card.rows[0]
    assert post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(exp)}}).status_code == 422
    # typing a different value removes the extraction flag
    assert post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(exp + 1)}}).status_code == 200


CORRIDOR_LAST = int(syn.value("T", syn.TYPO))
CORRIDOR2_LAST = int(syn.value("V", syn.TYPO))  # idle meter


def test_two_meters_in_one_photo(fapp: FastAPI, admin: TestClient) -> None:
    shoot(
        fapp,
        admin,
        answer(
            {"serial": syn.CORRIDOR_SERIAL, "registers": [{"digits_seen": f"0{CORRIDOR_LAST + 17}"}]},
            {"serial": syn.CORRIDOR2_SERIAL, "registers": [{"digits_seen": f"{CORRIDOR2_LAST:05d}"}]},
        ),
        at=T0,
    )
    q.process_pending(fapp)
    (card,) = cards(fapp)
    assert sorted(r.meter.serial for r in card.rows) == sorted([syn.CORRIDOR_SERIAL, syn.CORRIDOR2_SERIAL])
    assert card.clean, card.blockers()  # idle meter showing its last value again is not an "echo"
    admin.post(
        "/odecty/kontrola/hromadne", data={"card": card.key, f"photos_{card.key}": ",".join(map(str, card.photo_ids))}
    )
    assert count(fapp) == BASE + 2


def test_existing_reading_same_day_needs_overwrite_ack(fapp: FastAPI, admin: TestClient) -> None:
    shoot(fapp, admin, answer({"serial": SER, "registers": [{"digits_seen": f"0{LAST}"}]}), at=datetime(2026, 6, 3, 10))
    q.process_pending(fapp)
    (card,) = cards(fapp)
    row = card.rows[0]
    assert row.existing["T1"].value == D(LAST) and not row.issues["T1"]  # same value: nothing to do
    assert post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(LAST)}}).status_code == 200
    assert count(fapp) == BASE
    shoot(
        fapp,
        admin,
        answer({"serial": SER, "registers": [{"digits_seen": f"0{LAST + 1}"}]}),
        at=datetime(2026, 6, 3, 11),
    )
    q.process_pending(fapp)
    (card,) = cards(fapp)
    row = card.rows[0]
    codes = [i.code for i in row.issues["T1"]]
    assert "duplicate" in codes  # (the imported 3.6. value itself is unusually high -> "high" too)
    assert post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(LAST + 1)}}).status_code == 422
    acks = [c for c in codes if c != "duplicate"]
    assert (
        post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(LAST + 1), "acks": acks}}).status_code
        == 422
    )
    assert (
        post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(LAST + 1), "acks": codes}}).status_code
        == 200
    )
    assert [r.value for r in readings(fapp, SER) if r.taken_at == datetime(2026, 6, 3)] == [D(LAST + 1)]


def test_unknown_serial_offers_new_meter(fapp: FastAPI, admin: TestClient) -> None:
    shoot(fapp, admin, answer({"serial": NEW_SERIAL, "registers": [{"digits_seen": "0000004", "value": 0.04}]}), at=T0)
    q.process_pending(fapp)
    (card,) = cards(fapp)
    row = card.rows[0]
    assert row.meter is None and row.unknown_serial and row.serial_seen == NEW_SERIAL
    assert f"neznámé výr. číslo {NEW_SERIAL}" in card.blockers()
    page = admin.get("/odecty/kontrola").text
    assert "Neznámé výrobní číslo" in page and 'action="/odecty/kontrola/elektromer"' in page
    with fapp.state.sessionmaker() as db:
        office = db.get(Meter, meter_id(fapp, syn.NEW_OFFICE_SERIAL)).meter_point_id
    r = admin.post(
        "/odecty/kontrola/elektromer",
        data={
            "serial": NEW_SERIAL,
            "point_id": str(office),
            "registers": "T1",
            "digits": "5",
            "decimals_shown": "2",
            "installed_on": "2026-07-01",
            "replace_meter_id": str(meter_id(fapp, syn.NEW_OFFICE_SERIAL)),
            "back": card.key,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    with fapp.state.sessionmaker() as db:
        new = db.scalar(select(Meter).where(Meter.serial == NEW_SERIAL))
        old = db.get(Meter, meter_id(fapp, syn.NEW_OFFICE_SERIAL))
        assert new.meter_point_id == office and new.decimals_shown == 2 and old.removed_on == date(2026, 7, 1)
    (card,) = cards(fapp)
    row = card.rows[0]
    assert row.meter.serial == NEW_SERIAL and row.method == "exact" and row.values == {"T1": "0.04"}


def test_unassigned_photo_manual_meter_choice_and_discard(fapp: FastAPI, admin: TestClient) -> None:
    pid = shoot(fapp, admin, answer({"serial": None, "registers": [{"digits_seen": "77777"}]}), at=T0)
    q.process_pending(fapp)
    (card,) = cards(fapp)
    row = card.rows[0]
    assert row.meter is None and not card.clean
    r = post_card(admin, card, {row.key: {"meter": "", "T1": str(NEW)}})
    assert r.status_code == 422 and "Vyberte elektroměr" in r.text
    r = post_card(admin, card, {row.key: {"meter": meter_id(fapp, SER), "T1": str(NEW)}})
    assert r.status_code == 200 and readings(fapp, SER)[-1].value == D(NEW)
    # discard another one
    pid2 = shoot(fapp, admin, None, at=T0 + timedelta(hours=1))
    admin.post("/odecty/kontrola/zahodit", data={"photo_ids": str(pid2)})
    with fapp.state.sessionmaker() as db:
        assert db.get(Photo, pid2).review_status == "discarded" and db.get(Photo, pid).review_status == "confirmed"
    assert cards(fapp) == []


def test_stale_card_is_not_confirmed(fapp: FastAPI, admin: TestClient) -> None:
    shoot(fapp, admin, answer(PARA), at=T0)
    shoot(fapp, admin, answer(PARA), at=T0 + timedelta(seconds=3))
    q.process_pending(fapp)
    (card,) = cards(fapp)
    assert len(card.photo_ids) == 2
    shoot(fapp, admin, answer(PARA), at=T0 + timedelta(hours=3))  # same meter, same day: joins the card meanwhile
    q.process_pending(fapp)
    r = post_card(admin, card, {card.rows[0].key: {"meter": card.rows[0].meter.meter_id, "T1": str(NEW)}})
    assert r.status_code == 409 and count(fapp) == BASE
    admin.post(
        "/odecty/kontrola/hromadne", data={"card": card.key, f"photos_{card.key}": ",".join(map(str, card.photo_ids))}
    )
    assert count(fapp) == BASE
    # a card that disappeared (e.g. confirmed elsewhere) is answered with a refresh, nothing written
    r = post_card(admin, card.__class__(**{**card.__dict__, "key": "p999999"}), {})
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true" and count(fapp) == BASE


def test_reader_sees_queue_but_cannot_confirm(fapp: FastAPI, admin: TestClient, login_client) -> None:
    shoot(fapp, admin, answer(PARA), at=T0)
    q.process_pending(fapp)
    reader = login_client(fapp, "reader")
    page = reader.get("/odecty/kontrola")
    assert page.status_code == 200 and "Potvrdit a zapsat" not in page.text
    (card,) = cards(fapp)
    r = post_card(reader, card, {card.rows[0].key: {"meter": card.rows[0].meter.meter_id, "T1": str(NEW)}})
    assert r.status_code == 403
    assert reader.post("/odecty/kontrola/hromadne", data={"card": card.key}).status_code == 403
    assert reader.post("/odecty/kontrola/zahodit", data={"photo_ids": "1"}).status_code == 403
    assert count(fapp) == BASE


def test_card_fragment_polls_while_extracting(fapp: FastAPI, admin: TestClient) -> None:
    pid = shoot(fapp, admin, answer(PARA), at=T0)
    frag = admin.get(f"/odecty/kontrola/karta/p{pid}")
    assert 'hx-trigger="every 5s"' in frag.text and "rozpoznávám" in frag.text
    q.process_pending(fapp)
    frag = admin.get(f"/odecty/kontrola/karta/p{pid}")
    assert "every 5s" not in frag.text and f'value="{NEW}"' in frag.text


def test_second_read_disagreement_is_a_doubt_in_the_review(
    make_app: Callable[..., FastAPI], imported_db_url: str, login_client: Callable[..., TestClient]
) -> None:
    """EXTRACTOR_VERIFY end to end: settings -> queue extractor -> stored result -> review card."""
    app = make_app(
        imported_db_url,
        extractor="openai_compat",
        extractor_verify=True,
        extractor_base_url="http://model.invalid/v1",
        extractor_model="stub-vision",
    )
    admin = login_client(app, "admin")
    ex = q.get_extractor(app)
    assert ex.verify is True  # type: ignore[attr-defined]
    second = answer({"serial": SER, "registers": [{"digits_seen": f"0{NEW + 1}"}]})
    bodies = [answer(PARA), second]
    calls: list[int] = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        content = json.dumps(bodies.pop(0))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"cost": 0.001}})

    ex.transport = httpx.MockTransport(endpoint)  # type: ignore[attr-defined]
    shoot(app, admin, None, at=T0)
    q.process_pending(app)
    assert len(calls) == 2
    (card,) = cards(app)
    row = card.rows[0]
    assert row.meter.serial == SER and row.values == {"T1": str(NEW)}  # first read proposed, but doubtful
    assert [i.code for i in row.issues["T1"]] == ["low_confidence"] and not card.clean
    page = admin.get("/odecty/kontrola").text
    assert f"druhé čtení „0{NEW + 1}“" in page
    r = post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(NEW)}})
    assert r.status_code == 422 and count(app) == BASE
    r = post_card(admin, card, {row.key: {"meter": row.meter.meter_id, "T1": str(NEW), "acks": ["low_confidence"]}})
    assert r.status_code == 200 and readings(app, SER)[-1].value == D(NEW)
