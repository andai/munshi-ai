"""Transcript ingest: andai's own copy of every finished meeting's transcript and attendees.

Every `TICK_S` seconds the runner does two things:
- It calls `andai_claim_ingest(BATCH)`, the only cross-org read (migration 0006). The call hands
  out finished meetings of any org that have a platform id and are due: their transcript state is
  `none` or `pending`, they have had fewer than 8 attempts, and their retry time has passed. It
  leases each one (`FOR UPDATE SKIP LOCKED`), so two workers never take the same meeting.
- It runs andai-core's `meetings.ingest.ingest` for each claimed meeting under that meeting's org
  (row-level security), with the org's own platform key.

Retries, backoff and the final state are `ingest`'s. A failed meeting is logged and the loop goes
on.
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
from andai_core.meetings.ingest import ingest
from andai_core.platform.gateway_client import GatewayError, PlatformGateway

log = logging.getLogger(__name__)

TICK_S = 5.0
BATCH = 10


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TickResult:
    claimed: int = 0
    stored: int = 0
    pending: int = 0
    failed: int = 0


class IngestRunner:
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
        """(org id, meeting id) of the meetings this runner now holds a lease on."""
        with transaction(self.engine) as tx:  # andai_app with no org context
            rows = tx.execute(
                text("SELECT org_id, meeting_id FROM andai_claim_ingest(:n)"), {"n": self.batch}
            ).all()
        return [(row.org_id, row.meeting_id) for row in rows]

    async def tick(self) -> TickResult:
        result = TickResult()
        for org_id, meeting_id in await anyio.to_thread.run_sync(self.claim):
            result.claimed += 1
            try:
                done = await ingest(
                    self.engine, self.keyring, self.gateway, org_id, meeting_id, clock=self.clock
                )
            except GatewayError:
                result.failed += 1  # counted and logged by ingest; retried after the backoff
                continue
            except Exception:  # noqa: BLE001 - one meeting must not stop the others
                log.exception("meeting %s: transcript ingest failed", meeting_id)
                result.failed += 1
                continue
            if done is None:
                continue
            if done.state == "stored":
                result.stored += 1
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
                log.exception("ingest pass failed")
            await asyncio.sleep(every)
