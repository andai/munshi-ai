"""Deleting a meeting's audio, transcript and minutes (PORT-04): hidden at once, purged by
andai-worker within 15 minutes.

**Hide.** `request_delete(tx, actor, meeting_id, now=, ip=)` serves `DELETE /api/v1/meetings/{id}`.
Only the requester, admins and owners may delete (`access.can_delete`); for anyone else, a lead or a
grantee included, the meeting does not exist (404). A meeting the bot is still in is refused with
409 `meeting_live` (stop it first). Otherwise the meeting gets `deleted_at`, `deleted_by` and
`deletion_state = pending`, and the `meeting.delete` audit event is written in the same transaction.
From that commit on every read path answers 404 or leaves the meeting out (`access.accessible`), and
a second DELETE is 404. The answer is `{id, purge_by}`.

**Purge.** `purge(engine, keyring, gateway, org_id, meeting_id)` runs under the meeting's org for
each deletion `andai_claim_deletions()` hands andai-worker:

1. The platform first, with the org's own key: `DELETE /meetings/{platform meeting id}` through the
   gateway removes the recording objects (chunks and master) under the meeting's storage prefix,
   then its transcript rows. The platform keeps its meeting row as a content-free tombstone. A 404
   means there is nothing left there; a meeting that never reached the platform skips this step.
2. Then, in one transaction: the transcript segments (and with them their search entries), the
   shares, the consent record and the meeting row go; the minutes versions go with the row
   (cascade). When participants removed the bot within the last 12 hours, a content-free marker
   (org, link key, removed at) is kept in `meeting_removals`, so the no-rejoin rule (D-38) still
   holds. The `meeting.purge` audit event is written in that transaction. The audit events are all
   that is left of the meeting.

The platform goes first so that a failed attempt can be retried with the ids andai still holds. A
failed attempt (platform unreachable, 5xx, 409 because the platform's meeting is not terminal yet,
or any other error) is counted and retried after a backoff of 10 s doubling to 2 min; after
`MAX_ATTEMPTS` the deletion is `failed` and stays hidden (`munshi_deletions_pending` keeps counting
it).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio.to_thread
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.crypto import Keyring
from andai_core.db.models import (
    TERMINAL_STATUSES,
    ConsentRecord,
    Meeting,
    MeetingRemoval,
    MeetingShare,
    TranscriptSegment,
)
from andai_core.db.session import transaction
from andai_core.meetings.access import Actor, accessible, can_delete
from andai_core.meetings.service import NO_SUCH_MEETING, REMOVAL_WINDOW, MeetingError
from andai_core.platform import principals
from andai_core.platform.gateway_client import GatewayError, PlatformGateway

log = logging.getLogger(__name__)

PURGE_WITHIN = timedelta(minutes=15)
MAX_ATTEMPTS = 30
BACKOFF_BASE_S = 10
BACKOFF_CAP_S = 120

MEETING_LIVE = MeetingError(
    409, "meeting_live", "the bot is still in this meeting; stop it before deleting the meeting"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


# ─── hide ─────────────────────────────────────────────────────────────────────────────────────────


def request_delete(
    tx: Tx, actor: Actor, meeting_id: uuid.UUID, *, now: datetime, ip: str | None = None
) -> dict:
    """Hide the meeting and audit it in `tx`; raises the 404 or 409 refusal."""
    meeting = tx.scalar(
        select(Meeting)
        .where(Meeting.id == meeting_id, accessible(actor))
        .with_for_update(of=Meeting)
    )
    if meeting is None or not can_delete(tx, actor, meeting):
        raise NO_SUCH_MEETING
    if meeting.status not in TERMINAL_STATUSES:
        raise MEETING_LIVE
    meeting.deleted_at = now
    meeting.deleted_by = actor.user_id
    meeting.deletion_state = "pending"
    meeting.deletion_attempts = 0
    meeting.deletion_next_at = None
    meeting.updated_at = now
    audit.record(tx, actor.org_id, actor.user_id, "meeting.delete", f"meeting:{meeting.id}", ip)
    tx.flush()
    return {"id": str(meeting.id), "purge_by": (now + PURGE_WITHIN).isoformat()}


# ─── purge ────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PurgeResult:
    state: str  # purged | pending | failed


@dataclass(frozen=True)
class _Target:
    platform_meeting_id: int | None
    key: str | None


def backoff(attempts: int) -> timedelta:
    """The wait after the `attempts`-th failed attempt (1-based)."""
    return timedelta(seconds=min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_CAP_S))


def _target(
    engine: Engine, keyring: Keyring, org_id: uuid.UUID, meeting_id: uuid.UUID
) -> _Target | None:
    with transaction(engine, org_id=org_id) as tx:
        meeting = tx.scalar(
            select(Meeting).where(Meeting.id == meeting_id, Meeting.org_id == org_id)
        )
        if meeting is None or meeting.deletion_state != "pending":
            return None
        if meeting.platform_meeting_id is None:
            return _Target(platform_meeting_id=None, key=None)
        key = principals.open_key(tx, keyring, org_id)
        if key is None:
            raise GatewayError("delete meeting", None, "the org has no platform key")
        return _Target(platform_meeting_id=meeting.platform_meeting_id, key=key)


async def _delete_on_platform(gateway: PlatformGateway, target: _Target) -> None:
    if target.platform_meeting_id is None or target.key is None:
        return
    try:
        await gateway.delete_meeting(target.key, target.platform_meeting_id)
    except GatewayError as exc:
        if exc.status != 404:  # 404: nothing of it is left on the platform
            raise


def _remove(engine: Engine, org_id: uuid.UUID, meeting_id: uuid.UUID, now: datetime) -> bool:
    """Delete andai's rows of the meeting in one transaction; False if it is gone already."""
    with transaction(engine, org_id=org_id) as tx:
        meeting = tx.scalar(
            select(Meeting).where(Meeting.id == meeting_id).with_for_update()
        )
        if meeting is None or meeting.deletion_state == "none":
            return False
        consent = tx.scalar(select(ConsentRecord).where(ConsentRecord.meeting_id == meeting_id))
        if (
            consent is not None
            and consent.removed_at is not None
            and consent.removed_at > now - REMOVAL_WINDOW
        ):
            tx.add(
                MeetingRemoval(
                    org_id=org_id,
                    join_url_key=meeting.join_url_key,
                    removed_at=consent.removed_at,
                )
            )
        for table in (TranscriptSegment, MeetingShare, ConsentRecord):
            tx.execute(delete(table).where(table.org_id == org_id, table.meeting_id == meeting_id))
        # The minutes versions go with the row (the app role may not delete them itself).
        tx.execute(delete(Meeting).where(Meeting.id == meeting_id))
        audit.record(tx, org_id, None, "meeting.purge", f"meeting:{meeting_id}")
    return True


def _retry(engine: Engine, org_id: uuid.UUID, meeting_id: uuid.UUID, now: datetime) -> str:
    """Count a failed attempt; returns the new state (`pending`, or `failed` at the cap)."""
    with transaction(engine, org_id=org_id) as tx:
        meeting = tx.scalar(
            select(Meeting).where(Meeting.id == meeting_id).with_for_update()
        )
        if meeting is None or meeting.deletion_state != "pending":
            return "pending" if meeting is None else meeting.deletion_state
        attempts = meeting.deletion_attempts + 1
        meeting.deletion_attempts = attempts
        if attempts >= MAX_ATTEMPTS:
            meeting.deletion_state = "failed"
            meeting.deletion_next_at = None
        else:
            meeting.deletion_next_at = now + backoff(attempts)
        return meeting.deletion_state


async def purge(
    engine: Engine,
    keyring: Keyring,
    gateway: PlatformGateway,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> PurgeResult | None:
    """Remove one hidden meeting from the platform and from andai. None when it is not (or no
    longer) a pending deletion of `org_id`."""
    try:
        target = await anyio.to_thread.run_sync(
            lambda: _target(engine, keyring, org_id, meeting_id)
        )
        if target is None:
            return None
        await _delete_on_platform(gateway, target)
        now = clock()
        removed = await anyio.to_thread.run_sync(lambda: _remove(engine, org_id, meeting_id, now))
    except Exception as exc:  # noqa: BLE001 - every failed attempt counts toward the cap
        now = clock()
        state = await anyio.to_thread.run_sync(lambda: _retry(engine, org_id, meeting_id, now))
        if isinstance(exc, GatewayError):
            log.warning("meeting %s: purge failed (%s): %s", meeting_id, state, exc)
        else:
            log.exception("meeting %s: purge failed (%s)", meeting_id, state)
        return PurgeResult(state=state)
    return PurgeResult(state="purged") if removed else None


__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "MAX_ATTEMPTS",
    "MEETING_LIVE",
    "PURGE_WITHIN",
    "PurgeResult",
    "backoff",
    "purge",
    "request_delete",
]
