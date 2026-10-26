"""Each org's platform principal, against a stub admin-api (`httpx.MockTransport`): one per org,
idempotent on retry, its key sealed under the KEK and never returned or logged."""

from __future__ import annotations

import logging
import os
import uuid

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text

from andai_core.crypto import KEY_BYTES, DecryptionError, Keyring
from andai_core.db.session import transaction
from andai_core.org.service import create_org
from andai_core.platform import principals
from andai_core.platform.admin_client import HttpPlatformAdmin, PlatformAdminError
from conftest import FakeAdminApi, Harness, link_token, make_harness


def new_org(harness: Harness, engine: Engine, name: str | None = None, **kwargs):
    return create_org(
        engine,
        name=name or f"Org {uuid.uuid4().hex[:8]}",
        owner_email=f"{uuid.uuid4().hex}@example.test",
        owner_display_name=None,
        admin=harness.services.platform_admin,
        keyring=harness.services.keyring,
        now=harness.clock(),
        **kwargs,
    )


def principal_rows(superuser_engine: Engine, org_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT platform_user_id, key_ciphertext, key_id FROM platform_principals"
                    " WHERE org_id = :o"
                ),
                {"o": org_id},
            )
        ]


def org_count(superuser_engine: Engine, name: str) -> int:
    with superuser_engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM orgs WHERE name = :n"), {"n": name}
        ).scalar_one()


def test_each_org_gets_exactly_one_principal_of_its_own(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    a = new_org(harness, app_engine)
    b = new_org(harness, app_engine)

    [(a_user, a_cipher, a_key_id)] = principal_rows(superuser_engine, a.org_id)
    [(b_user, _, _)] = principal_rows(superuser_engine, b.org_id)
    assert a_user == a.platform_user_id and b_user == b.platform_user_id and a_user != b_user
    assert harness.admin_api.users == {
        principals.principal_email(a.org_id): a_user,
        principals.principal_email(b.org_id): b_user,
    }
    assert principals.principal_email(a.org_id) == f"org-{a.org_id}@principals.munshi.invalid"
    tokens = {t["user_id"]: t for t in harness.admin_api.tokens.values()}
    assert set(tokens) == {a_user, b_user}
    assert all(t["scopes"] == ["bot", "tx"] for t in tokens.values())
    assert a_key_id == harness.services.keyring.key_id
    assert tokens[a_user]["token"] not in a_cipher

    # Under org A's context only A's principal exists, and it opens to A's key.
    with transaction(app_engine, org_id=a.org_id) as tx:
        key = principals.open_key(tx, harness.services.keyring, a.org_id)
        assert key == tokens[a_user]["token"]
        assert principals.open_key(tx, harness.services.keyring, b.org_id) is None


def test_the_key_is_bound_to_its_org(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    a = new_org(harness, app_engine)
    b = new_org(harness, app_engine)
    [(_, cipher, key_id)] = principal_rows(superuser_engine, a.org_id)

    with pytest.raises(DecryptionError):
        harness.services.keyring.open(cipher, key_id, principals.aad(b.org_id))
    with pytest.raises(DecryptionError):
        Keyring(os.urandom(KEY_BYTES)).open(cipher, key_id, principals.aad(a.org_id))


def test_provisioning_again_reuses_the_platform_user_and_stores_once(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    admin = harness.services.platform_admin
    org = new_org(harness, app_engine)
    [(user_id, cipher, _)] = principal_rows(superuser_engine, org.org_id)

    again = principals.provision(admin, org.org_id)

    assert again.platform_user_id == user_id
    assert len(harness.admin_api.users) == 1
    with transaction(app_engine, org_id=org.org_id) as tx:
        keyring, now = harness.services.keyring, harness.clock()
        assert principals.store(tx, keyring, org.org_id, again, now) is False
    [(stored_user, stored_cipher, _)] = principal_rows(superuser_engine, org.org_id)
    assert (stored_user, stored_cipher) == (user_id, cipher)
    principals.discard(admin, again)
    assert again.token_id not in harness.admin_api.tokens
    assert len(harness.admin_api.tokens) == 1


def test_a_retry_after_a_failed_mint_reuses_the_user(harness: Harness) -> None:
    admin = harness.services.platform_admin
    org_id = uuid.uuid4()
    harness.admin_api.fail.add("mint token")
    with pytest.raises(PlatformAdminError):
        principals.provision(admin, org_id)
    created_user = harness.admin_api.users[principals.principal_email(org_id)]

    harness.admin_api.fail.clear()
    minted = principals.provision(admin, org_id)

    assert minted.platform_user_id == created_user
    assert len(harness.admin_api.users) == 1 and len(harness.admin_api.tokens) == 1


def test_a_failure_after_minting_revokes_the_key_and_stores_nothing(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    name = f"Doomed {uuid.uuid4().hex[:8]}"

    def explode(tx, created) -> None:
        raise RuntimeError("later step failed")

    with pytest.raises(RuntimeError):
        new_org(harness, app_engine, name=name, finish=explode)

    assert harness.admin_api.tokens == {}
    assert org_count(superuser_engine, name) == 0
    with superuser_engine.connect() as conn:
        orphans = conn.execute(
            text(
                "SELECT count(*) FROM platform_principals p"
                " LEFT JOIN orgs o ON o.id = p.org_id WHERE o.id IS NULL"
            )
        ).scalar_one()
    assert orphans == 0


def test_an_admin_api_failure_inserts_nothing(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    name = f"Unprovisioned {uuid.uuid4().hex[:8]}"
    harness.admin_api.fail.add("create user")

    with pytest.raises(PlatformAdminError):
        new_org(harness, app_engine, name=name)

    assert org_count(superuser_engine, name) == 0
    assert harness.admin_api.tokens == {}


def test_two_orgs_with_one_name_get_distinct_slugs(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    name = f"Same Name {uuid.uuid4().hex[:8]}"
    first = new_org(harness, app_engine, name=name)
    second = new_org(harness, app_engine, name=name)

    with superuser_engine.connect() as conn:
        slugs = dict(
            conn.execute(text("SELECT id, slug FROM orgs WHERE name = :n"), {"n": name}).all()
        )
    base = name.lower().replace(" ", "-")
    assert slugs == {first.org_id: base, second.org_id: f"{base}-{second.org_id.hex[:8]}"}
    assert len(principal_rows(superuser_engine, second.org_id)) == 1


def test_a_second_principal_for_an_org_is_refused(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    from sqlalchemy.exc import IntegrityError

    from andai_core.db.models import PlatformPrincipal

    org = new_org(harness, app_engine)
    with pytest.raises(IntegrityError):
        with transaction(app_engine, org_id=org.org_id) as tx:
            tx.add(
                PlatformPrincipal(
                    org_id=org.org_id, platform_user_id=1, key_ciphertext="x", key_id="k"
                )
            )
    assert len(principal_rows(superuser_engine, org.org_id)) == 1


def test_the_key_and_the_admin_key_never_appear_in_responses_or_logs(
    fresh_app_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    harness = make_harness(fresh_app_engine)
    client = harness.client()
    seen: list[str] = []

    def keep(response: httpx.Response) -> httpx.Response:
        seen.append(response.text + str(response.headers))
        return response

    keep(client.post("/api/v1/bootstrap", json={"email": "o@example.com", "org_name": "Keys"}))
    token = link_token(harness.mailer.outbox[0].text, "invite")
    password = "a long passphrase"
    keep(client.post("/api/v1/auth/invite/accept", json={"token": token, "password": password}))
    login = keep(
        client.post("/api/v1/auth/login", json={"email": "o@example.com", "password": password})
    )
    assert login.status_code == 200
    for path in ("/api/v1/me", "/api/v1/org/users", "/api/v1/teams", "/api/v1/org/security"):
        response = keep(client.get(path))
        assert response.status_code == 200, path
    keep(client.post("/api/v1/teams", json={"name": "A team"}))
    keep(client.post("/api/v1/org/users", json={"email": "m@example.com", "role": "member"}))

    [minted] = harness.admin_api.tokens.values()
    secrets_ = (minted["token"], FakeAdminApi.ADMIN_KEY)
    for secret in secrets_:
        assert all(secret not in text_ for text_ in seen)
        assert secret not in caplog.text
        assert all(secret not in mail.text for mail in harness.mailer.outbox)


def test_admin_client_errors_name_the_call_not_the_secrets() -> None:
    fake = FakeAdminApi()
    wrong = HttpPlatformAdmin(
        "http://admin-api.test", SecretStr("wrong-key"), transport=httpx.MockTransport(fake.handler)
    )
    with pytest.raises(PlatformAdminError) as refused:
        wrong.ensure_user("x@principals.munshi.invalid")
    assert str(refused.value) == "admin-api create user answered 403"

    unset = HttpPlatformAdmin(
        "http://admin-api.test", None, transport=httpx.MockTransport(fake.handler)
    )
    with pytest.raises(PlatformAdminError, match="PLATFORM_ADMIN_TOKEN is not set"):
        unset.ensure_user("x@principals.munshi.invalid")
    assert fake.calls == [("POST", "/admin/users")]

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    down = HttpPlatformAdmin(
        "http://admin-api.test",
        SecretStr(fake.ADMIN_KEY),
        transport=httpx.MockTransport(unreachable),
    )
    with pytest.raises(PlatformAdminError) as error:
        down.mint_token(1, ("bot", "tx"))
    assert str(error.value) == "admin-api mint token: ConnectError"
    assert fake.ADMIN_KEY not in str(error.value)


def test_admin_client_refuses_a_token_with_other_scopes() -> None:
    def narrow(request: httpx.Request) -> httpx.Response:
        body = {"id": 1, "token": "vxa_x", "user_id": 1, "scopes": ["bot"]}
        return httpx.Response(201, json=body)

    client = HttpPlatformAdmin(
        "http://admin-api.test", SecretStr("k"), transport=httpx.MockTransport(narrow)
    )
    with pytest.raises(PlatformAdminError, match="other scopes"):
        client.mint_token(1, ("bot", "tx"))


def test_minted_values_do_not_show_the_key_in_their_repr() -> None:
    minted = principals.Minted(platform_user_id=1, token_id=2, key="vxa_secret_value")
    assert "vxa_secret_value" not in repr(minted)
