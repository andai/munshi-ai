"""Opaque server-side sessions.

The browser holds a random token in the `andai_session` cookie (httpOnly, SameSite=Lax, Path=/,
Secure outside `ANDAI_ENV=dev`); the `sessions` table holds only its SHA-256. A session ends at
the first of: its absolute expiry (`expires_at`, set at login), `idle` without a request
(`last_seen_at`), logout, or `revoke_all` (password reset, disabling the user).

`stage` says how far the login got: `full` is a complete login; the two-factor stages admit only
their own routes (`stage_gate.py`). An `mfa_pending` session (password right, code not yet
entered) lives at most 5 minutes; an `enroll_required` session (the org requires two-factor login
and the user has not set it up) has the ordinary lifetimes.

`sessions` and `users` are global tables (no row-level security); every query here filters by
token hash or user id.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import Response
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as Tx

from andai_core.auth.tokens import hash_token, new_token
from andai_core.db.models import Session, User
from andai_core.settings import Settings

COOKIE_NAME = "andai_session"
FULL = "full"
MFA_PENDING = "mfa_pending"
ENROLL_REQUIRED = "enroll_required"
STAGES = (MFA_PENDING, ENROLL_REQUIRED, FULL)
PENDING_TTL = timedelta(minutes=5)
# `last_seen_at` is written at most this often, so a busy session is not an UPDATE per request.
TOUCH_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True)
class Lifetimes:
    idle: timedelta
    absolute: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> Lifetimes:
        return cls(
            idle=timedelta(seconds=settings.session_idle_seconds),
            absolute=timedelta(seconds=settings.session_max_seconds),
        )


@dataclass(frozen=True)
class ActiveSession:
    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    stage: str
    email: str
    display_name: str


def _inet(ip: str | None) -> str | None:
    if ip is None:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def create(
    tx: Tx,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    stage: str,
    lifetimes: Lifetimes,
    now: datetime,
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Insert a new session and return its token (the only time the token exists in clear)."""
    if stage not in STAGES:
        raise ValueError(f"unknown session stage {stage!r}")
    lifetime = min(lifetimes.absolute, PENDING_TTL) if stage == MFA_PENDING else lifetimes.absolute
    token, token_hash = new_token()
    tx.add(
        Session(
            id=uuid.uuid4(),
            token_hash=token_hash,
            user_id=user_id,
            org_id=org_id,
            stage=stage,
            created_at=now,
            last_seen_at=now,
            expires_at=now + lifetime,
            ip=_inet(ip),
            user_agent=None if user_agent is None else user_agent[:512],
        )
    )
    tx.flush()
    return token


def resolve(tx: Tx, token: str, *, lifetimes: Lifetimes, now: datetime) -> ActiveSession | None:
    """The live session for `token`, or None. An expired session, or one whose user is no longer
    active, is deleted on sight."""
    row = tx.execute(
        select(Session, User)
        .join(User, User.id == Session.user_id)
        .where(Session.token_hash == hash_token(token))
    ).first()
    if row is None:
        return None
    session, user = row
    if (
        now >= session.expires_at
        or now - session.last_seen_at >= lifetimes.idle
        or user.status != "active"
    ):
        tx.execute(delete(Session).where(Session.id == session.id))
        return None
    if now - session.last_seen_at >= TOUCH_INTERVAL:
        tx.execute(update(Session).where(Session.id == session.id).values(last_seen_at=now))
    return ActiveSession(
        id=session.id,
        user_id=user.id,
        org_id=session.org_id,
        stage=session.stage,
        email=user.email,
        display_name=user.display_name,
    )


def revoke(tx: Tx, token: str) -> bool:
    """End the session holding `token`; True if there was one."""
    result = tx.execute(delete(Session).where(Session.token_hash == hash_token(token)))
    return result.rowcount > 0


def revoke_id(tx: Tx, session_id: uuid.UUID) -> None:
    tx.execute(delete(Session).where(Session.id == session_id))


def revoke_all(tx: Tx, user_id: uuid.UUID) -> int:
    """End every session of `user_id`; returns how many there were."""
    result = tx.execute(delete(Session).where(Session.user_id == user_id))
    return result.rowcount


def revoke_in_org(tx: Tx, org_id: uuid.UUID, user_ids: list[uuid.UUID]) -> int:
    """End the sessions of `user_ids` that are signed in to `org_id`; returns how many."""
    if not user_ids:
        return 0
    result = tx.execute(
        delete(Session).where(Session.org_id == org_id, Session.user_id.in_(user_ids))
    )
    return result.rowcount


def set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_max_seconds,
        path="/",
        httponly=True,
        samesite="lax",
        secure=not settings.is_dev,
    )


def clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=not settings.is_dev
    )
