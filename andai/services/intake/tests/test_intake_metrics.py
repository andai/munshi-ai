"""andai-intake `GET /metrics`: the SMTP receiver's verdicts (a real aiosmtpd server and stdlib
smtplib, as in test_smtp_receive.py) and confirmation mail counted with `kind="confirmation"`
against a disposable Mailpit and a refusing relay."""

from __future__ import annotations

import asyncio
import functools
import re
import smtplib
import uuid
import zoneinfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from andai_core.auth.ratelimit import MemoryLimiter
from andai_core.mail import SmtpMailer
from andai_intake import metrics, store
from andai_intake.confirm import Confirmer, Notice, build
from andai_intake.main import IntakeServices, create_app
from andai_intake.settings import IntakeSettings
from conftest import Mailpit, mailpit_messages
from test_smtp_receive import ALIAS, LIMIT, NOTETAKER, _message, receiver


def _scrape(app_engine: Engine | None = None) -> str:
    services = None
    if app_engine is not None:
        services = IntakeServices(settings=IntakeSettings(ANDAI_ENV="dev"), engine=app_engine)
    with TestClient(create_app(services, run_jobs=False)) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=")
    return response.text


def _value(body: str, series: str) -> float:
    match = re.search(rf"^{re.escape(series)} (\S+)$", body, re.MULTILINE)
    assert match, series
    return float(match.group(1))


def _smtp(body: str) -> dict[str, float]:
    return {
        result: _value(body, f'munshi_intake_smtp_total{{result="{result}"}}')
        for result in metrics.SMTP_RESULTS
    }


def _delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {k: after[k] - before[k] for k in before if after[k] != before[k]}


def test_intake_metrics_endpoint_serves_every_result_with_or_without_a_database(
    app_engine: Engine,
) -> None:
    for body in (_scrape(), _scrape(app_engine)):
        assert set(_smtp(body)) == set(metrics.SMTP_RESULTS)
        assert _value(body, 'munshi_mail_sent_total{kind="confirmation",outcome="sent"}') >= 0
        assert "_created" not in body


def test_intake_smtp_verdicts_are_counted(app_engine: Engine) -> None:
    save = functools.partial(store.save, app_engine)

    def fails(raw, envelope):
        raise RuntimeError("database gone")

    before = _smtp(_scrape())
    with receiver(save) as rx:
        with rx.client() as smtp:  # one message to two notetaker addresses, one stranger
            refused = smtp.sendmail(
                "organiser@sender.test", [NOTETAKER, ALIAS, "other@munshi.test"], _message()
            )
            assert set(refused) == {"other@munshi.test"}
        with rx.client() as smtp:  # a sixth recipient
            smtp.ehlo()
            smtp.mail("organiser@sender.test")
            codes = [smtp.rcpt(a)[0] for a in [NOTETAKER, ALIAS] * 3]
            assert codes[-1] == 452
            smtp.rset()
        big = _message() + (b"y" * 998 + b"\r\n") * (LIMIT // 1000 + 1)
        with rx.client() as smtp:  # SIZE declared in MAIL FROM
            with pytest.raises(smtplib.SMTPSenderRefused):
                smtp.sendmail("organiser@sender.test", [NOTETAKER], big)
        with rx.client() as smtp:  # no SIZE: DATA grows past the limit
            smtp.ehlo()
            smtp.mail("organiser@sender.test")
            smtp.rcpt(NOTETAKER)
            assert smtp.data(big)[0] == 552
    with receiver(fails) as rx, rx.client() as smtp:
        with pytest.raises(smtplib.SMTPDataError):
            smtp.sendmail("organiser@sender.test", [NOTETAKER], _message())
    after = _scrape()

    assert _delta(before, _smtp(after)) == {
        "accept": 1,
        "unknown_rcpt": 1,
        "too_many_rcpt": 1,
        "too_large": 2,
        "store_failed": 1,
    }
    assert "sender.test" not in after and "munshi.test" not in after and "127.0.0.1" not in after


def _notice(to: str) -> Notice:
    return Notice(
        to=to,
        outcome="scheduled",
        reason=None,
        title="Synthetic weekly sync",
        platform="zoom",
        zone=zoneinfo.ZoneInfo("Asia/Kolkata"),
        zone_name="Asia/Kolkata",
    )


def _confirmations(body: str) -> tuple[float, float]:
    return (
        _value(body, 'munshi_mail_sent_total{kind="confirmation",outcome="sent"}'),
        _value(body, 'munshi_mail_sent_total{kind="confirmation",outcome="failed"}'),
    )


def test_intake_confirmation_mail_is_counted_with_its_kind(mailpit: Mailpit) -> None:
    to = f"{uuid.uuid4().hex}@example.test"
    settings = IntakeSettings(
        ANDAI_ENV="dev", SMTP_HOST=mailpit.smtp_host, SMTP_PORT=str(mailpit.smtp_port)
    )
    refusing = IntakeSettings(ANDAI_ENV="dev", SMTP_HOST="127.0.0.1", SMTP_PORT="1")
    assert build(_notice(to), lead_s=120, horizon_days=14, notetaker=None).kind == "confirmation"
    before = _confirmations(_scrape())

    sent = asyncio.run(Confirmer(SmtpMailer(settings), MemoryLimiter(30, 3600)).send(_notice(to)))
    dropped = asyncio.run(
        Confirmer(SmtpMailer(refusing), MemoryLimiter(30, 3600)).send(_notice(to))
    )

    assert (sent, dropped) == (True, False)
    assert len(mailpit_messages(mailpit.api_url, to)) == 1
    after = _confirmations(_scrape())
    assert (after[0] - before[0], after[1] - before[1]) == (1, 1)
