"""Invite acceptance: single use, expiring, sets the password, activates, grants the membership."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import Engine, text

from andai_core.auth.invites import invite_mail
from conftest import Harness, invite_user, link_token, new_member

ACCEPT = "/api/v1/auth/invite/accept"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/me"
PASSWORD = "invitee chosen passphrase"
INVALID_LINK = {"detail": "this link is invalid or has expired"}


def accept(harness: Harness, token: str, password: str = PASSWORD, **extra: str):
    return harness.client().post(ACCEPT, json={"token": token, "password": password, **extra})


def fresh_email() -> str:
    return f"{uuid.uuid4().hex}@example.test"


def user_row(superuser_engine: Engine, email: str):
    with superuser_engine.connect() as conn:
        return conn.execute(
            text("SELECT id, status, password_hash, display_name FROM users WHERE email = :e"),
            {"e": email},
        ).one_or_none()


def membership(superuser_engine: Engine, org_id: uuid.UUID, user_id: uuid.UUID):
    with superuser_engine.connect() as conn:
        return conn.execute(
            text("SELECT role FROM org_memberships WHERE org_id = :o AND user_id = :u"),
            {"o": org_id, "u": user_id},
        ).scalar_one_or_none()


def test_accept_activates_the_user_and_grants_the_role(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner = new_member(app_engine)
    email = fresh_email()
    token = invite_user(
        app_engine,
        harness,
        org_id=owner.org_id,
        email=email,
        role="admin",
        invited_by=owner.user_id,
    )
    invited = user_row(superuser_engine, email)
    assert invited.status == "invited" and invited.password_hash is None
    assert membership(superuser_engine, owner.org_id, invited.id) is None  # only on acceptance

    response = accept(harness, token, display_name="Ada Invitee")

    assert response.status_code == 200 and response.json() == {"status": "active"}
    active = user_row(superuser_engine, email)
    assert active.status == "active" and active.password_hash.startswith("$argon2id$")
    assert active.display_name == "Ada Invitee"
    assert membership(superuser_engine, owner.org_id, active.id) == "admin"
    client = harness.client()
    assert client.post(LOGIN, json={"email": email, "password": PASSWORD}).status_code == 200
    me = client.get(ME).json()
    assert me["role"] == "admin" and me["org"]["id"] == str(owner.org_id)
    with superuser_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT token_hash, accepted_at FROM invites WHERE email = :e"), {"e": email}
        ).one()
        actions = conn.execute(
            text("SELECT action, actor_user_id, target FROM audit_events WHERE org_id = :o"),
            {"o": owner.org_id},
        ).all()
    assert stored[0] == hashlib.sha256(token.encode()).hexdigest() and stored[1] is not None
    assert ("invite.accept", active.id, f"user:{active.id}") in actions
    for secret in (token, PASSWORD, stored[0]):
        assert secret not in response.text


def test_invite_is_single_use(harness: Harness, app_engine: Engine) -> None:
    owner = new_member(app_engine)
    token = invite_user(app_engine, harness, org_id=owner.org_id, email=fresh_email())
    assert accept(harness, token).status_code == 200

    again = accept(harness, token, "attacker passphrase 123")

    assert again.status_code == 400 and again.json() == INVALID_LINK


def test_invite_expires(harness: Harness, app_engine: Engine) -> None:
    owner = new_member(app_engine)
    token = invite_user(app_engine, harness, org_id=owner.org_id, email=fresh_email())
    harness.clock.advance(days=7, seconds=1)

    response = accept(harness, token)

    assert response.status_code == 400 and response.json() == INVALID_LINK


def test_unknown_token_is_refused(harness: Harness) -> None:
    response = accept(harness, "no-such-invite-" + uuid.uuid4().hex)
    assert response.status_code == 400 and response.json() == INVALID_LINK


def test_weak_password_does_not_consume_the_invite(harness: Harness, app_engine: Engine) -> None:
    owner = new_member(app_engine)
    token = invite_user(app_engine, harness, org_id=owner.org_id, email=fresh_email())

    assert accept(harness, token, "short").status_code == 422
    assert accept(harness, token).status_code == 200


def test_active_account_is_told_to_sign_in(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner = new_member(app_engine)
    existing = new_member(app_engine)  # active in another org
    token = invite_user(app_engine, harness, org_id=owner.org_id, email=existing.email)

    response = accept(harness, token, "takeover passphrase 1")

    assert response.status_code == 409
    assert membership(superuser_engine, owner.org_id, existing.user_id) is None
    # The password was not changed by the invite.
    login = harness.client().post(
        LOGIN, json={"email": existing.email, "password": existing.password}
    )
    assert login.status_code == 200


def test_disabled_user_cannot_accept(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner = new_member(app_engine)
    email = fresh_email()
    token = invite_user(app_engine, harness, org_id=owner.org_id, email=email)
    with superuser_engine.begin() as conn:
        conn.execute(text("UPDATE users SET status = 'disabled' WHERE email = :e"), {"e": email})

    assert accept(harness, token).status_code == 400


def test_invite_mail_carries_the_portal_link(harness: Harness) -> None:
    mail = invite_mail(
        harness.services.settings, email="x@example.test", org_name="Acme", token="tok", owner=False
    )
    assert mail.to == "x@example.test"
    assert mail.subject == "You are invited to Acme on munshi.ai"
    assert "http://portal.test/invite?token=tok" in mail.text
    assert link_token(mail.text, "invite") == "tok"
    assert "7 days" in mail.text
