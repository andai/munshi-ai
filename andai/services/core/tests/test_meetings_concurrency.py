"""Two people paste the same link at the same moment: one andai meeting, one platform dispatch."""

from __future__ import annotations

import threading

from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from conftest import give_principal, make_harness, new_member, role_org, sign_in, zoom_link

JOIN = "/api/v1/meetings/join"


def race(clients, link: str):
    """Send one join per client, released together; the dispatch stalls so both are in flight."""
    barrier = threading.Barrier(len(clients))
    responses: list = [None] * len(clients)

    def go(i: int) -> None:
        barrier.wait()
        responses[i] = clients[i].post(JOIN, json={"join_url": link})

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(clients))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return responses


def live_rows(engine: Engine, org_id) -> int:
    with transaction(engine, org_id=org_id) as tx:
        return tx.scalar(text("SELECT count(*) FROM meetings"))


def test_simultaneous_pastes_by_one_member_make_one_meeting(app_engine: Engine) -> None:
    harness = make_harness(app_engine)
    org = role_org(app_engine)
    give_principal(harness, app_engine, org.owner.org_id)
    harness.gateway.dispatch_delay = 0.5
    member = org.member
    link = zoom_link()

    responses = race([sign_in(harness, member), sign_in(harness, member)], link)

    assert sorted(r.status_code for r in responses) == [200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(harness.gateway.dispatches) == 1
    assert live_rows(app_engine, org.owner.org_id) == 1


def test_a_colleague_racing_without_access_gets_409_and_no_id(app_engine: Engine) -> None:
    harness = make_harness(app_engine)
    org = role_org(app_engine)
    give_principal(harness, app_engine, org.owner.org_id)
    harness.gateway.dispatch_delay = 0.5
    colleague = new_member(app_engine, role="member", org_id=org.owner.org_id)
    link = zoom_link()

    responses = race([sign_in(harness, org.member), sign_in(harness, colleague)], link)

    by_status = {r.status_code: r for r in responses}
    assert set(by_status) == {201, 409}, [r.text for r in responses]
    winner, loser = by_status[201], by_status[409]
    assert loser.json() == {
        "detail": "the bot is already in this meeting for your organisation",
        "code": "already_in_meeting",
    }
    assert winner.json()["id"] not in loser.text
    assert len(harness.gateway.dispatches) == 1
    assert live_rows(app_engine, org.owner.org_id) == 1


def test_many_simultaneous_pastes_still_make_one_meeting(app_engine: Engine) -> None:
    harness = make_harness(app_engine)
    org = role_org(app_engine)
    give_principal(harness, app_engine, org.owner.org_id)
    harness.gateway.dispatch_delay = 0.3
    link = zoom_link()
    clients = [sign_in(harness, org.admin) for _ in range(6)]

    responses = race(clients, link)

    assert [r.status_code for r in responses].count(201) == 1
    assert all(r.status_code in (200, 201) for r in responses), [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(harness.gateway.dispatches) == 1
