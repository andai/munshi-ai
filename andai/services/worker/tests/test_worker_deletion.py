"""Meeting deletion in andai-worker: claims through `andai_claim_deletions()` (the only cross-org
read), the platform delete with each org's own key before andai's rows go, retries with backoff
(platform 5xx, platform 409 while its meeting is not terminal), a platform 404 taken as done,
`failed` after the cap, at most one purge per meeting however many workers run, and the loop started
with the worker.

Every test runs on a brand-new database, so the claim sees only this test's meetings.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import Engine, insert, text

from andai_core.db.models import ConsentRecord, MeetingShare
from andai_core.db.session import transaction
from andai_core.mail import MemoryMailer
from andai_core.meetings import deletion, mom
from andai_core.meetings.access import Actor
from andai_core.meetings.service import Meetings
from andai_worker.deletion import DeletionRunner, TickResult
from andai_worker.main import WorkerServices, jobs
from andai_worker.settings import WorkerSettings
from .worker_support import (
    Harness,
    give_principal,
    make_harness,
    new_member,
    platform_meeting,
    seed_meeting,
    support,
)

ANDAI_TABLES = (
    "transcript_segments", "mom_documents", "meeting_shares", "consent_records", "meetings"
)


@dataclass
class Org:
    org_id: uuid.UUID
    user_id: uuid.UUID
    key: str

    @property
    def actor(self) -> Actor:
        return Actor(self.org_id, self.user_id, "owner")


@dataclass
class World:
    engine: Engine
    harness: Harness

    def org(self) -> Org:
        member = new_member(self.engine)
        key = give_principal(self.harness, self.engine, member.org_id)
        return Org(member.org_id, member.user_id, key)

    def meeting(self, org: Org, *, on_platform: bool = True, full: bool = True) -> tuple:
        """A finished meeting with (when `full`) stored segments, minutes, a share and a consent
        record; on the platform with its transcript and stored objects. Returns (andai id,
        platform id)."""
        platform_id, native = (None, None)
        if on_platform:
            platform_id, native = platform_meeting(self.harness, org.key)
            self.harness.gateway.objects[platform_id] = [
                f"org/{org.org_id}/recordings/1/{platform_id}/chunk-0001.webm",
                f"org/{org.org_id}/recordings/1/{platform_id}/master.webm",
            ]
        meeting_id = seed_meeting(
            self.engine, org.org_id, org.user_id,
            platform_meeting_id=platform_id, native_meeting_id=native,
        )
        if full:
            ids = support.seed_segments(
                self.engine, org.org_id, meeting_id, ["kal ki meeting", "budget final"]
            )
            with transaction(self.engine, org_id=org.org_id) as tx:
                mom.seed_version(tx, org.org_id, meeting_id, support.mom_body(ids + ids))
                tx.execute(
                    insert(MeetingShare).values(
                        org_id=org.org_id, meeting_id=meeting_id, grantee_type="user",
                        grantee_id=org.user_id, granted_by=org.user_id,
                    )
                )
                tx.add(
                    ConsentRecord(
                        meeting_id=meeting_id, org_id=org.org_id,
                        bot_display_name="munshi.ai (recording)", disclosure_text="notice",
                        privacy_notice_url="http://portal.test/privacy",
                        disclosure_channel="chat", admitted_at=self.harness.clock(),
                    )
                )
        return meeting_id, platform_id

    def delete(self, org: Org, meeting_id: uuid.UUID) -> None:
        with transaction(self.engine, org_id=org.org_id, user_id=org.user_id) as tx:
            deletion.request_delete(tx, org.actor, meeting_id, now=self.harness.clock())

    def runner(self, batch: int = 10) -> DeletionRunner:
        s = self.harness.services
        return DeletionRunner(self.engine, s.keyring, s.gateway, batch=batch, clock=s.clock)

    def rows(self, superuser: Engine, meeting_id: uuid.UUID) -> dict[str, int]:
        with superuser.connect() as conn:
            return {
                table: conn.execute(
                    text(
                        f"SELECT count(*) FROM {table} WHERE "
                        + ("id" if table == "meetings" else "meeting_id")
                        + " = :m"
                    ),
                    {"m": meeting_id},
                ).scalar_one()
                for table in ANDAI_TABLES
            }

    def state(self, superuser: Engine, meeting_id: uuid.UUID) -> tuple:
        with superuser.connect() as conn:
            return tuple(
                conn.execute(
                    text(
                        "SELECT deletion_state, deletion_attempts, deletion_next_at"
                        " FROM meetings WHERE id = :m"
                    ),
                    {"m": meeting_id},
                ).one()
            )

    def due_now(self, org: Org, meeting_id: uuid.UUID) -> None:
        with transaction(self.engine, org_id=org.org_id) as tx:
            tx.execute(
                text("UPDATE meetings SET deletion_next_at = now() WHERE id = :m"),
                {"m": meeting_id},
            )


GONE = dict.fromkeys(ANDAI_TABLES, 0)
KEPT = {
    "transcript_segments": 2, "mom_documents": 1, "meeting_shares": 1, "consent_records": 1,
    "meetings": 1,
}


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    return World(fresh_app_engine, make_harness(fresh_app_engine))


@pytest.fixture
def superuser(fresh_app_engine: Engine, postgres) -> Engine:
    from sqlalchemy import create_engine

    name = fresh_app_engine.url.database
    engine = create_engine(postgres.url("postgres", postgres.superuser_password, name))
    yield engine
    engine.dispose()


def _audit(superuser: Engine, meeting_id: uuid.UUID) -> list[tuple]:
    with superuser.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT action, actor_user_id FROM audit_events WHERE target = :t"
                    " ORDER BY at"
                ),
                {"t": f"meeting:{meeting_id}"},
            )
        ]


def test_a_tick_purges_deleted_meetings_of_every_org_from_the_platform_then_andai(
    world: World, superuser: Engine
) -> None:
    a, b = world.org(), world.org()
    gone_a, platform_a = world.meeting(a)
    gone_b, platform_b = world.meeting(b)
    kept, platform_kept = world.meeting(a)
    world.delete(a, gone_a)
    world.delete(b, gone_b)
    gateway = world.harness.gateway

    result = asyncio.run(world.runner().tick())

    assert result == TickResult(claimed=2, purged=2)
    assert sorted(gateway.deletions) == sorted([platform_a, platform_b])
    # The platform's stored objects and transcript are gone; the other meeting's are untouched.
    assert platform_a not in gateway.objects and platform_b not in gateway.objects
    assert platform_a not in gateway.transcripts and platform_b not in gateway.transcripts
    assert len(gateway.objects[platform_kept]) == 2 and gateway.transcripts[platform_kept]
    assert world.rows(superuser, gone_a) == GONE
    assert world.rows(superuser, gone_b) == GONE
    assert world.rows(superuser, kept) == KEPT
    # The audit events are all that is left of a purged meeting.
    assert _audit(superuser, gone_a) == [("meeting.delete", a.user_id), ("meeting.purge", None)]
    assert _audit(superuser, gone_b) == [("meeting.delete", b.user_id), ("meeting.purge", None)]

    assert asyncio.run(world.runner().tick()) == TickResult()  # nothing left to claim
    assert len(gateway.deletions) == 2


def test_a_meeting_that_never_reached_the_platform_needs_no_platform_call(
    world: World, superuser: Engine
) -> None:
    org = world.org()
    meeting_id, _ = world.meeting(org, on_platform=False)
    world.delete(org, meeting_id)

    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, purged=1)
    assert world.harness.gateway.deletions == []
    assert world.rows(superuser, meeting_id) == GONE


def test_a_platform_404_means_nothing_is_left_there(world: World, superuser: Engine) -> None:
    org = world.org()
    meeting_id, platform_id = world.meeting(org)
    world.harness.gateway.owners[platform_id] = -1  # the platform no longer knows it for this key
    world.delete(org, meeting_id)

    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, purged=1)
    assert world.rows(superuser, meeting_id) == GONE


def test_a_platform_error_is_retried_with_backoff_and_andai_keeps_its_rows_meanwhile(
    world: World, superuser: Engine
) -> None:
    org = world.org()
    meeting_id, platform_id = world.meeting(org)
    world.delete(org, meeting_id)
    gateway = world.harness.gateway
    gateway.fail["delete meeting"] = 503

    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, pending=1)
    state, attempts, next_at = world.state(superuser, meeting_id)
    assert (state, attempts) == ("pending", 1)
    assert next_at == world.harness.clock() + timedelta(seconds=10)
    assert world.rows(superuser, meeting_id) == KEPT  # the ids are kept for the retry
    assert asyncio.run(world.runner().tick()) == TickResult()  # backing off

    del gateway.fail["delete meeting"]
    world.due_now(org, meeting_id)
    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, purged=1)
    assert gateway.deletions == [platform_id]
    assert world.rows(superuser, meeting_id) == GONE


def test_a_platform_meeting_that_has_not_ended_is_retried_later(
    world: World, superuser: Engine
) -> None:
    org = world.org()
    meeting_id, platform_id = world.meeting(org)
    # andai has ended the meeting; the platform has not yet.
    world.harness.gateway.set(platform_id, status="stopping")
    world.delete(org, meeting_id)

    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, pending=1)
    assert world.state(superuser, meeting_id)[:2] == ("pending", 1)
    assert world.harness.gateway.deletions == []

    world.harness.gateway.set(platform_id, status="completed")
    world.due_now(org, meeting_id)
    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, purged=1)
    assert world.rows(superuser, meeting_id) == GONE


def test_the_backoff_doubles_from_10_s_to_2_min() -> None:
    assert [deletion.backoff(n).total_seconds() for n in range(1, 8)] == [
        10, 20, 40, 80, 120, 120, 120
    ]


def test_a_deletion_fails_after_its_last_attempt_and_stays_hidden(
    world: World, superuser: Engine
) -> None:
    org = world.org()
    meeting_id, _ = world.meeting(org)
    world.delete(org, meeting_id)
    with transaction(world.engine, org_id=org.org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET deletion_attempts = :n WHERE id = :m"),
            {"n": deletion.MAX_ATTEMPTS - 1, "m": meeting_id},
        )
    world.harness.gateway.fail["delete meeting"] = 500

    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, failed=1)
    assert world.state(superuser, meeting_id) == ("failed", deletion.MAX_ATTEMPTS, None)
    assert asyncio.run(world.runner().tick()) == TickResult()  # never claimed again
    with transaction(world.engine) as tx:
        backlog = tx.execute(text("SELECT pending FROM andai_deletion_backlog()")).scalar_one()
    assert backlog == 1


def test_an_unexpected_error_counts_as_an_attempt(
    world: World, superuser: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = world.org()
    meeting_id, _ = world.meeting(org)
    world.delete(org, meeting_id)

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(deletion, "_remove", broken)
    assert asyncio.run(world.runner().tick()) == TickResult(claimed=1, pending=1)
    assert world.state(superuser, meeting_id)[:2] == ("pending", 1)


def test_two_workers_never_purge_the_same_meeting(world: World, superuser: Engine) -> None:
    org = world.org()
    meetings = []
    for _ in range(8):
        meeting_id, _ = world.meeting(org, full=False)
        world.delete(org, meeting_id)
        meetings.append(meeting_id)
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
    assert sum(r.purged for r in results) == 8
    deletions = world.harness.gateway.deletions
    assert len(deletions) == 8 and len(set(deletions)) == 8
    assert all(world.rows(superuser, m)["meetings"] == 0 for m in meetings)


def test_the_worker_always_starts_the_deletion_loop(
    world: World, superuser: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    settings = WorkerSettings(ANDAI_ENV="dev", ANDAI_ALERT_EMAIL="")
    s = world.harness.services
    services = WorkerServices(
        settings=settings,
        engine=world.engine,
        meetings=Meetings(world.engine, s.keyring, s.gateway, settings, s.clock),
        mailer=MemoryMailer(),
    )
    org = world.org()
    meeting_id, _ = world.meeting(org)
    world.delete(org, meeting_id)

    async def main() -> int:
        tasks = jobs(services)
        try:
            for _ in range(100):
                if world.rows(superuser, meeting_id)["meetings"] == 0:
                    break
                await asyncio.sleep(0.05)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    with caplog.at_level(logging.INFO, logger="andai_worker.main"):
        started = asyncio.run(main())

    assert started == 2  # ingest and deletion: no canary links, no alert address
    assert world.rows(superuser, meeting_id) == GONE
    assert any("meeting deletion every" in r.getMessage() for r in caplog.records)
