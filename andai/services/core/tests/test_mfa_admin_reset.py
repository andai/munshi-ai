"""An admin or the owner resets a member's two-factor login (lost device): the enrollment and
recovery codes go, the member's sessions end, and the owner is out of an admin's reach."""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, text

from andai_core.auth.sessions import COOKIE_NAME
from conftest import (
    LOGIN_PATH,
    MFA_VERIFY_PATH,
    Harness,
    audit_actions,
    enrolled_member,
    new_member,
    session_count,
    sign_in,
    staged_session,
    totp_code,
)

ME = "/api/v1/me"
SECURITY = "/api/v1/org/security"


def reset_path(user_id: uuid.UUID) -> str:
    return f"/api/v1/org/users/{user_id}/mfa"


def mfa_rows(superuser_engine: Engine, user_id: uuid.UUID) -> tuple[int, int]:
    with superuser_engine.connect() as conn:
        totp = conn.execute(
            text("SELECT count(*) FROM user_totp WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
        codes = conn.execute(
            text("SELECT count(*) FROM recovery_codes WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
    return totp, codes


def audit_rows(superuser_engine: Engine, user_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text("SELECT action, actor_user_id, org_id FROM audit_events WHERE target = :t"),
                {"t": f"user:{user_id}"},
            )
        ]


def test_an_admin_resets_a_members_2fa_and_ends_their_sessions(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner = new_member(app_engine)
    admin = new_member(app_engine, role="admin", org_id=owner.org_id)
    member, totp = enrolled_member(harness, app_engine, role="member", org_id=owner.org_id)
    signed_in = sign_in(harness, member, totp)
    pending = harness.client()
    pending.post(LOGIN_PATH, json={"email": member.email, "password": member.password})
    # the enrolling session, a signed-in one and one waiting for its code
    assert session_count(superuser_engine, member.user_id) == 3
    assert mfa_rows(superuser_engine, member.user_id) == (1, 10)

    response = sign_in(harness, admin).delete(reset_path(member.user_id))

    assert response.status_code == 204
    assert mfa_rows(superuser_engine, member.user_id) == (0, 0)
    assert session_count(superuser_engine, member.user_id) == 0
    assert signed_in.get(ME).status_code == 401
    verify = pending.post(MFA_VERIFY_PATH, json={"code": totp_code(totp, harness.clock)})
    assert verify.status_code == 401
    assert ("mfa.admin_reset", admin.user_id, owner.org_id) in audit_rows(
        superuser_engine, member.user_id
    )
    assert "mfa.admin_reset" in audit_actions(superuser_engine, member.user_id)
    # Without the org requirement the member now signs in with the password alone.
    assert sign_in(harness, member).get(ME).json()["mfa_enabled"] is False


def test_after_a_reset_in_an_org_that_requires_2fa_the_member_must_enroll_again(
    harness: Harness, app_engine: Engine
) -> None:
    owner, owner_totp = enrolled_member(harness, app_engine)
    member, _ = enrolled_member(harness, app_engine, role="member", org_id=owner.org_id)
    owner_client = sign_in(harness, owner, owner_totp)
    assert owner_client.put(SECURITY, json={"require2fa": True}).status_code == 200

    assert owner_client.delete(reset_path(member.user_id)).status_code == 204

    client = harness.client()
    login = client.post(LOGIN_PATH, json={"email": member.email, "password": member.password})
    assert login.json() == {"stage": "enroll_required"}
    assert client.get(ME).json()["next_step"] == "mfa_enroll"


def test_members_cannot_reset_anyone(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner = new_member(app_engine)
    colleague, _ = enrolled_member(harness, app_engine, role="member", org_id=owner.org_id)
    member = new_member(app_engine, role="member", org_id=owner.org_id)

    response = sign_in(harness, member).delete(reset_path(colleague.user_id))

    assert response.status_code == 403
    assert mfa_rows(superuser_engine, colleague.user_id) == (1, 10)


def test_an_admin_cannot_reset_the_owner_but_the_owner_can_reset_an_admin(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner, owner_totp = enrolled_member(harness, app_engine)
    admin, admin_totp = enrolled_member(harness, app_engine, role="admin", org_id=owner.org_id)
    admin_client = sign_in(harness, admin, admin_totp)
    owner_client = sign_in(harness, owner, owner_totp)

    refused = admin_client.delete(reset_path(owner.user_id))

    assert refused.status_code == 403
    assert refused.json() == {"detail": "an admin cannot reset the owner's two-factor login"}
    assert mfa_rows(superuser_engine, owner.user_id) == (1, 10)
    assert owner_client.get(ME).status_code == 200
    assert owner_client.delete(reset_path(admin.user_id)).status_code == 204
    assert mfa_rows(superuser_engine, admin.user_id) == (0, 0)
    assert admin_client.get(ME).status_code == 401


def test_nobody_resets_their_own_2fa_through_the_admin_route(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner, owner_totp = enrolled_member(harness, app_engine)

    response = sign_in(harness, owner, owner_totp).delete(reset_path(owner.user_id))

    assert response.status_code == 403
    assert mfa_rows(superuser_engine, owner.user_id) == (1, 10)


def test_unknown_members_other_orgs_and_members_without_2fa(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner = new_member(app_engine)
    plain = new_member(app_engine, role="member", org_id=owner.org_id)
    stranger, _ = enrolled_member(harness, app_engine)  # owner of another org
    client = sign_in(harness, owner)

    assert client.delete(reset_path(uuid.uuid4())).status_code == 404
    assert client.delete(reset_path(stranger.user_id)).status_code == 404
    assert mfa_rows(superuser_engine, stranger.user_id) == (1, 10)
    assert client.delete(reset_path(plain.user_id)).status_code == 409
    assert client.delete("/api/v1/org/users/not-a-uuid/mfa").status_code == 422


def test_the_reset_route_needs_a_full_session(harness: Harness, app_engine: Engine) -> None:
    owner = new_member(app_engine)
    member, _ = enrolled_member(harness, app_engine, role="member", org_id=owner.org_id)

    assert harness.client().delete(reset_path(member.user_id)).status_code == 401
    staged = harness.client()
    staged.cookies.set(
        COOKIE_NAME, staged_session(app_engine, owner, "enroll_required", harness.clock)
    )
    gated = staged.delete(reset_path(member.user_id))
    assert gated.status_code == 401 and gated.json()["next_step"] == "mfa_enroll"
