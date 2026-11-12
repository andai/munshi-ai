"""User erasure: work every erasure request forward, within the 24-hour SLA (OPS-02).

Every `TICK_S` seconds the runner calls `andai_claim_erasures(BATCH, now)` (migration 0019), the
only cross-org read it makes: it hands out open requests of any org that are due, sets them
`running` and leases each one (`FOR UPDATE SKIP LOCKED`), so two workers never work the same
request at once. Each
claimed request goes through andai-core's `erasure.runner.run` under its audit org: stop the
person's live meetings, hide their meetings for the meeting purge (which `DeletionRunner` does,
platform first), scrub the org's other rows, anonymise the account when it is due, and audit the
result.

Waiting, retries, backoff and `failed` are the runner's. A request that raises here is logged and
the loop goes on; its lease runs out and it is claimed again. `munshi_erasures_pending`,
`munshi_erasure_oldest_pending_seconds` and `munshi_erasures_failed` (`metrics.py`) show what is
still waiting.
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

from andai_core.db.session import transaction
from andai_core.erasure.runner import run
from andai_core.meetings.service import Meetings

log = logging.getLogger(__name__)

TICK_S = 10.0
BATCH = 5


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TickResult:
    claimed: int = 0
    done: int = 0
    running: int = 0
    failed: int = 0


class ErasureRunner:
    def __init__(
        self,
        engine: Engine,
        meetings: Meetings,
        *,
        batch: int = BATCH,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.meetings = meetings
        self.batch = batch
        self.clock = clock

    def claim(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(audit org id, request id) of the requests this runner now holds a lease on."""
        with transaction(self.engine) as tx:  # andai_app with no org context
            rows = tx.execute(
                text(
                    "SELECT audit_org_id, request_id FROM andai_claim_erasures(:n, :at)"
                ),
                {"n": self.batch, "at": self.clock()},
            ).all()
        return [(row.audit_org_id, row.request_id) for row in rows]

    async def tick(self) -> TickResult:
        result = TickResult()
        for audit_org_id, request_id in await anyio.to_thread.run_sync(self.claim):
            result.claimed += 1
            try:
                state = await run(
                    self.engine, self.meetings, audit_org_id, request_id, clock=self.clock
                )
            except Exception:  # noqa: BLE001 - one request must not stop the others
                # Not even the failed attempt could be counted (the database is unreachable): the
                # lease runs out and the request is claimed again.
                log.exception("erasure %s: run failed", request_id)
                result.running += 1
                continue
            if state == "done":
                result.done += 1
            elif state == "failed":
                result.failed += 1
            elif state == "running":
                result.running += 1
        return result

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("erasure pass failed")
            await asyncio.sleep(every)
