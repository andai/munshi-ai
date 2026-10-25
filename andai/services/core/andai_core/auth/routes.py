"""Authentication routes under /api/v1.

Responses never contain a password, a password hash, a token or a token hash. Login failures look
the same whatever the cause (unknown email, wrong password, no password yet, disabled user), and a
password-reset request answers 202 whether or not the address has an account; its mail is sent
after the response.

Two-factor login: a user with confirmed TOTP gets an `mfa_pending` session from the password step
and a `full` one (new token) from `POST /auth/mfa/verify`. The enrollment routes under `/me/mfa`
return the otpauth URI and QR code (start) and the recovery codes (confirm, regenerate) exactly
once, with `Cache-Control: no-store`; nothing returns a TOTP secret or a recovery code afterwards.
Password reset never touches the enrollment, so it never bypasses the second step.
"""

from __future__ import annotations

import anyio.from_thread
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as Tx

from andai_core.auth import mfa, passwords, sessions
from andai_core.auth.bootstrap import AlreadyBootstrapped, bootstrap
from andai_core.auth.deps import (
    NOT_AUTHENTICATED,
    Services,
    client_ip,
    current_session,
    full_session,
    get_services,
    org_transaction,
)
from andai_core.auth.invites import AccountActive, InvalidInvite, accept_invite
from andai_core.auth.links import canonical_email
from andai_core.auth.ratelimit import login_key, reset_key
from andai_core.auth.resets import InvalidReset, confirm_reset, request_reset
from andai_core.db.models import Org, OrgMembership, User
from andai_core.db.session import set_context, transaction
from andai_core.mail import Mail, Mailer, MailError

router = APIRouter(prefix="/api/v1")

INVALID_LOGIN = "invalid email or password"
TOO_MANY_ATTEMPTS = "too many attempts; try again later"
INVALID_LINK = "this link is invalid or has expired"
INVALID_CODE = "invalid code"
NO_STORE = {"Cache-Control": "no-store"}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    email: str = Field(max_length=320)
    password: str = Field(max_length=passwords.MAX_LENGTH)


class ResetRequestBody(_Body):
    email: str = Field(max_length=320)


class SetPasswordBody(_Body):
    token: str = Field(min_length=1, max_length=256)
    password: str = Field(max_length=passwords.MAX_LENGTH)


class AcceptInviteBody(SetPasswordBody):
    display_name: str | None = Field(default=None, max_length=200)


class CodeBody(_Body):
    code: str = Field(min_length=1, max_length=64)


class BootstrapBody(_Body):
    email: str = Field(max_length=320)
    org_name: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)


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
    stage = sessions.FULL
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
            if mfa.is_enrolled(tx, user.id):
                stage = sessions.MFA_PENDING
            token = sessions.create(
                tx,
                user_id=user.id,
                org_id=membership.org_id,
                stage=stage,
                lifetimes=services.lifetimes,
                now=now,
                ip=ip,
                user_agent=request.headers.get("user-agent"),
            )

    if token is None:
        services.login_limiter.hit(key)
        return JSONResponse({"detail": INVALID_LOGIN}, status_code=401)
    services.login_limiter.clear(key)
    response = JSONResponse({"stage": stage})
    sessions.set_cookie(response, token, services.settings)
    return response


@router.post("/auth/mfa/verify")
def mfa_verify(
    body: CodeBody,
    request: Request,
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    """The second login step: a TOTP code or a recovery code for the `mfa_pending` session."""
    token = request.cookies.get(sessions.COOKIE_NAME)
    if not token:
        raise NOT_AUTHENTICATED
    with transaction(services.engine) as tx:
        outcome = mfa.verify_pending(
            tx,
            services.keyring,
            services.mfa_limiter,
            token=token,
            code=body.code,
            lifetimes=services.lifetimes,
            now=services.clock(),
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    if outcome.status == mfa.VERIFIED and outcome.token is not None:
        response = JSONResponse({"stage": sessions.FULL})
        sessions.set_cookie(response, outcome.token, services.settings)
        return response
    if outcome.status == mfa.WRONG:
        return JSONResponse({"detail": INVALID_CODE, "next_step": "mfa_verify"}, status_code=401)
    if outcome.status == mfa.THROTTLED:
        return JSONResponse(
            {"detail": TOO_MANY_ATTEMPTS, "next_step": "mfa_verify"},
            status_code=429,
            headers={"Retry-After": str(outcome.retry_after)},
        )
    if outcome.status == mfa.LOCKED:
        response = JSONResponse(
            {"detail": "too many wrong codes; sign in again", "next_step": "login"},
            status_code=401,
        )
        sessions.clear_cookie(response, services.settings)
        return response
    return JSONResponse({"detail": "no two-factor sign-in is pending"}, status_code=401)


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
        mfa_enabled = mfa.is_enrolled(tx, active.user_id)
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
        "mfa_enabled": mfa_enabled,
    }


# ─── two-factor enrollment (own account) ──────────────────────────────────────────────────────────


def enrollment_session(
    active: sessions.ActiveSession = Depends(current_session),
) -> sessions.ActiveSession:
    """A `full` session, or an `enroll_required` one (the org requires 2FA, not yet set up)."""
    if active.stage not in (sessions.FULL, sessions.ENROLL_REQUIRED):
        raise NOT_AUTHENTICATED
    return active


def _check_current_code(
    tx: Tx, services: Services, active: sessions.ActiveSession, code: str, ip: str | None
) -> None:
    """Require a current TOTP or recovery code from an enrolled user (HTTP errors otherwise).
    Wrong codes count against the same per-user limit as the login step."""
    key = mfa.failure_key(active.user_id)
    retry_after = services.mfa_limiter.blocked(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=429, detail=TOO_MANY_ATTEMPTS, headers={"Retry-After": str(retry_after)}
        )
    try:
        method = mfa.check(
            tx, services.keyring, user_id=active.user_id, code=code, now=services.clock()
        )
    except mfa.NotEnrolled:
        raise HTTPException(status_code=409, detail="two-factor login is not on") from None
    except mfa.InvalidCode:
        services.mfa_limiter.hit(key)
        raise HTTPException(status_code=400, detail=INVALID_CODE) from None
    if method == mfa.RECOVERY:
        mfa.record(
            tx,
            org_id=active.org_id,
            actor=active.user_id,
            action="mfa.recovery_use",
            user_id=active.user_id,
            ip=ip,
        )


@router.post("/me/mfa")
def mfa_start(
    active: sessions.ActiveSession = Depends(enrollment_session),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    """Start (or restart) TOTP enrollment; the URI and QR code are shown this once."""
    with org_transaction(services, active) as tx:
        try:
            enrollment = mfa.start(
                tx,
                services.keyring,
                user_id=active.user_id,
                email=active.email,
                now=services.clock(),
            )
        except mfa.AlreadyEnrolled:
            raise HTTPException(status_code=409, detail="two-factor login is already on") from None
        mfa.record(
            tx,
            org_id=active.org_id,
            actor=active.user_id,
            action="mfa.enroll",
            user_id=active.user_id,
            ip=ip,
        )
    return JSONResponse(
        {"otpauth_uri": enrollment.otpauth_uri, "qr_svg": enrollment.qr_svg}, headers=NO_STORE
    )


@router.post("/me/mfa/confirm")
def mfa_confirm(
    body: CodeBody,
    request: Request,
    active: sessions.ActiveSession = Depends(enrollment_session),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    """Confirm enrollment with a valid code; returns 10 recovery codes once. An `enroll_required`
    session is replaced by a `full` one (new token); a `full` session stays as it is."""
    now = services.clock()
    token: str | None = None
    with org_transaction(services, active) as tx:
        try:
            codes = mfa.confirm(
                tx, services.keyring, user_id=active.user_id, code=body.code, now=now
            )
        except mfa.NotEnrolled:
            raise HTTPException(
                status_code=409, detail="no enrollment to confirm; start one first"
            ) from None
        except mfa.InvalidCode:
            raise HTTPException(status_code=400, detail=INVALID_CODE) from None
        mfa.record(
            tx,
            org_id=active.org_id,
            actor=active.user_id,
            action="mfa.confirm",
            user_id=active.user_id,
            ip=ip,
        )
        if active.stage == sessions.ENROLL_REQUIRED:
            sessions.revoke_id(tx, active.id)
            token = sessions.create(
                tx,
                user_id=active.user_id,
                org_id=active.org_id,
                stage=sessions.FULL,
                lifetimes=services.lifetimes,
                now=now,
                ip=ip,
                user_agent=request.headers.get("user-agent"),
            )
    response = JSONResponse({"recovery_codes": codes, "stage": sessions.FULL}, headers=NO_STORE)
    if token is not None:
        sessions.set_cookie(response, token, services.settings)
    return response


@router.delete("/me/mfa", status_code=204)
def mfa_disable(
    body: CodeBody,
    active: sessions.ActiveSession = Depends(full_session),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    """Turn two-factor login off with a current code; refused while the org requires it."""
    with org_transaction(services, active) as tx:
        org = tx.get(Org, active.org_id)
        if org is None:
            raise NOT_AUTHENTICATED
        if org.require_2fa:
            raise HTTPException(
                status_code=403, detail="your organisation requires two-factor login"
            )
        _check_current_code(tx, services, active, body.code, ip)
        mfa.remove(tx, active.user_id)
        mfa.record(
            tx,
            org_id=active.org_id,
            actor=active.user_id,
            action="mfa.disable",
            user_id=active.user_id,
            ip=ip,
        )
    return Response(status_code=204)


@router.post("/me/mfa/recovery-codes")
def mfa_recovery_codes(
    body: CodeBody,
    active: sessions.ActiveSession = Depends(full_session),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    """Replace the recovery codes (a current code required); the new ones are shown once."""
    with org_transaction(services, active) as tx:
        _check_current_code(tx, services, active, body.code, ip)
        codes = mfa.regenerate_recovery_codes(tx, user_id=active.user_id, now=services.clock())
        mfa.record(
            tx,
            org_id=active.org_id,
            actor=active.user_id,
            action="mfa.recovery_regenerate",
            user_id=active.user_id,
            ip=ip,
        )
    return JSONResponse({"recovery_codes": codes}, headers=NO_STORE)


def _weak(exc: passwords.WeakPassword) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


async def _send_quietly(mailer: Mailer, mail: Mail) -> None:
    try:
        await mailer.send(mail)
    except MailError:
        pass  # logged by the mailer; the requester already has their answer


@router.post("/auth/password-reset", status_code=202)
def password_reset(
    body: ResetRequestBody,
    background: BackgroundTasks,
    services: Services = Depends(get_services),
) -> dict:
    email = normalize_email(body.email)
    key = reset_key(email)
    # At most 5 reset mails per address per 15 minutes; above that the request is answered and
    # dropped, so the endpoint cannot be used to flood someone's inbox.
    if services.reset_limiter.blocked(key) is None:
        mail = request_reset(
            services.engine, email=email, settings=services.settings, now=services.clock()
        )
        if mail is not None:
            services.reset_limiter.hit(key)
            background.add_task(_send_quietly, services.mailer, mail)
    return {"status": "accepted"}


@router.post("/auth/password-reset/confirm")
def password_reset_confirm(
    body: SetPasswordBody,
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    try:
        confirm_reset(
            services.engine, token=body.token, password=body.password, now=services.clock(), ip=ip
        )
    except passwords.WeakPassword as exc:
        raise _weak(exc) from None
    except InvalidReset:
        raise HTTPException(status_code=400, detail=INVALID_LINK) from None
    response = JSONResponse({"status": "password changed"})
    sessions.clear_cookie(response, services.settings)
    return response


@router.post("/auth/invite/accept")
def invite_accept(
    body: AcceptInviteBody,
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    try:
        accept_invite(
            services.engine,
            token=body.token,
            password=body.password,
            display_name=body.display_name,
            now=services.clock(),
            ip=ip,
        )
    except passwords.WeakPassword as exc:
        raise _weak(exc) from None
    except InvalidInvite:
        raise HTTPException(status_code=400, detail=INVALID_LINK) from None
    except AccountActive:
        raise HTTPException(
            status_code=409, detail="this account is already active; sign in instead"
        ) from None
    return {"status": "active"}


@router.post("/bootstrap", status_code=201)
def bootstrap_first_owner(
    body: BootstrapBody,
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    """Create the first org and owner; refused (409) once any user exists."""
    try:
        email = canonical_email(body.email)
        done = bootstrap(
            services.engine,
            email=email,
            org_name=body.org_name,
            display_name=body.display_name,
            settings=services.settings,
            now=services.clock(),
            send=lambda mail: anyio.from_thread.run(services.mailer.send, mail),
            ip=ip,
        )
    except ValueError as exc:  # an invalid email or org name
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except AlreadyBootstrapped:
        raise HTTPException(status_code=409, detail="already bootstrapped") from None
    except MailError:
        raise HTTPException(
            status_code=503, detail="the set-password mail could not be sent; nothing was created"
        ) from None
    return {"org_id": str(done.org_id), "user_id": str(done.user_id), "status": "invited"}
