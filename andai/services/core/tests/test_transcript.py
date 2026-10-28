"""A meeting's transcript in andai's shape: the segment normaliser over the platform's own goldens,
and `GET /api/v1/meetings/{id}/transcript` through andai-core against the in-memory gateway."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine

from andai_core.meetings.transcript import (
    UNKNOWN_SPEAKER,
    normalise_segment,
    segment_id,
    segments_of_frame,
)
from conftest import Harness, RoleOrg, give_principal, make_harness, role_org, sign_in, zoom_link

CONTRACTS = Path(__file__).resolve().parents[4] / "core" / "gateway" / "contracts"
FIELDS = {
    "id", "speaker", "start", "end", "text", "language", "final", "absolute_start", "absolute_end"
}
MEETING = uuid.UUID("00000000-0000-4000-8000-000000000042")


def golden(contract: str, name: str) -> dict:
    return json.loads((CONTRACTS / contract / "golden" / f"{name}.json").read_text())


# ─── the normaliser ───────────────────────────────────────────────────────────────────────────────


def test_the_ws_bundle_golden_gives_one_final_segment() -> None:
    frame = golden("ws.v1", "Transcript.bundle")
    base = datetime(2026, 3, 27, 10, 0, tzinfo=UTC)

    [raw] = segments_of_frame(frame)
    segment = normalise_segment(raw, meeting_id=MEETING, base=base)

    assert segment == {
        "id": segment_id(MEETING, "ch-0:42:1"),
        "speaker": "Alice",
        "start": 0.0,
        "end": 2.0,
        "text": "Hello from the 0.10.6 bundle",
        "language": "en",
        "final": True,
        "absolute_start": "2026-03-27T10:00:00Z",
        "absolute_end": "2026-03-27T10:00:02Z",
    }
    # Without the meeting's start the offsets are unknown; the times are still the producer's.
    unanchored = normalise_segment(raw, meeting_id=MEETING, base=None)
    assert (unanchored["start"], unanchored["end"]) == (None, None)
    assert unanchored["absolute_start"] == "2026-03-27T10:00:00Z"


def test_the_ws_single_segment_golden() -> None:
    frame = golden("ws.v1", "TranscriptionSegment.live")
    base = datetime(2026, 3, 27, 10, 0, tzinfo=UTC)

    [raw] = segments_of_frame(frame)
    segment = normalise_segment(raw, meeting_id=MEETING, base=base)

    assert segment["id"] == segment_id(MEETING, "ch-0:1:abc")
    assert (segment["speaker"], segment["text"], segment["language"]) == (
        "Alice", "Hello from G5 gate test", "en"
    )
    # `start_time` is an ISO time, `end_time` an offset: both end up as offset and time.
    assert (segment["start"], segment["end"]) == (0.0, 12.5)
    assert segment["absolute_start"] == "2026-03-27T10:00:00Z"
    assert segment["absolute_end"] == "2026-03-27T10:00:12.500000Z"
    assert segment["final"] is True


def test_the_rest_segment_golden_keeps_offsets_and_producer_times() -> None:
    raw = golden("api.v1", "TranscriptionSegment.example")

    segment = normalise_segment(raw, meeting_id=MEETING, base=None)

    assert set(segment) == FIELDS
    assert (segment["start"], segment["end"]) == (1.0, 2.5)
    assert segment["absolute_start"] == "2026-06-20T09:00:01Z"
    assert segment["absolute_end"] == "2026-06-20T09:00:02Z"
    assert (segment["speaker"], segment["final"]) == ("spk-Anna", True)
    # No platform segment id: timing and speaker identify it, the same way every time.
    assert segment["id"] == normalise_segment(dict(raw), meeting_id=MEETING, base=None)["id"]


def test_the_single_segment_envelope_and_pending_drafts() -> None:
    draft = {
        "segment_id": "conn-1:mock:0", "speaker": "Speaker A", "text": "mock utterance 0",
        "start": 0.0, "end": 0.8, "completed": False,
    }
    envelope = {"type": "transcript", "meeting": {"id": 7}, "segment": draft}
    bundle = {"type": "transcript", "speaker": "Speaker A", "confirmed": [], "pending": [draft]}

    for frame in (envelope, bundle):
        [raw] = segments_of_frame(frame)
        segment = normalise_segment(raw, meeting_id=MEETING, base=None)
        assert segment["final"] is False
        assert (segment["start"], segment["end"], segment["absolute_start"]) == (0.0, 0.8, None)


def test_epoch_seconds_become_offsets_from_the_meeting_start() -> None:
    base = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
    raw = {
        "segment_id": "s1", "speaker": "Asha", "text": "namaste",
        "start": base.timestamp() + 65.25, "end": base.timestamp() + 67.0, "completed": True,
        "language": "hi",
    }

    segment = normalise_segment(raw, meeting_id=MEETING, base=base)
    unanchored = normalise_segment(raw, meeting_id=MEETING, base=None)

    assert (segment["start"], segment["end"]) == (65.25, 67.0)
    assert segment["absolute_start"] == "2026-10-28T06:01:05.250000Z"
    assert (unanchored["start"], unanchored["absolute_start"]) == (None, segment["absolute_start"])


@pytest.mark.parametrize("speaker", ["", "   ", None])
def test_a_blank_speaker_is_unknown_speaker(speaker) -> None:
    raw = {"segment_id": "t1", "speaker": speaker, "text": "hello", "start": 1, "end": 2}

    assert normalise_segment(raw, meeting_id=MEETING, base=None)["speaker"] == UNKNOWN_SPEAKER


def test_segments_without_text_and_other_frames_are_dropped() -> None:
    for raw in ({"segment_id": "x", "text": "  "}, {"segment_id": "x"}):
        assert normalise_segment(raw, meeting_id=MEETING, base=None) is None
    for name in ("MeetingStatus.active", "ChatMessage.summary", "Subscribed.ack"):
        assert segments_of_frame(golden("ws.v1", name)) == []


def test_ids_are_andai_s_own() -> None:
    first = segment_id(MEETING, "ch-0:42:1")

    assert first == segment_id(MEETING, "ch-0:42:1")
    assert first != segment_id(uuid.uuid4(), "ch-0:42:1")
    assert re.fullmatch(r"seg_[0-9a-f]{24}", first)


# ─── the route ────────────────────────────────────────────────────────────────────────────────────


@dataclass
class World:
    harness: Harness
    engine: Engine
    org: RoleOrg
    meeting_id: str
    platform_id: int

    def client(self, role: str):
        return sign_in(self.harness, self.org.by_role(role))


def transcript_path(meeting_id: str) -> str:
    return f"/api/v1/meetings/{meeting_id}/transcript"


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
    return World(harness, app_engine, org, sent.json()["id"], platform_id)


def test_the_transcript_is_in_andai_s_shape_and_ordered(world: World) -> None:
    world.harness.gateway.set(world.platform_id, status="active", start_time="2026-10-28T06:00:00")
    world.harness.gateway.transcripts[world.platform_id] = [
        {"segment_id": "conn-9:mock:1", "speaker": "Speaker B", "text": "second", "start": 1.0,
         "end": 1.8, "completed": True, "language": "en"},
        {"segment_id": "conn-9:mock:0", "speaker": "", "text": "first", "start": 0.0,
         "end": 0.8, "completed": True, "language": "en"},
        {"segment_id": "conn-9:mock:2", "speaker": "Speaker A", "text": " ", "start": 2.0,
         "end": 2.5, "completed": False},
    ]

    response = world.client("lead").get(transcript_path(world.meeting_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meeting_id"] == world.meeting_id
    first, second = body["segments"]
    assert all(set(s) == FIELDS for s in body["segments"])
    assert (first["text"], first["speaker"], first["start"]) == ("first", UNKNOWN_SPEAKER, 0.0)
    assert first["absolute_start"] == "2026-10-28T06:00:00Z"
    assert (second["text"], second["speaker"], second["end"]) == ("second", "Speaker B", 1.8)
    assert first["id"] == segment_id(uuid.UUID(world.meeting_id), "conn-9:mock:0")
    # Nothing of the platform's identifiers reaches the viewer.
    assert "conn-9" not in response.text
    assert str(world.platform_id) not in response.text


def test_empty_before_the_first_segment(world: World) -> None:
    response = world.client("lead").get(transcript_path(world.meeting_id))

    assert response.status_code == 200
    assert response.json() == {"meeting_id": world.meeting_id, "segments": []}


def test_a_platform_404_is_an_empty_transcript_and_other_errors_are_502(world: World) -> None:
    lead = world.client("lead")

    world.harness.gateway.fail["get transcript"] = 404
    assert lead.get(transcript_path(world.meeting_id)).json()["segments"] == []
    world.harness.gateway.fail["get transcript"] = 500
    failed = lead.get(transcript_path(world.meeting_id))
    assert (failed.status_code, failed.json()["code"]) == (502, "platform_unavailable")


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("lead", True), ("owner", True), ("admin", True), ("member", False)],
)
def test_access_follows_the_auth_matrix(world: World, role: str, allowed: bool) -> None:
    world.harness.gateway.transcripts[world.platform_id] = [
        {"segment_id": "x:1", "speaker": "A", "text": "hello", "start": 0.0, "end": 1.0}
    ]

    response = world.client(role).get(transcript_path(world.meeting_id))

    if allowed:
        assert response.status_code == 200
        assert len(response.json()["segments"]) == 1
    else:
        # A team member who neither sent the bot nor leads the team: the meeting does not exist.
        assert (response.status_code, response.json()["code"]) == (404, "no_such_meeting")
        assert "hello" not in response.text


def test_anonymous_and_other_orgs_get_nothing(world: World, app_engine: Engine) -> None:
    other = role_org(app_engine)
    world.harness.gateway.transcripts[world.platform_id] = [
        {"segment_id": "x:1", "speaker": "A", "text": "secret words", "start": 0.0, "end": 1.0}
    ]

    anonymous = world.harness.client().get(transcript_path(world.meeting_id))
    stranger = sign_in(world.harness, other.owner).get(transcript_path(world.meeting_id))

    assert anonymous.status_code == 401
    assert stranger.status_code == 404
    assert "secret words" not in anonymous.text + stranger.text
