"""An erased account after the real path (admin request, then the runner's account step): signing
in with its former or its new address fails exactly like a disabled account, a password reset for
it sends nothing and answers like an unknown address, it is in no member list or directory, and the
records it leaves behind read as "Erased user"."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import Engine, text

from andai_core.db.models import TOMBSTONE_USER_ID
from andai_core.erasure import runner
from andai_core.meetings.service import Meetings
from conftest import (
    LOGIN_PATH,
    PASSWORD,
    Harness,
    Member,
    make_harness,
    new_member,
    seed_meeting,
    sign_in,
)

RESET = "/api/v1/auth/password-reset"


def _erase(harness: Harness, engine: Engine, admin: Member, subject: Member) -> uuid.UUID:
    response = sign_in(harness, admin).post(
        f"/api/v1/org/users/{subject.user_id}/erasure", json={"confirm": subject.email}
    )
    assert response.status_code == 202, response.text
    request_id = uuid.UUID(response.json()["id"])
    s = harness.services
    meetings = Meetings(engine, s.keyring, s.gateway, s.settings, s.clock)
    state = asyncio.run(runner.run(engine, meetings, admin.org_id, request_id, clock=s.clock))
    assert state == "done"
    return request_id


@pytest.fixture
def erased(app_engine: Engine, superuser_engine: Engine):
    harness = make_harness(app_engine)
    admin = new_member(app_engine)
    subject = new_member(app_engine, role="member", org_id=admin.org_id)
    sign_in(harness, subject)  # they acted in the org: a login.success names them
    request_id = _erase(harness, app_engine, admin, subject)
    with superuser_engine.connect() as conn:
        status, email = conn.execute(
            text("SELECT status, email FROM users WHERE id = :u"), {"u": subject.user_id}
        ).one()
    assert (status, email) == ("erased", f"erased-{request_id}@munshi.invalid")
    return harness, admin, subject, email


def test_signing_in_fails_exactly_like_a_disabled_account(
    erased, app_engine: Engine, superuser_engine: Engine
) -> None:
    harness, _, subject, new_address = erased
    disabled = new_member(app_engine)
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET status = 'disabled' WHERE id = :u"), {"u": disabled.user_id}
        )

    baseline = harness.client(ip="198.51.100.41").post(
        LOGIN_PATH, json={"email": disabled.email, "password": PASSWORD}
    )
    former = harness.client(ip="198.51.100.42").post(
        LOGIN_PATH, json={"email": subject.email, "password": PASSWORD}
    )
    current = harness.client(ip="198.51.100.43").post(
        LOGIN_PATH, json={"email": new_address, "password": PASSWORD}
    )

    for response in (former, current):
        assert response.status_code == baseline.status_code == 401
        assert response.content == baseline.content
        assert "set-cookie" not in response.headers


def test_a_password_reset_sends_nothing_and_answers_like_an_unknown_address(erased) -> None:
    harness, _, subject, new_address = erased
    before = len(harness.mailer.outbox)

    unknown = harness.client().post(RESET, json={"email": "nobody-at-all@example.test"})
    answers = [
        harness.client().post(RESET, json={"email": e}) for e in (subject.email, new_address)
    ]

    for response in answers:
        assert response.status_code == unknown.status_code
        assert response.content == unknown.content
    assert len(harness.mailer.outbox) == before


def test_no_member_list_or_directory_shows_them(erased) -> None:
    harness, admin, subject, _ = erased
    client = sign_in(harness, admin)

    users = client.get("/api/v1/org/users").json()["users"]
    people = client.get("/api/v1/org/directory").json()["users"]

    for listing in (users, people):
        ids = {u["id"] for u in listing}
        assert str(subject.user_id) not in ids
        assert str(TOMBSTONE_USER_ID) not in ids


def test_what_they_leave_behind_reads_as_erased_user(erased, app_engine: Engine) -> None:
    harness, admin, subject, _ = erased
    client = sign_in(harness, admin)
    # A meeting whose requester was re-pointed at the tombstone reads "Erased user".
    meeting_id = seed_meeting(app_engine, admin.org_id, TOMBSTONE_USER_ID, None)

    view = client.get(f"/api/v1/meetings/{meeting_id}").json()
    trail = client.get(
        "/api/v1/org/audit", params={"action": "login.success,user.erase.complete"}
    ).json()["items"]

    assert view["requested_by_name"] == "Erased user"
    actors = {item["actor"]["id"]: item["actor"]["name"] for item in trail}
    assert actors[str(subject.user_id)] == "Erased user"
    completed = [i for i in trail if i["action"] == "user.erase.complete"]
    assert [i["target"]["id"] for i in completed] == [str(subject.user_id)]
    # They acted on this page (login.success), so the target resolves to the same anonymous name.
    assert completed[0]["target"]["label"] == "Erased user"
