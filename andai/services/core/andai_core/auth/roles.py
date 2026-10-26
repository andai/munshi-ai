"""Org role checks for routes.

`require_role("owner", "admin")` is a dependency that admits a `full` session whose membership in
the session's org has one of the given roles and answers 403 otherwise (401 without a session).
It returns the caller's session and role, read inside the org's row-level-security context.
"""

from __future__ import annotations

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
from andai_core.db.models import ORG_ROLES, OrgMembership

FORBIDDEN = HTTPException(status_code=403, detail="not allowed for your role")


@dataclass(frozen=True)
class Caller:
    session: sessions.ActiveSession
    role: str


def require_role(*roles: str) -> Callable[..., Caller]:
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

    return dependency
