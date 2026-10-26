"""TOTP enrollment, the second login step, replay and lockout, recovery codes, disable and
regenerate, and password reset leaving two-factor login in place."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import timedelta

import pyotp
import pytest
from sqlalchemy import Engine, text

from andai_core.auth.sessions import COOKIE_NAME
from conftest import (
    LOGIN_PATH,
    MFA_PATH,
    MFA_VERIFY_PATH,
    PASSWORD,
    Harness,
    audit_actions,
    enroll,
    link_token,
    new_member,
    staged_session,
    totp_code,
)

ME = "/api/v1/me"
CONFIRM = f"{MFA_PATH}/confirm"
RECOVERY_CODES = f"{MFA_PATH}/recovery-codes"
RECOVERY_FORMAT = re.compile(r"[a-z2-9]{5}-[a-z2-9]{5}")


def login(client, email: str, password: str = PASSWORD):
    return client.post(LOGIN_PATH, json={"email": email, "password": password})


def verify(client, code: str):
    return client.post(MFA_VERIFY_PATH, json={"code": code})


def sessions_of(superuser_engine: Engine, user_id: uuid.UUID) -> list:
    with superuser_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT token_hash, stage, mfa_failures, expires_at, created_at FROM sessions"
                " WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": user_id},
        ).all()


def totp_row(superuser_engine: Engine, user_id: uuid.UUID):
    with superuser_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT secret_ciphertext, key_id, confirmed_at, last_used_step FROM user_totp"
                " WHERE user_id = :u"
            ),
            {"u": user_id},
        ).first()


def recovery_rows(superuser_engine: Engine, user_id: uuid.UUID) -> list:
    with superuser_engine.connect() as conn:
        return conn.execute(
            text("SELECT code_hash, used_at FROM recovery_codes WHERE user_id = :u"),
            {"u": user_id},
        ).all()


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def wrong_code(totp, clock) -> str:
    """A six-digit code that no step inside the accepted skew produces."""
    valid = {totp_code(totp, clock, k) for k in (-1, 0, 1)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in valid)


def with_cookie(harness: Harness, token: str):
    client = harness.client()
    client.cookies.set(COOKIE_NAME, token)
    return client


@pytest.fixture
def enrolled(harness: Harness, app_engine: Engine):
    """A member with confirmed TOTP; returns (member, totp, recovery codes)."""
    member = new_member(app_engine)
    client = harness.client()
    assert login(client, member.email).json() == {"stage": "full"}
    totp, codes = enroll(client, harness.clock)
    return member, totp, codes


# ─── enrollment ───────────────────────────────────────────────────────────────────────────────────


def test_start_returns_the_uri_and_qr_once_and_stores_the_secret_sealed(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    login(client, member.email)

    response = client.post(MFA_PATH)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"otpauth_uri", "qr_svg"}
    totp = pyotp.parse_uri(body["otpauth_uri"])
    assert body["otpauth_uri"].startswith("otpauth://totp/munshi.ai:")
    assert totp.issuer == "munshi.ai" and totp.name == member.email
    assert (totp.digits, totp.interval, totp.digest().name) == (6, 30, "sha1")
    assert body["qr_svg"].startswith("<svg") and "</svg>" in body["qr_svg"]
    row = totp_row(superuser_engine, member.user_id)
    assert row.confirmed_at is None
    assert row.key_id == harness.services.keyring.key_id
    assert totp.secret not in row.secret_ciphertext
    assert harness.services.keyring.open(
        row.secret_ciphertext, row.key_id, f"user_totp:{member.user_id}"
    ) == totp.secret.encode()
    me = client.get(ME)
    assert me.json()["mfa_enabled"] is False
    assert totp.secret not in me.text
    assert audit_actions(superuser_engine, member.user_id) == ["mfa.enroll"]


def test_restarting_an_unconfirmed_enrollment_replaces_the_secret(
    harness: Harness, app_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    login(client, member.email)
    first = pyotp.parse_uri(client.post(MFA_PATH).json()["otpauth_uri"])
    second = pyotp.parse_uri(client.post(MFA_PATH).json()["otpauth_uri"])
    assert first.secret != second.secret

    stale = client.post(CONFIRM, json={"code": totp_code(first, harness.clock)})
    fresh = client.post(CONFIRM, json={"code": totp_code(second, harness.clock)})

    assert stale.status_code == 400 and stale.json() == {"detail": "invalid code"}
    assert fresh.status_code == 200


def test_confirm_returns_ten_recovery_codes_once_and_a_full_session_stays_full(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    login(client, member.email)
    token_before = client.cookies[COOKIE_NAME]
    totp = pyotp.parse_uri(client.post(MFA_PATH).json()["otpauth_uri"])

    assert client.post(CONFIRM, json={"code": wrong_code(totp, harness.clock)}).status_code == 400
    assert client.post(CONFIRM, json={"code": "not-a-code"}).status_code == 400
    response = client.post(CONFIRM, json={"code": totp_code(totp, harness.clock)})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers  # a voluntary enrollment keeps its session
    body = response.json()
    assert body["stage"] == "full"
    codes = body["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(RECOVERY_FORMAT.fullmatch(c) for c in codes)
    rows = recovery_rows(superuser_engine, member.user_id)
    assert len(rows) == 10
    assert all(r.code_hash.startswith("$argon2id$") and r.used_at is None for r in rows)
    stored = " ".join(r.code_hash for r in rows)
    assert not any(c in stored or c.replace("-", "") in stored for c in codes)
    assert totp_row(superuser_engine, member.user_id).confirmed_at is not None
    assert client.cookies[COOKIE_NAME] == token_before
    assert [s.stage for s in sessions_of(superuser_engine, member.user_id)] == ["full"]
    me = client.get(ME)
    assert me.status_code == 200 and me.json()["mfa_enabled"] is True
    for code in codes:
        assert code not in me.text
    assert client.post(MFA_PATH).status_code == 409
    again = client.post(CONFIRM, json={"code": totp_code(totp, harness.clock, 1)})
    assert again.status_code == 409
    assert audit_actions(superuser_engine, member.user_id) == ["mfa.enroll", "mfa.confirm"]


def test_enroll_required_session_becomes_full_with_a_new_token_on_confirm(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    token = staged_session(app_engine, member, "enroll_required", harness.clock)
    client = with_cookie(harness, token)
    assert client.get(ME).status_code == 401  # not a full session yet

    totp = pyotp.parse_uri(client.post(MFA_PATH).json()["otpauth_uri"])
    response = client.post(CONFIRM, json={"code": totp_code(totp, harness.clock)})

    assert response.status_code == 200
    assert response.json()["stage"] == "full" and len(response.json()["recovery_codes"]) == 10
    new_token = response.cookies[COOKIE_NAME]
    assert new_token != token
    rows = sessions_of(superuser_engine, member.user_id)
    assert [(r.token_hash, r.stage) for r in rows] == [(sha(new_token), "full")]
    me = with_cookie(harness, new_token).get(ME)
    assert me.status_code == 200 and me.json()["stage"] == "full"
    assert with_cookie(harness, token).get(ME).status_code == 401


# ─── the second login step ────────────────────────────────────────────────────────────────────────


def test_login_with_2fa_is_pending_until_a_valid_code(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, totp, _ = enrolled
    client = harness.client()

    response = login(client, member.email)

    assert response.status_code == 200 and response.json() == {"stage": "mfa_pending"}
    pending = response.cookies[COOKIE_NAME]
    row = [r for r in sessions_of(superuser_engine, member.user_id) if r.token_hash == sha(pending)]
    assert row and row[0].stage == "mfa_pending"
    assert row[0].expires_at - row[0].created_at == timedelta(minutes=5)
    assert client.get(ME).status_code == 401

    done = verify(client, totp_code(totp, harness.clock))

    assert done.status_code == 200 and done.json() == {"stage": "full"}
    full = done.cookies[COOKIE_NAME]
    assert full != pending
    hashes = {r.token_hash: r.stage for r in sessions_of(superuser_engine, member.user_id)}
    assert sha(pending) not in hashes and hashes[sha(full)] == "full"
    me = client.get(ME)
    assert me.status_code == 200 and me.json()["stage"] == "full"


def test_a_code_is_accepted_once_per_time_step(harness: Harness, enrolled) -> None:
    member, totp, _ = enrolled
    first = harness.client()
    login(first, member.email)
    code = totp_code(totp, harness.clock)
    assert verify(first, code).status_code == 200

    second = harness.client()
    login(second, member.email)
    replay = verify(second, code)
    previous = verify(second, totp_code(totp, harness.clock, -1))  # older than the last used step

    assert replay.status_code == 401
    assert replay.json() == {"detail": "invalid code", "next_step": "mfa_verify"}
    assert previous.status_code == 401
    harness.clock.advance(seconds=30)
    assert verify(second, totp_code(totp, harness.clock)).status_code == 200


@pytest.mark.parametrize(("offset", "accepted"), [(-2, False), (-1, True), (1, True), (2, False)])
def test_clock_skew_of_one_step_only(
    harness: Harness, app_engine: Engine, offset: int, accepted: bool
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    login(client, member.email)
    totp, _ = enroll(client, harness.clock)
    harness.clock.advance(seconds=90)  # well past the confirming step, so -2 is not a replay
    pending = harness.client()
    login(pending, member.email)

    response = verify(pending, totp_code(totp, harness.clock, offset))

    assert (response.status_code == 200) is accepted


def test_five_wrong_codes_end_the_pending_session(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, totp, _ = enrolled
    client = harness.client()
    login(client, member.email)
    pending = client.cookies[COOKIE_NAME]
    right = totp_code(totp, harness.clock)
    wrong = wrong_code(totp, harness.clock)

    answers = [verify(client, wrong) for _ in range(4)]
    assert all(a.status_code == 401 and a.json()["next_step"] == "mfa_verify" for a in answers)
    failures = {r.token_hash: r.mfa_failures for r in sessions_of(superuser_engine, member.user_id)}
    assert failures[sha(pending)] == 4

    fifth = verify(client, wrong)

    assert fifth.status_code == 401
    assert fifth.json() == {"detail": "too many wrong codes; sign in again", "next_step": "login"}
    assert sha(pending) not in {r.token_hash for r in sessions_of(superuser_engine, member.user_id)}
    sixth = verify(with_cookie(harness, pending), right)  # even the right code: the attempt is over
    assert sixth.status_code == 401 and "next_step" not in sixth.json()
    assert "mfa.lockout" in audit_actions(superuser_engine, member.user_id)

    again = harness.client()
    assert login(again, member.email).json() == {"stage": "mfa_pending"}
    assert verify(again, right).status_code == 200


def test_wrong_codes_are_limited_per_user_across_sessions(harness: Harness, enrolled) -> None:
    member, totp, _ = enrolled
    right = totp_code(totp, harness.clock)
    wrong = wrong_code(totp, harness.clock)
    for _ in range(2):  # two sign-ins, five wrong codes each
        client = harness.client()
        login(client, member.email)
        statuses = [verify(client, wrong).json().get("next_step") for _ in range(5)]
        assert statuses[-1] == "login"

    client = harness.client()
    login(client, member.email)
    throttled = verify(client, right)

    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0


def test_pending_session_expires_after_five_minutes(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, totp, _ = enrolled
    in_time = harness.client()
    login(in_time, member.email)
    harness.clock.advance(minutes=4, seconds=59)
    assert verify(in_time, totp_code(totp, harness.clock)).status_code == 200

    harness.clock.advance(seconds=30)
    late = harness.client()
    login(late, member.email)
    pending = late.cookies[COOKIE_NAME]
    harness.clock.advance(minutes=5, seconds=1)

    response = verify(late, totp_code(totp, harness.clock))

    assert response.status_code == 401
    assert sha(pending) not in {r.token_hash for r in sessions_of(superuser_engine, member.user_id)}


def test_a_recovery_code_signs_in_once(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, _, codes = enrolled
    client = harness.client()
    login(client, member.email)

    typed = f"  {codes[3].upper().replace('-', ' ')} "  # case, spaces and hyphens do not matter
    response = verify(client, typed)

    assert response.status_code == 200 and response.json() == {"stage": "full"}
    used = [r for r in recovery_rows(superuser_engine, member.user_id) if r.used_at is not None]
    assert len(used) == 1
    assert "mfa.recovery_use" in audit_actions(superuser_engine, member.user_id)

    again = harness.client()
    login(again, member.email)
    assert verify(again, codes[3]).status_code == 401
    assert verify(again, codes[4]).status_code == 200


def test_password_reset_leaves_2fa_in_place(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, totp, codes = enrolled
    client = harness.client()
    requested = client.post("/api/v1/auth/password-reset", json={"email": member.email})
    assert requested.status_code == 202
    token = link_token(harness.mailer.outbox[-1].text, "reset-password")

    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "password": "a brand new passphrase"},
    )

    assert confirmed.status_code == 200
    assert not client.cookies.get(COOKIE_NAME)
    assert totp_row(superuser_engine, member.user_id).confirmed_at is not None
    assert len(recovery_rows(superuser_engine, member.user_id)) == 10
    response = login(client, member.email, "a brand new passphrase")
    assert response.json() == {"stage": "mfa_pending"}
    assert client.get(ME).status_code == 401
    assert verify(client, totp_code(totp, harness.clock)).status_code == 200


# ─── disable and regenerate ───────────────────────────────────────────────────────────────────────


def _signed_in(harness: Harness, member, totp):
    client = harness.client()
    login(client, member.email)
    assert verify(client, totp_code(totp, harness.clock)).status_code == 200
    harness.clock.advance(seconds=30)
    return client


def test_disable_requires_a_current_code(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, totp, _ = enrolled
    client = _signed_in(harness, member, totp)
    right = totp_code(totp, harness.clock)
    wrong = wrong_code(totp, harness.clock)

    assert client.request("DELETE", MFA_PATH).status_code == 422
    assert client.request("DELETE", MFA_PATH, json={"code": wrong}).status_code == 400
    response = client.request("DELETE", MFA_PATH, json={"code": right})

    assert response.status_code == 204
    assert totp_row(superuser_engine, member.user_id) is None
    assert recovery_rows(superuser_engine, member.user_id) == []
    assert client.get(ME).json()["mfa_enabled"] is False
    assert "mfa.disable" in audit_actions(superuser_engine, member.user_id)
    assert login(harness.client(), member.email).json() == {"stage": "full"}


def test_disable_is_refused_while_the_org_requires_2fa(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, totp, _ = enrolled
    client = _signed_in(harness, member, totp)
    with superuser_engine.begin() as conn:
        conn.execute(text("UPDATE orgs SET require_2fa = true WHERE id = :o"), {"o": member.org_id})

    response = client.request("DELETE", MFA_PATH, json={"code": totp_code(totp, harness.clock)})

    assert response.status_code == 403
    assert response.json() == {"detail": "your organisation requires two-factor login"}
    assert totp_row(superuser_engine, member.user_id).confirmed_at is not None


def test_regenerating_recovery_codes_requires_a_code_and_voids_the_old_ones(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, totp, old_codes = enrolled
    client = _signed_in(harness, member, totp)

    assert client.post(RECOVERY_CODES, json={"code": "zzzzz-zzzzz"}).status_code == 400
    response = client.post(RECOVERY_CODES, json={"code": totp_code(totp, harness.clock)})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    new_codes = response.json()["recovery_codes"]
    assert len(new_codes) == 10 and not set(new_codes) & set(old_codes)
    assert len(recovery_rows(superuser_engine, member.user_id)) == 10
    assert "mfa.recovery_regenerate" in audit_actions(superuser_engine, member.user_id)
    pending = harness.client()
    login(pending, member.email)
    assert verify(pending, old_codes[0]).status_code == 401
    assert verify(pending, new_codes[0]).status_code == 200


def test_disable_and_regenerate_refuse_a_user_without_2fa(
    harness: Harness, app_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    login(client, member.email)

    assert client.request("DELETE", MFA_PATH, json={"code": "123456"}).status_code == 409
    assert client.post(RECOVERY_CODES, json={"code": "123456"}).status_code == 409


def test_every_2fa_event_is_audited(
    harness: Harness, superuser_engine: Engine, enrolled
) -> None:
    member, totp, codes = enrolled
    client = _signed_in(harness, member, totp)
    assert client.post(RECOVERY_CODES, json={"code": codes[0]}).status_code == 200
    harness.clock.advance(seconds=30)
    assert client.request(
        "DELETE", MFA_PATH, json={"code": totp_code(totp, harness.clock)}
    ).status_code == 204

    assert sorted(audit_actions(superuser_engine, member.user_id)) == sorted(
        [
            "mfa.enroll",
            "mfa.confirm",
            "mfa.recovery_use",
            "mfa.recovery_regenerate",
            "mfa.disable",
        ]
    )
