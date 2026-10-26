"""Members, invites and teams under /api/v1.

* `GET /org/users` — the org's members (role, status, teams, whether 2FA is on) and pending
  invites; `POST /org/users` invites an email address with a role and mails the link. Admin or
  owner.
* `PATCH /org/users/{id}` — change a member's role or disable/enable their account (disabling ends
  every session they hold). Admin or owner; the owner is out of an admin's reach, nobody changes
  themselves, and `role: "owner"` (from the owner only) hands ownership on.
* `GET /teams` — the org's teams and their members, for any member. `POST /teams` creates one
  (admin or owner). `PUT /teams/{id}/members` replaces a team's members (admin, owner, or a lead
  of that team, who cannot change who leads it).

Roles are read per request (`require_role`), so changes apply on the next request. Every change is
audited. Responses never contain an invite token.
"""

from __future__ import annotations

import uuid
from typing import Literal

import anyio.from_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from andai_core.auth.deps import Services, client_ip, get_services, org_transaction
from andai_core.auth.invites import invite_mail
from andai_core.auth.links import InvalidEmail, canonical_email
from andai_core.auth.roles import Caller, require_role
from andai_core.db.models import Org
from andai_core.mail import MailError
from andai_core.org import service

router = APIRouter(prefix="/api/v1")

ADMINS = ("owner", "admin")
EVERYONE = ("owner", "admin", "member")
MAX_TEAM_MEMBERS = 1000


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteBody(_Body):
    email: str = Field(max_length=320)
    role: Literal["admin", "member"]


class MemberPatch(_Body):
    role: Literal["owner", "admin", "member"] | None = None
    status: Literal["active", "disabled"] | None = None


class TeamBody(_Body):
    name: str = Field(min_length=1, max_length=100)


class TeamMember(_Body):
    user_id: uuid.UUID
    role: Literal["lead", "member"]


class TeamMembersBody(_Body):
    members: list[TeamMember] = Field(max_length=MAX_TEAM_MEMBERS)


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, service.NotFound):
        return HTTPException(status_code=404, detail="not found")
    if isinstance(exc, service.Forbidden):
        return HTTPException(status_code=403, detail=str(exc) or "not allowed")
    if isinstance(exc, service.Conflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


ORG_ERRORS = (service.NotFound, service.Forbidden, service.Conflict, service.Invalid)


@router.get("/org/users")
def list_users(
    caller: Caller = Depends(require_role(*ADMINS)),
    services: Services = Depends(get_services),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        return service.list_members(tx, caller.session.org_id, services.clock())


@router.post("/org/users", status_code=201)
def invite_user(
    body: InviteBody,
    caller: Caller = Depends(require_role(*ADMINS)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    """Invite an address with a role and mail the set-password link. The mail goes out before
    the invite commits: if the relay refuses it, nothing is created (503)."""
    try:
        email = canonical_email(body.email)
    except InvalidEmail as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    org_id = caller.session.org_id
    try:
        with org_transaction(services, caller.session) as tx:
            org = tx.get(Org, org_id)
            if org is None:
                raise HTTPException(status_code=401, detail="not authenticated")
            invited = service.invite(
                tx,
                org_id=org_id,
                email=email,
                role=body.role,
                actor=caller.session.user_id,
                settings=services.settings,
                now=services.clock(),
                ip=ip,
            )
            mail = invite_mail(
                services.settings,
                email=email,
                org_name=org.name,
                token=invited.token,
                owner=False,
            )
            anyio.from_thread.run(services.mailer.send, mail)
    except ORG_ERRORS as exc:
        raise _http(exc) from None
    except MailError:
        raise HTTPException(
            status_code=503, detail="the invite mail could not be sent; nothing was created"
        ) from None
    return {
        "id": str(invited.id),
        "email": invited.email,
        "role": invited.role,
        "expires_at": invited.expires_at.isoformat(),
    }


@router.patch("/org/users/{user_id}")
def update_user(
    user_id: uuid.UUID,
    body: MemberPatch,
    caller: Caller = Depends(require_role(*ADMINS)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    if body.role is None and body.status is None:
        raise HTTPException(status_code=422, detail="give a role or a status")
    try:
        with org_transaction(services, caller.session) as tx:
            return service.update_member(
                tx,
                org_id=caller.session.org_id,
                caller_id=caller.session.user_id,
                caller_role=caller.role,
                user_id=user_id,
                role=body.role,
                status=body.status,
                ip=ip,
            )
    except ORG_ERRORS as exc:
        raise _http(exc) from None


@router.get("/teams")
def list_teams(
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        return {"teams": service.list_teams(tx, caller.session.org_id)}


@router.post("/teams", status_code=201)
def create_team(
    body: TeamBody,
    caller: Caller = Depends(require_role(*ADMINS)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    try:
        with org_transaction(services, caller.session) as tx:
            return service.create_team(
                tx,
                org_id=caller.session.org_id,
                name=body.name,
                actor=caller.session.user_id,
                ip=ip,
            )
    except ORG_ERRORS as exc:
        raise _http(exc) from None


@router.put("/teams/{team_id}/members")
def set_team_members(
    team_id: uuid.UUID,
    body: TeamMembersBody,
    caller: Caller = Depends(require_role(*ADMINS, team_lead=True)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    try:
        with org_transaction(services, caller.session) as tx:
            return service.set_team_members(
                tx,
                org_id=caller.session.org_id,
                team_id=team_id,
                members=[(m.user_id, m.role) for m in body.members],
                actor=caller.session.user_id,
                leads_locked=caller.role not in ADMINS,
                ip=ip,
            )
    except ORG_ERRORS as exc:
        raise _http(exc) from None
