"""Join-time dispatch of meetings scheduled from invites (INV-01, INV-04, INV-05, D-58).

Every `ANDAI_SCHEDULED_TICK_S` seconds (default 15, at most 30), on any number of workers:

1. **Claim.** `andai_claim_scheduled(BATCH, lead, now)` (migration 0016) hands out the `scheduled`,
   unskipped meetings of every org whose `scheduled_start` minus the join lead
   (`ANDAI_INTAKE_JOIN_LEAD_S`, default 120 s) has passed, each taken `FOR UPDATE SKIP LOCKED` and
   leased, so two workers never take the same one. A meeting scheduled for a meeting that has
   already started (`joined_now`) is due at once and goes on the next tick. A skipped meeting is
   never claimed; one un-skipped before its time is claimed like any other.
2. **Act as the requester.** The meeting's requester is read under its org's row-level security;
   someone who is no longer an active member of that org sends nothing, and the meeting is
   cancelled (`end_reason` `requester_inactive`).
3. **Dispatch.** `Meetings.start_scheduled` as that requester: the paste-link path on the existing
   row (live duplicate and D-38 re-checked, settings snapshot, recording notice, platform key,
   audit). A meeting whose occurrence is already over (claimed late, say after a worker outage) ends
   `cancelled` with `meeting_ended` and no bot is sent. A gateway that cannot be reached, times out
   or answers 5xx leaves it scheduled for the next claim after the backoff. A refusal ends the
   meeting `cancelled` or `failed` and is never retried; an org with no platform principal leaves it
   scheduled until the lease runs out (30 s, doubling) and fails it after 5 claims. A worker that
   dies mid-way leaves the meeting to the next claim once the lease expires.

The worker connects as `andai_app` and needs nothing beyond the claim function: every read and
write of a meeting happens under that meeting's org. Links are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import anyio.to_thread
from sqlalchemy import Engine, select, text

from andai_core.db.models import Meeting
from andai_core.db.session import transaction
from andai_core.meetings.service import Actor, Meetings, requester_actor

log = logging.getLogger(__name__)

TICK_S = 15.0
LEAD_S = 120
BATCH = 10
REQUESTER_INACTIVE = "requester_inactive"


@dataclass(frozen=True)
class Claimed:
    org_id: uuid.UUID
    meeting_id: uuid.UUID
    attempts: int


@dataclass
class TickResult:
    claimed: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    def count(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


class ScheduledRunner:
    def __init__(
        self,
        engine: Engine,
        meetings: Meetings,
        *,
        lead_s: int = LEAD_S,
        clock: Callable[[], datetime],
        batch: int = BATCH,
    ) -> None:
        self.engine = engine
        self.meetings = meetings
        self.lead = timedelta(seconds=lead_s)
        self.clock = clock
        self.batch = batch

    # ── 1. claim ──

    def claim(self) -> list[Claimed]:
        with transaction(self.engine) as tx:
            rows = tx.execute(
                text(
                    "SELECT org_id, meeting_id, attempts"
                    " FROM andai_claim_scheduled(:n, :lead, :at)"
                ),
                {"n": self.batch, "lead": int(self.lead.total_seconds()), "at": self.clock()},
            ).all()
        return [Claimed(row.org_id, row.meeting_id, row.attempts) for row in rows]

    # ── 2. the requester ──

    def requester(self, claimed: Claimed) -> Actor | None:
        with transaction(self.engine, org_id=claimed.org_id) as tx:
            user_id = tx.scalar(
                select(Meeting.requested_by).where(
                    Meeting.id == claimed.meeting_id, Meeting.org_id == claimed.org_id
                )
            )
        if user_id is None:
            return None
        return requester_actor(self.engine, claimed.org_id, user_id)

    # ── 3. dispatch ──

    async def start(self, claimed: Claimed) -> str:
        actor = await anyio.to_thread.run_sync(lambda: self.requester(claimed))
        if actor is None:
            dropped = await anyio.to_thread.run_sync(
                lambda: self.meetings.drop_scheduled(
                    claimed.org_id, claimed.meeting_id, REQUESTER_INACTIVE
                )
            )
            return "cancelled" if dropped else "left"
        started = await self.meetings.start_scheduled(actor, claimed.meeting_id, lead=self.lead)
        return started.outcome

    # ── one pass ──

    async def tick(self) -> TickResult:
        result = TickResult()
        for claimed in await anyio.to_thread.run_sync(self.claim):
            result.claimed += 1
            try:
                result.count(await self.start(claimed))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the lease brings it back; others go on
                log.warning(
                    "scheduled meeting %s: dispatch failed: %s",
                    claimed.meeting_id, type(exc).__name__,
                )
                result.count("error")
        return result

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                result = await self.tick()
                if result.claimed:
                    log.info("scheduled meetings: %s", result.outcomes)
                if result.claimed >= self.batch:
                    continue  # more may be due
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("scheduled dispatch pass failed")
            await asyncio.sleep(every)
