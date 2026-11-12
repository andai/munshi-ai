"""Outgoing mail through settings-only SMTP.

`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` and `SMTP_STARTTLS` name the
relay; the stack's default is its Mailpit service (a mail catcher with a web UI on 127.0.0.1), and
a deployment points them at its own relay. `MemoryMailer` keeps messages in a list for tests.

Every message `SmtpMailer` hands to the relay is counted in `munshi_mail_sent_total{kind,outcome}`
(`andai_core.metrics`): `kind` is the `Mail`'s kind (one of `metrics.MAIL_KINDS`), `outcome` is
`sent` or `failed`.

Mail bodies carry single-use links; they are never logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import aiosmtplib

from andai_core import metrics
from andai_core.settings import Settings

log = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str
    # Extra header fields, e.g. `Auto-Submitted: auto-replied` on andai-intake's confirmations.
    headers: tuple[tuple[str, str], ...] = ()
    # What the mail is, for `munshi_mail_sent_total{kind}`: one of `metrics.MAIL_KINDS`.
    kind: str = "other"

    def __post_init__(self) -> None:
        if self.kind not in metrics.MAIL_KINDS:
            raise ValueError(f"unknown mail kind: {self.kind!r}")


class MailError(Exception):
    """The relay did not accept the message (the reason never includes the body)."""


class Mailer(Protocol):
    async def send(self, mail: Mail) -> None: ...


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def message(self, mail: Mail) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = mail.to
        message["Subject"] = mail.subject
        for name, value in mail.headers:
            message[name] = value
        message.set_content(mail.text)
        return message

    async def send(self, mail: Mail) -> None:
        s = self.settings
        try:
            await aiosmtplib.send(
                self.message(mail),
                hostname=s.smtp_host,
                port=s.smtp_port,
                username=s.smtp_user or None,
                password=s.smtp_password.get_secret_value() if s.smtp_password else None,
                start_tls=s.smtp_starttls,
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            metrics.count_mail(mail.kind, "failed")
            log.error(
                "mail to the SMTP relay %s:%s failed: %s",
                s.smtp_host,
                s.smtp_port,
                type(exc).__name__,
            )
            raise MailError(f"SMTP relay {s.smtp_host}:{s.smtp_port} refused the message") from exc
        metrics.count_mail(mail.kind, "sent")


class MemoryMailer:
    def __init__(self) -> None:
        self.outbox: list[Mail] = []

    async def send(self, mail: Mail) -> None:
        self.outbox.append(mail)
