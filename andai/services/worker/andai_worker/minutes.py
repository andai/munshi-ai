"""Minutes of meeting: every finished meeting with a stored transcript gets its minutes (MOM-01).

Every `TICK_S` seconds the runner does two things:
- It calls `andai_claim_mom(BATCH)` (migration 0010), the only cross-org read. It hands out
  meetings of any org that are over, have their transcript stored, still owe minutes (`none`, or a
  `pending` retry or regenerate that is due), are not canaries or deleted, and hold no live lease.
  Each comes with a fresh lease token for 15 minutes, so two workers never run the same meeting
  and a run that outlives its lease cannot write.
- It runs andai-core's `meetings.minutes_job.run_one` for the claimed meetings, together, each
  under its own org (row-level security), with the deployment's one LLM route (D-47).

This claim is the guarantee behind the platform's webhook (`POST /internal/platform-events`): a
meeting whose webhook never arrived is still claimed within one tick of its transcript being
stored. Retries, backoff and the final state are `run_one`'s. The runner is started only when
`ANDAI_LLM_BASE_URL` and `ANDAI_LLM_MODEL` are set (`main.jobs`); otherwise nothing is claimed and
meetings stay `none`.
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
from andai_core.llm.client import LlmClient
from andai_core.meetings.minutes_job import MomResult, run_one
from andai_core.meetings.mom_writer import DEFAULT_TIMEZONE

log = logging.getLogger(__name__)

TICK_S = 5.0
BATCH = 4


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TickResult:
    claimed: int = 0
    stored: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    lost: int = 0


class MomRunner:
    def __init__(
        self,
        engine: Engine,
        llm: LlmClient,
        *,
        batch: int = BATCH,
        timezone: str = DEFAULT_TIMEZONE,
        default_language: str = "en",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.llm = llm
        self.batch = batch
        self.timezone = timezone
        self.default_language = default_language
        self.clock = clock

    def claim(self) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
        """(org id, meeting id, lease token) of the meetings this runner now holds a lease on."""
        with transaction(self.engine) as tx:  # andai_app with no org context
            rows = tx.execute(
                text("SELECT org_id, meeting_id, lease FROM andai_claim_mom(:n)"),
                {"n": self.batch},
            ).all()
        return [(row.org_id, row.meeting_id, row.lease) for row in rows]

    async def _one(self, org_id: uuid.UUID, meeting_id: uuid.UUID, lease: uuid.UUID) -> MomResult:
        try:
            return await run_one(
                self.engine,
                self.llm,
                org_id,
                meeting_id,
                lease,
                timezone=self.timezone,
                default_language=self.default_language,
                clock=self.clock,
            )
        except Exception:  # noqa: BLE001 - one meeting must not stop the others
            log.exception("meeting %s: minutes run failed", meeting_id)
            return MomResult(state="pending", error="internal_error")

    async def tick(self) -> TickResult:
        result = TickResult()
        claimed = await anyio.to_thread.run_sync(self.claim)
        result.claimed = len(claimed)
        outcomes = await asyncio.gather(*(self._one(*job) for job in claimed))
        for outcome in outcomes:
            state = outcome.state if outcome.state in ("stored", "skipped", "failed", "lost") else (
                "pending"
            )
            setattr(result, state, getattr(result, state) + 1)
        return result

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("minutes pass failed")
            await asyncio.sleep(every)
