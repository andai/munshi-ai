"""Invitations: a single-use emailed link that lets a person set a password and join an org.

`create_invite` (run in a transaction whose `app.org_id` is the inviting org) records the invite
with only the SHA-256 of its token and creates the user as `invited` if they are new. Membership
is granted only when the invite is accepted. `accept_invite` finds the invite through the
`app.invite_token_hash` pre-context policy (the org is unknown until then), consumes it, sets
the password, activates the user and adds the membership with the invite's role.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth import passwords
from andai_core.auth.links import portal_link
from andai_core.auth.tokens import hash_token, new_token
from andai_core.db.models import Invite, OrgMembership, User
from andai_core.db.session import set_context, transaction
from andai_core.mail import Mail
from andai_core.settings import Settings


class InvalidInvite(Exception):
    """Unknown, expired or already used invite (one message for all three)."""


class AccountActive(Exception):
    """The invited address already has an active account; it signs in instead."""


@dataclass(frozen=True)
class Accepted:
    org_id: uuid.UUID
    user_id: uuid.UUID


def create_invite(
    tx: Tx,
    *,
    org_id: uuid.UUID,
    email: str,
    role: str,
    invited_by: uuid.UUID | None,
    settings: Settings,
    now: datetime,
) -> str:
    """Record an invite for the canonical `email` and return its token (never stored)."""
    if tx.scalar(select(User.id).where(User.email == email)) is None:
        tx.add(User(id=uuid.uuid4(), email=email, display_name=email.split("@")[0]))
        tx.flush()
    token, token_hash = new_token()
    tx.add(
        Invite(
            id=uuid.uuid4(),
            org_id=org_id,
            email=email,
            role=role,
            token_hash=token_hash,
            invited_by=invited_by,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.invite_ttl_seconds),
        )
    )
    tx.flush()
    return token


def invite_mail(settings: Settings, *, email: str, org_name: str, token: str, owner: bool) -> Mail:
    link = portal_link(settings, "invite", token)
    days = max(1, settings.invite_ttl_seconds // 86400)
    if owner:
        subject = f"Set your munshi.ai password for {org_name}"
        intro = f"You are the owner of {org_name} on munshi.ai."
    else:
        subject = f"You are invited to {org_name} on munshi.ai"
        intro = f"You have been invited to join {org_name} on munshi.ai."
    text = (
        f"{intro}\n\n"
        f"Set your password here (the link works once and expires in {days} days):\n\n"
        f"{link}\n\n"
        "If you did not expect this message, ignore it.\n"
    )
    return Mail(to=email, subject=subject, text=text)


def accept_invite(
    engine: Engine,
    *,
    token: str,
    password: str,
    display_name: str | None,
    now: datetime,
    ip: str | None = None,
) -> Accepted:
    passwords.check_new(password)
    token_hash = hash_token(token)
    with transaction(engine, invite_token_hash=token_hash) as tx:
        invite = tx.scalar(select(Invite).where(Invite.token_hash == token_hash))
        if invite is None or invite.accepted_at is not None or invite.expires_at <= now:
            raise InvalidInvite
        set_context(tx, org_id=invite.org_id)
        user = tx.scalar(select(User).where(User.email == invite.email).with_for_update())
        if user is None or user.status == "disabled":
            raise InvalidInvite
        if user.status == "active":
            raise AccountActive
        claimed = tx.execute(
            update(Invite)
            .where(Invite.id == invite.id, Invite.accepted_at.is_(None))
            .values(accepted_at=now)
        ).rowcount
        if claimed != 1:
            raise InvalidInvite
        user.password_hash = passwords.hash_password(password)
        user.status = "active"
        if display_name and display_name.strip():
            user.display_name = display_name.strip()
        tx.execute(
            insert(OrgMembership)
            .values(org_id=invite.org_id, user_id=user.id, role=invite.role)
            .on_conflict_do_nothing()
        )
        audit.record(tx, invite.org_id, user.id, "invite.accept", f"user:{user.id}", ip)
        return Accepted(org_id=invite.org_id, user_id=user.id)
