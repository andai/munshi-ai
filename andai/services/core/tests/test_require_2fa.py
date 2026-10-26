"""The org's require-2FA switch: who may read and change it, the sessions it ends, and the
`enroll_required` login it forces on members without two-factor login."""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, text

from andai_core.auth.sessions import COOKIE_NAME
from conftest import (
    LOGIN_PATH,
    MFA_PATH,
    MFA_VERIFY_PATH,
    Harness,
    enroll,
    enrolled_member,
    new_member,
    session_count,
    sign_in,
    totp_code,
)

SECURITY = "/api/v1/org/security"
ME = "/api/v1/me"


def org_audit(superuser_engine: Engine, org_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT action, actor_user_id FROM audit_events"
                    " WHERE org_id = :o AND target = :t ORDER BY at"
                ),
                {"o": org_id, "t": f"org:{org_id}"},
            )
        ]


def test_members_read_the_switch_and_only_admins_and_the_owner_change_it(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner = new_member(app_engine)
    admin = new_member(app_engine, role="admin", org_id=owner.org_id)
    member = new_member(app_engine, role="member", org_id=owner.org_id)
    member_client = sign_in(harness, member)

    assert harness.client().get(SECURITY).status_code == 401
    assert member_client.get(SECURITY).json() == {"require2fa": False}
    refused = member_client.put(SECURITY, json={"require2fa": True})
    assert refused.status_code == 403
    assert member_client.get(SECURITY).json() == {"require2fa": False}
    assert sign_in(harness, admin).put(SECURITY, json={"require2fa": "maybe"}).status_code == 422

    # Both enroll first, so turning the switch on does not sign them out.
    owner_client = sign_in(harness, owner)
    enroll(owner_client, harness.clock)
    admin_client = sign_in(harness, admin)
    enroll(admin_client, harness.clock)

    assert admin_client.put(SECURITY, json={"require2fa": True}).json() == {"require2fa": True}
    assert owner_client.get(SECURITY).json() == {"require2fa": True}
    assert owner_client.put(SECURITY, json={"require2fa": False}).json() == {"require2fa": False}
    assert owner_client.put(SECURITY, json={"require2fa": False}).status_code == 200  # no change
    assert org_audit(superuser_engine, owner.org_id) == [
        ("org.require_2fa.enable", admin.user_id),
        ("org.require_2fa.disable", owner.user_id),
    ]


def test_turning_it_on_forces_non_enrolled_members_into_enrollment(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner, owner_totp = enrolled_member(harness, app_engine)
    enrolled, enrolled_totp = enrolled_member(
        harness, app_engine, role="member", org_id=owner.org_id
    )
    plain = new_member(app_engine, role="member", org_id=owner.org_id)
    owner_client = sign_in(harness, owner, owner_totp)
    enrolled_client = sign_in(harness, enrolled, enrolled_totp)
    plain_clients = [sign_in(harness, plain), sign_in(harness, plain)]
    assert session_count(superuser_engine, plain.user_id) == 2

    assert owner_client.put(SECURITY, json={"require2fa": True}).status_code == 200

    assert session_count(superuser_engine, plain.user_id) == 0
    assert all(c.get(ME).status_code == 401 for c in plain_clients)
    assert owner_client.get(ME).status_code == 200
    assert enrolled_client.get(ME).status_code == 200

    # The next login of the non-enrolled member reaches only the enrollment routes.
    client = harness.client()
    login = client.post(LOGIN_PATH, json={"email": plain.email, "password": plain.password})
    assert login.json() == {"stage": "enroll_required"}
    for method, path in (("GET", ME), ("GET", SECURITY), ("PUT", SECURITY)):
        gated = client.request(method, path, json={"require2fa": False})
        assert gated.status_code == 401, (method, path)
        assert gated.json()["next_step"] == "mfa_enroll"
    assert owner_client.get(SECURITY).json() == {"require2fa": True}

    totp, codes = enroll(client, harness.clock)
    assert len(codes) == 10
    me = client.get(ME)
    assert me.status_code == 200 and me.json()["stage"] == "full"
    assert me.json()["mfa_enabled"] is True

    # From now on that member is challenged like everyone else.
    again = harness.client()
    assert again.post(
        LOGIN_PATH, json={"email": plain.email, "password": plain.password}
    ).json() == {"stage": "mfa_pending"}
    assert again.post(MFA_VERIFY_PATH, json={"code": totp_code(totp, harness.clock)}).json() == {
        "stage": "full"
    }

    # Enrolled members are challenged, not sent to enrollment.
    challenged = harness.client()
    assert challenged.post(
        LOGIN_PATH, json={"email": enrolled.email, "password": enrolled.password}
    ).json() == {"stage": "mfa_pending"}


def test_an_admin_without_2fa_who_turns_it_on_is_signed_out_too(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner = new_member(app_engine)
    admin = new_member(app_engine, role="admin", org_id=owner.org_id)
    client = sign_in(harness, admin)

    response = client.put(SECURITY, json={"require2fa": True})

    assert response.status_code == 200 and response.json() == {"require2fa": True}
    assert 'andai_session=""' in response.headers["set-cookie"]
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert session_count(superuser_engine, admin.user_id) == 0
    fresh = harness.client()
    assert fresh.post(
        LOGIN_PATH, json={"email": admin.email, "password": admin.password}
    ).json() == {"stage": "enroll_required"}


def test_turning_it_off_lets_non_enrolled_members_sign_in_fully(
    harness: Harness, app_engine: Engine
) -> None:
    owner, owner_totp = enrolled_member(harness, app_engine)
    plain = new_member(app_engine, role="member", org_id=owner.org_id)
    owner_client = sign_in(harness, owner, owner_totp)
    owner_client.put(SECURITY, json={"require2fa": True})
    blocked = harness.client()
    assert blocked.post(
        LOGIN_PATH, json={"email": plain.email, "password": plain.password}
    ).json() == {"stage": "enroll_required"}

    owner_client.put(SECURITY, json={"require2fa": False})

    assert sign_in(harness, plain).get(ME).status_code == 200


def test_disabling_own_2fa_is_refused_while_it_is_required(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner, owner_totp = enrolled_member(harness, app_engine)
    client = sign_in(harness, owner, owner_totp)
    client.put(SECURITY, json={"require2fa": True})

    code = totp_code(owner_totp, harness.clock)
    response = client.request("DELETE", MFA_PATH, json={"code": code})

    assert response.status_code == 403
    with superuser_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM user_totp WHERE user_id = :u AND confirmed_at IS NOT NULL"),
            {"u": owner.user_id},
        ).scalar_one() == 1


def test_sessions_in_other_orgs_are_left_alone(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    owner, owner_totp = enrolled_member(harness, app_engine)
    outsider = new_member(app_engine)  # another org, no 2FA
    outsider_client = sign_in(harness, outsider)

    sign_in(harness, owner, owner_totp).put(SECURITY, json={"require2fa": True})

    assert outsider_client.get(ME).status_code == 200
    assert outsider_client.cookies.get(COOKIE_NAME)
    assert session_count(superuser_engine, outsider.user_id) == 1
