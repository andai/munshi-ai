"""Logins are audited in the user's oldest org without changing what login answers."""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, text

from andai_core.db.models import OrgMembership, User
from andai_core.db.session import transaction
from conftest import CLIENT_IP, PASSWORD, Harness, new_member

LOGIN = "/api/v1/auth/login"
INVALID = {"detail": "invalid email or password"}


def _login(client, email: str, password: str = PASSWORD):
    return client.post(LOGIN, json={"email": email, "password": password})


def _events(superuser_engine: Engine, *, org_id=None, target=None) -> list[tuple]:
    """(org_id, actor_user_id, action, target, ip) rows, oldest first."""
    where, params = [], {}
    if org_id is not None:
        where.append("org_id = :o")
        params["o"] = org_id
    if target is not None:
        where.append("target = :t")
        params["t"] = target
    with superuser_engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT org_id, actor_user_id, action, target, host(ip) FROM audit_events"
                    f" WHERE {' AND '.join(where)} ORDER BY at, id"
                ),
                params,
            )
        ]


def _set_status(superuser_engine: Engine, user_id: uuid.UUID, status: str) -> None:
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET status = :s WHERE id = :u"), {"s": status, "u": user_id}
        )


def test_success_is_audited_with_the_user_as_actor(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    response = _login(harness.client(), member.email)
    assert response.status_code == 200
    target = f"user:{member.user_id}"
    assert _events(superuser_engine, target=target) == [
        (member.org_id, member.user_id, "login.success", target, CLIENT_IP)
    ]


def test_wrong_password_is_audited_without_an_actor(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    response = _login(harness.client(), member.email, "not the password")
    assert (response.status_code, response.json()) == (401, INVALID)
    target = f"user:{member.user_id}"
    assert _events(superuser_engine, target=target) == [
        (member.org_id, None, "login.failure", target, CLIENT_IP)
    ]


def test_disabled_user_is_audited_as_a_failure(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    _set_status(superuser_engine, member.user_id, "disabled")
    response = _login(harness.client(), member.email)  # the right password
    assert (response.status_code, response.json()) == (401, INVALID)
    assert [e[1:3] for e in _events(superuser_engine, target=f"user:{member.user_id}")] == [
        (None, "login.failure")
    ]


def test_a_user_without_a_password_yet_is_audited_as_a_failure(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine, password=None)
    response = _login(harness.client(), member.email, "anything at all")
    assert (response.status_code, response.json()) == (401, INVALID)
    assert [e[2] for e in _events(superuser_engine, target=f"user:{member.user_id}")] == [
        "login.failure"
    ]


def test_events_go_to_the_oldest_membership_only(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    later = new_member(app_engine)  # another org, which the member joins second
    with transaction(app_engine, org_id=later.org_id) as tx:
        tx.add(OrgMembership(org_id=later.org_id, user_id=member.user_id, role="member"))
    client = harness.client()
    _login(client, member.email, "wrong")
    _login(client, member.email)
    target = f"user:{member.user_id}"
    assert [(e[0], e[2]) for e in _events(superuser_engine, target=target)] == [
        (member.org_id, "login.failure"),
        (member.org_id, "login.success"),
    ]


def test_unknown_email_writes_nothing(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    witness = new_member(app_engine)
    before = _events(superuser_engine, org_id=witness.org_id)
    email = f"{uuid.uuid4().hex}@example.test"
    response = _login(harness.client(), email)
    assert (response.status_code, response.json()) == (401, INVALID)
    with superuser_engine.connect() as conn:
        mentions = conn.execute(
            text("SELECT count(*) FROM audit_events WHERE target LIKE :e OR action LIKE :e"),
            {"e": f"%{email}%"},
        ).scalar_one()
    assert mentions == 0
    assert _events(superuser_engine, org_id=witness.org_id) == before


def test_a_user_with_no_org_writes_nothing(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    from andai_core.auth.passwords import hash_password

    user_id = uuid.uuid4()
    email = f"{user_id.hex}@example.test"
    with transaction(app_engine) as tx:
        tx.add(
            User(
                id=user_id,
                email=email,
                display_name="No Org",
                password_hash=hash_password(PASSWORD),
            )
        )
    client = harness.client()
    assert _login(client, email).status_code == 401
    assert _login(client, email, "wrong").status_code == 401
    assert _events(superuser_engine, target=f"user:{user_id}") == []


def test_rate_limited_attempts_write_nothing(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    statuses = [_login(client, member.email, "wrong").status_code for _ in range(7)]
    assert statuses == [401] * 5 + [429] * 2
    events = _events(superuser_engine, target=f"user:{member.user_id}")
    assert [e[2] for e in events] == ["login.failure"] * 5


def test_failure_responses_stay_identical(harness: Harness, app_engine: Engine) -> None:
    """An audited failure (known member) answers exactly like an unaudited one (unknown email)."""
    member = new_member(app_engine)
    client = harness.client()
    wrong = _login(client, member.email, "wrong")
    unknown = _login(client, f"{uuid.uuid4().hex}@example.test")
    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.json() == INVALID
        assert "set-cookie" not in response.headers
    assert wrong.content == unknown.content
    assert sorted(wrong.headers.keys()) == sorted(unknown.headers.keys())
