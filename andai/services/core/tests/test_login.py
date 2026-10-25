"""Login, logout, sessions, rate limiting, the Origin check and the client address."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from sqlalchemy import Engine, text

from andai_core.auth import passwords
from andai_core.auth.clientip import ClientIpResolver
from andai_core.auth.sessions import COOKIE_NAME
from andai_core.db.session import transaction
from conftest import CLIENT_IP, PASSWORD, Harness, make_harness, new_member

LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/me"
INVALID = {"detail": "invalid email or password"}


def login(client, email: str, password: str = PASSWORD):
    return client.post(LOGIN, json={"email": email, "password": password})


def session_rows(superuser_engine: Engine, user_id: uuid.UUID) -> list:
    with superuser_engine.connect() as conn:
        return conn.execute(
            text("SELECT token_hash, stage, host(ip) FROM sessions WHERE user_id = :u"),
            {"u": user_id},
        ).all()


def set_status(superuser_engine: Engine, user_id: uuid.UUID, status: str) -> None:
    with superuser_engine.begin() as conn:
        conn.execute(text("UPDATE users SET status = :s WHERE id = :u"), {"s": status, "u": user_id})


# ─── login, me, logout ────────────────────────────────────────────────────────────────────────────


def test_login_sets_a_session_cookie_and_me_answers(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()

    response = login(client, member.email.upper())  # emails are matched case-insensitively

    assert response.status_code == 200
    assert response.json() == {"stage": "full"}
    cookie = response.headers["set-cookie"]
    token = response.cookies[COOKIE_NAME]
    assert len(token) >= 43  # 32 random bytes, base64url
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/" in lowered
    assert "secure" not in lowered  # dev posture
    assert token not in response.text
    rows = session_rows(superuser_engine, member.user_id)
    assert [(r[0], r[1], r[2]) for r in rows] == [
        (hashlib.sha256(token.encode()).hexdigest(), "full", CLIENT_IP)
    ]

    me = client.get(ME)
    assert me.status_code == 200
    assert me.json() == {
        "user": {"id": str(member.user_id), "email": member.email, "display_name": "Test Member"},
        "org": {
            "id": str(member.org_id),
            "name": f"Org {member.org_id.hex[:8]}",
            "slug": f"org-{member.org_id.hex}",
        },
        "role": "owner",
        "stage": "full",
        "mfa_enabled": False,
    }
    for secret in ("password", "hash", "token"):
        assert secret not in me.text


def test_cookie_is_secure_outside_dev(app_engine: Engine) -> None:
    member = new_member(app_engine)
    client = make_harness(app_engine, ANDAI_ENV="production").client()

    response = login(client, member.email)

    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_me_without_a_session_is_401(harness: Harness) -> None:
    client = harness.client()
    assert client.get(ME).status_code == 401
    client.cookies.set(COOKIE_NAME, "not-a-session")
    assert client.get(ME).json() == {"detail": "not authenticated"}


def test_logout_ends_the_session(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    token = login(client, member.email).cookies[COOKIE_NAME]

    response = client.post(LOGOUT)

    assert response.status_code == 204
    assert COOKIE_NAME in response.headers["set-cookie"]  # cleared in the browser
    assert session_rows(superuser_engine, member.user_id) == []
    replay = harness.client()
    replay.cookies.set(COOKIE_NAME, token)
    assert replay.get(ME).status_code == 401
    assert harness.client().post(LOGOUT).status_code == 204  # idempotent without a cookie


def test_wrong_password_is_401(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    response = login(harness.client(), member.email, "not the password at all")
    assert response.status_code == 401
    assert response.json() == INVALID
    assert "set-cookie" not in response.headers


def test_failure_responses_are_identical(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    known = new_member(app_engine)
    invited = new_member(app_engine, password=None, status="invited")
    disabled = new_member(app_engine)
    set_status(superuser_engine, disabled.user_id, "disabled")

    cases = {
        "unknown email": login(harness.client(ip="198.51.100.21"), "nobody@example.test"),
        "wrong password": login(harness.client(ip="198.51.100.22"), known.email, "wrong-password!"),
        "no password yet": login(harness.client(ip="198.51.100.23"), invited.email),
        "disabled, right password": login(harness.client(ip="198.51.100.24"), disabled.email),
    }

    for name, response in cases.items():
        assert response.status_code == 401, name
        assert response.content == b'{"detail":"invalid email or password"}', name
        assert "set-cookie" not in response.headers, name
    assert session_rows(superuser_engine, disabled.user_id) == []


def test_unknown_email_spends_a_password_verification(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = passwords.dummy_verify
    monkeypatch.setattr(passwords, "dummy_verify", lambda p: (calls.append(p), real(p)))

    login(harness.client(), f"{uuid.uuid4().hex}@example.test", "some password here")

    assert calls == ["some password here"]


def test_password_is_stored_as_argon2id_and_rehashed_on_login(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET password_hash = :h WHERE id = :u"),
            {"h": weak, "u": member.user_id},
        )

    assert login(harness.client(), member.email).status_code == 200

    with superuser_engine.connect() as conn:
        stored, last_login = conn.execute(
            text("SELECT password_hash, last_login_at FROM users WHERE id = :u"),
            {"u": member.user_id},
        ).one()
    assert stored.startswith("$argon2id$") and stored != weak
    assert not passwords.needs_rehash(stored)
    assert passwords.verify(stored, PASSWORD)
    assert last_login is not None


# ─── rate limiting ────────────────────────────────────────────────────────────────────────────────


def test_sixth_attempt_is_rate_limited(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    client = harness.client()

    for _ in range(5):
        assert login(client, member.email, "wrong-password!").status_code == 401
    sixth = login(client, member.email)  # even the right password

    assert sixth.status_code == 429
    assert sixth.json() == {"detail": "too many attempts; try again later"}
    assert 0 < int(sixth.headers["retry-after"]) <= 900
    assert "set-cookie" not in sixth.headers
    # The key is (email, client address): the same email from elsewhere is not blocked.
    assert login(harness.client(ip="198.51.100.99"), member.email).status_code == 200


def test_unknown_email_is_limited_like_a_known_one(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    known, unknown = harness.client(ip="198.51.100.31"), harness.client(ip="198.51.100.32")
    ghost = f"{uuid.uuid4().hex}@example.test"
    results = []
    for _ in range(6):
        results.append(
            (
                login(known, member.email, "wrong-password!"),
                login(unknown, ghost, "wrong-password!"),
            )
        )
    for a, b in results:
        assert (a.status_code, a.content) == (b.status_code, b.content)
    assert results[-1][0].status_code == 429


def test_success_clears_earlier_failures(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    client = harness.client()
    for _ in range(4):
        login(client, member.email, "wrong-password!")
    assert login(client, member.email).status_code == 200
    for _ in range(4):
        assert login(client, member.email, "wrong-password!").status_code == 401
    assert login(client, member.email).status_code == 200


# ─── session lifecycle ────────────────────────────────────────────────────────────────────────────


def test_login_rotates_the_token(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    first = login(client, member.email).cookies[COOKIE_NAME]

    second = login(client, member.email).cookies[COOKIE_NAME]  # the browser still sends `first`

    assert first != second
    hashes = [r[0] for r in session_rows(superuser_engine, member.user_id)]
    assert hashes == [hashlib.sha256(second.encode()).hexdigest()]
    stale = harness.client()
    stale.cookies.set(COOKIE_NAME, first)
    assert stale.get(ME).status_code == 401
    assert client.get(ME).status_code == 200


def test_planted_token_never_becomes_a_session(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    client = harness.client()
    planted = "attacker-chosen-token-value-0123456789abcdef"
    client.cookies.set(COOKIE_NAME, planted)

    issued = login(client, member.email).cookies[COOKIE_NAME]

    assert issued != planted
    attacker = harness.client()
    attacker.cookies.set(COOKIE_NAME, planted)
    assert attacker.get(ME).status_code == 401


def test_idle_and_absolute_expiry(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    client = harness.client()
    login(client, member.email)

    harness.clock.advance(hours=11, minutes=59)
    assert client.get(ME).status_code == 200  # touches last_seen_at
    harness.clock.advance(hours=11, minutes=59)
    assert client.get(ME).status_code == 200
    harness.clock.advance(hours=12)
    assert client.get(ME).status_code == 401  # idle

    login(client, member.email)
    start = harness.clock.now
    while client.get(ME).status_code == 200:  # active every 11 h, never idle for 12
        harness.clock.advance(hours=11)
        assert harness.clock.now - start <= timedelta(days=8)
    elapsed = harness.clock.now - start
    assert timedelta(days=7) <= elapsed < timedelta(days=7, hours=11)  # absolute cap


def test_disabled_user_cannot_log_in_and_their_session_is_rejected(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    assert login(client, member.email).status_code == 200

    set_status(superuser_engine, member.user_id, "disabled")

    assert client.get(ME).status_code == 401
    assert session_rows(superuser_engine, member.user_id) == []  # deleted on sight
    assert login(harness.client(ip="198.51.100.41"), member.email).json() == INVALID


def test_disable_user_ends_every_session(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    from andai_core.auth.users import disable_user

    member = new_member(app_engine)
    other = new_member(app_engine, role="admin", org_id=member.org_id)
    phone, laptop = harness.client(), harness.client(ip="198.51.100.42")
    login(phone, member.email)
    login(laptop, member.email)

    with transaction(app_engine, org_id=member.org_id, user_id=other.user_id) as tx:
        ended = disable_user(tx, org_id=member.org_id, user_id=member.user_id, actor=other.user_id)

    assert ended == 2
    assert phone.get(ME).status_code == 401 and laptop.get(ME).status_code == 401
    assert session_rows(superuser_engine, member.user_id) == []
    with superuser_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM users WHERE id = :u"), {"u": member.user_id}
        ).scalar_one()
        actions = conn.execute(
            text("SELECT action, actor_user_id FROM audit_events WHERE org_id = :o"),
            {"o": member.org_id},
        ).all()
    assert status == "disabled"
    assert ("user.disable", other.user_id) in actions


# ─── Origin check ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "http://portal.test.evil.example", "null", None]
)
def test_cross_site_state_change_is_refused(
    harness: Harness, app_engine: Engine, superuser_engine: Engine, origin: str | None
) -> None:
    member = new_member(app_engine)
    client = harness.client(origin=origin)

    response = login(client, member.email)

    assert response.status_code == 403
    assert response.json() == {"detail": "cross-site request refused"}
    assert session_rows(superuser_engine, member.user_id) == []


def test_origin_check_passes_safe_methods_and_the_portal(
    harness: Harness, app_engine: Engine
) -> None:
    member = new_member(app_engine)
    portal = harness.client(origin="HTTP://Portal.Test:80")  # same origin, other spelling
    assert login(portal, member.email).status_code == 200
    cross = harness.client(origin="https://evil.example")
    cross.cookies.set(COOKIE_NAME, portal.cookies[COOKIE_NAME])
    assert cross.get(ME).status_code == 200  # GET is not state-changing
    assert cross.post(LOGOUT).status_code == 403  # a cross-site logout is refused too


def test_validation_errors_do_not_echo_the_input(harness: Harness) -> None:
    secret = "x" * (passwords.MAX_LENGTH + 1)
    response = harness.client().post(LOGIN, json={"email": "a@example.test", "password": secret})
    assert response.status_code == 422
    assert secret not in response.text


# ─── client address ───────────────────────────────────────────────────────────────────────────────


def test_client_ip_from_an_untrusted_peer_ignores_x_forwarded_for() -> None:
    resolver = ClientIpResolver(["10.0.0.0/8"])
    assert resolver.client_ip("203.0.113.5", "192.0.2.1") == "203.0.113.5"
    assert resolver.client_ip("203.0.113.5", None) == "203.0.113.5"
    assert resolver.client_ip("testclient", "192.0.2.1") is None


def test_client_ip_from_a_trusted_proxy_uses_x_forwarded_for() -> None:
    resolver = ClientIpResolver(["10.0.0.0/8", "172.16.0.9"])
    assert resolver.client_ip("10.1.2.3", "192.0.2.1") == "192.0.2.1"
    # Right to left, skipping trusted hops; a client-supplied left part is not believed.
    assert resolver.client_ip("10.1.2.3", "1.1.1.1, 192.0.2.1, 172.16.0.9") == "192.0.2.1"
    assert resolver.client_ip("10.1.2.3", "garbage, 192.0.2.1") == "10.1.2.3"
    assert resolver.client_ip("::ffff:10.1.2.3", "2001:db8::1") == "2001:db8::1"


def test_client_ip_trusts_a_peer_resolved_from_the_portal_hostname() -> None:
    now = [0.0]
    lookups: list[str] = []
    answers = {"andai-portal": {"172.20.0.7"}}

    def resolve(host: str) -> set[str]:
        lookups.append(host)
        return answers[host]

    resolver = ClientIpResolver(["andai-portal"], resolve=resolve, clock=lambda: now[0])

    assert resolver.client_ip("172.20.0.7", "192.0.2.50") == "192.0.2.50"
    assert resolver.client_ip("172.20.0.8", "192.0.2.50") == "172.20.0.8"
    now[0] = 29.0
    answers["andai-portal"] = {"172.20.0.8"}  # the portal moved; the cached answer still holds
    assert resolver.client_ip("172.20.0.8", "192.0.2.50") == "172.20.0.8"
    assert lookups == ["andai-portal"]
    now[0] = 31.0
    assert resolver.client_ip("172.20.0.8", "192.0.2.50") == "192.0.2.50"
    assert lookups == ["andai-portal", "andai-portal"]


def test_unresolvable_proxy_hostname_trusts_nobody() -> None:
    def fail(host: str) -> set[str]:
        raise OSError("no such host")

    resolver = ClientIpResolver(["andai-portal"], resolve=fail)
    assert resolver.client_ip("172.20.0.7", "192.0.2.50") == "172.20.0.7"


def test_login_records_the_forwarded_address_only_via_the_trusted_proxy(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(app_engine)
    harness = make_harness(app_engine, ANDAI_TRUSTED_PROXIES="10.9.0.0/16")

    spoofed = harness.client(ip="203.0.113.77")
    response = spoofed.post(
        LOGIN,
        json={"email": member.email, "password": PASSWORD},
        headers={"X-Forwarded-For": "192.0.2.200"},
    )
    assert response.status_code == 200
    via_portal = harness.client(ip="10.9.0.5")
    via_portal.post(
        LOGIN,
        json={"email": member.email, "password": PASSWORD},
        headers={"X-Forwarded-For": "192.0.2.201"},
    )

    ips = sorted(r[2] for r in session_rows(superuser_engine, member.user_id))
    assert ips == ["192.0.2.201", "203.0.113.77"]


def test_limiter_key_follows_the_resolved_client_address(app_engine: Engine) -> None:
    member = new_member(app_engine)
    harness = make_harness(app_engine, ANDAI_TRUSTED_PROXIES="10.9.0.0/16")
    proxy = harness.client(ip="10.9.0.5")

    def attempt(forwarded: str):
        return proxy.post(
            LOGIN,
            json={"email": member.email, "password": "wrong-password!"},
            headers={"X-Forwarded-For": forwarded},
        )

    for _ in range(5):
        attempt("192.0.2.10")
    assert attempt("192.0.2.10").status_code == 429
    assert attempt("192.0.2.11").status_code == 401  # another client behind the same proxy
    # An untrusted peer cannot dodge its own limit by inventing X-Forwarded-For values.
    direct = harness.client(ip="203.0.113.90")
    for i in range(5):
        direct.post(
            LOGIN,
            json={"email": member.email, "password": "wrong-password!"},
            headers={"X-Forwarded-For": f"192.0.2.{100 + i}"},
        )
    assert (
        direct.post(
            LOGIN,
            json={"email": member.email, "password": PASSWORD},
            headers={"X-Forwarded-For": "192.0.2.250"},
        ).status_code
        == 429
    )
