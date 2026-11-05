"""Who sent a message, and which org's admins see it when it is quarantined (D-58).

The intake connects as `andai_app` and reads identity only through two SECURITY DEFINER functions
(migration 0013). It never holds a broader database role.

`resolve(engine, sender)` maps a From address to a member. It succeeds only when the address belongs
to an active user with a membership in exactly one org; it returns that user and org, and the team
when the user is in exactly one team there. Anything else is `unknown_user`:

* no active user with that address (none, disabled, or invited and never signed in), or one without
  a membership: the org is attributed by `attribute_org`;
* an active user in several orgs: the org stays NULL, because nothing in the mail says which org it
  is for. This is a known limit; the message is shown to the operator only.

`attribute_org(engine, claimed_from)` names the org whose admins see a quarantined message. That is
the org of the claimed From address when it is an active member of exactly one org, which holds for
a forged From too (`sender_unverified`). Otherwise it is the one org whose `allowed_email_domains`
holds the address's domain. When several orgs claim the domain, when the address is a member of
several orgs, or when nothing matches, it is NULL: an operator-only quarantine.

Addresses are matched as `users.email` is stored: lower-case. Plus-addressing is folded only toward a
stored address without a `+`: `ana+notes@x` is tried as itself first, then as `ana@x`. A stored
`ana+work@x` is matched only exactly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Engine, text

from andai_core.db.session import transaction


@dataclass(frozen=True)
class Membership:
    user_id: uuid.UUID
    org_id: uuid.UUID
    team_id: uuid.UUID | None


@dataclass(frozen=True)
class Resolution:
    user_id: uuid.UUID | None
    org_id: uuid.UUID | None
    team_id: uuid.UUID | None
    reason: str | None  # None when resolved, else "unknown_user"

    @property
    def resolved(self) -> bool:
        return self.reason is None


def candidates(sender: str) -> list[str]:
    """The addresses tried for `sender`, in order: as given, then without a `+tag`."""
    address = sender.strip().lower()
    local, at, domain = address.rpartition("@")
    tried = [address]
    if at and "+" in local:
        base = local.split("+", 1)[0]
        if base:
            tried.append(f"{base}@{domain}")
    return tried


def domain_of(address: str) -> str:
    return address.strip().rpartition("@")[2].rstrip(".").lower()


def memberships(engine: Engine, sender: str) -> list[Membership]:
    """The memberships of the active user `sender` names, after case and plus folding."""
    with transaction(engine) as tx:
        for address in candidates(sender):
            rows = tx.execute(
                text("SELECT user_id, org_id, team_id FROM andai_resolve_sender(:e)"),
                {"e": address},
            ).all()
            if rows:
                return [Membership(r.user_id, r.org_id, r.team_id) for r in rows]
    return []


def orgs_for_domain(engine: Engine, domain: str) -> list[uuid.UUID]:
    with transaction(engine) as tx:
        return list(
            tx.execute(text("SELECT andai_orgs_for_domain(:d)"), {"d": domain}).scalars()
        )


def _attribute(
    engine: Engine, sender: str, found: list[Membership]
) -> uuid.UUID | None:
    if len(found) == 1:
        return found[0].org_id
    if len(found) > 1:
        return None
    orgs = orgs_for_domain(engine, domain_of(sender))
    return orgs[0] if len(orgs) == 1 else None


def attribute_org(engine: Engine, claimed_from: str | None) -> uuid.UUID | None:
    """The org whose admins see a quarantined message from `claimed_from`, or None."""
    if not claimed_from or "@" not in claimed_from:
        return None
    return _attribute(engine, claimed_from, memberships(engine, claimed_from))


def resolve(engine: Engine, sender: str) -> Resolution:
    """The member `sender` (an authenticated From address) is, or `unknown_user`."""
    found = memberships(engine, sender)
    if len(found) == 1:
        only = found[0]
        return Resolution(only.user_id, only.org_id, only.team_id, None)
    return Resolution(None, _attribute(engine, sender, found), None, "unknown_user")
