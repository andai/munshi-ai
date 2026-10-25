"""First-run bootstrap, over HTTP and from the CLI, on a brand-new database."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import Engine, create_engine, text

from andai_core.__main__ import main as cli
from andai_core.auth.bootstrap import AlreadyBootstrapped, bootstrap, slugify
from andai_core.mail import MailError, MemoryMailer
from conftest import Harness, link_token, make_harness, make_settings

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


def test_bootstrap_then_owner_sets_password_and_signs_in(fresh_app_engine: Engine) -> None:
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
    assert sorted(actions) == [("invite.accept", False), ("org.bootstrap", True)]


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


def test_mail_failure_creates_nothing_and_bootstrap_can_retry(fresh_app_engine: Engine) -> None:
    harness = make_harness(fresh_app_engine)
    harness.services.mailer = FailingMailer()

    failed = harness.client().post(BOOTSTRAP, json={"email": "a@example.com", "org_name": "A"})

    assert failed.status_code == 503
    assert counts(fresh_app_engine) == {"users": 0}
    harness.services.mailer = harness.mailer
    retry = harness.client().post(BOOTSTRAP, json={"email": "a@example.com", "org_name": "A"})
    assert retry.status_code == 201


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


def test_cli_bootstrap(
    fresh_app_engine: Engine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from andai_core.settings import get_settings

    monkeypatch.setenv("ANDAI_PORTAL_ORIGIN", "http://portal.test")
    get_settings.cache_clear()
    mailer = MemoryMailer()
    try:
        code = cli(
            ["bootstrap", "--email", "Boss@Example.com", "--org", "Boss Co", "--name", "Boss"],
            engine=fresh_app_engine,
            mailer=mailer,
        )
        out = capsys.readouterr()
        again = cli(
            ["bootstrap", "--email", "x@example.com", "--org", "X"],
            engine=fresh_app_engine,
            mailer=mailer,
        )
        err = capsys.readouterr().err
    finally:
        get_settings.cache_clear()

    assert code == 0
    assert "created org 'Boss Co'" in out.out and "boss@example.com" in out.out
    token = link_token(mailer.outbox[0].text, "invite")
    assert token not in out.out + out.err
    assert again == 1 and "users already exist" in err
    assert len(mailer.outbox) == 1


def test_cli_rejects_a_bad_address(
    fresh_app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli(["bootstrap", "--email", "nope", "--org", "X"], engine=fresh_app_engine)
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
