"""Org and team role checks for routes.

`require_role("owner", "admin")` is a dependency that admits a `full` session whose membership in
the session's org has one of the given roles and answers 403 otherwise (401 without a session).
With `team_lead=True` the route has a `team_id` path parameter, and a lead of that team is admitted
as well; a team that is not in the session's org answers 404 before any role is checked.

Roles are read from the database on every request, so a role change takes effect on the caller's
next request. The dependency returns the caller's session, org role and (for team routes) their
role in the team, read inside the org's row-level-security context.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from sqlalchemy import select

from andai_core.auth import sessions
from andai_core.auth.deps import (
    NOT_AUTHENTICATED,
    Services,
    full_session,
    get_services,
    org_transaction,
)
from andai_core.db.models import ORG_ROLES, OrgMembership, Team, TeamMembership

FORBIDDEN = HTTPException(status_code=403, detail="not allowed for your role")
NO_SUCH_TEAM = HTTPException(status_code=404, detail="no such team")
LEAD = "lead"


@dataclass(frozen=True)
class Caller:
    session: sessions.ActiveSession
    role: str
    team_role: str | None = None


def require_role(*roles: str, team_lead: bool = False) -> Callable[..., Caller]:
    unknown = set(roles) - set(ORG_ROLES)
    if not roles or unknown:
        raise ValueError(f"require_role needs org roles from {ORG_ROLES}, got {roles}")

    def dependency(
        active: sessions.ActiveSession = Depends(full_session),
        services: Services = Depends(get_services),
    ) -> Caller:
        with org_transaction(services, active) as tx:
            role = tx.scalar(
                select(OrgMembership.role).where(
                    OrgMembership.org_id == active.org_id,
                    OrgMembership.user_id == active.user_id,
                )
            )
        if role is None:
            raise NOT_AUTHENTICATED
        if role not in roles:
            raise FORBIDDEN
        return Caller(session=active, role=role)

    def team_dependency(
        team_id: uuid.UUID,
        active: sessions.ActiveSession = Depends(full_session),
        services: Services = Depends(get_services),
    ) -> Caller:
        with org_transaction(services, active) as tx:
            role = tx.scalar(
                select(OrgMembership.role).where(
                    OrgMembership.org_id == active.org_id,
                    OrgMembership.user_id == active.user_id,
                )
            )
            if role is None:
                raise NOT_AUTHENTICATED
            team = tx.scalar(
                select(Team.id).where(Team.id == team_id, Team.org_id == active.org_id)
            )
            if team is None:
                raise NO_SUCH_TEAM
            team_role = tx.scalar(
                select(TeamMembership.role).where(
                    TeamMembership.org_id == active.org_id,
                    TeamMembership.team_id == team_id,
                    TeamMembership.user_id == active.user_id,
                )
            )
        if role not in roles and team_role != LEAD:
            raise FORBIDDEN
        return Caller(session=active, role=role, team_role=team_role)

    return team_dependency if team_lead else dependency
