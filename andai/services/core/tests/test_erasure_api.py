"""Asking for an erasure through the API (OPS-02): `POST /api/v1/org/users/{id}/erasure` and
`GET /api/v1/org/erasures`. Who may ask, the guards, that access ends in the request's
transaction, the audit event, and that the listing holds ids and states only."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from conftest import (
    LOGIN_PATH,
    PASSWORD,
    Harness,
    RoleOrg,
    make_harness,
    new_member,
    role_org,
    session_count,
    sign_in,
)


def _path(user_id: uuid.UUID) -> str:
    return f"/api/v1/org/users/{user_id}/erasure"


@pytest.fixture
def world(app_engine: Engine) -> tuple[Harness, RoleOrg]:
    return make_harness(app_engine), role_org(app_engine)


def _requests(superuser_engine: Engine, subject: uuid.UUID) -> list[dict]:
    with superuser_engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT id, org_id, audit_org_id, requested_by, source, account, state, step"
                    " FROM erasure_requests WHERE subject_user_id = :s ORDER BY created_at"
                ),
                {"s": subject},
            ).mappings()
        ]


def _memberships(superuser_engine: Engine, user_id: uuid.UUID) -> tuple[int, int]:
    with superuser_engine.connect() as conn:
        orgs = conn.execute(
            text("SELECT count(*) FROM org_memberships WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
        teams = conn.execute(
            text("SELECT count(*) FROM team_memberships WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
    return orgs, teams


def _audit(superuser_engine: Engine, org_id: uuid.UUID, subject: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT action, actor_user_id FROM audit_events WHERE org_id = :o"
                    " AND target = :t AND action LIKE 'user.erase.%' ORDER BY at"
                ),
                {"o": org_id, "t": f"user:{subject}"},
            )
        ]


@pytest.mark.parametrize("caller", ["owner", "admin"])
def test_an_admin_erases_a_member_and_access_ends_at_once(
    world, app_engine: Engine, superuser_engine: Engine, caller: str
) -> None:
    harness, org = world
    subject = org.member
    subject_client = sign_in(harness, subject)
    assert subject_client.get("/api/v1/me").status_code == 200
    assert _memberships(superuser_engine, subject.user_id) == (1, 2)
    admin = sign_in(harness, org.by_role(caller))

    response = admin.post(_path(subject.user_id), json={"confirm": subject.email.upper()})

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"id", "state", "eraseBy"}
    assert body["state"] == "pending"
    erase_by = datetime.fromisoformat(body["eraseBy"])
    assert erase_by - harness.clock() == timedelta(hours=24)
    # Access ends in the request's transaction: membership, teams and sessions are gone.
    assert _memberships(superuser_engine, subject.user_id) == (0, 0)
    assert session_count(superuser_engine, subject.user_id) == 0
    assert subject_client.get("/api/v1/me").status_code == 401
    relogin = harness.client().post(
        LOGIN_PATH, json={"email": subject.email, "password": PASSWORD}
    )
    wrong = harness.client().post(LOGIN_PATH, json={"email": subject.email, "password": "nope"})
    assert relogin.status_code == wrong.status_code == 401
    assert relogin.json() == wrong.json()
    [request] = _requests(superuser_engine, subject.user_id)
    assert request["id"] == uuid.UUID(body["id"])
    assert request["org_id"] == request["audit_org_id"] == org.owner.org_id
    assert request["requested_by"] == org.by_role(caller).user_id
    assert (request["source"], request["account"], request["state"], request["step"]) == (
        "org_admin", True, "pending", "stop_live"
    )
    assert _audit(superuser_engine, org.owner.org_id, subject.user_id) == [
        ("user.erase.request", org.by_role(caller).user_id)
    ]


def test_the_account_stays_while_another_membership_remains(
    world, app_engine: Engine, superuser_engine: Engine
) -> None:
    harness, org = world
    elsewhere = new_member(app_engine)
    from andai_core.db.models import OrgMembership
    from andai_core.db.session import transaction

    with transaction(app_engine, org_id=elsewhere.org_id) as tx:
        tx.add(OrgMembership(org_id=elsewhere.org_id, user_id=org.member.user_id, role="member"))
    admin = sign_in(harness, org.admin)

    response = admin.post(_path(org.member.user_id), json={"confirm": org.member.email})

    assert response.status_code == 202, response.text
    [request] = _requests(superuser_engine, org.member.user_id)
    assert request["account"] is False
    assert _memberships(superuser_engine, org.member.user_id) == (1, 0)
    # They can still sign in, into the org they are left in.
    client = sign_in(harness, org.member)
    assert client.get("/api/v1/me").json()["org"]["id"] == str(elsewhere.org_id)


def test_a_wrong_confirmation_changes_nothing(world, superuser_engine: Engine) -> None:
    harness, org = world
    admin = sign_in(harness, org.admin)

    for confirm in ("someone-else@example.test", org.lead.email):
        response = admin.post(_path(org.member.user_id), json={"confirm": confirm})
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "confirm_mismatch"
    assert admin.post(_path(org.member.user_id), json={}).status_code == 422
    assert _requests(superuser_engine, org.member.user_id) == []
    assert _memberships(superuser_engine, org.member.user_id) == (1, 2)


def test_the_owner_and_oneself_cannot_be_erased(world, superuser_engine: Engine) -> None:
    harness, org = world
    admin = sign_in(harness, org.admin)
    owner = sign_in(harness, org.owner)

    to_owner = admin.post(_path(org.owner.user_id), json={"confirm": org.owner.email})
    self_admin = admin.post(_path(org.admin.user_id), json={"confirm": org.admin.email})
    self_owner = owner.post(_path(org.owner.user_id), json={"confirm": org.owner.email})

    assert (to_owner.status_code, to_owner.json()["code"]) == (409, "owner_cannot_be_erased")
    assert (self_admin.status_code, self_admin.json()["code"]) == (409, "cannot_erase_self")
    assert (self_owner.status_code, self_owner.json()["code"]) == (409, "cannot_erase_self")
    for member in (org.owner, org.admin):
        assert _requests(superuser_engine, member.user_id) == []


def test_a_second_request_is_refused_while_one_is_open(world, superuser_engine: Engine) -> None:
    harness, org = world
    admin = sign_in(harness, org.admin)
    first = admin.post(_path(org.member.user_id), json={"confirm": org.member.email})
    assert first.status_code == 202

    again = admin.post(_path(org.member.user_id), json={"confirm": org.member.email})

    assert (again.status_code, again.json()["code"]) == (409, "erasure_pending")
    assert len(_requests(superuser_engine, org.member.user_id)) == 1


@pytest.mark.parametrize("caller", ["member", "lead"])
def test_members_and_leads_may_not_ask(world, superuser_engine: Engine, caller: str) -> None:
    harness, org = world
    client = sign_in(harness, org.by_role(caller))
    target = org.admin

    response = client.post(_path(target.user_id), json={"confirm": target.email})
    listing = client.get("/api/v1/org/erasures")

    assert response.status_code == 403
    assert listing.status_code == 403
    assert _requests(superuser_engine, target.user_id) == []


def test_a_person_of_another_org_is_no_member(
    world, app_engine: Engine, superuser_engine: Engine
) -> None:
    harness, org = world
    stranger = new_member(app_engine, role="member", org_id=new_member(app_engine).org_id)
    admin = sign_in(harness, org.admin)

    response = admin.post(_path(stranger.user_id), json={"confirm": stranger.email})
    unknown = admin.post(_path(uuid.uuid4()), json={"confirm": "x@example.test"})

    assert (response.status_code, response.json()["code"]) == (404, "no_such_member")
    assert unknown.status_code == 404
    assert _requests(superuser_engine, stranger.user_id) == []
    assert _memberships(superuser_engine, stranger.user_id) == (1, 0)


def test_the_listing_holds_ids_and_states_only(world, app_engine: Engine) -> None:
    harness, org = world
    admin = sign_in(harness, org.admin)
    for subject in (org.member, org.lead):
        harness.clock.advance(seconds=1)
        sent = admin.post(_path(subject.user_id), json={"confirm": subject.email})
        assert sent.status_code == 202

    body = admin.get("/api/v1/org/erasures").json()

    assert body["next"] is None
    assert [item["subjectId"] for item in body["items"]] == [
        str(org.lead.user_id), str(org.member.user_id)
    ]
    for item in body["items"]:
        assert set(item) == {"id", "subjectId", "state", "step", "createdAt", "finishedAt", "error"}
        assert (item["state"], item["step"], item["finishedAt"], item["error"]) == (
            "pending", "stop_live", None, None
        )
    text_body = admin.get("/api/v1/org/erasures").text.lower()
    for subject in (org.member, org.lead):
        assert subject.email.lower() not in text_body
    assert "test member" not in text_body
    # Another org's admin sees none of them.
    other = sign_in(harness, new_member(app_engine))
    assert other.get("/api/v1/org/erasures").json() == {"items": [], "next": None}
    assert admin.get("/api/v1/org/erasures", params={"cursor": "bogus!"}).status_code == 422


def test_the_listing_pages_by_fifty(world, app_engine: Engine) -> None:
    harness, org = world
    from andai_core.db.session import transaction
    from andai_core.erasure.requests import create_request

    with transaction(app_engine, org_id=org.owner.org_id) as tx:
        for n in range(53):
            create_request(
                tx,
                subject=uuid.uuid4(),
                org_id=org.owner.org_id,
                audit_org_id=org.owner.org_id,
                source="operator",
                requested_by=None,
                account=False,
                now=harness.clock() + timedelta(seconds=n),
            )
    admin = sign_in(harness, org.admin)

    first = admin.get("/api/v1/org/erasures").json()
    second = admin.get("/api/v1/org/erasures", params={"cursor": first["next"]}).json()

    assert len(first["items"]) == 50 and first["next"]
    assert len(second["items"]) == 3 and second["next"] is None
    ids = [i["id"] for i in first["items"] + second["items"]]
    assert len(set(ids)) == 53
