"""Sharing a meeting inside the org (PORT-03): `GET/POST /api/v1/meetings/{id}/shares` and
`DELETE /api/v1/meetings/{id}/shares/{share_id}`, their audit events, and what the meeting view
tells each caller (`permissions`, `shared_with`).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text

from andai_core import audit
from andai_core.db.session import transaction
from andai_core.meetings import shares
from conftest import (
    CALLERS,
    DELETERS,
    SHARERS,
    STOPPERS,
    AccessOrg,
    access_org,
    new_member,
    new_team,
)

ARROW = "→"


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> AccessOrg:
    w = access_org(app_engine)
    w.meeting_id = w.meeting()
    return w


def path(meeting_id: uuid.UUID, share_id: str | uuid.UUID | None = None) -> str:
    base = f"/api/v1/meetings/{meeting_id}/shares"
    return base if share_id is None else f"{base}/{share_id}"


def share_rows(superuser_engine: Engine, meeting_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            (row.grantee_type, row.grantee_id)
            for row in conn.execute(
                text(
                    "SELECT grantee_type, grantee_id FROM meeting_shares WHERE meeting_id = :m"
                    " ORDER BY grantee_type, grantee_id"
                ),
                {"m": meeting_id},
            )
        ]


def audit_rows(superuser_engine: Engine, meeting_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT action, actor_user_id, target FROM audit_events"
                    " WHERE target LIKE :t ORDER BY at, action"
                ),
                {"t": f"meeting:{meeting_id} {ARROW} %"},
            )
        ]


def _refused(response, code: str = "no_such_meeting") -> bool:
    return response.status_code == 404 and response.json()["code"] == code


# ─── grant and revoke ─────────────────────────────────────────────────────────────────────────────


def test_grant_to_a_user_then_revoke(world: AccessOrg, superuser_engine: Engine) -> None:
    meeting_id = world.meeting(shares=False)
    requester = world.client("requester")
    newcomer = world.people["unrelated"]
    reader = world.client("unrelated")
    assert _refused(reader.get(f"/api/v1/meetings/{meeting_id}"))

    granted = requester.post(
        path(meeting_id), json={"grantee_type": "user", "grantee_id": str(newcomer.user_id)}
    )

    assert granted.status_code == 201, granted.text
    share = granted.json()
    assert set(share) == {"id", "grantee_type", "grantee_id", "name", "granted_by", "created_at"}
    assert (share["grantee_type"], share["grantee_id"], share["name"]) == (
        "user", str(newcomer.user_id), "Test Member"
    )
    assert share_rows(superuser_engine, meeting_id) == [("user", newcomer.user_id)]
    assert reader.get(f"/api/v1/meetings/{meeting_id}").status_code == 200
    listed = reader.get("/api/v1/meetings", params={"section": "past", "filter": "shared"})
    assert str(meeting_id) in [i["id"] for i in listed.json()["sections"]["past"]["items"]]

    revoked = requester.delete(path(meeting_id, share["id"]))

    assert revoked.status_code == 204
    assert share_rows(superuser_engine, meeting_id) == []
    assert _refused(reader.get(f"/api/v1/meetings/{meeting_id}"))
    assert _refused(requester.delete(path(meeting_id, share["id"])), "no_such_share")
    target = f"meeting:{meeting_id} {ARROW} user:{newcomer.user_id}"
    who = world.people["requester"].user_id
    assert audit_rows(superuser_engine, meeting_id) == [
        ("share.grant", who, target), ("share.revoke", who, target)
    ]


def test_grant_to_a_team_reaches_its_members(world: AccessOrg, superuser_engine: Engine) -> None:
    meeting_id = world.meeting(shares=False)
    reader = world.client("team_grantee")
    assert _refused(reader.get(f"/api/v1/meetings/{meeting_id}"))

    granted = world.client("lead").post(
        path(meeting_id), json={"grantee_type": "team", "grantee_id": str(world.shared_team)}
    )

    assert granted.status_code == 201, granted.text
    assert granted.json()["name"].startswith("Team ")
    assert reader.get(f"/api/v1/meetings/{meeting_id}").status_code == 200
    assert audit_rows(superuser_engine, meeting_id) == [
        ("share.grant", world.people["lead"].user_id,
         f"meeting:{meeting_id} {ARROW} team:{world.shared_team}")
    ]


def test_a_duplicate_grant_is_the_same_share(world: AccessOrg, superuser_engine: Engine) -> None:
    meeting_id = world.meeting(shares=False)
    body = {"grantee_type": "user", "grantee_id": str(world.people["unrelated"].user_id)}
    admin = world.client("admin")

    first = admin.post(path(meeting_id), json=body)
    second = admin.post(path(meeting_id), json=body)

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert len(share_rows(superuser_engine, meeting_id)) == 1
    assert [a for a, *_ in audit_rows(superuser_engine, meeting_id)] == ["share.grant"]


# ─── nothing outside the org ──────────────────────────────────────────────────────────────────────


def test_no_grantee_outside_the_org(
    world: AccessOrg, app_engine: Engine, superuser_engine: Engine
) -> None:
    other = world.people["other_org"]
    other_team = new_team(app_engine, other.org_id, {other.user_id: "lead"})
    disabled = new_member(app_engine, role="member", org_id=world.org_id, status="disabled")
    invited = new_member(app_engine, role="member", org_id=world.org_id, status="invited")
    meeting_id = world.meeting(shares=False)
    requester = world.client("requester")

    for grantee_type, grantee_id in (
        ("user", other.user_id),
        ("team", other_team),
        ("user", uuid.uuid4()),
        ("team", uuid.uuid4()),
        ("user", world.team),  # a team's id named as a user
        ("team", world.people["lead"].user_id),  # a user's id named as a team
        ("user", disabled.user_id),
        ("user", invited.user_id),
    ):
        response = requester.post(
            path(meeting_id), json={"grantee_type": grantee_type, "grantee_id": str(grantee_id)}
        )
        assert _refused(response, "no_such_grantee"), (grantee_type, response.text)

    for body in (
        {"grantee_type": "org", "grantee_id": str(other.org_id)},
        {"grantee_type": "email", "grantee_id": "someone@elsewhere.test"},
        {"grantee_type": "user", "grantee_id": "not-a-uuid"},
        {"grantee_type": "user"},
        {"grantee_type": "user", "grantee_id": str(other.user_id), "org_id": str(other.org_id)},
    ):
        assert requester.post(path(meeting_id), json=body).status_code == 422, body

    assert share_rows(superuser_engine, meeting_id) == []
    assert audit_rows(superuser_engine, meeting_id) == []


def test_a_share_of_another_meeting_cannot_be_revoked_through_this_one(
    world: AccessOrg, superuser_engine: Engine
) -> None:
    mine = world.meeting()
    with superuser_engine.connect() as conn:
        share_id = conn.execute(
            text("SELECT id FROM meeting_shares WHERE meeting_id = :m LIMIT 1"), {"m": mine}
        ).scalar_one()
    other_meeting = world.meeting(shares=False)

    response = world.client("requester").delete(path(other_meeting, share_id))

    assert _refused(response, "no_such_share")
    assert len(share_rows(superuser_engine, mine)) == 2


# ─── who may share ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", CALLERS)
def test_who_may_grant_list_and_revoke(
    world: AccessOrg, caller: str, superuser_engine: Engine
) -> None:
    meeting_id = world.meeting()
    client = world.client(caller)
    body = {"grantee_type": "user", "grantee_id": str(world.people["unrelated"].user_id)}

    granted = client.post(path(meeting_id), json=body)
    listed = client.get(path(meeting_id))
    with superuser_engine.connect() as conn:
        some_share = conn.execute(
            text("SELECT id FROM meeting_shares WHERE meeting_id = :m AND grantee_type = 'team'"),
            {"m": meeting_id},
        ).scalar_one()
    revoked = client.delete(path(meeting_id, some_share))

    if caller in SHARERS:
        assert granted.status_code == 201, granted.text
        assert listed.status_code == 200
        assert {s["grantee_type"] for s in listed.json()["shares"]} == {"user", "team"}
        assert revoked.status_code == 204
    else:
        # Grantees read the meeting but cannot re-share it; for everyone else it does not exist.
        for response in (granted, listed, revoked):
            assert _refused(response), response.text
        assert len(share_rows(superuser_engine, meeting_id)) == 2


@pytest.mark.parametrize("caller", CALLERS)
def test_the_view_says_what_the_caller_may_do(world: AccessOrg, caller: str) -> None:
    response = world.client(caller).get(f"/api/v1/meetings/{world.meeting_id}")
    if response.status_code == 404:
        return  # not a reader: covered by test_meeting_access

    body = response.json()
    assert body["permissions"] == {
        "can_share": caller in SHARERS,
        "can_delete": caller in DELETERS,
        "can_stop": caller in STOPPERS,
    }
    if caller in SHARERS:
        assert sorted((s["grantee_type"], s["grantee_id"]) for s in body["shared_with"]) == sorted(
            [("user", str(world.people["user_grantee"].user_id)),
             ("team", str(world.shared_team))]
        )
        assert all(s["name"] for s in body["shared_with"])
    else:
        assert body["shared_with"] is None


def test_permission_examples(world: AccessOrg) -> None:
    def perms(caller: str) -> dict:
        return world.client(caller).get(f"/api/v1/meetings/{world.meeting_id}").json()[
            "permissions"
        ]

    assert perms("user_grantee") == {"can_share": False, "can_delete": False, "can_stop": False}
    assert perms("requester") == {"can_share": True, "can_delete": True, "can_stop": True}
    assert perms("lead") == {"can_share": True, "can_delete": False, "can_stop": True}


# ─── one transaction ──────────────────────────────────────────────────────────────────────────────


def test_a_failed_audit_write_leaves_no_share(
    world: AccessOrg, monkeypatch: pytest.MonkeyPatch, superuser_engine: Engine
) -> None:
    meeting_id = world.meeting()
    actor = world.actor("requester")
    unrelated = world.people["unrelated"].user_id
    with superuser_engine.connect() as conn:
        team_share = conn.execute(
            text("SELECT id FROM meeting_shares WHERE meeting_id = :m AND grantee_type = 'team'"),
            {"m": meeting_id},
        ).scalar_one()

    def broken(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit, "record", broken)
    with pytest.raises(RuntimeError):
        with transaction(world.engine, org_id=actor.org_id, user_id=actor.user_id) as tx:
            shares.grant(tx, actor, meeting_id, "user", unrelated, ip=None)
    with pytest.raises(RuntimeError):
        with transaction(world.engine, org_id=actor.org_id, user_id=actor.user_id) as tx:
            shares.revoke(tx, actor, meeting_id, team_share, ip=None)

    assert ("user", unrelated) not in share_rows(superuser_engine, meeting_id)
    assert ("team", world.shared_team) in share_rows(superuser_engine, meeting_id)
