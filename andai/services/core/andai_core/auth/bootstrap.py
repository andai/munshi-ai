"""First-run bootstrap: the first org and its owner.

Allowed only while the `users` table is empty (checked under a transaction-scoped advisory lock,
so two concurrent bootstraps cannot both pass). Creates the org, the owner as an `invited` user
with an owner membership, and an owner invite whose link is mailed so the owner sets a password.
The mail is sent before the transaction commits: if the relay refuses it, nothing is created and
bootstrap can be run again.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select, text

from andai_core import audit
from andai_core.auth.invites import create_invite, invite_mail
from andai_core.db.models import Org, OrgMembership, User
from andai_core.db.session import set_context, transaction
from andai_core.mail import Mail
from andai_core.settings import Settings

# pg_advisory_xact_lock key for bootstrap ("andaiboo" as a big-endian int64).
BOOTSTRAP_LOCK = int.from_bytes(b"andaiboo", "big", signed=True)
MAX_ORG_NAME = 200


class AlreadyBootstrapped(Exception):
    """Users exist; bootstrap has done its job."""


@dataclass(frozen=True)
class Bootstrapped:
    org_id: uuid.UUID
    user_id: uuid.UUID


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:63].strip("-") or "org"


def bootstrap(
    engine: Engine,
    *,
    email: str,
    org_name: str,
    display_name: str | None,
    settings: Settings,
    now: datetime,
    send: Callable[[Mail], None],
    ip: str | None = None,
) -> Bootstrapped:
    """`email` must be canonical (`links.canonical_email`)."""
    org_name = org_name.strip()
    if not org_name or len(org_name) > MAX_ORG_NAME:
        raise ValueError(f"the org name must be 1 to {MAX_ORG_NAME} characters")
    with transaction(engine) as tx:
        tx.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BOOTSTRAP_LOCK})
        if tx.scalar(select(User.id).limit(1)) is not None:
            raise AlreadyBootstrapped
        org_id, user_id = uuid.uuid4(), uuid.uuid4()
        set_context(tx, org_id=org_id)
        tx.add(Org(id=org_id, name=org_name, slug=slugify(org_name)))
        tx.add(
            User(
                id=user_id,
                email=email,
                display_name=(display_name or "").strip() or email.split("@")[0],
            )
        )
        tx.flush()
        tx.add(OrgMembership(org_id=org_id, user_id=user_id, role="owner"))
        tx.flush()
        token = create_invite(
            tx,
            org_id=org_id,
            email=email,
            role="owner",
            invited_by=None,
            settings=settings,
            now=now,
        )
        audit.record(tx, org_id, None, "org.bootstrap", f"org:{org_id}", ip)
        send(invite_mail(settings, email=email, org_name=org_name, token=token, owner=True))
    return Bootstrapped(org_id=org_id, user_id=user_id)
