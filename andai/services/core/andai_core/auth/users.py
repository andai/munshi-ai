"""Account state changes that end sessions."""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth import sessions
from andai_core.db.models import User


def disable_user(
    tx: Tx,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: uuid.UUID | None,
    ip: str | None = None,
) -> int:
    """Disable `user_id`, end every session they hold, and audit it in `org_id` (the caller's
    transaction must carry that org as `app.org_id`). Returns the number of sessions ended."""
    tx.execute(update(User).where(User.id == user_id).values(status="disabled"))
    ended = sessions.revoke_all(tx, user_id)
    audit.record(tx, org_id, actor, "user.disable", f"user:{user_id}", ip)
    return ended
