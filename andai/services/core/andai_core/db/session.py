"""Database access for andai-core.

The application connects only as the RLS-bound app role (`ANDAI_DB_URL_APP`). Every unit of work
runs in `transaction()`, which sets the row-level-security context for that transaction alone:

    with transaction(engine, org_id=org.id, user_id=user.id) as tx:
        tx.execute(select(Team))

The context is set with `set_config(name, value, is_local => true)`, the parameterised form of
`SET LOCAL`: Postgres discards it at COMMIT or ROLLBACK, so it never survives into the next
transaction on a pooled connection. Policies read it with `current_setting(name, true)`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from andai_core.settings import Settings, get_settings

ORG_ID = "app.org_id"
USER_ID = "app.user_id"
INVITE_TOKEN_HASH = "app.invite_token_hash"


def create_app_engine(url: str | None = None, *, settings: Settings | None = None) -> Engine:
    """Engine for the app role. Without `url`, reads `ANDAI_DB_URL_APP`."""
    if url is None:
        secret = (settings or get_settings()).db_url_app
        if secret is None:
            raise RuntimeError("ANDAI_DB_URL_APP is not set")
        url = secret.get_secret_value()
    return create_engine(url, pool_pre_ping=True)


def set_context(
    tx: Session,
    *,
    org_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    invite_token_hash: str | None = None,
) -> None:
    """Set the transaction-local RLS context. Only the values given are set."""
    values = {
        ORG_ID: None if org_id is None else str(uuid.UUID(str(org_id))),
        USER_ID: None if user_id is None else str(uuid.UUID(str(user_id))),
        INVITE_TOKEN_HASH: invite_token_hash,
    }
    for name, value in values.items():
        if value is not None:
            tx.execute(
                text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
            )


@contextmanager
def transaction(
    engine: Engine,
    *,
    org_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    invite_token_hash: str | None = None,
) -> Iterator[Session]:
    """One transaction with its RLS context; commits on success, rolls back on error."""
    with Session(engine, expire_on_commit=False) as tx, tx.begin():
        set_context(tx, org_id=org_id, user_id=user_id, invite_token_hash=invite_token_hash)
        yield tx
