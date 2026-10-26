"""Mail through real SMTP: a disposable Mailpit receives it, its HTTP API reads it back."""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import Engine

from andai_core.mail import Mail, MailError, SmtpMailer
from conftest import (
    PASSWORD,
    PORTAL_ORIGIN,
    AdminApiServer,
    Mailpit,
    link_token,
    mailpit_messages,
    make_harness,
    make_settings,
    new_member,
)

NEW_PASSWORD = "delivered by smtp 2026"


def smtp_settings(mailpit: Mailpit, **extra: str):
    return make_settings(
        SMTP_HOST=mailpit.smtp_host,
        SMTP_PORT=str(mailpit.smtp_port),
        SMTP_FROM="munshi.ai <no-reply@munshi.invalid>",
        **extra,
    )


def test_smtp_mailer_delivers_to_mailpit(mailpit: Mailpit) -> None:
    to = f"{uuid.uuid4().hex}@example.test"
    mailer = SmtpMailer(smtp_settings(mailpit))

    asyncio.run(mailer.send(Mail(to=to, subject="Hello from andai-core", text="line one\n")))

    [message] = mailpit_messages(mailpit.api_url, to)
    assert message["Subject"] == "Hello from andai-core"
    assert message["From"]["Address"] == "no-reply@munshi.invalid"
    assert message["Text"].strip() == "line one"


def test_unreachable_relay_raises_mail_error(mailpit: Mailpit) -> None:
    settings = make_settings(SMTP_HOST="127.0.0.1", SMTP_PORT="1")
    with pytest.raises(MailError) as caught:
        asyncio.run(SmtpMailer(settings).send(Mail(to="a@example.test", subject="s", text="body")))
    assert "body" not in str(caught.value)


def test_password_reset_mail_arrives_and_works(mailpit: Mailpit, app_engine: Engine) -> None:
    member = new_member(app_engine)
    harness = make_harness(
        app_engine, SMTP_HOST=mailpit.smtp_host, SMTP_PORT=str(mailpit.smtp_port)
    )
    harness.services.mailer = SmtpMailer(harness.services.settings)
    client = harness.client()

    answer = client.post("/api/v1/auth/password-reset", json={"email": member.email})
    assert answer.status_code == 202

    [message] = mailpit_messages(mailpit.api_url, member.email)
    assert message["Subject"] == "Reset your munshi.ai password"
    token = link_token(message["Text"], "reset-password")
    confirm = client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "password": NEW_PASSWORD}
    )
    assert confirm.status_code == 200
    old = client.post("/api/v1/auth/login", json={"email": member.email, "password": PASSWORD})
    new = client.post("/api/v1/auth/login", json={"email": member.email, "password": NEW_PASSWORD})
    assert (old.status_code, new.status_code) == (401, 200)


def test_unknown_address_gets_no_mail(mailpit: Mailpit, app_engine: Engine) -> None:
    harness = make_harness(
        app_engine, SMTP_HOST=mailpit.smtp_host, SMTP_PORT=str(mailpit.smtp_port)
    )
    harness.services.mailer = SmtpMailer(harness.services.settings)
    ghost = f"{uuid.uuid4().hex}@example.test"

    answer = harness.client().post("/api/v1/auth/password-reset", json={"email": ghost})

    assert answer.status_code == 202
    assert mailpit_messages(mailpit.api_url, ghost, timeout=2.0) == []


def test_cli_bootstrap_mails_the_owner_through_smtp(
    mailpit: Mailpit, fresh_app_engine: Engine, admin_api_server: AdminApiServer
) -> None:
    owner = f"{uuid.uuid4().hex}@example.test"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANDAI_", "SMTP_", "PLATFORM_"))
    }
    env.update(
        ANDAI_DB_URL_APP=fresh_app_engine.url.render_as_string(hide_password=False),
        ANDAI_PORTAL_ORIGIN=PORTAL_ORIGIN,
        ANDAI_KEK=base64.b64encode(os.urandom(32)).decode(),
        PLATFORM_ADMIN_URL=admin_api_server.url,
        PLATFORM_ADMIN_TOKEN=admin_api_server.fake.ADMIN_KEY,
        SMTP_HOST=mailpit.smtp_host,
        SMTP_PORT=str(mailpit.smtp_port),
    )

    result = subprocess.run(
        [sys.executable, "-m", "andai_core", "bootstrap", "--email", owner, "--org", "Smtp Org"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    [message] = mailpit_messages(mailpit.api_url, owner)
    assert message["Subject"] == "Set your munshi.ai password for Smtp Org"
    token = link_token(message["Text"], "invite")
    assert token not in result.stdout + result.stderr
    # The org's principal was provisioned over HTTP; its key appears in no output.
    [minted] = admin_api_server.fake.tokens.values()
    assert minted["scopes"] == ["bot", "tx"]
    assert minted["token"] not in result.stdout + result.stderr
    harness = make_harness(fresh_app_engine)
    accepted = harness.client().post(
        "/api/v1/auth/invite/accept", json={"token": token, "password": PASSWORD}
    )
    assert accepted.status_code == 200
