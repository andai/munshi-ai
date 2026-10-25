"""Authentication routes under /api/v1.

Responses never contain a password, a password hash, a token or a token hash. Login failures look
the same whatever the cause (unknown email, wrong password, no password yet, disabled user).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as Tx

from andai_core.auth import passwords, sessions
from andai_core.auth.deps import (
    NOT_AUTHENTICATED,
    Services,
    client_ip,
    full_session,
    get_services,
    org_transaction,
)
from andai_core.auth.ratelimit import login_key
from andai_core.db.models import Org, OrgMembership, User
from andai_core.db.session import set_context, transaction

router = APIRouter(prefix="/api/v1")

INVALID_LOGIN = "invalid email or password"
TOO_MANY_ATTEMPTS = "too many attempts; try again later"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    email: str = Field(max_length=320)
    password: str = Field(max_length=passwords.MAX_LENGTH)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _first_org(tx: Tx, user: User) -> OrgMembership | None:
    """The user's oldest membership (read through the `app.user_id` pre-context policy)."""
    set_context(tx, user_id=user.id)
    return tx.scalar(
        select(OrgMembership)
        .where(OrgMembership.user_id == user.id)
        .order_by(OrgMembership.created_at, OrgMembership.org_id)
        .limit(1)
    )


@router.post("/auth/login")
def login(
    body: LoginBody,
    request: Request,
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    email = normalize_email(body.email)
    key = login_key(email, ip)
    retry_after = services.login_limiter.blocked(key)
    if retry_after is not None:
        return JSONResponse(
            {"detail": TOO_MANY_ATTEMPTS},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    token: str | None = None
    with transaction(services.engine) as tx:
        user = tx.scalar(select(User).where(User.email == email))
        if user is None or user.password_hash is None:
            passwords.dummy_verify(body.password)
            ok = False
        else:
            ok = passwords.verify(user.password_hash, body.password) and user.status == "active"
        membership = _first_org(tx, user) if ok and user is not None else None
        if user is not None and membership is not None:
            now = services.clock()
            if passwords.needs_rehash(user.password_hash or ""):
                user.password_hash = passwords.hash_password(body.password)
            user.last_login_at = now
            # A new token on every login: a session the browser already held is ended, so a
            # token planted before login (session fixation) never becomes authenticated.
            presented = request.cookies.get(sessions.COOKIE_NAME)
            if presented:
                sessions.revoke(tx, presented)
            token = sessions.create(
                tx,
                user_id=user.id,
                org_id=membership.org_id,
                stage=sessions.FULL,
                lifetimes=services.lifetimes,
                now=now,
                ip=ip,
                user_agent=request.headers.get("user-agent"),
            )

    if token is None:
        services.login_limiter.hit(key)
        return JSONResponse({"detail": INVALID_LOGIN}, status_code=401)
    services.login_limiter.clear(key)
    response = JSONResponse({"stage": sessions.FULL})
    sessions.set_cookie(response, token, services.settings)
    return response


@router.post("/auth/logout", status_code=204)
def logout(request: Request, services: Services = Depends(get_services)) -> Response:
    token = request.cookies.get(sessions.COOKIE_NAME)
    if token:
        with transaction(services.engine) as tx:
            sessions.revoke(tx, token)
    response = Response(status_code=204)
    sessions.clear_cookie(response, services.settings)
    return response


@router.get("/me")
def me(
    active: sessions.ActiveSession = Depends(full_session),
    services: Services = Depends(get_services),
) -> dict:
    with org_transaction(services, active) as tx:
        org = tx.get(Org, active.org_id)
        role = tx.scalar(
            select(OrgMembership.role).where(
                OrgMembership.org_id == active.org_id, OrgMembership.user_id == active.user_id
            )
        )
    if org is None or role is None:
        raise NOT_AUTHENTICATED
    return {
        "user": {
            "id": str(active.user_id),
            "email": active.email,
            "display_name": active.display_name,
        },
        "org": {"id": str(org.id), "name": org.name, "slug": org.slug},
        "role": role,
        "stage": active.stage,
    }
