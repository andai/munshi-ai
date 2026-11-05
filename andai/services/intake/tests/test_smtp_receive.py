"""The SMTP receiver end to end: a real aiosmtpd server on an ephemeral port, stdlib smtplib as the
sending server, and a disposable Postgres at head behind `store.save`."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import secrets
import smtplib
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from andai_core.db.session import transaction
from andai_intake import replay, store
from andai_intake.smtp import IntakeHandler, start_smtp
from andai_intake.store import Envelope, Stored

NOTETAKER = "notetaker@munshi.test"
ALIAS = "notes@munshi.test"
LIMIT = 64 * 1024


@dataclass
class Receiver:
    host: str
    port: int

    def client(self, helo: str = "mx.sender.test") -> smtplib.SMTP:
        return smtplib.SMTP(self.host, self.port, local_hostname=helo, timeout=10)


@contextmanager
def receiver(save: Callable[[bytes, Envelope], Stored]) -> Iterator[Receiver]:
    """The receiver in its own event loop on a background thread, on 127.0.0.1 and a free port."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    handler = IntakeHandler(save, [NOTETAKER, ALIAS.upper()])
    server = asyncio.run_coroutine_threadsafe(
        start_smtp(handler, "127.0.0.1", 0, data_size_limit=LIMIT, hostname="intake.test"), loop
    ).result(10)
    try:
        yield Receiver("127.0.0.1", server.sockets[0].getsockname()[1])
    finally:

        async def close() -> None:
            server.close()

        asyncio.run_coroutine_threadsafe(close(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()


def _message(subject: str = "Weekly sync") -> bytes:
    return (
        b"From: Organiser <organiser@sender.test>\r\n"
        b"To: " + NOTETAKER.encode() + b"\r\n"
        b"Subject: " + subject.encode() + b"\r\n"
        b"Message-ID: <" + secrets.token_hex(8).encode() + b"@sender.test>\r\n"
        b"\r\n"
        b"Body line\r\n"
        b".A line that starts with a dot\r\n"
        b"nonce " + secrets.token_hex(16).encode() + b"\r\n"
    )


def _rows(engine: Engine, raw: bytes) -> list:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, raw, mail_from, rcpt_to, host(peer_ip) AS peer, helo, message_id,"
                " status FROM inbound_mail WHERE raw_sha256 = :s"
            ),
            {"s": hashlib.sha256(raw).hexdigest()},
        ).all()


@pytest.fixture
def save(app_engine: Engine) -> Callable[[bytes, Envelope], Stored]:
    return functools.partial(store.save, app_engine)


def test_an_accepted_message_is_stored_with_its_envelope(
    save, superuser_engine: Engine
) -> None:
    raw = _message()
    with receiver(save) as rx, rx.client() as smtp:
        refused = smtp.sendmail("organiser@sender.test", [NOTETAKER, "Notes@Munshi.Test"], raw)
    assert refused == {}

    [row] = _rows(superuser_engine, raw)
    assert bytes(row.raw) == raw  # byte for byte, dot-stuffing undone, CRLF kept
    assert row.mail_from == "organiser@sender.test"
    assert row.rcpt_to == [NOTETAKER, "Notes@Munshi.Test"]
    assert (row.peer, row.helo, row.status) == ("127.0.0.1", "mx.sender.test", "received")
    assert row.message_id.startswith("<") and row.message_id.endswith("@sender.test>")


def test_an_unknown_recipient_is_refused_and_nothing_is_stored(
    save, superuser_engine: Engine
) -> None:
    raw = _message()
    with receiver(save) as rx, rx.client() as smtp:
        smtp.ehlo()
        assert smtp.mail("organiser@sender.test")[0] == 250
        code, _ = smtp.rcpt("someone-else@munshi.test")
        assert code == 550
        smtp.rset()
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            smtp.sendmail("organiser@sender.test", ["someone-else@munshi.test"], raw)
    assert _rows(superuser_engine, raw) == []


def test_only_the_notetaker_copy_is_kept_when_other_recipients_are_refused(
    save, superuser_engine: Engine
) -> None:
    raw = _message()
    with receiver(save) as rx, rx.client() as smtp:
        refused = smtp.sendmail("organiser@sender.test", [NOTETAKER, "other@munshi.test"], raw)
    assert set(refused) == {"other@munshi.test"}
    [row] = _rows(superuser_engine, raw)
    assert row.rcpt_to == [NOTETAKER]


def test_oversize_mail_is_refused_whether_declared_or_not(
    save, superuser_engine: Engine
) -> None:
    raw = _message() + b"x" * 70 + b"\r\n"
    big = raw + (b"y" * 998 + b"\r\n") * (LIMIT // 1000 + 1)
    with receiver(save) as rx:
        with rx.client() as smtp:  # SIZE declared in MAIL FROM (smtplib adds it)
            with pytest.raises(smtplib.SMTPSenderRefused) as declared:
                smtp.sendmail("organiser@sender.test", [NOTETAKER], big)
        assert declared.value.smtp_code == 552
        with rx.client() as smtp:  # no SIZE: refused once DATA grows past the limit
            smtp.ehlo()
            assert smtp.mail("organiser@sender.test")[0] == 250
            assert smtp.rcpt(NOTETAKER)[0] == 250
            assert smtp.data(big)[0] == 552
    assert _rows(superuser_engine, big) == []


def test_a_sixth_recipient_is_refused_with_452() -> None:
    def never(raw: bytes, envelope: Envelope) -> Stored:  # pragma: no cover - never reached
        raise AssertionError("nothing should be stored")

    with receiver(never) as rx, rx.client() as smtp:
        smtp.ehlo()
        assert smtp.mail("organiser@sender.test")[0] == 250
        # The cap counts accepted recipients, whatever their address.
        codes = [smtp.rcpt(a)[0] for a in [NOTETAKER, ALIAS] * 3]
        assert codes == [250, 250, 250, 250, 250, 452]
        smtp.rset()


def test_a_store_failure_answers_451_and_leaves_nothing_behind(
    app_engine: Engine, superuser_engine: Engine, migrated
) -> None:
    raw = _message()

    def fails_before_commit(data: bytes, envelope: Envelope) -> Stored:
        with transaction(app_engine) as tx:
            tx.execute(
                text(
                    "SELECT * FROM andai_store_inbound(:r, :f, :t, CAST(:p AS inet), :h, NULL)"
                ),
                {
                    "r": data,
                    "f": envelope.mail_from,
                    "t": list(envelope.rcpt_to),
                    "p": envelope.peer_ip,
                    "h": envelope.helo,
                },
            )
            raise RuntimeError("connection dropped before commit")

    with receiver(fails_before_commit) as rx, rx.client() as smtp:
        with pytest.raises(smtplib.SMTPDataError) as refused:
            smtp.sendmail("organiser@sender.test", [NOTETAKER], raw)
    assert refused.value.smtp_code == 451
    assert _rows(superuser_engine, raw) == []

    # A database that cannot be reached at all.
    unreachable = create_engine(
        migrated.app_url.replace(f":{migrated.port}/", ":1/"), pool_pre_ping=True
    )
    try:
        with receiver(functools.partial(store.save, unreachable)) as rx, rx.client() as smtp:
            with pytest.raises(smtplib.SMTPDataError) as down:
                smtp.sendmail("organiser@sender.test", [NOTETAKER], raw)
    finally:
        unreachable.dispose()
    assert down.value.smtp_code == 451
    assert _rows(superuser_engine, raw) == []


def test_the_same_message_delivered_twice_is_accepted_twice_and_stored_once(
    save, superuser_engine: Engine
) -> None:
    raw = _message()
    with receiver(save) as rx:
        for _ in range(2):
            with rx.client() as smtp:
                assert smtp.sendmail("organiser@sender.test", [NOTETAKER], raw) == {}
    assert len(_rows(superuser_engine, raw)) == 1


def test_replay_stores_the_file_bytes(
    save, superuser_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    crlf = _message("Replayed with CRLF")
    bare_lf = _message("Replayed with LF").replace(b"\r\n", b"\n").rstrip(b"\n")
    (tmp_path / "crlf.eml").write_bytes(crlf)
    (tmp_path / "lf.eml").write_bytes(bare_lf)

    with receiver(save) as rx:
        code = replay.main(
            [
                "--host", rx.host,
                "--port", str(rx.port),
                "--mail-from", "forwarder@sender.test",
                "--rcpt", NOTETAKER,
                "--helo", "replay.test",
                str(tmp_path / "crlf.eml"),
                str(tmp_path / "lf.eml"),
            ]
        )
        refused = replay.main(
            ["--host", rx.host, "--port", str(rx.port), "--mail-from", "x@sender.test",
             "--rcpt", "nobody@munshi.test", str(tmp_path / "crlf.eml")]
        )
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and refused == 1
    assert out[0].startswith("crlf.eml: 250") and out[1].startswith("lf.eml: 250")
    assert out[2].startswith("crlf.eml: 550")

    [row] = _rows(superuser_engine, crlf)  # sha256 of the stored bytes equals the file's
    assert (row.mail_from, row.rcpt_to, row.helo) == (
        "forwarder@sender.test", [NOTETAKER], "replay.test"
    )
    wire = bare_lf.replace(b"\n", b"\r\n") + b"\r\n"
    assert replay.wire_bytes(bare_lf) == wire
    [lf_row] = _rows(superuser_engine, wire)
    assert bytes(lf_row.raw) == wire
