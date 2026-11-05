"""Scheduled meetings: the ones andai-intake creates from invites, and the skip toggle (INV-05).

A meeting created from an invite is `scheduled` (D-42: the Upcoming section) with source
`invite_mail`, its start in UTC (`scheduled_start`), the invite event it came from and the
occurrence's original start (`occurrence_key`, unchanged when the occurrence moves). andai-intake
writes these rows through the helpers below, in the transaction of the org the invite's sender
belongs to:

* `insert_scheduled` adds one occurrence;
* `update_scheduled` moves it, changes its link or title, or re-keys it to another occurrence; it
  also brings back an occurrence a later invite cancelled (`INVITE_CANCELLED`) that a newer one
  restores, as long as no bot was ever sent for it;
* `cancel_scheduled` cancels it (`cancelled`, end reason `invite_cancelled`).

Each touches only a meeting that is still `scheduled` (or, for a restore, cancelled by an invite and
never dispatched): once a bot is on its way, mail changes nothing.

**Skip.** `skip` / `unskip` (`POST` / `DELETE /api/v1/meetings/{id}/skip`) set and clear
`skipped_at` on a scheduled meeting, audited `meeting.skip` / `meeting.unskip`. Only callers who may
share the meeting (requester, a lead of its team, admins and owners: `access.can_share`) may; for
anyone else, grantees included, it does not exist (404). A meeting that is no longer scheduled is
409 `not_scheduled`. A skipped meeting is never dispatched, and later invite updates to that
occurrence keep the skip. Setting a skip twice, or clearing one that is not set, changes nothing and
audits nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.db.models import Meeting
from andai_core.meetings.access import Actor, accessible, can_share
from andai_core.meetings.service import (
    NO_SUCH_MEETING,
    MeetingError,
    join_url_key,
    view,
)
from andai_core.meetings.title import set_title

INVITE_CANCELLED = "invite_cancelled"
SOURCE = "invite_mail"
NOT_SCHEDULED = MeetingError(
    409, "not_scheduled", "only a meeting that is still scheduled can be skipped"
)
CHANGEABLE = (
    "scheduled_start",
    "occurrence_key",
    "invite_event_id",
    "join_url",
    "platform",
    "title",
)


# ─── skip ─────────────────────────────────────────────────────────────────────────────────────────


def _skippable(tx: Tx, actor: Actor, meeting_id: uuid.UUID) -> Meeting:
    meeting = tx.scalar(
        select(Meeting)
        .where(Meeting.id == meeting_id, accessible(actor))
        .with_for_update(of=Meeting)
    )
    if meeting is None or not can_share(tx, actor, meeting):
        raise NO_SUCH_MEETING
    if meeting.status != "scheduled":
        raise NOT_SCHEDULED
    return meeting


def skip(
    tx: Tx, actor: Actor, meeting_id: uuid.UUID, *, now: datetime, ip: str | None = None
) -> dict:
    """Skip a scheduled meeting; returns the meeting as `actor` sees it."""
    meeting = _skippable(tx, actor, meeting_id)
    if meeting.skipped_at is None:
        meeting.skipped_at = now
        meeting.skipped_by = actor.user_id
        meeting.updated_at = now
        audit.record(tx, actor.org_id, actor.user_id, "meeting.skip", f"meeting:{meeting.id}", ip)
        tx.flush()
    return view(tx, actor, meeting)


def unskip(
    tx: Tx, actor: Actor, meeting_id: uuid.UUID, *, now: datetime, ip: str | None = None
) -> dict:
    """Clear the skip of a scheduled meeting; returns the meeting as `actor` sees it."""
    meeting = _skippable(tx, actor, meeting_id)
    if meeting.skipped_at is not None:
        meeting.skipped_at = None
        meeting.skipped_by = None
        meeting.updated_at = now
        audit.record(
            tx, actor.org_id, actor.user_id, "meeting.unskip", f"meeting:{meeting.id}", ip
        )
        tx.flush()
    return view(tx, actor, meeting)


# ─── writes from invites (andai-intake) ───────────────────────────────────────────────────────────


def insert_scheduled(
    tx: Tx,
    *,
    meeting_id: uuid.UUID,
    org_id: uuid.UUID,
    invite_event_id: uuid.UUID,
    occurrence_key: datetime,
    scheduled_start: datetime,
    requested_by: uuid.UUID,
    team_id: uuid.UUID | None,
    join_url: str,
    platform: str,
    title: str | None,
    language: str,
    now: datetime,
) -> Meeting:
    """Add one scheduled occurrence in `tx` (the org's transaction)."""
    meeting = Meeting(
        id=meeting_id,
        org_id=org_id,
        team_id=team_id,
        requested_by=requested_by,
        source=SOURCE,
        platform=platform,
        join_url=join_url,
        join_url_key=join_url_key(join_url),
        status="scheduled",
        settings_snapshot={"language": language},
        scheduled_start=scheduled_start,
        invite_event_id=invite_event_id,
        occurrence_key=occurrence_key,
        created_at=now,
        updated_at=now,
    )
    set_title(tx, meeting, title)
    tx.flush()
    return meeting


def _restorable(meeting: Meeting) -> bool:
    return (
        meeting.status == "cancelled"
        and meeting.end_reason == INVITE_CANCELLED
        and meeting.platform_meeting_id is None
        and meeting.deleted_at is None
    )


def update_scheduled(
    tx: Tx, meeting_id: uuid.UUID, *, now: datetime, restore: bool = False, **changes
) -> bool:
    """Apply `changes` (any of `CHANGEABLE`) to a scheduled meeting; with `restore`, also bring a
    meeting an invite cancelled back to `scheduled`. Returns whether the meeting was changed."""
    unknown = set(changes) - set(CHANGEABLE)
    if unknown:
        raise ValueError(f"not changeable: {sorted(unknown)}")
    meeting = tx.get(Meeting, meeting_id, with_for_update=True)
    if meeting is None or meeting.deleted_at is not None:
        return False
    if meeting.status != "scheduled" and not (restore and _restorable(meeting)):
        return False
    if meeting.status != "scheduled":
        meeting.status = "scheduled"
        meeting.end_reason = None
        meeting.ended_at = None
    for name, value in changes.items():
        if name == "title":
            set_title(tx, meeting, value)
        elif name == "join_url":
            meeting.join_url = value
            meeting.join_url_key = join_url_key(value)
        else:
            setattr(meeting, name, value)
    meeting.updated_at = now
    tx.flush()
    return True


def cancel_scheduled(tx: Tx, meeting_id: uuid.UUID, *, now: datetime) -> bool:
    """Cancel a scheduled meeting because its invite says so. Returns whether it was cancelled."""
    meeting = tx.get(Meeting, meeting_id, with_for_update=True)
    if meeting is None or meeting.status != "scheduled":
        return False
    meeting.status = "cancelled"
    meeting.failure_reason = None
    meeting.end_reason = INVITE_CANCELLED
    meeting.ended_at = now
    meeting.updated_at = now
    tx.flush()
    return True


__all__ = [
    "CHANGEABLE",
    "INVITE_CANCELLED",
    "NOT_SCHEDULED",
    "SOURCE",
    "cancel_scheduled",
    "insert_scheduled",
    "skip",
    "unskip",
    "update_scheduled",
]
