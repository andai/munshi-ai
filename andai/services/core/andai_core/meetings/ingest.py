"""andai's own copy of a finished meeting's transcript and attendees.

`ingest(engine, keyring, gateway, org_id, meeting_id)` runs under the meeting's org and does this:

1. It reads the final transcript (`GET /transcripts/by-id/{id}`) and the participants
   (`GET /meetings/{platform}/{native}/participants`) with the org's own key.
2. It turns each segment into andai's shape with `transcript.normalise_segment`. Each row keeps:
   - `text_raw`, exactly the platform's text;
   - `text_canonical` (Roman Hinglish) and `script`, from `andai_core.text`;
   - `search_text`, the folded form search indexes.
3. In one transaction it replaces the meeting's segments and stores the de-duplicated attendees,
   then sets `transcript_state = stored`.

It works only on a terminal meeting that has a platform id; for any other it does nothing and
returns None. Running it again is safe: the rows are replaced, so a re-run of the same transcript
leaves the same rows.

**Retries.**
- An empty transcript from a meeting the bot recorded stays `pending`. Its `transcript_attempts`
  goes up and `transcript_next_at` moves by the backoff (30 s, doubling, at most 30 min), because
  the platform may still be writing it. At the cap (`MAX_ATTEMPTS`) it is stored empty.
- A meeting the bot never recorded has nothing to wait for, so it is stored empty at once.
- A platform error counts as an attempt in the same way and is raised for the caller's log. At the
  cap the state becomes `failed`.

**Attendees** are a list of `{name, source}`. Calendar invitees come first (`source: invite`, the
email when there is no name), then speakers (`source: speaker`): those from the participants answer
and those of the stored segments. Names are compared with case and spacing ignored, and the first
spelling is kept; `Unknown speaker` is never listed. The platform answers participants for its newest
meeting with the native id; an answer for another platform meeting is not used.

andai-worker calls this for each meeting `andai_claim_ingest()` hands it (`andai_worker/ingest.py`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio.to_thread
from sqlalchemy import Engine, delete, insert, select

from andai_core.crypto import Keyring
from andai_core.db.models import TERMINAL_STATUSES, Meeting, TranscriptSegment
from andai_core.db.session import transaction
from andai_core.meetings.transcript import (
    UNKNOWN_SPEAKER,
    normalise_segment,
    order,
    parse_time,
)
from andai_core.platform import principals
from andai_core.platform.gateway_client import GatewayError, PlatformGateway
from andai_core.text import canonical, fold, script_of

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 30 * 60


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IngestResult:
    state: str  # stored | pending | failed
    segments: int
    attendees: int


@dataclass(frozen=True)
class _Target:
    platform: str
    native_meeting_id: str | None
    platform_meeting_id: int
    started_at: datetime | None
    attempts: int
    key: str


def backoff(attempts: int) -> timedelta:
    """The wait after the `attempts`-th failed attempt (1-based)."""
    return timedelta(seconds=min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_CAP_S))


def _target(engine: Engine, keyring: Keyring, org_id: uuid.UUID, meeting_id: uuid.UUID):
    with transaction(engine, org_id=org_id) as tx:
        meeting = tx.scalar(
            select(Meeting).where(Meeting.id == meeting_id, Meeting.org_id == org_id)
        )
        if (
            meeting is None
            or meeting.status not in TERMINAL_STATUSES
            or meeting.platform_meeting_id is None
        ):
            return None
        key = principals.open_key(tx, keyring, org_id)
        if key is None:
            raise GatewayError("get transcript", None, "the org has no platform key")
        return _Target(
            platform=meeting.platform,
            native_meeting_id=meeting.native_meeting_id,
            platform_meeting_id=meeting.platform_meeting_id,
            started_at=meeting.started_at,
            attempts=meeting.transcript_attempts,
            key=key,
        )


def _clean(name: object) -> str:
    return " ".join(name.split()) if isinstance(name, str) else ""


def attendees_of(participants: dict | None, segments: list[dict], platform_id: int) -> list[dict]:
    """Invitees, then speakers, each name once (case and spacing ignored)."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(name: object, source: str) -> None:
        clean = _clean(name)
        if not clean or clean == UNKNOWN_SPEAKER or clean.casefold() in seen:
            return
        seen.add(clean.casefold())
        out.append({"name": clean, "source": source})

    rows = []
    if participants is not None and participants.get("meeting_id") == platform_id:
        found = participants.get("participants")
        rows = [p for p in found if isinstance(p, dict)] if isinstance(found, list) else []
    for p in rows:
        if p.get("source") == "invite":
            add(_clean(p.get("name")) or p.get("email"), "invite")
    for p in rows:
        if p.get("source") == "speaker":
            add(p.get("name"), "speaker")
    for segment in segments:
        add(segment["speaker"], "speaker")
    return out


async def _participants(gateway: PlatformGateway, target: _Target) -> dict | None:
    if target.native_meeting_id is None:
        return None
    try:
        return await gateway.get_participants(
            target.key, target.platform, target.native_meeting_id
        )
    except GatewayError as exc:
        if exc.status == 404:  # the platform knows no participants for it
            return None
        raise


def _segments(document: dict, meeting_id: uuid.UUID, started_at: datetime | None) -> list[dict]:
    base = parse_time(document.get("start_time")) or started_at
    raw = document.get("segments")
    by_id: dict[str, dict] = {}
    for item in raw if isinstance(raw, list) else []:
        segment = normalise_segment(item, meeting_id=meeting_id, base=base)
        if segment is not None:
            by_id[segment["id"]] = segment  # a repeated platform segment: the last one wins
    return order(list(by_id.values()))


def _row(org_id: uuid.UUID, meeting_id: uuid.UUID, seq: int, segment: dict) -> dict:
    raw = segment["text"]
    roman = canonical(raw)
    return {
        "meeting_id": meeting_id,
        "id": segment["id"],
        "org_id": org_id,
        "seq": seq,
        "start_s": segment["start"],
        "end_s": segment["end"],
        "absolute_start": parse_time(segment["absolute_start"]),
        "absolute_end": parse_time(segment["absolute_end"]),
        "speaker": segment["speaker"],
        "text_raw": raw,
        "text_canonical": roman,
        "script": script_of(raw),
        "language": segment["language"],
        "search_text": fold(roman),
    }


def _store(
    engine: Engine,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    segments: list[dict],
    attendees: list[dict],
    now: datetime,
) -> None:
    with transaction(engine, org_id=org_id) as tx:
        meeting = tx.scalar(
            select(Meeting).where(Meeting.id == meeting_id).with_for_update()
        )
        if meeting is None:  # removed while the platform was being read
            return
        tx.execute(delete(TranscriptSegment).where(TranscriptSegment.meeting_id == meeting_id))
        if segments:
            tx.execute(
                insert(TranscriptSegment),
                [_row(org_id, meeting_id, seq, s) for seq, s in enumerate(segments)],
            )
        meeting.attendees = attendees
        meeting.transcript_state = "stored"
        meeting.transcript_next_at = None
        meeting.transcript_at = now


def _retry(
    engine: Engine, org_id: uuid.UUID, meeting_id: uuid.UUID, now: datetime, *, give_up: str
) -> str:
    """Count a failed attempt; returns the new state (`pending`, or `give_up` at the cap)."""
    with transaction(engine, org_id=org_id) as tx:
        meeting = tx.scalar(
            select(Meeting).where(Meeting.id == meeting_id).with_for_update()
        )
        if meeting is None:
            return "pending"
        attempts = meeting.transcript_attempts + 1
        meeting.transcript_attempts = attempts
        if attempts >= MAX_ATTEMPTS:
            meeting.transcript_state = give_up
            meeting.transcript_next_at = None
        else:
            meeting.transcript_state = "pending"
            meeting.transcript_next_at = now + backoff(attempts)
        return meeting.transcript_state


async def ingest(
    engine: Engine,
    keyring: Keyring,
    gateway: PlatformGateway,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> IngestResult | None:
    """Copy one finished meeting's transcript and attendees into andai. None when the meeting is
    not (or no longer) a terminal meeting of `org_id` with a platform id."""
    target = await anyio.to_thread.run_sync(
        lambda: _target(engine, keyring, org_id, meeting_id)
    )
    if target is None:
        return None
    try:
        document = await gateway.get_transcript(target.key, target.platform_meeting_id)
        participants = await _participants(gateway, target)
    except GatewayError as exc:
        empty_on_404 = exc.status == 404 and exc.what == "get transcript"
        if not empty_on_404:
            now = clock()
            state = await anyio.to_thread.run_sync(
                lambda: _retry(engine, org_id, meeting_id, now, give_up="failed")
            )
            log.warning("meeting %s: transcript ingest failed (%s): %s", meeting_id, state, exc)
            raise
        document, participants = {}, None  # the platform has no transcript for it (yet)

    segments = _segments(document, meeting_id, target.started_at)
    now = clock()
    if not segments and target.started_at is not None:
        state = await anyio.to_thread.run_sync(
            lambda: _retry(engine, org_id, meeting_id, now, give_up="stored")
        )
        if state != "stored":
            return IngestResult(state=state, segments=0, attendees=0)
    attendees = attendees_of(participants, segments, target.platform_meeting_id)
    await anyio.to_thread.run_sync(
        lambda: _store(engine, org_id, meeting_id, segments, attendees, now)
    )
    return IngestResult(state="stored", segments=len(segments), attendees=len(attendees))


__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "MAX_ATTEMPTS",
    "IngestResult",
    "attendees_of",
    "backoff",
    "ingest",
]
