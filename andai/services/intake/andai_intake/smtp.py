"""The notetaker mailbox's SMTP receiver (D-4): aiosmtpd, in andai-intake's own event loop.

Every decision is taken inside the SMTP session, so nothing is ever bounced after acceptance
(no backscatter):

* `RCPT TO` — only the configured notetaker addresses (case-insensitive) are accepted; any other
  recipient gets 550. At most `MAX_RECIPIENTS` recipients per message; the next one gets 452.
* size — `data_size_limit` makes aiosmtpd answer 552 to a `MAIL FROM` declaring a larger SIZE and
  to DATA that grows past it.
* `DATA` — the raw bytes exactly as received (`envelope.original_content`, never re-serialised:
  DKIM verification needs them byte for byte), the envelope, the peer address and the HELO name are
  stored by `store.save()` in a worker thread, so the event loop never waits on the database. The
  answer is 250 only after the row is committed; any failure to store answers 451 so the sending
  server retries.

Each verdict is counted in `munshi_intake_smtp_total{result}` (`metrics.py`): `accept`,
`unknown_rcpt`, `too_many_rcpt`, `store_failed`, and `too_large` for every 552 aiosmtpd answers
itself (`IntakeSMTP.push`).

No STARTTLS requirement and no AUTH: the receiver is the notetaker's inbound MX (RFC 3207 §4).
Message content is never logged. aiosmtpd's own `mail.log` logger writes data lines at DEBUG and
addresses at INFO, so it is held at WARNING.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable, Iterable
from typing import AnyStr

from aiosmtpd.smtp import SMTP, Envelope as SmtpEnvelope, Session

from andai_intake import metrics
from andai_intake.settings import MAX_RECIPIENTS
from andai_intake.store import Envelope, Stored

log = logging.getLogger(__name__)
logging.getLogger("mail.log").setLevel(logging.WARNING)

Store = Callable[[bytes, Envelope], Stored]

TOO_MANY_RECIPIENTS = "452 4.5.3 Too many recipients"
UNKNOWN_RECIPIENT = "550 5.1.1 No such mailbox here"
NOT_STORED = "451 4.3.0 Message not stored; try again later"
ACCEPTED = "250 2.0.0 Message accepted"
# aiosmtpd's own answer to a declared SIZE or DATA past `data_size_limit`.
TOO_LARGE_CODE = "552"


def _peer_ip(session: Session) -> str | None:
    peer = session.peer
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return None


class IntakeHandler:
    """aiosmtpd handler: recipient check and store-then-250."""

    def __init__(
        self, store: Store, addresses: Iterable[str], *, max_recipients: int = MAX_RECIPIENTS
    ) -> None:
        self.store = store
        self.addresses = frozenset(a.strip().lower() for a in addresses if a.strip())
        self.max_recipients = max_recipients

    async def handle_RCPT(  # noqa: N802  (aiosmtpd hook name)
        self,
        server: SMTP,
        session: Session,
        envelope: SmtpEnvelope,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        if len(envelope.rcpt_tos) >= self.max_recipients:
            metrics.count_smtp("too_many_rcpt")
            return TOO_MANY_RECIPIENTS
        if address.strip().lower() not in self.addresses:
            metrics.count_smtp("unknown_rcpt")
            return UNKNOWN_RECIPIENT
        envelope.rcpt_tos.append(address)
        envelope.rcpt_options.extend(rcpt_options)
        return "250 2.1.5 OK"

    async def handle_DATA(  # noqa: N802  (aiosmtpd hook name)
        self, server: SMTP, session: Session, envelope: SmtpEnvelope
    ) -> str:
        raw = envelope.original_content
        if not raw or not envelope.rcpt_tos:
            metrics.count_smtp("store_failed")
            return NOT_STORED
        received = Envelope(
            mail_from=envelope.mail_from or "",
            rcpt_to=tuple(envelope.rcpt_tos),
            peer_ip=_peer_ip(session),
            helo=session.host_name,
        )
        try:
            stored = await asyncio.to_thread(self.store, raw, received)
        except Exception as exc:  # the sender retries; only the error type is logged
            log.warning("inbound message not stored: %s", type(exc).__name__)
            metrics.count_smtp("store_failed")
            return NOT_STORED
        log.info(
            "inbound message %s %s (%d bytes)",
            stored.id,
            "already stored" if stored.duplicate else "stored",
            len(raw),
        )
        metrics.count_smtp("accept")
        return ACCEPTED


class IntakeSMTP(SMTP):
    """aiosmtpd's SMTP session, counting the size refusals it answers without the handler."""

    async def push(self, status: AnyStr) -> None:
        code = status[:3] if isinstance(status, str) else status[:3].decode("ascii", "replace")
        if code == TOO_LARGE_CODE:
            metrics.count_smtp("too_large")
        await super().push(status)


async def start_smtp(
    handler: IntakeHandler,
    host: str,
    port: int,
    *,
    data_size_limit: int,
    hostname: str | None = None,
) -> asyncio.Server:
    """Listen on `host:port` in the running loop; the caller closes the returned server."""
    name = hostname or socket.gethostname()
    loop = asyncio.get_running_loop()
    return await loop.create_server(
        lambda: IntakeSMTP(
            handler,
            data_size_limit=data_size_limit,
            decode_data=False,
            enable_SMTPUTF8=True,
            require_starttls=False,
            hostname=name,
        ),
        host=host,
        port=port,
    )
