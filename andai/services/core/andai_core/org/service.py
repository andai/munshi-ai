"""Org administration: members, invites and teams.

Every function runs in the caller's transaction, whose row-level-security context is the caller's
org (`app.org_id`) and user (`app.user_id`). `users` is a global table, so people are always reached
through an `org_memberships` row of this org; and because the membership read policy also admits
the caller's own memberships in other orgs, every membership query names the org explicitly.

Rules the routes rely on:

* Nobody changes their own role or status here, the owner is out of an admin's reach, and only the
  owner hands ownership on (the previous owner becomes an admin).
* Invites are for `admin` or `member`; inviting an address again voids its earlier pending invite.
  Membership is granted when the invite is accepted.
* A team lead may change who is in their team, but not who leads it.

Each change writes one audit event in the same transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth import users
from andai_core.auth.invites import create_invite
from andai_core.auth.tokens import hash_token
from andai_core.db.models import Invite, OrgMembership, Team, TeamMembership, User, UserTotp
from andai_core.settings import Settings

INVITE_ROLES = ("admin", "member")
OWNER = "owner"


class NotFound(Exception):
    """The member or team is not in this org (or does not exist)."""


class Forbidden(Exception):
    """The caller's role does not allow this change."""


class Conflict(Exception):
    """The change contradicts the current state."""


class Invalid(Exception):
    """The request is well-formed but not acceptable."""


@dataclass(frozen=True)
class Invited:
    id: uuid.UUID
    email: str
    role: str
    expires_at: datetime
    token: str  # mailed, never returned


# ─── members ──────────────────────────────────────────────────────────────────────────────────────


def _member_row(tx: Tx, org_id: uuid.UUID, user_id: uuid.UUID) -> tuple[OrgMembership, User]:
    row = tx.execute(
        select(OrgMembership, User)
        .join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
    ).first()
    if row is None:
        raise NotFound
    return row[0], row[1]


def list_members(tx: Tx, org_id: uuid.UUID, now: datetime) -> dict:
    """The org's members (with their teams) and its pending invites."""
    rows = tx.execute(
        select(OrgMembership, User)
        .join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_id == org_id)
        .order_by(OrgMembership.created_at, User.email)
    ).all()
    user_ids = [user.id for _, user in rows]
    enrolled = set(
        tx.scalars(
            select(UserTotp.user_id).where(
                UserTotp.user_id.in_(user_ids), UserTotp.confirmed_at.is_not(None)
            )
        ).all()
    ) if user_ids else set()
    teams: dict[uuid.UUID, list[dict]] = {}
    for team_id, name, user_id, role in tx.execute(
        select(Team.id, Team.name, TeamMembership.user_id, TeamMembership.role)
        .join(TeamMembership, TeamMembership.team_id == Team.id)
        .where(Team.org_id == org_id, TeamMembership.org_id == org_id)
        .order_by(Team.name)
    ):
        teams.setdefault(user_id, []).append({"id": str(team_id), "name": name, "role": role})
    invites = tx.scalars(
        select(Invite)
        .where(Invite.org_id == org_id, Invite.accepted_at.is_(None), Invite.expires_at > now)
        .order_by(Invite.created_at)
    ).all()
    return {
        "users": [
            {
                **member_view(membership, user),
                "mfa_enabled": user.id in enrolled,
                "teams": teams.get(user.id, []),
            }
            for membership, user in rows
        ],
        "invites": [
            {
                "id": str(invite.id),
                "email": invite.email,
                "role": invite.role,
                "expires_at": invite.expires_at.isoformat(),
            }
            for invite in invites
        ],
    }


def member_view(membership: OrgMembership, user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "status": user.status,
        "role": membership.role,
    }


def invite(
    tx: Tx,
    *,
    org_id: uuid.UUID,
    email: str,
    role: str,
    actor: uuid.UUID,
    settings: Settings,
    now: datetime,
    ip: str | None,
) -> Invited:
    """Invite the canonical `email` with `role`; an earlier pending invite for it is voided."""
    if role not in INVITE_ROLES:
        raise Invalid(f"invites are for one of {INVITE_ROLES}")
    member = tx.scalar(
        select(OrgMembership.user_id)
        .join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_id == org_id, User.email == email)
    )
    if member is not None:
        raise Conflict("this person is already a member")
    tx.execute(
        update(Invite)
        .where(
            Invite.org_id == org_id,
            Invite.email == email,
            Invite.accepted_at.is_(None),
            Invite.expires_at > now,
        )
        .values(expires_at=now)
    )
    token = create_invite(
        tx, org_id=org_id, email=email, role=role, invited_by=actor, settings=settings, now=now
    )
    created = tx.execute(
        select(Invite).where(Invite.token_hash == hash_token(token))
    ).scalar_one()
    audit.record(tx, org_id, actor, "invite.create", f"invite:{created.id}", ip)
    return Invited(
        id=created.id, email=email, role=role, expires_at=created.expires_at, token=token
    )


def update_member(
    tx: Tx,
    *,
    org_id: uuid.UUID,
    caller_id: uuid.UUID,
    caller_role: str,
    user_id: uuid.UUID,
    role: str | None,
    status: str | None,
    ip: str | None,
) -> dict:
    """Change a member's org role and/or account status ("active" or "disabled")."""
    membership, user = _member_row(tx, org_id, user_id)
    if user_id == caller_id:
        raise Forbidden("you cannot change your own role or status")
    if membership.role == OWNER and caller_role != OWNER:
        raise Forbidden("an admin cannot change the owner")
    if role is not None and role != membership.role:
        if role == OWNER:
            if caller_role != OWNER:
                raise Forbidden("only the owner can hand on ownership")
            tx.execute(
                update(OrgMembership)
                .where(OrgMembership.org_id == org_id, OrgMembership.user_id == caller_id)
                .values(role="admin")
            )
            membership.role = OWNER
            audit.record(tx, org_id, caller_id, "org.owner_transfer", f"user:{user_id}", ip)
        else:
            membership.role = role
            audit.record(tx, org_id, caller_id, "member.role_change", f"user:{user_id}", ip)
    if status is not None and status != user.status:
        if status == "disabled":
            users.disable_user(tx, org_id=org_id, user_id=user_id, actor=caller_id, ip=ip)
            user.status = "disabled"
        elif status == "active":
            if user.status != "disabled" or user.password_hash is None:
                raise Conflict("only a disabled account can be enabled")
            user.status = "active"
            audit.record(tx, org_id, caller_id, "user.enable", f"user:{user_id}", ip)
        else:
            raise Invalid("status must be active or disabled")
    tx.flush()
    return member_view(membership, user)


# ─── teams ────────────────────────────────────────────────────────────────────────────────────────


def _team_members(tx: Tx, org_id: uuid.UUID, team_ids: list[uuid.UUID]) -> dict:
    found: dict[uuid.UUID, list[dict]] = {team_id: [] for team_id in team_ids}
    if not team_ids:
        return found
    for team_id, role, user in tx.execute(
        select(TeamMembership.team_id, TeamMembership.role, User)
        .join(User, User.id == TeamMembership.user_id)
        .where(TeamMembership.org_id == org_id, TeamMembership.team_id.in_(team_ids))
        .order_by(TeamMembership.role, User.email)
    ):
        found[team_id].append(
            {
                "user_id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
                "role": role,
            }
        )
    return found


def team_view(tx: Tx, org_id: uuid.UUID, team: Team) -> dict:
    members = _team_members(tx, org_id, [team.id])[team.id]
    return {"id": str(team.id), "name": team.name, "members": members}


def list_teams(tx: Tx, org_id: uuid.UUID) -> list[dict]:
    teams = tx.scalars(select(Team).where(Team.org_id == org_id).order_by(Team.name)).all()
    members = _team_members(tx, org_id, [team.id for team in teams])
    return [
        {"id": str(team.id), "name": team.name, "members": members[team.id]} for team in teams
    ]


def create_team(
    tx: Tx, *, org_id: uuid.UUID, name: str, actor: uuid.UUID, ip: str | None
) -> dict:
    name = name.strip()
    if not name:
        raise Invalid("a team needs a name")
    team = Team(id=uuid.uuid4(), org_id=org_id, name=name)
    try:
        with tx.begin_nested():
            tx.add(team)
            tx.flush()
    except IntegrityError:
        raise Conflict("a team with this name exists") from None
    audit.record(tx, org_id, actor, "team.create", f"team:{team.id}", ip)
    return {"id": str(team.id), "name": team.name, "members": []}


def set_team_members(
    tx: Tx,
    *,
    org_id: uuid.UUID,
    team_id: uuid.UUID,
    members: list[tuple[uuid.UUID, str]],
    actor: uuid.UUID,
    leads_locked: bool,
    ip: str | None,
) -> dict:
    """Replace the team's members with `members` ((user_id, team role) pairs). With
    `leads_locked` (the caller is a team lead, not an admin) the set of leads must not change."""
    team = tx.scalar(select(Team).where(Team.id == team_id, Team.org_id == org_id))
    if team is None:
        raise NotFound
    wanted = dict(members)
    if len(wanted) != len(members):
        raise Invalid("each person may appear once")
    in_org = set(
        tx.scalars(
            select(OrgMembership.user_id).where(
                OrgMembership.org_id == org_id, OrgMembership.user_id.in_(list(wanted))
            )
        ).all()
    ) if wanted else set()
    if in_org != set(wanted):
        raise Invalid("every team member must be a member of this organisation")
    if leads_locked:
        current_leads = set(
            tx.scalars(
                select(TeamMembership.user_id).where(
                    TeamMembership.org_id == org_id,
                    TeamMembership.team_id == team_id,
                    TeamMembership.role == "lead",
                )
            ).all()
        )
        if current_leads != {user_id for user_id, role in wanted.items() if role == "lead"}:
            raise Forbidden("only an admin changes who leads a team")
    tx.execute(
        delete(TeamMembership).where(
            TeamMembership.org_id == org_id, TeamMembership.team_id == team_id
        )
    )
    tx.add_all(
        TeamMembership(team_id=team_id, org_id=org_id, user_id=user_id, role=role)
        for user_id, role in wanted.items()
    )
    tx.flush()
    audit.record(tx, org_id, actor, "team.members_set", f"team:{team_id}", ip)
    return team_view(tx, org_id, team)
