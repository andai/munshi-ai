"""Org members: the admin's list, invites by email and role, role changes, disabling (which ends
sessions), and the owner's protections."""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, text

from andai_core.mail import MailError
from conftest import (
    LOGIN_PATH,
    PASSWORD,
    Harness,
    audit_actions,
    link_token,
    role_org,
    session_count,
    sign_in,
)

USERS = "/api/v1/org/users"
ACCEPT = "/api/v1/auth/invite/accept"
ME = "/api/v1/me"
INVITEE_PASSWORD = "the invitee's own passphrase"


def user_path(user_id: uuid.UUID) -> str:
    return f"{USERS}/{user_id}"


def org_audit(superuser_engine: Engine, org_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text("SELECT action, actor_user_id, target FROM audit_events WHERE org_id = :o"),
                {"o": org_id},
            )
        ]


def test_an_admin_invites_and_the_invitee_appears_active_with_the_role(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    admin = sign_in(harness, org.admin)
    email = f"Invitee-{uuid.uuid4().hex[:8]}@Example.TEST"

    response = admin.post(USERS, json={"email": email, "role": "admin"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "email", "role", "expires_at"}
    assert body["email"] == email.lower() and body["role"] == "admin"
    [mail] = harness.mailer.outbox
    assert mail.to == email.lower()
    assert mail.subject.startswith("You are invited to Org ")
    token = link_token(mail.text, "invite")
    assert token not in response.text

    listing = admin.get(USERS).json()
    assert [i["email"] for i in listing["invites"]] == [email.lower()]
    assert email.lower() not in [u["email"] for u in listing["users"]]

    accepted = harness.client().post(ACCEPT, json={"token": token, "password": INVITEE_PASSWORD})
    assert accepted.status_code == 200

    listing = admin.get(USERS).json()
    assert listing["invites"] == []
    [row] = [u for u in listing["users"] if u["email"] == email.lower()]
    assert row["status"] == "active" and row["role"] == "admin" and row["teams"] == []
    login = harness.client().post(
        LOGIN_PATH, json={"email": email.lower(), "password": INVITEE_PASSWORD}
    )
    assert login.status_code == 200
    actions = [a for a, *_ in org_audit(superuser_engine, org.owner.org_id)]
    assert "invite.create" in actions and "invite.accept" in actions


def test_the_member_list_shows_roles_teams_and_2fa(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)

    users = {u["id"]: u for u in sign_in(harness, org.owner).get(USERS).json()["users"]}

    assert set(users) == {
        str(m.user_id) for m in (org.owner, org.admin, org.member, org.lead)
    }
    assert users[str(org.owner.user_id)]["role"] == "owner"
    assert users[str(org.lead.user_id)]["teams"] == [
        {"id": str(org.team), "name": users[str(org.lead.user_id)]["teams"][0]["name"],
         "role": "lead"}
    ]
    assert {t["id"] for t in users[str(org.member.user_id)]["teams"]} == {
        str(org.team), str(org.other_team)
    }
    assert all(u["mfa_enabled"] is False and u["status"] == "active" for u in users.values())
    for row in users.values():
        assert "password_hash" not in row


def test_inviting_again_voids_the_earlier_link(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)
    owner = sign_in(harness, org.owner)
    email = f"again-{uuid.uuid4().hex[:8]}@example.test"

    assert owner.post(USERS, json={"email": email, "role": "member"}).status_code == 201
    assert owner.post(USERS, json={"email": email, "role": "admin"}).status_code == 201

    first, second = (link_token(m.text, "invite") for m in harness.mailer.outbox)
    anon = harness.client()
    assert anon.post(ACCEPT, json={"token": first, "password": PASSWORD}).status_code == 400
    assert anon.post(ACCEPT, json={"token": second, "password": PASSWORD}).status_code == 200
    [row] = [u for u in owner.get(USERS).json()["users"] if u["email"] == email]
    assert row["role"] == "admin"


def test_invite_refusals(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)
    admin = sign_in(harness, org.admin)

    assert admin.post(USERS, json={"email": org.member.email, "role": "member"}).status_code == 409
    assert admin.post(USERS, json={"email": "x@example.test", "role": "owner"}).status_code == 422
    assert admin.post(USERS, json={"email": "not-an-address", "role": "member"}).status_code == 422
    assert admin.post(USERS, json={"email": "x@example.test"}).status_code == 422
    assert harness.mailer.outbox == []


def test_a_refused_invite_mail_creates_nothing(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    class FailingMailer:
        async def send(self, mail) -> None:
            raise MailError("relay down")

    org = role_org(app_engine)
    admin = sign_in(harness, org.admin)
    harness.services.mailer = FailingMailer()
    email = f"nomail-{uuid.uuid4().hex[:8]}@example.test"

    response = admin.post(USERS, json={"email": email, "role": "member"})

    assert response.status_code == 503
    with superuser_engine.connect() as conn:
        invites = conn.execute(
            text("SELECT count(*) FROM invites WHERE email = :e"), {"e": email}
        ).scalar_one()
    assert invites == 0
    assert "invite.create" not in [a for a, *_ in org_audit(superuser_engine, org.owner.org_id)]


def test_disabling_a_member_ends_their_sessions_and_blocks_login(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    member = sign_in(harness, org.member)
    sign_in(harness, org.member)
    assert session_count(superuser_engine, org.member.user_id) == 2

    response = sign_in(harness, org.admin).patch(
        user_path(org.member.user_id), json={"status": "disabled"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert session_count(superuser_engine, org.member.user_id) == 0
    assert member.get(ME).status_code == 401
    login = harness.client().post(
        LOGIN_PATH, json={"email": org.member.email, "password": org.member.password}
    )
    assert login.status_code == 401
    assert "user.disable" in audit_actions(superuser_engine, org.member.user_id)

    enabled = sign_in(harness, org.owner).patch(
        user_path(org.member.user_id), json={"status": "active"}
    )
    assert enabled.status_code == 200 and enabled.json()["status"] == "active"
    assert sign_in(harness, org.member).get(ME).status_code == 200
    assert "user.enable" in audit_actions(superuser_engine, org.member.user_id)


def test_role_changes_are_audited(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)

    response = sign_in(harness, org.admin).patch(
        user_path(org.member.user_id), json={"role": "admin"}
    )

    assert response.status_code == 200 and response.json()["role"] == "admin"
    assert ("member.role_change", org.admin.user_id, f"user:{org.member.user_id}") in org_audit(
        superuser_engine, org.owner.org_id
    )


def test_the_owner_is_out_of_an_admins_reach(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    owner_session = sign_in(harness, org.owner)
    admin = sign_in(harness, org.admin)
    owner = user_path(org.owner.user_id)

    assert admin.patch(owner, json={"role": "member"}).status_code == 403
    assert admin.patch(owner, json={"status": "disabled"}).status_code == 403
    assert admin.delete(f"{owner}/mfa").status_code == 403
    assert admin.patch(user_path(org.member.user_id), json={"role": "owner"}).status_code == 403

    assert owner_session.get(ME).json()["role"] == "owner"
    assert session_count(superuser_engine, org.owner.user_id) == 1


def test_nobody_changes_themselves(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)
    for who in (org.owner, org.admin):
        client = sign_in(harness, who)
        assert client.patch(user_path(who.user_id), json={"role": "member"}).status_code == 403
        assert client.patch(user_path(who.user_id), json={"status": "disabled"}).status_code == 403


def test_only_the_owner_hands_on_ownership(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    owner = sign_in(harness, org.owner)

    response = owner.patch(user_path(org.admin.user_id), json={"role": "owner"})

    assert response.status_code == 200 and response.json()["role"] == "owner"
    assert owner.get(ME).json()["role"] == "admin"
    assert sign_in(harness, org.admin).get(ME).json()["role"] == "owner"
    # The former owner, now an admin, cannot take it back.
    assert owner.patch(user_path(org.admin.user_id), json={"role": "member"}).status_code == 403
    assert "org.owner_transfer" in audit_actions(superuser_engine, org.admin.user_id)


def test_unknown_members_and_empty_changes(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)
    admin = sign_in(harness, org.admin)

    assert admin.patch(user_path(uuid.uuid4()), json={"role": "member"}).status_code == 404
    assert admin.patch(user_path(org.member.user_id), json={}).status_code == 422
    assert admin.patch(
        user_path(org.member.user_id), json={"role": "superuser"}
    ).status_code == 422
    assert admin.patch(user_path(org.member.user_id), json={"status": "active"}).status_code == 200


def test_the_list_holds_only_this_orgs_memberships_for_a_multi_org_admin(
    harness: Harness, app_engine: Engine
) -> None:
    from andai_core.db.models import OrgMembership
    from andai_core.db.session import transaction
    from conftest import new_member

    org = role_org(app_engine)
    elsewhere = new_member(app_engine)
    # The admin also belongs to another org (the membership read policy shows a user their own
    # memberships in every org).
    with transaction(app_engine, org_id=elsewhere.org_id) as tx:
        tx.add(OrgMembership(org_id=elsewhere.org_id, user_id=org.admin.user_id, role="member"))

    users = sign_in(harness, org.admin).get(USERS).json()["users"]

    assert sorted(u["id"] for u in users) == sorted(
        str(m.user_id) for m in (org.owner, org.admin, org.member, org.lead)
    )
    assert [u["role"] for u in users if u["id"] == str(org.admin.user_id)] == ["admin"]
    assert elsewhere.email not in str(users)
