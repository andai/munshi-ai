"""Each org's one principal on the meeting platform.

An org acts on the meeting platform (dispatching bots, reading transcripts) as a platform user of
its own, `org-<org_id>@principals.munshi.invalid`, with one API key scoped `bot,tx`. `provision`
finds or creates that user through the admin-api (the same user on a retry) and mints the key;
`store` seals the key under the keyring (associated data `platform_principals:<org_id>`) and
inserts the `platform_principals` row, whose primary key is the org id, so an org never has two.

The key is write-only: it exists in clear only between minting and sealing, and inside andai-core
when `open_key` is called to use it. No route returns it and nothing logs it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as Tx

from andai_core.crypto import Keyring
from andai_core.db.models import PlatformPrincipal
from andai_core.platform.admin_client import PlatformAdmin, PlatformAdminError

log = logging.getLogger(__name__)

SCOPES = ("bot", "tx")
EMAIL_DOMAIN = "principals.munshi.invalid"


@dataclass(frozen=True)
class Minted:
    platform_user_id: int
    token_id: int
    key: str = field(repr=False)


def principal_email(org_id: uuid.UUID) -> str:
    return f"org-{org_id}@{EMAIL_DOMAIN}"


def aad(org_id: uuid.UUID) -> str:
    return f"platform_principals:{org_id}"


def provision(admin: PlatformAdmin, org_id: uuid.UUID) -> Minted:
    """Find or create the org's platform user and mint its key. Raises `PlatformAdminError`."""
    user_id = admin.ensure_user(principal_email(org_id))
    token = admin.mint_token(user_id, SCOPES)
    return Minted(platform_user_id=user_id, token_id=token.id, key=token.token)


def discard(admin: PlatformAdmin, minted: Minted) -> None:
    """Revoke a minted key that was never stored (best effort: a failure is logged)."""
    try:
        admin.revoke_token(minted.token_id)
    except PlatformAdminError as exc:
        log.warning("could not revoke unused platform token %s: %s", minted.token_id, exc)


def store(tx: Tx, keyring: Keyring, org_id: uuid.UUID, minted: Minted, now: datetime) -> bool:
    """Seal and insert the org's principal (the transaction's `app.org_id` must be `org_id`).
    Returns False, inserting nothing, if the org already has one."""
    sealed = keyring.seal(minted.key, aad(org_id))
    table = PlatformPrincipal.__table__
    inserted = tx.execute(
        insert(table)
        .values(
            org_id=org_id,
            platform_user_id=minted.platform_user_id,
            key_ciphertext=sealed.ciphertext,
            key_id=sealed.key_id,
            created_at=now,
            rotated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["org_id"])
        .returning(table.c.org_id)
    ).first()
    return inserted is not None


def open_key(tx: Tx, keyring: Keyring, org_id: uuid.UUID) -> str | None:
    """The org's platform key in clear, for andai-core's own calls to the platform."""
    row = tx.scalar(select(PlatformPrincipal).where(PlatformPrincipal.org_id == org_id))
    if row is None:
        return None
    return keyring.open(row.key_ciphertext, row.key_id, aad(org_id)).decode()
