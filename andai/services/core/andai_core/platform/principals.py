"""Each org's one principal on the meeting platform.

An org acts on the meeting platform (dispatching bots, reading transcripts) as a platform user of
its own, `org-<org_id>@principals.munshi.invalid`, with one API key scoped `bot,tx`. `provision`
finds or creates that user through the admin-api (the same user on a retry) and mints the key;
`store` seals the key under the keyring (associated data `platform_principals:<org_id>`) and
inserts the `platform_principals` row, whose primary key is the org id, so an org never has two.

The key is write-only: it exists in clear only between minting and sealing, and inside andai-core
when `open_key` is called to use it. No route returns it and nothing logs it.

`configure` sets the principal's platform settings: every object the platform stores for the org's
meetings goes under the storage prefix `org/<org_id>`, diagnostic signal tapes are off, and the
org's concurrent-bot cap is applied. `create_org` configures a new principal before the org is
committed; `sync` re-applies the same settings to every existing principal (`python -m andai_core
principals-sync`).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.crypto import Keyring
from andai_core.db.models import PlatformPrincipal
from andai_core.db.session import transaction
from andai_core.platform.admin_client import PlatformAdmin, PlatformAdminError

log = logging.getLogger(__name__)

SCOPES = ("bot", "tx")
EMAIL_DOMAIN = "principals.munshi.invalid"
DEFAULT_MAX_CONCURRENT_BOTS = 3


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


def storage_prefix(org_id: uuid.UUID) -> str:
    return f"org/{uuid.UUID(str(org_id))}"


def settings_patch(org_id: uuid.UUID) -> dict:
    """The principal's platform `data`: the org's storage prefix and diagnostic tapes off."""
    return {
        "storage_prefix": storage_prefix(org_id),
        "diagnostics": {"capture_signal": False},
    }


def configure(
    admin: PlatformAdmin, platform_user_id: int, org_id: uuid.UUID, *, max_concurrent_bots: int
) -> None:
    """Apply the org's platform settings to its principal. Idempotent. Raises
    `PlatformAdminError`."""
    admin.update_user(
        platform_user_id, data=settings_patch(org_id), max_concurrent_bots=max_concurrent_bots
    )


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


@dataclass(frozen=True)
class SyncResult:
    orgs: int
    configured: int
    failed: int


def org_ids_with_principal(engine: Engine) -> list[uuid.UUID]:
    """Every org that has a principal, across orgs.

    The app role sees `platform_principals` only for the org in its context, so it cannot list them
    directly; `andai_principal_org_ids()` (migration 0003) is a SECURITY DEFINER function owned by
    the NOLOGIN role `andai_scan`, which may read only the `org_id` column."""
    with transaction(engine) as tx:
        return [uuid.UUID(str(v)) for v in tx.scalars(text("SELECT andai_principal_org_ids()"))]


def sync(engine: Engine, admin: PlatformAdmin, *, max_concurrent_bots: int) -> SyncResult:
    """Re-apply `configure` to every org's principal, one org per transaction (with that org's
    RLS context). An org whose admin-api call fails is counted and the rest continue."""
    configured = failed = 0
    org_ids = org_ids_with_principal(engine)
    for org_id in org_ids:
        try:
            with transaction(engine, org_id=org_id) as tx:
                row = tx.scalar(
                    select(PlatformPrincipal).where(PlatformPrincipal.org_id == org_id)
                )
                if row is None:  # removed since the listing
                    continue
                configure(
                    admin, row.platform_user_id, org_id, max_concurrent_bots=max_concurrent_bots
                )
                audit.record(tx, org_id, None, "platform_principal.configure", f"org:{org_id}")
        except PlatformAdminError as exc:
            failed += 1
            log.warning("could not configure the platform principal of org %s: %s", org_id, exc)
            continue
        configured += 1
    return SyncResult(orgs=len(org_ids), configured=configured, failed=failed)
