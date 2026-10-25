"""Org security settings: requiring two-factor login, and resetting a member's two-factor login.

`PUT /security` with `require2fa: true` ends, in this org, every session of each member without a
confirmed TOTP enrollment; their next login gets an `enroll_required` session that reaches only
the enrollment routes. `DELETE /users/{id}/mfa` is for a member who lost their device: it removes
their TOTP enrollment and recovery codes and ends all their sessions. An admin cannot reset the
owner, and nobody resets their own two-factor login here (that needs a current code, under
`/api/v1/me/mfa`). Both changes are audited.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth import mfa, sessions
from andai_core.auth.deps import (
    NOT_AUTHENTICATED,
    Services,
    client_ip,
    get_services,
    org_transaction,
)
from andai_core.auth.roles import Caller, require_role
from andai_core.db.models import Org, OrgMembership, UserTotp

router = APIRouter(prefix="/api/v1/org")


class SecurityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require2fa: bool


@router.get("/security")
def get_security(
    caller: Caller = Depends(require_role("owner", "admin", "member")),
    services: Services = Depends(get_services),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        org = tx.get(Org, caller.session.org_id)
    if org is None:
        raise NOT_AUTHENTICATED
    return {"require2fa": org.require_2fa}


def _not_enrolled_members(tx: Tx, org_id: uuid.UUID) -> list[uuid.UUID]:
    members = tx.scalars(
        select(OrgMembership.user_id).where(OrgMembership.org_id == org_id)
    ).all()
    if not members:
        return []
    enrolled = set(
        tx.scalars(
            select(UserTotp.user_id).where(
                UserTotp.user_id.in_(members), UserTotp.confirmed_at.is_not(None)
            )
        ).all()
    )
    return [user_id for user_id in members if user_id not in enrolled]


@router.put("/security")
def put_security(
    body: SecurityBody,
    caller: Caller = Depends(require_role("owner", "admin")),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    """Turn the org's two-factor requirement on or off."""
    org_id = caller.session.org_id
    caller_signed_out = False
    with org_transaction(services, caller.session) as tx:
        org = tx.get(Org, org_id, with_for_update=True)
        if org is None:
            raise NOT_AUTHENTICATED
        if org.require_2fa != body.require2fa:
            org.require_2fa = body.require2fa
            if body.require2fa:
                not_enrolled = _not_enrolled_members(tx, org_id)
                sessions.revoke_in_org(tx, org_id, not_enrolled)
                caller_signed_out = caller.session.user_id in not_enrolled
            action = "org.require_2fa.enable" if body.require2fa else "org.require_2fa.disable"
            audit.record(tx, org_id, caller.session.user_id, action, f"org:{org_id}", ip)
    response = JSONResponse({"require2fa": body.require2fa})
    if caller_signed_out:
        sessions.clear_cookie(response, services.settings)
    return response


@router.delete("/users/{user_id}/mfa", status_code=204)
def reset_member_mfa(
    user_id: uuid.UUID,
    caller: Caller = Depends(require_role("owner", "admin")),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    """Clear a member's TOTP enrollment and recovery codes and end their sessions."""
    if user_id == caller.session.user_id:
        raise HTTPException(
            status_code=403,
            detail="reset your own two-factor login from your security settings",
        )
    org_id = caller.session.org_id
    with org_transaction(services, caller.session) as tx:
        role = tx.scalar(
            select(OrgMembership.role).where(
                OrgMembership.org_id == org_id, OrgMembership.user_id == user_id
            )
        )
        if role is None:
            raise HTTPException(status_code=404, detail="no such member")
        if role == "owner" and caller.role != "owner":
            raise HTTPException(
                status_code=403, detail="an admin cannot reset the owner's two-factor login"
            )
        if not mfa.remove(tx, user_id):
            raise HTTPException(
                status_code=409, detail="this member has not set up two-factor login"
            )
        sessions.revoke_all(tx, user_id)
        audit.record(tx, org_id, caller.session.user_id, "mfa.admin_reset", f"user:{user_id}", ip)
    return Response(status_code=204)
