"""Append-only audit trail.

`record` writes one `audit_events` row inside the caller's transaction, so the event commits or
rolls back with the action it describes. The app role may only INSERT into `audit_events` (no
SELECT, UPDATE or DELETE), and row-level security requires the row's org to be the transaction's
`app.org_id`. The insert therefore carries no RETURNING clause. Once written, no role can change
or remove a row (migration 0017's triggers).

Every action must be registered in `andai_core.audit_actions`; `record` refuses any other.
"""

from __future__ import annotations

import uuid

from sqlalchemy import insert
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from andai_core.audit_actions import is_registered
from andai_core.db.models import AuditEvent


def record(
    tx: Session | Connection,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    target: str,
    ip: str | None = None,
) -> uuid.UUID:
    """Insert one audit event in `tx` and return its id."""
    if not action or not target:
        raise ValueError("an audit event needs an action and a target")
    if not is_registered(action):
        raise ValueError(f"unregistered audit action {action!r}; add it to audit_actions.ACTIONS")
    event_id = uuid.uuid4()
    tx.execute(
        insert(AuditEvent.__table__).values(
            id=event_id,
            org_id=org_id,
            actor_user_id=actor_user_id,
            action=action,
            target=target,
            ip=ip,
        )
    )
    return event_id
