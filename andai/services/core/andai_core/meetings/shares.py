"""Sharing a meeting inside the org (PORT-03): grant, revoke and list.

* Who may share: the requester, a lead of the meeting's team, admins and owners
  (`access.can_share`). Anyone else, a grantee included, gets the 404 the meeting answers to
  someone who may not see it.
* Grantees: a user who is an active member of the caller's org, or a team of the caller's org,
  looked up in the transaction that writes the grant. Anything else (another org's person or team,
  an unknown id, a disabled or not-yet-joined user) is 404 `no_such_grantee` and nothing is stored.
  There is no link, email or outside grantee: a share can only name an id of this org.
* A grant gives read access only (meeting, transcript, live view); a team grant reaches whoever is
  in the team at each read (`access.accessible`). Granting the same grantee again returns the
  existing share.
* Every grant and revoke writes an audit event (`share.grant`, `share.revoke`; target
  `meeting:<id> → user:<id>` or `team:<id>`) in the same transaction, so the event and the change
  commit or roll back together. A revoke applies on the grantee's next request, and ends an open
  live stream within one keepalive (`live.py`).
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.db.models import Meeting, MeetingShare, OrgMembership, Team, User
from andai_core.meetings.access import Actor, can_share, share_grants
from andai_core.meetings.service import NO_SUCH_MEETING, MeetingError, readable

NO_SUCH_GRANTEE = MeetingError(
    404, "no_such_grantee", "no such person or team in your organisation"
)
NO_SUCH_SHARE = MeetingError(404, "no_such_share", "no such share")
ARROW = "→"


def _shareable(tx: Tx, actor: Actor, meeting_id: uuid.UUID) -> Meeting:
    meeting = readable(tx, actor, meeting_id)
    if not can_share(tx, actor, meeting):
        raise NO_SUCH_MEETING
    return meeting


def _grantee_in_org(tx: Tx, actor: Actor, grantee_type: str, grantee_id: uuid.UUID) -> bool:
    if grantee_type == "user":
        found = exists().where(
            OrgMembership.org_id == actor.org_id,
            OrgMembership.user_id == grantee_id,
            User.id == OrgMembership.user_id,
            User.status == "active",
        )
    elif grantee_type == "team":
        found = exists().where(Team.org_id == actor.org_id, Team.id == grantee_id)
    else:
        return False
    return bool(tx.scalar(select(found)))


def target(meeting_id: uuid.UUID, grantee_type: str, grantee_id: uuid.UUID) -> str:
    return f"meeting:{meeting_id} {ARROW} {grantee_type}:{grantee_id}"


def _described(tx: Tx, meeting: Meeting, share_id: uuid.UUID) -> dict:
    [share] = [g for g in share_grants(tx, meeting) if g["id"] == str(share_id)]
    return share


def list_shares(tx: Tx, actor: Actor, meeting_id: uuid.UUID) -> dict:
    return {"shares": share_grants(tx, _shareable(tx, actor, meeting_id))}


def grant(
    tx: Tx,
    actor: Actor,
    meeting_id: uuid.UUID,
    grantee_type: str,
    grantee_id: uuid.UUID,
    *,
    ip: str | None,
) -> tuple[dict, bool]:
    """Share the meeting with a user or team of the caller's org. Returns the share and whether
    it is new (an existing grant to the same grantee is returned as it is)."""
    meeting = _shareable(tx, actor, meeting_id)
    if not _grantee_in_org(tx, actor, grantee_type, grantee_id):
        raise NO_SUCH_GRANTEE
    new_id = tx.scalar(
        pg_insert(MeetingShare)
        .values(
            id=uuid.uuid4(),
            org_id=actor.org_id,
            meeting_id=meeting.id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            granted_by=actor.user_id,
        )
        .on_conflict_do_nothing(index_elements=["meeting_id", "grantee_type", "grantee_id"])
        .returning(MeetingShare.id)
    )
    if new_id is None:
        share_id = tx.scalar(
            select(MeetingShare.id).where(
                MeetingShare.meeting_id == meeting.id,
                MeetingShare.grantee_type == grantee_type,
                MeetingShare.grantee_id == grantee_id,
            )
        )
        return _described(tx, meeting, share_id), False
    audit.record(
        tx, actor.org_id, actor.user_id, "share.grant",
        target(meeting.id, grantee_type, grantee_id), ip,
    )
    return _described(tx, meeting, new_id), True


def revoke(
    tx: Tx, actor: Actor, meeting_id: uuid.UUID, share_id: uuid.UUID, *, ip: str | None
) -> None:
    meeting = _shareable(tx, actor, meeting_id)
    share = tx.scalar(
        select(MeetingShare).where(
            MeetingShare.id == share_id, MeetingShare.meeting_id == meeting.id
        )
    )
    if share is None:
        raise NO_SUCH_SHARE
    tx.delete(share)
    tx.flush()
    audit.record(
        tx, actor.org_id, actor.user_id, "share.revoke",
        target(meeting.id, share.grantee_type, share.grantee_id), ip,
    )


__all__ = ["NO_SUCH_GRANTEE", "NO_SUCH_SHARE", "grant", "list_shares", "revoke", "target"]
