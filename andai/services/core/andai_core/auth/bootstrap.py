"""First-run bootstrap: the first org and its owner.

Allowed only while the `users` table is empty (checked under a transaction-scoped advisory lock,
so two concurrent bootstraps cannot both pass). The org is created by `org.service.create_org`
(with its platform principal), the owner as an `invited` user with an owner membership, and an
owner invite whose link is mailed so the owner sets a password. The mail is sent before the
transaction commits: if the relay refuses it, or the platform refuses the principal, nothing is
created and bootstrap can be run again.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth.invites import create_invite, invite_mail
from andai_core.crypto import Keyring
from andai_core.db.models import User
from andai_core.mail import Mail
from andai_core.org.service import CreatedOrg, create_org
from andai_core.platform.admin_client import PlatformAdmin
from andai_core.settings import Settings

# pg_advisory_xact_lock key for bootstrap ("andaiboo" as a big-endian int64).
BOOTSTRAP_LOCK = int.from_bytes(b"andaiboo", "big", signed=True)


class AlreadyBootstrapped(Exception):
    """Users exist; bootstrap has done its job."""


@dataclass(frozen=True)
class Bootstrapped:
    org_id: uuid.UUID
    user_id: uuid.UUID


def _only_on_an_empty_database(tx: Tx) -> None:
    tx.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BOOTSTRAP_LOCK})
    if tx.scalar(select(User.id).limit(1)) is not None:
        raise AlreadyBootstrapped


def bootstrap(
    engine: Engine,
    *,
    email: str,
    org_name: str,
    display_name: str | None,
    settings: Settings,
    now: datetime,
    send: Callable[[Mail], None],
    admin: PlatformAdmin,
    keyring: Keyring,
    ip: str | None = None,
) -> Bootstrapped:
    """`email` must be canonical (`links.canonical_email`). Raises `ValueError` for a bad org
    name, `AlreadyBootstrapped`, `MailError` or `PlatformAdminError`."""

    def invite_the_owner(tx: Tx, created: CreatedOrg) -> None:
        token = create_invite(
            tx,
            org_id=created.org_id,
            email=email,
            role="owner",
            invited_by=None,
            settings=settings,
            now=now,
        )
        audit.record(tx, created.org_id, None, "org.bootstrap", f"org:{created.org_id}", ip)
        send(invite_mail(settings, email=email, org_name=org_name.strip(), token=token, owner=True))

    created = create_org(
        engine,
        name=org_name,
        owner_email=email,
        owner_display_name=display_name,
        admin=admin,
        keyring=keyring,
        now=now,
        ip=ip,
        guard=_only_on_an_empty_database,
        finish=invite_the_owner,
    )
    return Bootstrapped(org_id=created.org_id, user_id=created.owner_id)
