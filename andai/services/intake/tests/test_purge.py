"""Retention (D-58) and the store's dedup, against a disposable Postgres at head.

The purge runs on the database clock, so a test ages rows by moving their `received_at` back (as
the superuser): a row received N days ago is what the purge sees N days after delivery.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

import pytest
from sqlalchemy import Engine, text

from andai_intake.purge import Purged, purge_once
from andai_intake.store import Envelope, message_id, save

ENVELOPE = Envelope(
    mail_from="organiser@example.test",
    rcpt_to=("notetaker@munshi.test",),
    peer_ip="192.0.2.25",
    helo="mx.example.test",
)


def _message() -> bytes:
    return (
        b"Message-ID: <" + secrets.token_hex(8).encode() + b"@example.test>\r\n"
        b"Subject: Weekly sync\r\n\r\n" + secrets.token_bytes(32)
    )


def _age(engine: Engine, mail_id: uuid.UUID, days: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE inbound_mail SET received_at = now() - make_interval(secs => :s)"
                " WHERE id = :i"
            ),
            {"s": days * 86400, "i": mail_id},
        )


def _state(engine: Engine, mail_id: uuid.UUID) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT raw IS NOT NULL AS has_raw FROM inbound_mail WHERE id = :i"),
            {"i": mail_id},
        ).first()
    if row is None:
        return "deleted"
    return "raw" if row.has_raw else "purged"


def test_raw_goes_at_7_days_and_the_row_at_30(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    ages = {"fresh": 0, "six_days": 6.9, "seven_days": 7.01, "29_days": 29.9, "30_days": 30.01}
    ids = {label: save(app_engine, _message(), ENVELOPE).id for label in ages}
    for label, days in ages.items():
        _age(superuser_engine, ids[label], days)

    purged = purge_once(app_engine, raw_days=7, row_days=30)

    assert purged.raw_purged >= 2 and purged.rows_deleted >= 1
    assert {label: _state(superuser_engine, i) for label, i in ids.items()} == {
        "fresh": "raw",
        "six_days": "raw",
        "seven_days": "purged",
        "29_days": "purged",
        "30_days": "deleted",
    }
    # A second run finds nothing more to do for these rows.
    assert purge_once(app_engine, raw_days=7, row_days=30) == Purged(0, 0)


def test_purge_refuses_retention_that_drops_rows_before_their_raw(app_engine: Engine) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="raw_days <= row_days"):
        purge_once(app_engine, raw_days=7, row_days=3)
    with pytest.raises(DBAPIError, match="raw_days <= row_days"):
        purge_once(app_engine, raw_days=0, row_days=30)


def test_the_same_bytes_within_24_hours_are_stored_once(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    raw = _message()
    first = save(app_engine, raw, ENVELOPE)
    again = save(app_engine, raw, Envelope("other@example.test", ("notes@munshi.test",), None, None))
    assert first.duplicate is False
    assert (again.id, again.duplicate) == (first.id, True)

    # After 24 hours the same bytes are a new delivery.
    _age(superuser_engine, first.id, 1.01)
    later = save(app_engine, raw, ENVELOPE)
    assert later.duplicate is False and later.id != first.id

    with superuser_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM inbound_mail WHERE raw_sha256 = :s"),
            {"s": hashlib.sha256(raw).hexdigest()},
        ).scalar_one()
    assert count == 2


def test_message_id_is_read_from_the_headers_only() -> None:
    folded = b"Subject: x\r\nMessage-ID:\r\n <abc@example.test>\r\n\r\nMessage-ID: <body@x>\r\n"
    assert message_id(folded) == "<abc@example.test>"
    assert message_id(b"Subject: x\n\nMessage-ID: <body@x>\n") is None
    assert message_id(b"Message-ID: <" + b"a" * 2000 + b">\r\n\r\n") == "<" + "a" * 997
