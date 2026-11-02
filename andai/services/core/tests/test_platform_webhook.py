"""The platform's system webhook, `POST /internal/platform-events` (`platform/webhook.py`): HMAC
over the raw body with a 300 s window, fail closed without a secret, dedup on `event_id`, the nudge
of the meeting's transcript copy, and the record and nudge in one transaction. Runs against the
disposable Postgres; requests carry no `Origin` and no cookie, like the platform's."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from andai_core.main import create_app
from andai_core.platform import webhook
from conftest import Harness, make_harness, new_member, seed_meeting

SECRET = "whsec-" + secrets.token_hex(16)
PATH = "/internal/platform-events"


def envelope(platform_id: int | None, *, event_type: str = "meeting.completed", **extra) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "api_version": "2026-10-01",
        "created_at": datetime.now(UTC).isoformat(),
        "data": {"meeting": {"id": platform_id, "status": "completed", "user_id": 7}},
        **extra,
    }


def signed(body: bytes, *, secret: str = SECRET, at: datetime | None = None) -> dict[str, str]:
    ts = str(int((at or datetime.now(UTC)).timestamp()))
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature": webhook.sign(secret, ts, body),
    }


def raw(event: dict) -> bytes:
    # Formatted unlike json.dumps' default, so the HMAC is proven over the bytes as sent.
    return json.dumps(event, indent=2, separators=(" ,", " : ")).encode()


@dataclass
class World:
    harness: Harness
    engine: Engine
    org_id: uuid.UUID
    meeting_id: uuid.UUID
    platform_id: int

    def client(self, *, raise_server_exceptions: bool = True) -> TestClient:
        return TestClient(
            create_app(self.harness.services),
            raise_server_exceptions=raise_server_exceptions,
        )

    def post(self, event: dict | bytes, *, headers: dict | None = None, client=None):
        body = event if isinstance(event, bytes) else raw(event)
        headers = signed(body) if headers is None else headers
        return (client or self.client()).post(PATH, content=body, headers=headers)

    def meeting(self) -> dict:
        with transaction(self.engine, org_id=self.org_id) as tx:
            return dict(
                tx.execute(
                    text(
                        "SELECT transcript_state, transcript_next_at, transcript_next_at <= now()"
                        " AS due FROM meetings WHERE id = :m"
                    ),
                    {"m": self.meeting_id},
                ).mappings().one()
            )

    def events(self, event_id: str) -> list[dict]:
        with transaction(self.engine) as tx:
            return [
                dict(r)
                for r in tx.execute(
                    text(
                        "SELECT event_id, event_type, platform_meeting_id"
                        " FROM platform_webhook_events WHERE event_id = :e"
                    ),
                    {"e": event_id},
                ).mappings()
            ]


def _platform_id() -> int:
    return 10**12 + uuid.uuid4().int % 10**9


def make_world(engine: Engine, **settings: str) -> World:
    harness = make_harness(engine, **settings)
    member = new_member(engine)
    meeting_id = seed_meeting(engine, member.org_id, member.user_id, None, status="done")
    platform_id = _platform_id()
    with transaction(engine, org_id=member.org_id) as tx:
        # The transcript copy is waiting for a retry (the platform was still writing it).
        tx.execute(
            text(
                "UPDATE meetings SET platform_meeting_id = :p, transcript_state = 'pending',"
                " transcript_attempts = 1, transcript_next_at = now() + interval '20 minutes'"
                " WHERE id = :m"
            ),
            {"p": platform_id, "m": meeting_id},
        )
    return World(harness, engine, member.org_id, meeting_id, platform_id)


@pytest.fixture
def world(app_engine: Engine) -> World:
    return make_world(app_engine, ANDAI_PLATFORM_WEBHOOK_SECRET=SECRET)


def test_a_signed_completion_is_recorded_and_makes_the_transcript_copy_due(world: World) -> None:
    event = envelope(world.platform_id)
    assert world.meeting()["due"] is False

    response = world.post(event)

    assert (response.status_code, response.json()) == (202, {"status": "accepted"})
    assert world.events(event["event_id"]) == [
        {
            "event_id": event["event_id"],
            "event_type": "meeting.completed",
            "platform_meeting_id": world.platform_id,
        }
    ]
    meeting = world.meeting()
    assert meeting["transcript_state"] == "pending" and meeting["due"] is True


def test_without_a_secret_every_request_is_refused_and_nothing_is_written(
    app_engine: Engine,
) -> None:
    unset = make_world(app_engine)
    event = envelope(unset.platform_id)

    response = unset.post(event)

    assert (response.status_code, response.json()) == (
        503, {"detail": "platform webhook not configured"}
    )
    assert unset.events(event["event_id"]) == []
    assert unset.meeting()["due"] is False


def _bad_headers(body: bytes) -> dict[str, dict[str, str]]:
    good = signed(body)
    now = datetime.now(UTC)
    return {
        "wrong secret": signed(body, secret="not-the-secret"),
        "wrong body": signed(body + b" "),
        "no signature": {k: v for k, v in good.items() if k != "X-Webhook-Signature"},
        "no timestamp": {k: v for k, v in good.items() if k != "X-Webhook-Timestamp"},
        "bare hex": {**good, "X-Webhook-Signature": good["X-Webhook-Signature"][7:]},
        "timestamp not a number": {**good, "X-Webhook-Timestamp": "soon"},
        "signature for another timestamp": {
            **good, "X-Webhook-Timestamp": str(int(good["X-Webhook-Timestamp"]) + 1)
        },
        "older than 300 s": signed(body, at=now - timedelta(seconds=301)),
        "301 s in the future": signed(body, at=now + timedelta(seconds=301)),
    }


@pytest.mark.parametrize(
    "case",
    [
        "wrong secret",
        "wrong body",
        "no signature",
        "no timestamp",
        "bare hex",
        "timestamp not a number",
        "signature for another timestamp",
        "older than 300 s",
        "301 s in the future",
    ],
)
def test_an_unauthenticated_request_is_401_and_writes_nothing(world: World, case: str) -> None:
    event = envelope(world.platform_id)
    body = raw(event)

    response = world.post(body, headers=_bad_headers(body)[case])

    assert (response.status_code, response.json()) == (401, {"detail": "invalid signature"})
    assert world.events(event["event_id"]) == []
    assert world.meeting()["due"] is False


def test_the_window_admits_a_timestamp_within_300_s(world: World) -> None:
    for skew in (-299, 299):
        event = envelope(world.platform_id)
        body = raw(event)
        at = datetime.now(UTC) + timedelta(seconds=skew)
        assert world.post(body, headers=signed(body, at=at)).status_code == 202


def test_a_redelivered_event_is_acknowledged_and_ignored(world: World) -> None:
    event = envelope(world.platform_id)
    assert world.post(event).status_code == 202
    with transaction(world.engine, org_id=world.org_id) as tx:  # the copy went back to waiting
        tx.execute(
            text(
                "UPDATE meetings SET transcript_next_at = now() + interval '20 minutes'"
                " WHERE id = :m"
            ),
            {"m": world.meeting_id},
        )

    again = world.post(event)

    assert (again.status_code, again.json()) == (200, {"status": "duplicate"})
    assert len(world.events(event["event_id"])) == 1
    assert world.meeting()["due"] is False  # a redelivery nudges nothing


def test_an_unknown_meeting_is_answered_like_a_known_one(world: World) -> None:
    known = world.post(envelope(world.platform_id))
    event = envelope(_platform_id())

    unknown = world.post(event)

    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
    assert unknown.headers.get("content-length") == known.headers.get("content-length")
    assert len(world.events(event["event_id"])) == 1


@pytest.mark.parametrize(
    "event_type", ["bot.failed", "meeting.started", "meeting.status_change", None]
)
def test_other_events_are_ignored_and_not_recorded(world: World, event_type: str | None) -> None:
    event = envelope(world.platform_id, event_type=event_type)

    response = world.post(event)

    assert (response.status_code, response.json()) == (202, {"status": "ignored"})
    assert world.events(event["event_id"]) == []
    assert world.meeting()["due"] is False


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"{not json", "the body is not JSON"),
        (b"[1, 2]", "the body is not an event"),
        (b'{"event_type": "meeting.completed"}', "the event has no id"),
    ],
)
def test_a_signed_body_that_is_not_an_event_is_400(world: World, body: bytes, detail: str) -> None:
    response = world.post(body)

    assert (response.status_code, response.json()) == (400, {"detail": detail})


def test_a_meeting_without_an_id_is_recorded_and_nudges_nothing(world: World) -> None:
    event = envelope(None)

    response = world.post(event)

    assert response.status_code == 202
    assert world.events(event["event_id"])[0]["platform_meeting_id"] is None
    assert world.meeting()["due"] is False


def test_an_oversized_body_is_refused_before_it_is_read(world: World) -> None:
    body = b"x" * (webhook.MAX_BODY_BYTES + 1)

    response = world.post(body)

    assert response.status_code == 413


def test_a_failure_after_the_insert_leaves_no_event_so_the_retry_is_processed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = envelope(world.platform_id)

    def broken(tx, platform_id: int) -> None:
        raise RuntimeError("injected failure after the insert")

    monkeypatch.setattr(webhook, "nudge", broken)
    failed = world.post(event, client=world.client(raise_server_exceptions=False))
    assert failed.status_code == 500
    assert world.events(event["event_id"]) == []

    monkeypatch.undo()
    retried = world.post(event)

    assert retried.status_code == 202
    assert len(world.events(event["event_id"])) == 1
    assert world.meeting()["due"] is True


def test_a_deleted_meeting_is_not_nudged(world: World) -> None:
    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            text(
                "UPDATE meetings SET deleted_at = now(), deletion_state = 'pending'"
                " WHERE id = :m"
            ),
            {"m": world.meeting_id},
        )

    assert world.post(envelope(world.platform_id)).status_code == 202
    assert world.meeting()["due"] is False


def test_a_stored_transcript_is_left_alone(world: World) -> None:
    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            text(
                "UPDATE meetings SET transcript_state = 'stored', transcript_next_at = NULL"
                " WHERE id = :m"
            ),
            {"m": world.meeting_id},
        )

    assert world.post(envelope(world.platform_id)).status_code == 202
    meeting = world.meeting()
    assert (meeting["transcript_state"], meeting["transcript_next_at"]) == ("stored", None)


def test_the_route_lives_outside_api_and_answers_post_only(world: World) -> None:
    client = world.client()
    assert not webhook.PATH.startswith("/api")
    assert client.get(PATH).status_code == 405
    assert client.post("/api" + PATH, content=b"{}").status_code in (403, 404)


def test_the_signature_matches_the_platforms_own_scheme() -> None:
    # meeting-api `webhooks/delivery.py`: sha256=<hmac_sha256(secret, "<ts>." + payload)>.
    import hashlib
    import hmac

    body = b'{"a": 1}'
    ts = str(int(time.time()))
    expected = hmac.new(b"k", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    assert webhook.sign("k", ts, body) == f"sha256={expected}"
