"""Password reset by email.

`request_reset` issues a 30-minute, single-use link for an active account and returns the mail to
send, or None for an unknown, invited or disabled address; the caller answers the same either
way. Issuing a link voids any earlier unused link of that user. `confirm_reset` consumes the
link, sets the new password and ends every session of the user.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth import passwords, sessions
from andai_core.auth.links import portal_link
from andai_core.auth.tokens import hash_token, new_token
from andai_core.db.models import OrgMembership, PasswordReset, User
from andai_core.db.session import set_context, transaction
from andai_core.mail import Mail
from andai_core.settings import Settings


class InvalidReset(Exception):
    """Unknown, expired or already used link (one message for all three)."""


def reset_mail(settings: Settings, *, email: str, token: str) -> Mail:
    minutes = max(1, settings.reset_ttl_seconds // 60)
    text = (
        "Someone asked to reset the password of your munshi.ai account.\n\n"
        f"Choose a new password here (the link works once and expires in {minutes} minutes):\n\n"
        f"{portal_link(settings, 'reset-password', token)}\n\n"
        "Setting a new password signs you out everywhere. If you did not ask for this, ignore\n"
        "this message; your password stays as it is.\n"
    )
    return Mail(to=email, subject="Reset your munshi.ai password", text=text)


def request_reset(engine: Engine, *, email: str, settings: Settings, now: datetime) -> Mail | None:
    with transaction(engine) as tx:
        user = tx.scalar(select(User).where(User.email == email))
        if user is None or user.status != "active":
            return None
        tx.execute(
            update(PasswordReset)
            .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
            .values(used_at=now)
        )
        token, token_hash = new_token()
        tx.add(
            PasswordReset(
                id=uuid.uuid4(),
                user_id=user.id,
                token_hash=token_hash,
                created_at=now,
                expires_at=now + timedelta(seconds=settings.reset_ttl_seconds),
            )
        )
    return reset_mail(settings, email=user.email, token=token)


def _audit_in_every_org(tx: Tx, user_id: uuid.UUID, action: str, ip: str | None) -> None:
    set_context(tx, user_id=user_id)
    org_ids = tx.scalars(
        select(OrgMembership.org_id).where(OrgMembership.user_id == user_id)
    ).all()
    for org_id in org_ids:
        set_context(tx, org_id=org_id)
        audit.record(tx, org_id, user_id, action, f"user:{user_id}", ip)


def confirm_reset(
    engine: Engine, *, token: str, password: str, now: datetime, ip: str | None = None
) -> int:
    """Set the new password; returns how many sessions were ended."""
    passwords.check_new(password)
    with transaction(engine) as tx:
        reset = tx.scalar(
            select(PasswordReset)
            .where(PasswordReset.token_hash == hash_token(token))
            .with_for_update()
        )
        if reset is None or reset.used_at is not None or reset.expires_at <= now:
            raise InvalidReset
        user = tx.get(User, reset.user_id, with_for_update=True)
        if user is None or user.status != "active":
            raise InvalidReset
        reset.used_at = now
        user.password_hash = passwords.hash_password(password)
        ended = sessions.revoke_all(tx, user.id)
        _audit_in_every_org(tx, user.id, "password.reset", ip)
        return ended
