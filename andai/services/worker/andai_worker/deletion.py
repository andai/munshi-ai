"""Meeting deletion: purge every meeting its users deleted, within 15 minutes (PORT-04).

Every `TICK_S` seconds the runner does two things:
- It calls `andai_claim_deletions(BATCH)` (migration 0009), the only cross-org read it makes. The
  call hands out deleted meetings of any org whose purge is due (`deletion_state = pending` and
  the retry time passed) and leases each one (`FOR UPDATE SKIP LOCKED`), so two workers never purge
  the same meeting at once.
- It runs andai-core's `meetings.deletion.purge` for each claimed meeting under that meeting's org
  (row-level security), with the org's own platform key: the platform's recording objects and
  transcript first, then andai's rows.

Retries, backoff and the `failed` state are `purge`'s. A meeting whose purge fails is logged and
the loop goes on; `munshi_deletions_pending` and `munshi_deletion_oldest_pending_seconds`
(`metrics.py`) show what is still waiting.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio.to_thread
from sqlalchemy import Engine, text

from andai_core.crypto import Keyring
from andai_core.db.session import transaction
from andai_core.meetings.deletion import purge
from andai_core.platform.gateway_client import PlatformGateway

log = logging.getLogger(__name__)

TICK_S = 10.0
BATCH = 10


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TickResult:
    claimed: int = 0
    purged: int = 0
    pending: int = 0
    failed: int = 0


class DeletionRunner:
    def __init__(
        self,
        engine: Engine,
        keyring: Keyring,
        gateway: PlatformGateway,
        *,
        batch: int = BATCH,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.keyring = keyring
        self.gateway = gateway
        self.batch = batch
        self.clock = clock

    def claim(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(org id, meeting id) of the deletions this runner now holds a lease on."""
        with transaction(self.engine) as tx:  # andai_app with no org context
            rows = tx.execute(
                text("SELECT org_id, meeting_id FROM andai_claim_deletions(:n)"),
                {"n": self.batch},
            ).all()
        return [(row.org_id, row.meeting_id) for row in rows]

    async def tick(self) -> TickResult:
        result = TickResult()
        for org_id, meeting_id in await anyio.to_thread.run_sync(self.claim):
            result.claimed += 1
            try:
                done = await purge(
                    self.engine, self.keyring, self.gateway, org_id, meeting_id, clock=self.clock
                )
            except Exception:  # noqa: BLE001 - one meeting must not stop the others
                # Not even the failed attempt could be counted (the database is unreachable): the
                # lease runs out and the meeting is claimed again.
                log.exception("meeting %s: purge failed", meeting_id)
                result.pending += 1
                continue
            if done is None:
                continue
            if done.state == "purged":
                result.purged += 1
            elif done.state == "failed":
                result.failed += 1
            else:
                result.pending += 1
        return result

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("deletion pass failed")
            await asyncio.sleep(every)
