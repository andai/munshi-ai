"""Transcript ingest in andai-worker: claims through `andai_claim_ingest()` (the only cross-org
read), the copy stored under each meeting's own org, at most one ingest per meeting however many
workers run, and the loop started with the worker.

Every test runs on a brand-new database, so the claim sees only this test's meetings.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections import Counter
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from andai_core.mail import MemoryMailer
from andai_core.meetings.service import Meetings
from andai_worker.ingest import IngestRunner, TickResult
from andai_worker.main import WorkerServices, jobs
from andai_worker.settings import WorkerSettings
from .worker_support import (
    Harness,
    give_principal,
    make_harness,
    new_member,
    platform_meeting,
    seed_meeting,
)


@dataclass
class Org:
    org_id: uuid.UUID
    user_id: uuid.UUID
    key: str


@dataclass
class World:
    engine: Engine
    harness: Harness

    def org(self) -> Org:
        member = new_member(self.engine)
        key = give_principal(self.harness, self.engine, member.org_id)
        return Org(member.org_id, member.user_id, key)

    def meeting(
        self, org: Org, *, status: str = "done", on_platform: bool = True, **kwargs
    ) -> uuid.UUID:
        platform_id, native = (None, None)
        if on_platform:
            platform_id, native = platform_meeting(self.harness, org.key, **kwargs)
        return seed_meeting(
            self.engine,
            org.org_id,
            org.user_id,
            status=status,
            failure_reason="internal_error" if status == "failed" else None,
            platform_meeting_id=platform_id,
            native_meeting_id=native,
        )

    def runner(self, batch: int = 10) -> IngestRunner:
        s = self.harness.services
        return IngestRunner(self.engine, s.keyring, s.gateway, batch=batch, clock=s.clock)

    def state(self, org: Org, meeting_id: uuid.UUID) -> str:
        with transaction(self.engine, org_id=org.org_id) as tx:
            return tx.execute(
                text("SELECT transcript_state FROM meetings WHERE id = :m"), {"m": meeting_id}
            ).scalar_one()

    def segments(self, org_id: uuid.UUID | None, meeting_id: uuid.UUID | None = None) -> int:
        with transaction(self.engine, org_id=org_id) as tx:
            if meeting_id is None:
                return tx.execute(text("SELECT count(*) FROM transcript_segments")).scalar_one()
            return tx.execute(
                text("SELECT count(*) FROM transcript_segments WHERE meeting_id = :m"),
                {"m": meeting_id},
            ).scalar_one()

    def transcript_reads(self) -> Counter:
        return Counter(
            path for method, path in self.harness.gateway.calls
            if method == "GET" and path.startswith("/transcripts/by-id/")
        )


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    return World(fresh_app_engine, make_harness(fresh_app_engine))


def test_a_tick_stores_finished_meetings_of_every_org_under_their_own_org(world: World) -> None:
    a, b = world.org(), world.org()
    done_a = world.meeting(a)
    failed_b = world.meeting(b, status="failed")
    cancelled_b = world.meeting(b, status="cancelled")
    live_a = world.meeting(a, status="recording")
    processing_b = world.meeting(b, status="processing")
    never_sent = world.meeting(a, on_platform=False)

    result = asyncio.run(world.runner().tick())

    assert result == TickResult(claimed=3, stored=3, pending=0, failed=0)
    assert world.state(a, done_a) == "stored"
    assert world.state(b, failed_b) == world.state(b, cancelled_b) == "stored"
    assert world.state(a, live_a) == world.state(b, processing_b) == "none"
    assert world.state(a, never_sent) == "none"
    assert world.segments(a.org_id, done_a) == 2
    assert world.segments(b.org_id, failed_b) == 2
    # Each org sees only its own copy; with no org context the app role sees nothing.
    assert world.segments(b.org_id, done_a) == 0
    assert world.segments(a.org_id, failed_b) == 0
    assert world.segments(None) == 0
    with transaction(world.engine) as tx:
        assert tx.execute(text("SELECT count(*) FROM meetings")).scalar_one() == 0

    assert asyncio.run(world.runner().tick()) == TickResult()  # nothing left to claim
    assert sum(world.transcript_reads().values()) == 3


def test_two_workers_never_ingest_the_same_meeting(world: World) -> None:
    org = world.org()
    meetings = [world.meeting(org) for _ in range(8)]
    start = threading.Barrier(2)
    results: list[TickResult] = []

    def worker() -> None:
        runner = world.runner(batch=3)
        start.wait()
        for _ in range(6):
            results.append(asyncio.run(runner.tick()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sum(r.claimed for r in results) == 8
    assert sum(r.stored for r in results) == 8
    reads = world.transcript_reads()
    assert len(reads) == 8 and set(reads.values()) == {1}
    assert all(world.state(org, m) == "stored" for m in meetings)


def test_a_failing_platform_leaves_the_meeting_pending_and_the_loop_going(world: World) -> None:
    org = world.org()
    meeting = world.meeting(org)
    world.harness.gateway.fail["get transcript"] = 503

    result = asyncio.run(world.runner().tick())

    assert result == TickResult(claimed=1, failed=1)
    assert world.state(org, meeting) == "pending"
    assert asyncio.run(world.runner().tick()) == TickResult()  # backing off

    del world.harness.gateway.fail["get transcript"]
    with transaction(world.engine, org_id=org.org_id) as tx:  # the backoff has passed
        tx.execute(text("UPDATE meetings SET transcript_next_at = now() WHERE id = :m"),
                   {"m": meeting})
    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, stored=1)


def test_the_worker_always_starts_the_ingest_loop(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    settings = WorkerSettings(ANDAI_ENV="dev", ANDAI_ALERT_EMAIL="")
    assert not settings.canary_urls and not settings.alert_to
    s = world.harness.services
    services = WorkerServices(
        settings=settings,
        engine=world.engine,
        meetings=Meetings(world.engine, s.keyring, s.gateway, settings, s.clock),
        mailer=MemoryMailer(),
    )
    org = world.org()
    meeting = world.meeting(org)

    async def main() -> int:
        tasks = jobs(services)
        try:
            for _ in range(100):
                if world.state(org, meeting) == "stored":
                    break
                await asyncio.sleep(0.05)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    with caplog.at_level(logging.INFO, logger="andai_worker.main"):
        started = asyncio.run(main())

    assert started == 1  # ingest only: no canary links, no alert address
    assert world.state(org, meeting) == "stored"
    assert any("transcript ingest every" in r.getMessage() for r in caplog.records)
