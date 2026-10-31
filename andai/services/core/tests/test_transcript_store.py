"""andai's own copy of a finished meeting's transcript and attendees (`meetings/ingest.py`), and the
transcript route serving it. Runs against the in-memory gateway and the disposable Postgres."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from andai_core.meetings.ingest import BACKOFF_BASE_S, MAX_ATTEMPTS, IngestResult, ingest
from andai_core.meetings.transcript import UNKNOWN_SPEAKER, segment_id
from andai_core.platform.gateway_client import GatewayError
from andai_core.text import fold
from conftest import Harness, RoleOrg, give_principal, make_harness, role_org, sign_in, zoom_link

STORED_FIELDS = {
    "id", "speaker", "start", "end", "text", "language", "final", "absolute_start", "absolute_end",
    "text_raw", "text_canonical", "script",
}
LIVE_FIELDS = STORED_FIELDS - {"text_raw", "text_canonical", "script"}

LINES = [
    {"segment_id": "c:1", "speaker": "Asha Rao", "text": "कल की मीटिंग में deadline तय हुई",
     "start": 0.0, "end": 2.5, "completed": True, "language": "hi"},
    {"segment_id": "c:2", "speaker": "Vikram", "text": "क्या हम Friday तक ship कर सकते हैं",
     "start": 3.0, "end": 5.0, "completed": True, "language": "hi"},
    {"segment_id": "c:3", "speaker": "", "text": "Let's do it.", "start": 5.5, "end": 6.0,
     "completed": True, "language": "en"},
    {"segment_id": "c:4", "speaker": "Asha Rao", "text": "ठीक है।", "start": 6.5, "end": 7.0,
     "completed": True, "language": "hi"},
    {"segment_id": "c:5", "speaker": "Vikram", "text": "  ", "start": 7.5, "end": 8.0},
]


@dataclass
class World:
    harness: Harness
    engine: Engine
    org: RoleOrg
    meeting_id: uuid.UUID
    platform_id: int

    @property
    def org_id(self) -> uuid.UUID:
        return self.org.owner.org_id

    def client(self, role: str = "lead"):
        return sign_in(self.harness, self.org.by_role(role))

    def ingest(self) -> IngestResult | None:
        s = self.harness.services
        return asyncio.run(
            ingest(self.engine, s.keyring, s.gateway, self.org_id, self.meeting_id, clock=s.clock)
        )

    def finish(self, status: str = "done", *, started: bool = True) -> None:
        self.update(
            status=status,
            failure_reason="internal_error" if status == "failed" else None,
            started_at=self.harness.clock() if started else None,
        )
        self.harness.gateway.set(self.platform_id, status="completed")

    def update(self, **values: object) -> None:
        assignments = ", ".join(f"{column} = :{column}" for column in values)
        with transaction(self.engine, org_id=self.org_id) as tx:
            tx.execute(
                text(f"UPDATE meetings SET {assignments} WHERE id = :m"),
                {"m": self.meeting_id, **values},
            )

    def meeting(self):
        with transaction(self.engine, org_id=self.org_id) as tx:
            return tx.execute(
                text(
                    "SELECT transcript_state, transcript_attempts, transcript_next_at,"
                    " transcript_at, attendees FROM meetings WHERE id = :m"
                ),
                {"m": self.meeting_id},
            ).one()

    def rows(self) -> list:
        with transaction(self.engine, org_id=self.org_id) as tx:
            return tx.execute(
                text(
                    "SELECT id, seq, speaker, start_s, end_s, text_raw, text_canonical, script,"
                    " language, search_text FROM transcript_segments WHERE meeting_id = :m"
                    " ORDER BY seq"
                ),
                {"m": self.meeting_id},
            ).all()


@pytest.fixture
def world(app_engine: Engine) -> World:
    harness = make_harness(app_engine)
    org = role_org(app_engine)
    give_principal(harness, app_engine, org.owner.org_id)
    sent = sign_in(harness, org.lead).post(
        "/api/v1/meetings/join", json={"join_url": zoom_link(), "team_id": str(org.team)}
    )
    assert sent.status_code == 201, sent.text
    platform_id = max(harness.gateway.meetings)
    harness.gateway.set(platform_id, start_time="2026-10-31T06:00:00Z")
    harness.gateway.transcripts[platform_id] = [dict(line) for line in LINES]
    return World(harness, app_engine, org, uuid.UUID(sent.json()["id"]), platform_id)


def transcript_path(meeting_id: uuid.UUID) -> str:
    return f"/api/v1/meetings/{meeting_id}/transcript"


# ─── ingest ───────────────────────────────────────────────────────────────────────────────────────


def test_ingest_stores_raw_canonical_script_and_folded_text(world: World) -> None:
    world.finish()

    result = world.ingest()

    assert result == IngestResult(state="stored", segments=4, attendees=2)
    rows = world.rows()
    assert [r.seq for r in rows] == [0, 1, 2, 3]
    assert [r.id for r in rows] == [
        segment_id(world.meeting_id, f"c:{n}") for n in (1, 2, 3, 4)
    ]
    first, second, third, fourth = rows
    assert first.text_raw == "कल की मीटिंग में deadline तय हुई"  # exactly as the platform sent it
    assert first.text_canonical == "kal ki meeting mein deadline tay hui"
    assert (first.script, second.script, third.script, fourth.script) == (
        "mixed", "mixed", "latn", "deva"
    )
    assert second.text_canonical == "kya ham Friday tak ship kar sakte hain"
    assert third.text_raw == third.text_canonical == "Let's do it."
    assert fourth.text_canonical == "theek hai."
    assert all(r.search_text == fold(r.text_canonical) for r in rows)
    assert (first.speaker, third.speaker) == ("Asha Rao", UNKNOWN_SPEAKER)
    assert (first.start_s, first.end_s, first.language) == (0.0, 2.5, "hi")
    state, attempts, next_at, stored_at, _ = world.meeting()
    assert (state, attempts, next_at) == ("stored", 0, None)
    assert stored_at is not None


def test_ingest_is_idempotent_and_a_rerun_replaces_the_rows(world: World) -> None:
    world.finish()
    world.ingest()
    first = world.rows()

    assert world.ingest() == IngestResult(state="stored", segments=4, attendees=2)
    assert world.rows() == first

    world.harness.gateway.transcripts[world.platform_id] = [dict(LINES[1])]
    world.ingest()
    [only] = world.rows()
    assert (only.id, only.seq) == (segment_id(world.meeting_id, "c:2"), 0)


def test_a_platform_error_keeps_the_stored_copy(world: World) -> None:
    world.finish()
    world.ingest()
    before = world.rows()

    world.update(transcript_state="pending")
    world.harness.gateway.fail["get participants"] = 500
    with pytest.raises(GatewayError):
        world.ingest()

    assert world.rows() == before


def test_an_empty_transcript_stays_pending_and_is_retried(world: World) -> None:
    world.finish()
    world.harness.gateway.transcripts[world.platform_id] = []

    result = world.ingest()

    assert result == IngestResult(state="pending", segments=0, attendees=0)
    state, attempts, next_at, stored_at, _ = world.meeting()
    assert (state, attempts, stored_at) == ("pending", 1, None)
    assert next_at == world.harness.clock() + timedelta(seconds=BACKOFF_BASE_S)
    assert world.rows() == []

    world.harness.gateway.transcripts[world.platform_id] = [dict(line) for line in LINES]
    assert world.ingest().state == "stored"
    assert len(world.rows()) == 4


def test_an_empty_transcript_at_the_cap_is_stored_empty(world: World) -> None:
    world.finish()
    world.harness.gateway.transcripts[world.platform_id] = []
    world.update(transcript_state="pending", transcript_attempts=MAX_ATTEMPTS - 1)

    assert world.ingest() == IngestResult(state="stored", segments=0, attendees=0)
    assert world.meeting().transcript_state == "stored"


def test_a_meeting_the_bot_never_recorded_is_stored_empty_at_once(world: World) -> None:
    world.finish("failed", started=False)
    world.harness.gateway.transcripts[world.platform_id] = []

    assert world.ingest() == IngestResult(state="stored", segments=0, attendees=0)
    state, attempts, *_ = world.meeting()
    assert (state, attempts) == ("stored", 0)


def test_platform_errors_back_off_and_fail_after_the_cap(world: World) -> None:
    world.finish()
    world.harness.gateway.fail["get transcript"] = 503

    with pytest.raises(GatewayError):
        world.ingest()
    state, attempts, next_at, _, _ = world.meeting()
    assert (state, attempts) == ("pending", 1)
    assert next_at == world.harness.clock() + timedelta(seconds=BACKOFF_BASE_S)

    with pytest.raises(GatewayError):
        world.ingest()
    _, attempts, next_at, _, _ = world.meeting()
    assert attempts == 2
    assert next_at == world.harness.clock() + timedelta(seconds=2 * BACKOFF_BASE_S)

    world.update(transcript_attempts=MAX_ATTEMPTS - 1)
    with pytest.raises(GatewayError):
        world.ingest()
    state, attempts, next_at, stored_at, _ = world.meeting()
    assert (state, attempts, next_at, stored_at) == ("failed", MAX_ATTEMPTS, None, None)
    assert world.rows() == []


def test_live_and_unfinished_meetings_are_not_ingested(world: World) -> None:
    world.update(status="recording", started_at=world.harness.clock())

    assert world.ingest() is None
    assert world.meeting().transcript_state == "none"
    assert world.rows() == []


def test_attendees_are_invitees_then_speakers_without_duplicates(world: World) -> None:
    world.harness.gateway.invitees[world.platform_id] = [
        {"name": "Asha Rao", "email": "asha@example.test"},
        {"name": None, "email": "ravi@example.test"},
        {"name": "  asha   RAO ", "email": None},
        {"name": None, "email": None},
    ]
    world.harness.gateway.transcripts[world.platform_id] += [
        {"segment_id": "c:6", "speaker": "ASHA RAO", "text": "haan", "start": 9.0, "end": 9.5},
        {"segment_id": "c:7", "speaker": "Meera", "text": "ok", "start": 10.0, "end": 10.5},
    ]
    world.finish()

    world.ingest()

    assert world.meeting().attendees == [
        {"name": "Asha Rao", "source": "invite"},
        {"name": "ravi@example.test", "source": "invite"},
        {"name": "Vikram", "source": "speaker"},
        {"name": "Meera", "source": "speaker"},
    ]


def test_attendees_come_from_the_transcript_when_participants_are_unknown(world: World) -> None:
    world.harness.gateway.fail["get participants"] = 404
    world.finish()

    assert world.ingest().state == "stored"
    assert world.meeting().attendees == [
        {"name": "Asha Rao", "source": "speaker"},
        {"name": "Vikram", "source": "speaker"},
    ]


def test_another_org_reads_none_of_the_stored_rows(world: World, app_engine: Engine) -> None:
    world.finish()
    world.ingest()
    other = role_org(app_engine)

    with transaction(app_engine, org_id=other.owner.org_id) as tx:
        seen = tx.execute(
            text("SELECT count(*) FROM transcript_segments WHERE meeting_id = :m"),
            {"m": world.meeting_id},
        ).scalar_one()
    with transaction(app_engine) as tx:
        anonymous = tx.execute(text("SELECT count(*) FROM transcript_segments")).scalar_one()
    assert (seen, anonymous) == (0, 0)
    assert len(world.rows()) == 4


# ─── the route and the view ───────────────────────────────────────────────────────────────────────


def test_the_route_serves_the_stored_copy_with_the_same_ids(world: World) -> None:
    lead = world.client()
    world.update(status="recording", started_at=world.harness.clock())
    live = lead.get(transcript_path(world.meeting_id)).json()["segments"]
    assert all(set(s) == LIVE_FIELDS for s in live)

    world.finish()
    world.ingest()
    world.harness.gateway.fail["get transcript"] = 500  # the stored copy needs no platform call

    response = lead.get(transcript_path(world.meeting_id))

    assert response.status_code == 200, response.text
    stored = response.json()["segments"]
    assert [s["id"] for s in stored] == [s["id"] for s in live]
    assert all(set(s) == STORED_FIELDS for s in stored)
    first = stored[0]
    assert first["text"] == first["text_raw"] == "कल की मीटिंग में deadline तय हुई"
    assert first["text_canonical"] == "kal ki meeting mein deadline tay hui"
    assert (first["script"], first["final"], first["start"]) == ("mixed", True, 0.0)
    assert first["absolute_start"] == live[0]["absolute_start"] == "2026-10-31T06:00:00Z"
    assert [s["text"] for s in stored] == [s["text"] for s in live]


def test_a_finished_meeting_not_yet_stored_is_read_from_the_platform(world: World) -> None:
    world.finish()

    segments = world.client().get(transcript_path(world.meeting_id)).json()["segments"]

    assert len(segments) == 4 and all(set(s) == LIVE_FIELDS for s in segments)


@pytest.mark.parametrize(("role", "allowed"), [("lead", True), ("admin", True), ("member", False)])
def test_the_stored_copy_follows_the_same_access(world: World, role: str, allowed: bool) -> None:
    world.finish()
    world.ingest()

    response = world.client(role).get(transcript_path(world.meeting_id))

    assert response.status_code == (200 if allowed else 404)
    assert ("deadline" in response.text) is allowed


def test_the_meeting_view_carries_title_attendees_and_transcript_state(world: World) -> None:
    lead = world.client()
    before = lead.get(f"/api/v1/meetings/{world.meeting_id}").json()
    assert (before["title"], before["attendees"], before["transcript_state"]) == (None, [], "none")

    world.finish()
    world.update(title="Weekly sync")
    world.ingest()

    after = lead.get(f"/api/v1/meetings/{world.meeting_id}").json()
    assert after["title"] == "Weekly sync"
    assert after["transcript_state"] == "stored"
    assert after["attendees"] == [
        {"name": "Asha Rao", "source": "speaker"},
        {"name": "Vikram", "source": "speaker"},
    ]
