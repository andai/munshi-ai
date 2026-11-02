"""Recap mail: the requester and the meeting's team get the first minutes once (MOM-05).

Every `TICK_S` seconds the runner calls `andai_claim_recap(BATCH)` (migration 0010), the only
cross-org read. It hands out meetings of any org whose recap is `pending` and due, with fewer than
5 attempts, not deleted and not canaries, and leases each for 2 minutes with a fresh token, so two
workers never mail the same meeting. For each claimed meeting it runs andai-core's
`meetings.recap.send_one` under that meeting's org (row-level security) with the deployment's SMTP
relay. Audience, body, send-once and backoff are `send_one`'s.

A recap becomes `pending` only when a meeting's first minutes are stored, so the runner always
runs: with the LLM route off it simply finds nothing to send.
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
from andai_core.mail import Mailer
from andai_core.meetings.mom_writer import DEFAULT_TIMEZONE
from andai_core.meetings.recap import send_one

log = logging.getLogger(__name__)

TICK_S = 5.0
BATCH = 10


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TickResult:
    claimed: int = 0
    sent: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    lost: int = 0


class RecapRunner:
    def __init__(
        self,
        engine: Engine,
        mailer: Mailer,
        *,
        portal_origin: str,
        batch: int = BATCH,
        timezone: str = DEFAULT_TIMEZONE,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.mailer = mailer
        self.portal_origin = portal_origin
        self.batch = batch
        self.timezone = timezone
        self.clock = clock

    def claim(self) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
        """(org id, meeting id, lease token) of the recaps this runner now holds a lease on."""
        with transaction(self.engine) as tx:  # andai_app with no org context
            rows = tx.execute(
                text("SELECT org_id, meeting_id, lease FROM andai_claim_recap(:n)"),
                {"n": self.batch},
            ).all()
        return [(row.org_id, row.meeting_id, row.lease) for row in rows]

    async def tick(self) -> TickResult:
        result = TickResult()
        for org_id, meeting_id, lease in await anyio.to_thread.run_sync(self.claim):
            result.claimed += 1
            try:
                state = await send_one(
                    self.engine,
                    self.mailer,
                    org_id,
                    meeting_id,
                    lease,
                    portal_origin=self.portal_origin,
                    timezone=self.timezone,
                    clock=self.clock,
                )
            except Exception:  # noqa: BLE001 - one meeting must not stop the others
                log.exception("meeting %s: recap run failed", meeting_id)
                state = "pending"
            setattr(result, state, getattr(result, state) + 1)
        return result

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("recap pass failed")
            await asyncio.sleep(every)
