"""Invite rate limits (D-58, INV-03): sliding one-hour windows in the stack's Valkey.

Three counters, each an andai-core `ValkeyLimiter` (`MemoryLimiter` in tests):

* `intake:peer:<ip>`: every message from one delivering IP, verified or not
  (`ANDAI_INTAKE_RATE_PEER_PER_H`, 120). It is checked first, so a flood costs no DNS.
* `intake:sender:<sha256>`: verified mail from one sender (`ANDAI_INTAKE_RATE_SENDER_PER_H`, 20).
  The address is hashed, so Valkey never holds it.
* `intake:org:<id>`: verified mail for one org (`ANDAI_INTAKE_RATE_ORG_PER_H`, 200).

Only mail that passed the sender check counts against a sender or an org. A forged From therefore
cannot use up a member's quota; unverified mail is bounded by the peer limit alone. A message over
any limit is quarantined `rate_limited`. A refused message is not counted, so a sender who keeps
sending is admitted again as their older messages leave the window.

`admit_*` checks and then counts; the two steps are not atomic. Two processors racing on the last
slot can let one extra message through. The intake runs one processor loop.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import redis

from andai_core.auth.ratelimit import Limiter, MemoryLimiter, ValkeyLimiter
from andai_intake.auth.verdict import SenderAuth

WINDOW_SECONDS = 3600
SENDER_PER_H = 20
ORG_PER_H = 200
PEER_PER_H = 120
PREFIX = "andai:limit:"


def sender_key(sender: str) -> str:
    digest = hashlib.sha256(f"intake-sender\x00{sender.strip().lower()}".encode()).hexdigest()
    return f"intake:sender:{digest}"


def org_key(org_id: uuid.UUID) -> str:
    return f"intake:org:{org_id}"


def peer_key(peer_ip: str | None) -> str:
    return f"intake:peer:{peer_ip or '-'}"


@dataclass
class IntakeLimits:
    sender: Limiter
    org: Limiter
    peer: Limiter

    @classmethod
    def valkey(
        cls,
        client: redis.Redis,
        *,
        sender_per_h: int = SENDER_PER_H,
        org_per_h: int = ORG_PER_H,
        peer_per_h: int = PEER_PER_H,
        clock: Callable[[], float] | None = None,
    ) -> IntakeLimits:
        extra = {} if clock is None else {"clock": clock}

        def make(limit: int) -> ValkeyLimiter:
            return ValkeyLimiter(client, limit, WINDOW_SECONDS, prefix=PREFIX, **extra)

        return cls(make(sender_per_h), make(org_per_h), make(peer_per_h))

    @classmethod
    def from_settings(cls, settings) -> IntakeLimits:
        client = redis.Redis.from_url(
            settings.redis_url, socket_timeout=5, socket_connect_timeout=5
        )
        return cls.valkey(
            client,
            sender_per_h=settings.intake_rate_sender_per_h,
            org_per_h=settings.intake_rate_org_per_h,
            peer_per_h=settings.intake_rate_peer_per_h,
        )

    @classmethod
    def memory(
        cls,
        *,
        sender_per_h: int = SENDER_PER_H,
        org_per_h: int = ORG_PER_H,
        peer_per_h: int = PEER_PER_H,
        clock: Callable[[], float] | None = None,
    ) -> IntakeLimits:
        extra = {} if clock is None else {"clock": clock}

        def make(limit: int) -> MemoryLimiter:
            return MemoryLimiter(limit, WINDOW_SECONDS, **extra)

        return cls(make(sender_per_h), make(org_per_h), make(peer_per_h))

    def admit_peer(self, peer_ip: str | None) -> bool:
        """Count one message from `peer_ip`; False (not counted) when it is over its limit."""
        key = peer_key(peer_ip)
        if self.peer.blocked(key) is not None:
            return False
        self.peer.hit(key)
        return True

    def admit_verified(self, sender: str, org_id: uuid.UUID) -> bool:
        """Count one verified message against its sender and org; False (nothing counted) when
        either is over its limit."""
        keys = (
            (self.sender, sender_key(sender)),
            (self.org, org_key(org_id)),
        )
        if any(limiter.blocked(key) is not None for limiter, key in keys):
            return False
        for limiter, key in keys:
            limiter.hit(key)
        return True

    def admit_checked(self, auth: SenderAuth, org_id: uuid.UUID | None) -> bool:
        """The sender and org limits for a checked message. Mail that failed the check, or has no
        resolved org, is not counted: it is quarantined for its own reason."""
        if not auth.verified or auth.sender is None or org_id is None:
            return True
        return self.admit_verified(auth.sender, org_id)
