"""Canary joins: the product's own dispatch path, on a schedule, into standing test meetings.

For each platform with a canary link (`ANDAI_CANARY_{MEET,TEAMS,ZOOM}_URL`) one `canary_runs` row is
always `scheduled` or `running` (a partial unique index keeps it to one, whichever worker inserts
it). Every tick, on any number of workers:

1. **Schedule.** A configured platform with no pending run gets one, due
   `ANDAI_CANARY_INTERVAL_S` after its previous run started (at once if it never ran).
2. **Claim.** Due runs are taken with `SELECT … FOR UPDATE SKIP LOCKED` and marked `running` in
   the same transaction, so each due run starts on exactly one worker.
3. **Dispatch.** `andai_core.meetings.service.Meetings.join` as the canary org's owner with
   source `canary` (exempt from the 12 h no-rejoin rule), never as a customer org. A refusal before
   any meeting exists fails the run at once.
4. **Follow.** andai-core's reconciler keeps the canary meeting's status current; the worker reads
   it under the canary org's context. A meeting the bot was admitted to (`started_at` set, or
   `recording` / `processing` / `done`) makes the run a `success`, and the bot is stopped
   `HOLD_S` seconds later so a canary never holds a bot slot; a `failed` meeting makes it a
   `failure` with the meeting's `JoinFailureReason`. The run is `done` once the meeting has ended.
   A run with no answer after the lobby timeout plus `GRACE` is failed and its bot stopped.

Following a run is idempotent (guarded updates; a second stop is harmless), so two workers may
look at the same running run. The canary link is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import anyio.to_thread
from sqlalchemy import Engine, select, text, update
from sqlalchemy.dialects.postgresql import insert

from andai_core.db.models import TERMINAL_STATUSES, CanaryRun, Meeting
from andai_core.db.session import transaction
from andai_core.meetings.service import Actor, MeetingError, Meetings, canary_actor

log = logging.getLogger(__name__)

TICK_S = 60.0
# How long an admitted canary bot stays before it is stopped.
HOLD_S = 60.0
# A run with no meeting answer after the lobby timeout plus this is failed.
GRACE = timedelta(minutes=5)
# A running run whose dispatch never recorded a meeting (the worker stopped mid-dispatch).
LOST_DISPATCH = timedelta(minutes=2)
ADMITTED = ("recording", "processing", "done")
PENDING = ("scheduled", "running")
PENDING_PREDICATE = text("state IN ('scheduled', 'running')")

# A refusal before any meeting exists, as a JoinFailureReason.
REFUSAL_REASONS = {
    "not_a_meeting_link": "meeting_not_found",
    "already_in_meeting": "internal_error",
}


def tick_seconds(interval_s: int) -> float:
    """Ticks often enough for a short test interval, at most once a minute."""
    return float(min(TICK_S, max(1, interval_s // 4)))


@dataclass(frozen=True)
class Claimed:
    run_id: uuid.UUID
    platform: str


@dataclass
class TickResult:
    scheduled: int = 0
    started: int = 0
    decided: int = 0
    finished: int = 0


class CanaryRunner:
    def __init__(
        self,
        engine: Engine,
        meetings: Meetings,
        urls: dict[str, str],
        *,
        interval_s: int,
        clock: Callable[[], datetime],
        hold_s: float = HOLD_S,
    ) -> None:
        self.engine = engine
        self.meetings = meetings
        self.urls = urls
        self.interval = timedelta(seconds=interval_s)
        self.clock = clock
        self.hold = timedelta(seconds=hold_s)
        self.give_up = timedelta(seconds=meetings.settings.lobby_timeout_s) + GRACE
        self._actor: Actor | None = None
        self._warned = False

    def actor(self) -> Actor | None:
        if self._actor is None:
            self._actor = canary_actor(self.engine)
        return self._actor

    # ── 1. schedule ──

    def schedule(self) -> int:
        now = self.clock()
        created = 0
        with transaction(self.engine) as tx:
            for platform in sorted(self.urls):
                last = tx.scalar(
                    select(CanaryRun.started_at)
                    .where(CanaryRun.platform == platform, CanaryRun.started_at.is_not(None))
                    .order_by(CanaryRun.started_at.desc())
                    .limit(1)
                )
                due = now if last is None else last + self.interval
                inserted = tx.execute(
                    insert(CanaryRun)
                    .values(id=uuid.uuid4(), platform=platform, state="scheduled", due_at=due)
                    .on_conflict_do_nothing(
                        index_elements=["platform"],
                        # Literal, so the arbiter still matches the partial index once the
                        # driver prepares the statement (a generic plan keeps parameters).
                        index_where=PENDING_PREDICATE,
                    )
                    .returning(CanaryRun.id)
                )
                created += len(inserted.all())
        return created

    # ── 2. claim ──

    def claim(self) -> list[Claimed]:
        now = self.clock()
        with transaction(self.engine) as tx:
            runs = tx.scalars(
                select(CanaryRun)
                .where(
                    CanaryRun.state == "scheduled",
                    CanaryRun.due_at <= now,
                    CanaryRun.platform.in_(sorted(self.urls)),
                )
                .with_for_update(skip_locked=True)
            ).all()
            for run in runs:
                run.state, run.started_at = "running", now
            return [Claimed(run.id, run.platform) for run in runs]

    # ── 3. dispatch ──

    def _record_meeting(self, run_id: uuid.UUID, meeting_id: uuid.UUID) -> None:
        with transaction(self.engine) as tx:
            tx.execute(
                update(CanaryRun)
                .where(CanaryRun.id == run_id, CanaryRun.state == "running")
                .values(meeting_id=meeting_id)
            )

    def _fail(self, run_id: uuid.UUID, reason: str) -> None:
        now = self.clock()
        with transaction(self.engine) as tx:
            tx.execute(
                update(CanaryRun)
                .where(CanaryRun.id == run_id, CanaryRun.state == "running")
                .values(state="done", outcome="failure", reason=reason, finished_at=now)
            )

    async def start(self, claimed: Claimed, actor: Actor) -> None:
        url = self.urls[claimed.platform]
        try:
            result = await self.meetings.join(
                actor, join_url=url, language=None, team_id=None, source="canary"
            )
        except MeetingError as refusal:
            reason = REFUSAL_REASONS.get(refusal.code, "internal_error")
            log.warning(
                "canary %s on %s refused: %s (%s)", claimed.run_id, claimed.platform,
                refusal.code, reason,
            )
            await anyio.to_thread.run_sync(lambda: self._fail(claimed.run_id, reason))
            return
        meeting_id = uuid.UUID(result.meeting["id"])
        await anyio.to_thread.run_sync(lambda: self._record_meeting(claimed.run_id, meeting_id))

    # ── 4. follow ──

    def _running(self) -> list[CanaryRun]:
        with transaction(self.engine) as tx:
            return list(tx.scalars(select(CanaryRun).where(CanaryRun.state == "running")).all())

    def _meetings(self, actor: Actor, ids: list[uuid.UUID]) -> dict[uuid.UUID, Meeting]:
        if not ids:
            return {}
        with transaction(self.engine, org_id=actor.org_id) as tx:
            rows = tx.scalars(
                select(Meeting).where(Meeting.org_id == actor.org_id, Meeting.id.in_(ids))
            ).all()
            return {m.id: m for m in rows}

    def _decide(self, run: CanaryRun, meeting: Meeting | None, now: datetime) -> tuple[str, bool]:
        """Write what the meeting says about `run`; returns (what happened, stop the bot?)."""
        values: dict = {}
        stop = False
        happened = ""
        if run.outcome is None:
            if meeting is None:
                if now - run.started_at >= LOST_DISPATCH:
                    values = {"outcome": "failure", "reason": "internal_error"}
            elif meeting.started_at is not None or meeting.status in ADMITTED:
                values = {"outcome": "success", "recorded_at": now}
            elif meeting.status == "failed":
                values = {"outcome": "failure", "reason": meeting.failure_reason or "internal_error"}
            elif meeting.status == "cancelled":
                values = {"outcome": "failure", "reason": "internal_error"}
            elif now - run.started_at >= self.give_up:
                values = {"outcome": "failure", "reason": "admission_timeout"}
                stop = True
            if values:
                happened = "decided"
        ended = meeting is None or meeting.status in TERMINAL_STATUSES
        outcome = values.get("outcome", run.outcome)
        if outcome == "success" and not ended:
            recorded = values.get("recorded_at", run.recorded_at) or now
            stop = stop or now - recorded >= self.hold
        if outcome is not None and (ended or outcome == "failure"):
            values.update(state="done", finished_at=now)
            happened = "finished"
        if values:
            with transaction(self.engine) as tx:
                tx.execute(
                    update(CanaryRun)
                    .where(CanaryRun.id == run.id, CanaryRun.state == "running")
                    .values(**values)
                )
        return happened, stop and meeting is not None and not ended

    async def follow(self, actor: Actor, result: TickResult) -> None:
        runs = await anyio.to_thread.run_sync(self._running)
        ids = [r.meeting_id for r in runs if r.meeting_id is not None]
        meetings = await anyio.to_thread.run_sync(lambda: self._meetings(actor, ids))
        for run in runs:
            meeting = meetings.get(run.meeting_id) if run.meeting_id else None
            now = self.clock()
            happened, stop = await anyio.to_thread.run_sync(
                lambda: self._decide(run, meeting, now)
            )
            if happened == "decided":
                result.decided += 1
            elif happened == "finished":
                result.finished += 1
            if stop and meeting is not None:
                try:
                    await self.meetings.stop(actor, meeting.id)
                except MeetingError as exc:
                    if exc.code != "not_running":
                        log.warning("canary %s: stop refused: %s", run.id, exc.code)

    # ── one pass ──

    async def tick(self) -> TickResult:
        result = TickResult()
        actor = await anyio.to_thread.run_sync(self.actor)
        if actor is None:
            if not self._warned:
                log.warning("canaries are configured but there is no canary org; run canary-setup")
                self._warned = True
            return result
        result.scheduled = await anyio.to_thread.run_sync(self.schedule)
        for claimed in await anyio.to_thread.run_sync(self.claim):
            await self.start(claimed, actor)
            result.started += 1
        await self.follow(actor, result)
        return result

    async def run(self, every: float) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("canary pass failed")
            await asyncio.sleep(every)


def canary_history(engine: Engine, platform: str, limit: int = 5) -> list[tuple]:
    """The latest decided canary runs on `platform` (start time, outcome, reason), latest first."""
    with transaction(engine) as tx:
        return list(
            tx.execute(
                text(
                    "SELECT started_at, outcome, reason FROM canary_runs"
                    " WHERE platform = :p AND outcome IS NOT NULL"
                    " ORDER BY started_at DESC LIMIT :n"
                ),
                {"p": platform, "n": limit},
            ).all()
        )
