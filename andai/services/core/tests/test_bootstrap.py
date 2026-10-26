"""First-run bootstrap, over HTTP and from the CLI, on a brand-new database: the first org (with
exactly one platform principal, provisioned through a stub admin-api) and its owner."""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import Engine, create_engine, text

from andai_core.__main__ import main as cli
from andai_core.auth.bootstrap import AlreadyBootstrapped, bootstrap
from andai_core.crypto import KEY_BYTES, Keyring
from andai_core.mail import MailError, MemoryMailer
from andai_core.org.service import slugify
from conftest import FakeAdminApi, Harness, link_token, make_harness, make_settings

BOOTSTRAP = "/api/v1/bootstrap"
ACCEPT = "/api/v1/auth/invite/accept"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/me"
OWNER_PASSWORD = "the owner's own passphrase"


class FailingMailer:
    async def send(self, mail) -> None:
        raise MailError("relay down")


def counts(engine: Engine) -> dict[str, int]:
    """Users in the database (a global table, readable by the app role without context)."""
    with engine.connect() as conn:
        return {"users": conn.execute(text("SELECT count(*) FROM users")).scalar_one()}


def stored(engine: Engine, postgres) -> dict[str, int]:
    """Orgs and platform principals really stored (as the superuser: RLS hides them otherwise)."""
    url = engine.url.set(username="postgres", password=postgres.superuser_password)
    su = create_engine(url)
    try:
        with su.connect() as conn:
            return {
                table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in ("orgs", "platform_principals")
            }
    finally:
        su.dispose()


def principal_row(engine: Engine, postgres) -> tuple:
    url = engine.url.set(username="postgres", password=postgres.superuser_password)
    su = create_engine(url)
    try:
        with su.connect() as conn:
            return tuple(
                conn.execute(
                    text(
                        "SELECT org_id, platform_user_id, key_ciphertext, key_id"
                        " FROM platform_principals"
                    )
                ).one()
            )
    finally:
        su.dispose()


def test_bootstrap_then_owner_sets_password_and_signs_in(
    fresh_app_engine: Engine, postgres
) -> None:
    harness = make_harness(fresh_app_engine)
    client = harness.client()

    response = client.post(
        BOOTSTRAP,
        json={"email": "Owner@Example.COM", "org_name": "Acme Widgets", "display_name": "Ann"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "invited" and set(body) == {"org_id", "user_id", "status"}
    [mail] = harness.mailer.outbox
    assert mail.to == "owner@example.com"
    assert mail.subject == "Set your munshi.ai password for Acme Widgets"
    token = link_token(mail.text, "invite")
    assert token not in response.text

    # The owner cannot sign in before setting a password.
    early = client.post(LOGIN, json={"email": "owner@example.com", "password": OWNER_PASSWORD})
    assert early.status_code == 401
    assert client.post(ACCEPT, json={"token": token, "password": OWNER_PASSWORD}).status_code == 200
    login = client.post(LOGIN, json={"email": "owner@example.com", "password": OWNER_PASSWORD})
    assert login.status_code == 200
    me = client.get(ME).json()
    assert me["role"] == "owner"
    assert me["org"] == {"id": body["org_id"], "name": "Acme Widgets", "slug": "acme-widgets"}
    assert me["user"]["display_name"] == "Ann"

    # Exactly one principal: the platform user of this org, with a bot,tx key sealed under the KEK.
    assert stored(fresh_app_engine, postgres) == {"orgs": 1, "platform_principals": 1}
    org_id, platform_user_id, ciphertext, key_id = principal_row(fresh_app_engine, postgres)
    assert str(org_id) == body["org_id"]
    assert harness.admin_api.users == {f"org-{org_id}@principals.munshi.invalid": platform_user_id}
    [minted] = harness.admin_api.tokens.values()
    assert minted["user_id"] == platform_user_id and minted["scopes"] == ["bot", "tx"]
    assert minted["token"] not in ciphertext and key_id == harness.services.keyring.key_id
    for text_seen in (response.text, login.text, client.get(ME).text, mail.text):
        assert minted["token"] not in text_seen


def test_audit_records_bootstrap_and_acceptance(fresh_app_engine: Engine, postgres) -> None:
    harness = make_harness(fresh_app_engine)
    client = harness.client()
    org_id = client.post(BOOTSTRAP, json={"email": "o@example.com", "org_name": "Org"}).json()[
        "org_id"
    ]
    token = link_token(harness.mailer.outbox[0].text, "invite")
    client.post(ACCEPT, json={"token": token, "password": OWNER_PASSWORD})

    url = fresh_app_engine.url.set(username="postgres", password=postgres.superuser_password)
    su = create_engine(url)
    try:
        with su.connect() as conn:
            actions = conn.execute(
                text("SELECT action, actor_user_id IS NULL FROM audit_events WHERE org_id = :o"),
                {"o": org_id},
            ).all()
    finally:
        su.dispose()
    assert sorted(actions) == [
        ("invite.accept", False),
        ("org.bootstrap", True),
        ("platform_principal.provision", True),
    ]


def test_second_bootstrap_is_refused(fresh_app_engine: Engine) -> None:
    harness = make_harness(fresh_app_engine)
    client = harness.client()
    first = client.post(BOOTSTRAP, json={"email": "a@example.com", "org_name": "A"})
    assert first.status_code == 201

    again = client.post(BOOTSTRAP, json={"email": "b@example.com", "org_name": "B"})

    assert again.status_code == 409 and again.json() == {"detail": "already bootstrapped"}
    assert counts(fresh_app_engine) == {"users": 1}
    assert len(harness.mailer.outbox) == 1


def test_bootstrap_is_refused_on_a_database_with_users(harness: Harness, app_engine: Engine) -> None:
    from conftest import new_member

    new_member(app_engine)
    response = harness.client().post(BOOTSTRAP, json={"email": "c@example.com", "org_name": "C"})
    assert response.status_code == 409


def test_mail_failure_creates_nothing_and_bootstrap_can_retry(
    fresh_app_engine: Engine, postgres
) -> None:
    harness = make_harness(fresh_app_engine)
    harness.services.mailer = FailingMailer()

    failed = harness.client().post(BOOTSTRAP, json={"email": "a@example.com", "org_name": "A"})

    assert failed.status_code == 503
    assert counts(fresh_app_engine) == {"users": 0}
    assert stored(fresh_app_engine, postgres) == {"orgs": 0, "platform_principals": 0}
    # The key minted for the org that was never stored is revoked again.
    assert harness.admin_api.tokens == {}
    harness.services.mailer = harness.mailer
    retry = harness.client().post(BOOTSTRAP, json={"email": "a@example.com", "org_name": "A"})
    assert retry.status_code == 201
    assert stored(fresh_app_engine, postgres) == {"orgs": 1, "platform_principals": 1}
    assert len(harness.admin_api.tokens) == 1


@pytest.mark.parametrize("failing", ["create user", "mint token"])
def test_an_admin_api_failure_creates_no_org(
    fresh_app_engine: Engine, postgres, failing: str
) -> None:
    harness = make_harness(fresh_app_engine)
    harness.admin_api.fail.add(failing)

    failed = harness.client().post(BOOTSTRAP, json={"email": "a@example.com", "org_name": "A"})

    assert failed.status_code == 503
    assert "nothing was created" in failed.json()["detail"]
    assert counts(fresh_app_engine) == {"users": 0}
    assert stored(fresh_app_engine, postgres) == {"orgs": 0, "platform_principals": 0}
    assert harness.mailer.outbox == []
    harness.admin_api.fail.clear()
    assert harness.client().post(
        BOOTSTRAP, json={"email": "a@example.com", "org_name": "A"}
    ).status_code == 201
    assert stored(fresh_app_engine, postgres) == {"orgs": 1, "platform_principals": 1}


def test_a_refused_admin_key_creates_no_org(fresh_app_engine: Engine, postgres) -> None:
    import httpx
    from pydantic import SecretStr

    from andai_core.platform.admin_client import HttpPlatformAdmin

    harness = make_harness(fresh_app_engine)
    harness.services.platform_admin = HttpPlatformAdmin(
        "http://admin-api.test",
        SecretStr("wrong-key"),
        transport=httpx.MockTransport(harness.admin_api.handler),
    )

    failed = harness.client().post(BOOTSTRAP, json={"email": "a@example.com", "org_name": "A"})

    assert failed.status_code == 503 and "wrong-key" not in failed.text
    assert stored(fresh_app_engine, postgres) == {"orgs": 0, "platform_principals": 0}


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-address", "org_name": "A"},
        {"email": "a@example.com", "org_name": "   "},
        {"email": "a@example.com"},
    ],
)
def test_invalid_bootstrap_input_is_422(fresh_app_engine: Engine, body: dict) -> None:
    harness = make_harness(fresh_app_engine)
    assert harness.client().post(BOOTSTRAP, json=body).status_code == 422
    assert counts(fresh_app_engine) == {"users": 0}


def test_concurrent_bootstraps_create_one_owner(fresh_app_engine: Engine) -> None:
    settings = make_settings()
    barrier = threading.Barrier(4)
    results: list[str] = []
    from datetime import UTC, datetime

    admin_api = FakeAdminApi()
    keyring = Keyring(os.urandom(KEY_BYTES))

    def run(i: int) -> None:
        barrier.wait()
        try:
            bootstrap(
                fresh_app_engine,
                email=f"racer{i}@example.com",
                org_name=f"Org {i}",
                display_name=None,
                settings=settings,
                now=datetime.now(UTC),
                send=lambda mail: None,
                admin=admin_api.client(),
                keyring=keyring,
            )
            results.append("created")
        except AlreadyBootstrapped:
            results.append("refused")

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(results) == ["created", "refused", "refused", "refused"]
    assert counts(fresh_app_engine) == {"users": 1}
    # The refused ones never reached the admin-api: they wait on the lock before provisioning.
    assert len(admin_api.users) == 1 and len(admin_api.tokens) == 1


def test_cli_bootstrap(
    fresh_app_engine: Engine,
    postgres,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from andai_core.settings import get_settings

    monkeypatch.setenv("ANDAI_PORTAL_ORIGIN", "http://portal.test")
    monkeypatch.setenv("ANDAI_KEK", base64.b64encode(os.urandom(KEY_BYTES)).decode())
    get_settings.cache_clear()
    mailer = MemoryMailer()
    admin_api = FakeAdminApi()
    try:
        code = cli(
            ["bootstrap", "--email", "Boss@Example.com", "--org", "Boss Co", "--name", "Boss"],
            engine=fresh_app_engine,
            mailer=mailer,
            admin=admin_api.client(),
        )
        out = capsys.readouterr()
        again = cli(
            ["bootstrap", "--email", "x@example.com", "--org", "X"],
            engine=fresh_app_engine,
            mailer=mailer,
            admin=admin_api.client(),
        )
        err = capsys.readouterr().err
    finally:
        get_settings.cache_clear()

    assert code == 0
    assert "created org 'Boss Co'" in out.out and "boss@example.com" in out.out
    token = link_token(mailer.outbox[0].text, "invite")
    assert token not in out.out + out.err
    [minted] = admin_api.tokens.values()
    assert minted["token"] not in out.out + out.err + err
    assert again == 1 and "users already exist" in err
    assert len(mailer.outbox) == 1
    assert stored(fresh_app_engine, postgres) == {"orgs": 1, "platform_principals": 1}


def test_cli_refuses_to_bootstrap_without_a_kek(
    fresh_app_engine: Engine,
    postgres,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from andai_core.settings import get_settings

    monkeypatch.delenv("ANDAI_KEK", raising=False)
    monkeypatch.delenv("ANDAI_KEK_FILE", raising=False)
    get_settings.cache_clear()
    admin_api = FakeAdminApi()
    try:
        code = cli(
            ["bootstrap", "--email", "a@example.com", "--org", "A"],
            engine=fresh_app_engine,
            mailer=MemoryMailer(),
            admin=admin_api.client(),
        )
    finally:
        get_settings.cache_clear()

    assert code == 2 and "ANDAI_KEK" in capsys.readouterr().err
    assert admin_api.calls == []
    assert stored(fresh_app_engine, postgres) == {"orgs": 0, "platform_principals": 0}


def test_cli_rejects_a_bad_address(
    fresh_app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli(
        ["bootstrap", "--email", "nope", "--org", "X"],
        engine=fresh_app_engine,
        admin=FakeAdminApi().client(),
        keyring=Keyring(os.urandom(KEY_BYTES)),
    )
    assert code == 2 and "not a valid email address" in capsys.readouterr().err


def test_slugify() -> None:
    assert slugify("Acme Widgets, Ltd.") == "acme-widgets-ltd"
    assert slugify("!!!") == "org"
    assert len(slugify("x" * 300)) == 63
    assert slugify(f"Org {uuid.UUID(int=1)}").startswith("org-00000000")


def test_canonical_email() -> None:
    from andai_core.auth.links import InvalidEmail, canonical_email

    assert canonical_email("  Owner@Example.COM ") == "owner@example.com"
    assert canonical_email("dev@team.test") == "dev@team.test"
    for bad in ("nope", "a@corp.local", "a@example.invalid", "a@localhost", "a b@example.com"):
        with pytest.raises(InvalidEmail):
            canonical_email(bad)
