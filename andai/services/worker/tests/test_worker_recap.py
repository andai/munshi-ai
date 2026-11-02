"""Recap mail in andai-worker: `RecapRunner` claims through `andai_claim_recap()` and mails each
meeting's first minutes once, under its own org, however many workers run; the loop always starts
with the worker.

Every test runs on a brand-new database, so the claim sees only this test's meetings.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from andai_core.mail import Mail, MemoryMailer
from andai_core.meetings.mom import seed_version
from andai_core.meetings.service import Meetings
from andai_worker.main import WorkerServices, jobs
from andai_worker.recap import BATCH, TICK_S, RecapRunner, TickResult
from andai_worker.settings import WorkerSettings
from .worker_support import make_harness, new_member, seed_meeting, support

PORTAL = "http://portal.test"


class SlowMailer(MemoryMailer):
    """A relay slow enough for a second runner to try the same meeting."""

    async def send(self, mail: Mail) -> None:
        await asyncio.sleep(0.05)
        await super().send(mail)


@dataclass
class World:
    engine: Engine

    def meeting(self, org_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
        meeting_id = seed_meeting(self.engine, org_id, user_id)
        ids = support.seed_segments(
            self.engine, org_id, meeting_id, [("Asha", "ठीक है, मैं report भेज दूँगा")]
        )
        with transaction(self.engine, org_id=org_id) as tx:
            seed_version(tx, org_id, meeting_id, support.mom_body(ids))
            tx.execute(
                text(
                    "UPDATE meetings SET mom_state = 'stored', recap_state = 'pending' WHERE id = :m"
                ),
                {"m": meeting_id},
            )
        return meeting_id

    def state(self, org_id: uuid.UUID, meeting_id: uuid.UUID) -> str:
        with transaction(self.engine, org_id=org_id) as tx:
            return tx.execute(
                text("SELECT recap_state FROM meetings WHERE id = :m"), {"m": meeting_id}
            ).scalar_one()


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    return World(fresh_app_engine)


def test_the_runner_defaults() -> None:
    assert (TICK_S, BATCH) == (5.0, 10)


def test_a_tick_mails_every_org_once_under_their_own_org(world: World) -> None:
    a, b = new_member(world.engine), new_member(world.engine)
    meetings = [(a.org_id, world.meeting(a.org_id, a.user_id)) for _ in range(2)]
    meetings.append((b.org_id, world.meeting(b.org_id, b.user_id)))
    mailer = MemoryMailer()
    runner = RecapRunner(world.engine, mailer, portal_origin=PORTAL)

    first = asyncio.run(runner.tick())
    second = asyncio.run(runner.tick())

    assert first == TickResult(claimed=3, sent=3)
    assert second == TickResult()
    assert sorted(m.to for m in mailer.outbox) == sorted([a.email, a.email, b.email])
    links = {line for m in mailer.outbox for line in m.text.splitlines() if "/meetings/" in line}
    assert links == {f"{PORTAL}/meetings/{meeting_id}" for _, meeting_id in meetings}
    for org_id, meeting_id in meetings:
        assert world.state(org_id, meeting_id) == "sent"


def test_concurrent_runners_claim_each_recap_once(world: World) -> None:
    member = new_member(world.engine)
    ids = [world.meeting(member.org_id, member.user_id) for _ in range(4)]
    mailer = SlowMailer()
    one = RecapRunner(world.engine, mailer, portal_origin=PORTAL, batch=2)
    two = RecapRunner(world.engine, mailer, portal_origin=PORTAL, batch=2)

    async def both() -> list[TickResult]:
        return list(await asyncio.gather(one.tick(), two.tick(), one.tick(), two.tick()))

    results = asyncio.run(both())

    assert sum(r.claimed for r in results) == 4
    assert sum(r.sent for r in results) == 4
    assert len(mailer.outbox) == 4
    for meeting_id in ids:
        assert world.state(member.org_id, meeting_id) == "sent"


def test_the_worker_always_runs_the_recap_loop(world: World) -> None:
    settings = WorkerSettings(ANDAI_ENV="dev", ANDAI_ALERT_EMAIL="", ANDAI_LLM_BASE_URL="")
    member = new_member(world.engine)
    meeting_id = world.meeting(member.org_id, member.user_id)
    harness = make_harness(world.engine)
    s = harness.services
    mailer = MemoryMailer()
    services = WorkerServices(
        settings=settings,
        engine=world.engine,
        meetings=Meetings(world.engine, s.keyring, s.gateway, settings, s.clock),
        mailer=mailer,
    )

    async def main() -> list[str]:
        tasks = jobs(services)
        try:
            for _ in range(120):
                if world.state(member.org_id, meeting_id) == "sent":
                    break
                await asyncio.sleep(0.05)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return [task.get_coro().__qualname__ for task in tasks]

    started = asyncio.run(main())

    assert any("RecapRunner.run" in name for name in started)
    assert world.state(member.org_id, meeting_id) == "sent"
    assert [m.to for m in mailer.outbox] == [member.email]
