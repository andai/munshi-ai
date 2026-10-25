"""Password reset: same answer for every address, single-use 30-minute link, ends all sessions."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import Engine, text

from andai_core.auth.sessions import COOKIE_NAME
from conftest import PASSWORD, Harness, link_token, new_member

REQUEST = "/api/v1/auth/password-reset"
CONFIRM = "/api/v1/auth/password-reset/confirm"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/me"
NEW_PASSWORD = "a brand new passphrase 42"
INVALID_LINK = {"detail": "this link is invalid or has expired"}


def request_token(harness: Harness, email: str) -> str:
    before = len(harness.mailer.outbox)
    assert harness.client().post(REQUEST, json={"email": email}).status_code == 202
    assert len(harness.mailer.outbox) == before + 1
    mail = harness.mailer.outbox[-1]
    assert mail.to == email
    return link_token(mail.text, "reset-password")


def confirm(harness: Harness, token: str, password: str = NEW_PASSWORD):
    return harness.client().post(CONFIRM, json={"token": token, "password": password})


def login(harness: Harness, email: str, password: str, ip: str = "198.51.100.60"):
    return harness.client(ip=ip).post(LOGIN, json={"email": email, "password": password})


def test_every_address_gets_the_same_answer(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    known = new_member(app_engine)
    invited = new_member(app_engine, password=None, status="invited")
    disabled = new_member(app_engine)
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET status = 'disabled' WHERE id = :u"), {"u": disabled.user_id}
        )
    client = harness.client()

    answers = [
        client.post(REQUEST, json={"email": email})
        for email in (
            known.email,
            f"{uuid.uuid4().hex}@example.test",
            invited.email,
            disabled.email,
            "not-even-an-address",
        )
    ]

    assert {(r.status_code, r.content) for r in answers} == {(202, b'{"status":"accepted"}')}
    assert [m.to for m in harness.mailer.outbox] == [known.email]


def test_reset_sets_the_password_and_ends_every_session(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    phone, laptop = harness.client(ip="198.51.100.61"), harness.client(ip="198.51.100.62")
    for client in (phone, laptop):
        answer = client.post(LOGIN, json={"email": member.email, "password": PASSWORD})
        assert answer.status_code == 200

    token = request_token(harness, member.email)
    mail = harness.mailer.outbox[-1]
    assert "30 minutes" in mail.text
    assert f"http://portal.test/reset-password?token={token}" in mail.text
    response = confirm(harness, token)

    assert response.status_code == 200
    assert response.json() == {"status": "password changed"}
    assert phone.get(ME).status_code == 401 and laptop.get(ME).status_code == 401
    assert login(harness, member.email, PASSWORD).status_code == 401
    assert login(harness, member.email, NEW_PASSWORD).status_code == 200
    with superuser_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT token_hash, used_at FROM password_resets WHERE user_id = :u"),
            {"u": member.user_id},
        ).one()
        actions = conn.execute(
            text("SELECT action, actor_user_id FROM audit_events WHERE org_id = :o"),
            {"o": member.org_id},
        ).all()
    assert stored[0] == hashlib.sha256(token.encode()).hexdigest() and stored[1] is not None
    assert ("password.reset", member.user_id) in actions


def test_link_is_single_use(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    token = request_token(harness, member.email)
    assert confirm(harness, token).status_code == 200

    again = confirm(harness, token, "yet another passphrase 7")

    assert again.status_code == 400 and again.json() == INVALID_LINK
    assert login(harness, member.email, NEW_PASSWORD).status_code == 200


def test_link_expires_after_30_minutes(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    token = request_token(harness, member.email)
    harness.clock.advance(minutes=29, seconds=59)
    assert confirm(harness, token).status_code == 200

    late = request_token(harness, member.email)
    harness.clock.advance(minutes=30)
    response = confirm(harness, late, "too late passphrase 99")
    assert response.status_code == 400 and response.json() == INVALID_LINK


def test_a_new_link_voids_the_previous_one(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    first = request_token(harness, member.email)
    second = request_token(harness, member.email)

    assert confirm(harness, first).status_code == 400
    assert confirm(harness, second).status_code == 200


def test_unknown_token_is_refused(harness: Harness) -> None:
    response = confirm(harness, "no-such-token-" + uuid.uuid4().hex)
    assert response.status_code == 400 and response.json() == INVALID_LINK


def test_weak_password_is_refused_without_using_the_link(
    harness: Harness, app_engine: Engine
) -> None:
    member = new_member(app_engine)
    token = request_token(harness, member.email)

    weak = confirm(harness, token, "short")

    assert weak.status_code == 422
    assert weak.json() == {"detail": "password must be at least 12 characters"}
    assert confirm(harness, token).status_code == 200


def test_disabled_after_request_cannot_use_the_link(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    token = request_token(harness, member.email)
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET status = 'disabled' WHERE id = :u"), {"u": member.user_id}
        )
    assert confirm(harness, token).status_code == 400


def test_reset_mails_are_capped_per_address(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    client = harness.client()
    answers = [client.post(REQUEST, json={"email": member.email}).status_code for _ in range(7)]
    assert answers == [202] * 7
    assert len(harness.mailer.outbox) == 5


def test_confirm_clears_the_cookie_and_never_returns_secrets(
    harness: Harness, app_engine: Engine
) -> None:
    member = new_member(app_engine)
    token = request_token(harness, member.email)
    response = confirm(harness, token)
    assert COOKIE_NAME in response.headers["set-cookie"]
    for secret in (token, NEW_PASSWORD, hashlib.sha256(token.encode()).hexdigest()):
        assert secret not in response.text


@pytest.mark.parametrize("origin", ["https://evil.example", None])
def test_cross_site_reset_calls_are_refused(harness: Harness, origin: str | None) -> None:
    client = harness.client(origin=origin)
    assert client.post(REQUEST, json={"email": "a@example.test"}).status_code == 403
    assert client.post(CONFIRM, json={"token": "x", "password": NEW_PASSWORD}).status_code == 403
