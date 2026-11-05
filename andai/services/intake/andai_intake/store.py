"""The durable store behind the SMTP receiver: one `inbound_mail` row per accepted message.

`save()` runs in a worker thread (the SMTP handler calls it through `asyncio.to_thread`) and stores
the raw bytes exactly as received, with the envelope, peer address and HELO, through
`andai_store_inbound` (migration 0012) in one transaction. It returns only after the commit, so the
receiver answers 250 only for a message that is on disk. The same raw bytes delivered again within
24 hours (an SMTP retry, a second notetaker alias) are stored once; the function reports the
earlier row as a duplicate.

The raw bytes are never logged; only the Message-ID header is read here, from the header block.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from email.policy import compat32

from sqlalchemy import Engine, text

from andai_core.db.session import transaction

MESSAGE_ID_MAX = 998


@dataclass(frozen=True)
class Envelope:
    mail_from: str
    rcpt_to: tuple[str, ...]
    peer_ip: str | None
    helo: str | None


@dataclass(frozen=True)
class Stored:
    id: uuid.UUID
    duplicate: bool


def _header_block(raw: bytes) -> bytes:
    for separator in (b"\r\n\r\n", b"\n\n"):
        end = raw.find(separator)
        if end != -1:
            return raw[: end + len(separator)]
    return raw


def message_id(raw: bytes) -> str | None:
    """The message's Message-ID header, unfolded and trimmed, or None."""
    headers = BytesHeaderParser(policy=compat32).parsebytes(_header_block(raw))
    value = headers.get("Message-ID")
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value[:MESSAGE_ID_MAX] or None


def save(engine: Engine, raw: bytes, envelope: Envelope) -> Stored:
    """Store one received message and commit; raises on any database error."""
    with transaction(engine) as tx:
        row = tx.execute(
            text(
                "SELECT id, duplicate FROM andai_store_inbound("
                ":raw, :mail_from, :rcpt_to, CAST(:peer_ip AS inet), :helo, :message_id)"
            ),
            {
                "raw": raw,
                "mail_from": envelope.mail_from,
                "rcpt_to": list(envelope.rcpt_to),
                "peer_ip": envelope.peer_ip,
                "helo": envelope.helo,
                "message_id": message_id(raw),
            },
        ).one()
    return Stored(id=row.id, duplicate=row.duplicate)
