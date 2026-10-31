"""A meeting's transcript in andai's shape, read through andai-core with the org's key.

`GET /api/v1/meetings/{id}/transcript` (AUTH_MATRIX: requester, a lead of the meeting's team,
admins and owners; 404 for anyone else) reads the platform transcript of the stored platform
meeting id (`GET /transcripts/by-id/{id}`) and answers

    {"meeting_id": "<andai id>", "segments": [segment, ...]}

ordered by `start`; an empty list before the first segment, or while the bot has no platform
meeting yet. It is also the polling source for a viewer whose live stream (`live.py`) dropped.

Once andai holds its own copy of a finished meeting (`transcript_state = stored`, `ingest.py`),
the route serves that copy from `transcript_segments` with no platform call. Each stored segment
also carries `text_raw` (the platform's text exactly), `text_canonical` (Roman Hinglish) and
`script` (`latn | deva | mixed | other`); `text` stays the raw text and the ids are the same.

**Segment** — the one shape andai returns, live or polled:

    {id, speaker, start, end, text, language, final, absolute_start, absolute_end}

* `id` is andai's own: a hash of the andai meeting id and the platform's segment id, so it is
  stable across polls, the live stream and reconnects, and carries no platform identifiers.
* `speaker` is the platform's name, or `Unknown speaker` when it is blank (Teams diarisation can
  leave it empty).
* `start`, `end` are seconds from the start of the meeting. Producers send either an offset (the
  mock bot, stored rows) or epoch seconds (the live pipeline); epoch values are made relative to
  the meeting's start when it is known, else `null`.
* `absolute_start`, `absolute_end` are ISO-8601 UTC times: the producer's own when it sent them,
  else derived from epoch seconds or from the meeting's start plus the offset.
* `final` is the platform's `completed`: a pending draft may still change or be retracted.

`normalise_segment` and `segments_of_frame` are pure; `live.py` uses them for gateway frames.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import anyio.to_thread
from sqlalchemy import Engine, select

from andai_core.crypto import Keyring
from andai_core.db.models import TranscriptSegment
from andai_core.db.session import transaction
from andai_core.meetings.service import Actor, MeetingError, readable
from andai_core.platform import principals
from andai_core.platform.gateway_client import GatewayError, PlatformGateway

log = logging.getLogger(__name__)

UNKNOWN_SPEAKER = "Unknown speaker"
# At or above this, a `start`/`end` is epoch seconds, not an offset (2001-09-09).
EPOCH_THRESHOLD_S = 1_000_000_000

PLATFORM_UNAVAILABLE = MeetingError(
    502, "platform_unavailable", "could not reach the meeting platform; try again"
)


# ─── the meeting a viewer may watch ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Watched:
    """One meeting as the transcript and live routes need it, read after the access check."""

    id: uuid.UUID
    status: str
    failure_reason: str | None
    end_reason: str | None
    platform: str
    native_meeting_id: str | None
    platform_meeting_id: int | None
    bot_name: str
    started_at: datetime | None
    key: str | None = field(default=None, repr=False)
    transcript_state: str = "none"


def watched(engine: Engine, keyring: Keyring, actor: Actor, meeting_id: uuid.UUID) -> Watched:
    """The meeting `actor` may read (else the 404 refusal), with the org's key once the platform
    has a meeting for it."""
    with transaction(engine, org_id=actor.org_id, user_id=actor.user_id) as tx:
        meeting = readable(tx, actor, meeting_id)
        key = None
        if meeting.platform_meeting_id is not None:
            key = principals.open_key(tx, keyring, actor.org_id)
        snapshot = meeting.settings_snapshot or {}
        return Watched(
            id=meeting.id,
            status=meeting.status,
            failure_reason=meeting.failure_reason,
            end_reason=meeting.end_reason,
            platform=meeting.platform,
            native_meeting_id=meeting.native_meeting_id,
            platform_meeting_id=meeting.platform_meeting_id,
            bot_name=str(snapshot.get("bot_name") or ""),
            started_at=meeting.started_at,
            key=key,
            transcript_state=meeting.transcript_state,
        )


# ─── the normaliser ───────────────────────────────────────────────────────────────────────────────


def segment_id(meeting_id: uuid.UUID, platform_segment_id: str) -> str:
    """andai's id of a platform segment of this meeting."""
    digest = hashlib.blake2b(
        f"{meeting_id}:{platform_segment_id}".encode(), digest_size=12
    ).hexdigest()
    return f"seg_{digest}"


def parse_time(value: object) -> datetime | None:
    """An ISO-8601 time (UTC when it has no offset), else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _point(
    raw_value: object, absolute: object, base: datetime | None
) -> tuple[float | None, str | None]:
    """(seconds from the meeting's start, ISO time) of one end of a segment."""
    when = parse_time(absolute)
    number = _number(raw_value)
    if number is None and isinstance(raw_value, str):
        when = when or parse_time(raw_value)  # `start_time` sent as an ISO time
    offset: float | None = None
    if number is not None and number >= EPOCH_THRESHOLD_S:
        when = when or datetime.fromtimestamp(number, UTC)
    elif number is not None:
        offset = number
        if when is None and base is not None:
            when = base + timedelta(seconds=number)
    if offset is None and when is not None and base is not None:
        offset = (when - base).total_seconds()
    return (
        None if offset is None else round(offset, 3),
        None if when is None else _iso(when),
    )


def normalise_segment(
    raw: dict, *, meeting_id: uuid.UUID, base: datetime | None
) -> dict | None:
    """One platform segment (a stored row, a live bundle entry or a `transcription_segment`
    frame) as andai's segment; None when it has no text."""
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    start, absolute_start = _point(
        raw.get("start", raw.get("start_time")), raw.get("absolute_start_time"), base
    )
    end, absolute_end = _point(
        raw.get("end", raw.get("end_time")), raw.get("absolute_end_time"), base
    )
    speaker = raw.get("speaker")
    speaker = speaker.strip() if isinstance(speaker, str) else ""
    platform_id = raw.get("segment_id")
    if not isinstance(platform_id, str) or not platform_id:
        # A row stored without an id: its timing and speaker identify it.
        platform_id = f"{raw.get('start', raw.get('start_time'))}|{raw.get('speaker')}"
    language = raw.get("language")
    return {
        "id": segment_id(meeting_id, platform_id),
        "speaker": speaker or UNKNOWN_SPEAKER,
        "start": start,
        "end": end,
        "text": text,
        "language": language if isinstance(language, str) and language else None,
        "final": raw.get("completed") is True,
        "absolute_start": absolute_start,
        "absolute_end": absolute_end,
    }


def segments_of_frame(frame: dict) -> list[dict]:
    """The raw segments a live transcript frame carries: the bundle's `confirmed` and `pending`,
    the single-segment envelope's `segment`, or a `transcription_segment` frame itself."""
    kind = frame.get("type")
    if kind == "transcription_segment":
        return [frame]
    if kind != "transcript":
        return []
    if isinstance(frame.get("segment"), dict):
        return [frame["segment"]]
    out: list[dict] = []
    for name in ("confirmed", "pending"):
        items = frame.get(name)
        if isinstance(items, list):
            out += [item for item in items if isinstance(item, dict)]
    return out


def order(segments: list[dict]) -> list[dict]:
    """Segments by start (unknown starts last, by their absolute start)."""
    return sorted(
        segments,
        key=lambda s: (s["start"] is None, s["start"] or 0.0, s["absolute_start"] or ""),
    )


# ─── andai's stored copy ───────────────────────────────────────────────────────────────────────────


def stored_segment(row: TranscriptSegment) -> dict:
    """A stored row as andai's segment, with its raw, canonical and script fields."""
    return {
        "id": row.id,
        "speaker": row.speaker,
        "start": row.start_s,
        "end": row.end_s,
        "text": row.text_raw,
        "language": row.language,
        "final": True,
        "absolute_start": None if row.absolute_start is None else _iso(row.absolute_start),
        "absolute_end": None if row.absolute_end is None else _iso(row.absolute_end),
        "text_raw": row.text_raw,
        "text_canonical": row.text_canonical,
        "script": row.script,
    }


def stored_segments(engine: Engine, actor: Actor, meeting_id: uuid.UUID) -> list[dict]:
    """The stored copy of a meeting `actor` was already allowed to read, in order."""
    with transaction(engine, org_id=actor.org_id, user_id=actor.user_id) as tx:
        rows = tx.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting_id)
            .order_by(TranscriptSegment.seq)
        )
        return [stored_segment(row) for row in rows]


# ─── the route ────────────────────────────────────────────────────────────────────────────────────


async def read_transcript(
    engine: Engine,
    keyring: Keyring,
    gateway: PlatformGateway,
    actor: Actor,
    meeting_id: uuid.UUID,
) -> dict:
    meeting = await anyio.to_thread.run_sync(
        lambda: watched(engine, keyring, actor, meeting_id)
    )
    answer: dict = {"meeting_id": str(meeting.id), "segments": []}
    if meeting.transcript_state == "stored":
        answer["segments"] = await anyio.to_thread.run_sync(
            lambda: stored_segments(engine, actor, meeting.id)
        )
        return answer
    if meeting.platform_meeting_id is None or meeting.key is None:
        return answer
    try:
        document = await gateway.get_transcript(meeting.key, meeting.platform_meeting_id)
    except GatewayError as exc:
        if exc.status == 404:  # no transcript stored for it (yet)
            return answer
        log.warning("meeting %s: transcript read failed: %s", meeting.id, exc)
        raise PLATFORM_UNAVAILABLE from None
    base = parse_time(document.get("start_time")) or meeting.started_at
    raw = document.get("segments")
    segments = [
        s
        for s in (
            normalise_segment(item, meeting_id=meeting.id, base=base)
            for item in (raw if isinstance(raw, list) else [])
        )
        if s is not None
    ]
    answer["segments"] = order(segments)
    return answer


__all__ = [
    "UNKNOWN_SPEAKER",
    "Watched",
    "normalise_segment",
    "order",
    "parse_time",
    "read_transcript",
    "segment_id",
    "segments_of_frame",
    "stored_segment",
    "stored_segments",
    "watched",
]
