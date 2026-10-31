"""Who may read, stop, share and delete a meeting (`.work/AUTH_MATRIX.md`, meeting rows).

One predicate decides reads everywhere: the meeting page, its transcript, the live relay (checked
again at every keepalive), the meeting list and search all filter with `accessible(actor)` in SQL,
so a share revoked or a team membership changed applies on the next request on every path.

A meeting of the caller's org is readable by:

* org admins and owners;
* its requester;
* a lead of the meeting's team;
* a user it is shared with;
* a member of a team it is shared with, as the team is at the time of the read.

Anyone else, and anyone of another org, reads nothing: the routes answer 404 as if the meeting did
not exist. Shares give read access only. Stopping and sharing stay with the requester, the
meeting's team lead, admins and owners; deleting with the requester, admins and owners.

Every check names the org first, so an admin of one org is nobody in another.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.orm import Session as Tx
from sqlalchemy.orm import aliased

from andai_core.db.models import Meeting, MeetingShare, Team, TeamMembership, User

ADMINS = ("owner", "admin")


@dataclass(frozen=True)
class Actor:
    org_id: uuid.UUID
    user_id: uuid.UUID
    role: str  # org role

    @property
    def is_admin(self) -> bool:
        return self.role in ADMINS


def _leads_team(actor: Actor) -> ColumnElement[bool]:
    """The actor leads the meeting's team (correlated to `Meeting`)."""
    return exists().where(
        TeamMembership.org_id == actor.org_id,
        TeamMembership.team_id == Meeting.team_id,
        TeamMembership.user_id == actor.user_id,
        TeamMembership.role == "lead",
    )


def shared_with(actor: Actor) -> ColumnElement[bool]:
    """The meeting is shared with the actor, or with a team the actor is in now (correlated to
    `Meeting`)."""
    mine = aliased(TeamMembership)
    return or_(
        exists().where(
            MeetingShare.org_id == actor.org_id,
            MeetingShare.meeting_id == Meeting.id,
            MeetingShare.grantee_type == "user",
            MeetingShare.grantee_id == actor.user_id,
        ),
        exists().where(
            MeetingShare.org_id == actor.org_id,
            MeetingShare.meeting_id == Meeting.id,
            MeetingShare.grantee_type == "team",
            mine.org_id == actor.org_id,
            mine.team_id == MeetingShare.grantee_id,
            mine.user_id == actor.user_id,
        ),
    )


def accessible(actor: Actor) -> ColumnElement[bool]:
    """The meetings `actor` may read, as a condition on `Meeting`."""
    in_org = Meeting.org_id == actor.org_id
    if actor.is_admin:
        return in_org
    return and_(
        in_org,
        or_(Meeting.requested_by == actor.user_id, _leads_team(actor), shared_with(actor)),
    )


def _leads(tx: Tx, actor: Actor, meeting: Meeting) -> bool:
    if meeting.team_id is None:
        return False
    return bool(
        tx.scalar(
            select(
                exists().where(
                    TeamMembership.org_id == actor.org_id,
                    TeamMembership.team_id == meeting.team_id,
                    TeamMembership.user_id == actor.user_id,
                    TeamMembership.role == "lead",
                )
            )
        )
    )


def can_read(tx: Tx, actor: Actor, meeting: Meeting) -> bool:
    if meeting.org_id != actor.org_id:
        return False
    if actor.is_admin or meeting.requested_by == actor.user_id:
        return True
    return bool(
        tx.scalar(select(exists().where(Meeting.id == meeting.id, accessible(actor))))
    )


def can_stop(tx: Tx, actor: Actor, meeting: Meeting) -> bool:
    if meeting.org_id != actor.org_id:
        return False
    return actor.is_admin or meeting.requested_by == actor.user_id or _leads(tx, actor, meeting)


def can_share(tx: Tx, actor: Actor, meeting: Meeting) -> bool:
    return can_stop(tx, actor, meeting)


def can_delete(tx: Tx, actor: Actor, meeting: Meeting) -> bool:  # noqa: ARG001 - one signature
    if meeting.org_id != actor.org_id:
        return False
    return actor.is_admin or meeting.requested_by == actor.user_id


def permissions(tx: Tx, actor: Actor, meeting: Meeting) -> dict[str, bool]:
    """What `actor` may do with a meeting they can read, for the API view."""
    manage = can_stop(tx, actor, meeting)
    return {
        "can_share": manage,
        "can_delete": can_delete(tx, actor, meeting),
        "can_stop": manage,
    }


def share_grants(tx: Tx, meeting: Meeting) -> list[dict]:
    """Who the meeting is shared with, oldest grant first: each share's id, grantee type, id and
    display name (a person's or a team's; null if the team is gone)."""
    rows = tx.execute(
        select(MeetingShare, User.display_name, Team.name)
        .outerjoin(
            User, and_(MeetingShare.grantee_type == "user", User.id == MeetingShare.grantee_id)
        )
        .outerjoin(
            Team,
            and_(
                MeetingShare.grantee_type == "team",
                Team.id == MeetingShare.grantee_id,
                Team.org_id == MeetingShare.org_id,
            ),
        )
        .where(MeetingShare.org_id == meeting.org_id, MeetingShare.meeting_id == meeting.id)
        .order_by(MeetingShare.created_at, MeetingShare.id)
    ).all()
    return [
        {
            "id": str(share.id),
            "grantee_type": share.grantee_type,
            "grantee_id": str(share.grantee_id),
            "name": user_name if share.grantee_type == "user" else team_name,
            "granted_by": str(share.granted_by),
            "created_at": share.created_at.isoformat(),
        }
        for share, user_name, team_name in rows
    ]


__all__ = [
    "ADMINS",
    "Actor",
    "accessible",
    "can_delete",
    "can_read",
    "can_share",
    "can_stop",
    "permissions",
    "share_grants",
    "shared_with",
]
