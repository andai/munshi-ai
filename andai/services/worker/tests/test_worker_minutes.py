"""Minutes in andai-worker: `MomRunner` claims through `andai_claim_mom()` and stores one version
per meeting under its own org, however many workers run; the loop starts with the worker only when
the LLM route is configured, and without it nothing is claimed.

Every test runs on a brand-new database, so the claim sees only this test's meetings. The model is
`andai_core.testing.fake_llm`, in-process.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from andai_core.llm.client import LlmClient, LlmConfig
from andai_core.mail import MemoryMailer
from andai_core.meetings.service import Meetings
from andai_core.testing import fake_llm
from andai_worker.main import WorkerServices, jobs
from andai_worker.minutes import BATCH, TICK_S, MomRunner, TickResult
from andai_worker.settings import WorkerSettings
from .worker_support import make_harness, new_member, seed_meeting, support


class Counting(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.ASGITransport(app=fake_llm.app)
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await asyncio.sleep(0.05)  # long enough for a second runner to try the same meeting
        return await self.inner.handle_async_request(request)


def llm(transport: httpx.AsyncBaseTransport) -> LlmClient:
    return LlmClient(
        LlmConfig(base_url="http://fake-llm.test", api_key=None, model="fake-model"),
        transport=transport,
    )


@dataclass
class World:
    engine: Engine

    def meeting(self, org_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
        meeting_id = seed_meeting(self.engine, org_id, user_id)
        support.seed_segments(
            self.engine, org_id, meeting_id, [("Asha", "ठीक है, मैं report भेज दूँगा")]
        )
        return meeting_id

    def state(self, org_id: uuid.UUID, meeting_id: uuid.UUID) -> tuple[str, int, int]:
        with transaction(self.engine, org_id=org_id) as tx:
            row = tx.execute(
                text(
                    "SELECT mom_state, mom_attempts,"
                    " (SELECT count(*) FROM mom_documents d WHERE d.meeting_id = m.id) AS versions"
                    " FROM meetings m WHERE id = :m"
                ),
                {"m": meeting_id},
            ).one()
        return row.mom_state, row.mom_attempts, row.versions


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    return World(fresh_app_engine)


def test_the_runner_defaults() -> None:
    assert (TICK_S, BATCH) == (5.0, 4)


def test_a_tick_stores_minutes_for_every_org_under_their_own_org(world: World) -> None:
    a, b = new_member(world.engine), new_member(world.engine)
    meetings = [(a.org_id, world.meeting(a.org_id, a.user_id)) for _ in range(3)]
    meetings.append((b.org_id, world.meeting(b.org_id, b.user_id)))
    transport = Counting()
    runner = MomRunner(world.engine, llm(transport))

    first = asyncio.run(runner.tick())
    second = asyncio.run(runner.tick())
    third = asyncio.run(runner.tick())

    assert first == TickResult(claimed=4, stored=4)  # BATCH is 4
    assert second == TickResult() and third == TickResult()
    assert transport.calls == 4
    for org_id, meeting_id in meetings:
        assert world.state(org_id, meeting_id) == ("stored", 0, 1)


def test_two_workers_never_generate_the_same_meeting_twice(world: World) -> None:
    member = new_member(world.engine)
    ids = [world.meeting(member.org_id, member.user_id) for _ in range(3)]
    transport = Counting()
    one, two = MomRunner(world.engine, llm(transport)), MomRunner(world.engine, llm(transport))

    async def both() -> list[TickResult]:
        return list(await asyncio.gather(one.tick(), two.tick(), one.tick(), two.tick()))

    results = asyncio.run(both())

    assert sum(r.claimed for r in results) == 3
    assert sum(r.stored for r in results) == 3
    assert transport.calls == 3
    for meeting_id in ids:
        assert world.state(member.org_id, meeting_id) == ("stored", 0, 1)


def test_a_failing_model_leaves_the_meeting_pending_and_the_loop_going(world: World) -> None:
    member = new_member(world.engine)
    meeting_id = world.meeting(member.org_id, member.user_id)
    down = llm(httpx.MockTransport(lambda request: httpx.Response(502)))

    result = asyncio.run(MomRunner(world.engine, down).tick())

    assert result == TickResult(claimed=1, pending=1)
    assert world.state(member.org_id, meeting_id) == ("pending", 1, 0)


def _services(world: World, settings: WorkerSettings) -> WorkerServices:
    harness = make_harness(world.engine)
    s = harness.services
    return WorkerServices(
        settings=settings,
        engine=world.engine,
        meetings=Meetings(world.engine, s.keyring, s.gateway, settings, s.clock),
        mailer=MemoryMailer(),
    )


def _run_jobs(services: WorkerServices, until, seconds: float = 6.0) -> list[str]:
    async def main() -> list[str]:
        tasks = jobs(services)
        try:
            for _ in range(int(seconds / 0.05)):
                if until():
                    break
                await asyncio.sleep(0.05)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return [task.get_coro().__qualname__ for task in tasks]

    return asyncio.run(main())


def test_without_an_llm_route_the_loop_is_not_started_and_nothing_is_claimed(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    settings = WorkerSettings(ANDAI_ENV="dev", ANDAI_ALERT_EMAIL="", ANDAI_LLM_BASE_URL="")
    member = new_member(world.engine)
    meeting_id = world.meeting(member.org_id, member.user_id)

    with caplog.at_level(logging.INFO, logger="andai_worker.main"):
        started = _run_jobs(_services(world, settings), lambda: False, seconds=0.5)

    assert not any("MomRunner" in name for name in started)
    assert len(started) == 5  # scheduled dispatch, ingest, recap mail, deletion and erasure
    assert world.state(member.org_id, meeting_id) == ("none", 0, 0)
    off = [r.getMessage() for r in caplog.records if "minutes off" in r.getMessage()]
    assert len(off) == 1


def test_with_an_llm_route_the_worker_runs_the_minutes_loop(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from andai_worker import main as worker_main

    transport = Counting()
    real_client = worker_main.LlmClient
    monkeypatch.setattr(
        worker_main, "LlmClient", lambda config: real_client(config, transport=transport)
    )
    settings = WorkerSettings(
        ANDAI_ENV="dev",
        ANDAI_ALERT_EMAIL="",
        ANDAI_LLM_BASE_URL="http://fake-llm.test",
        ANDAI_LLM_MODEL="fake-model",
    )
    member = new_member(world.engine)
    meeting_id = world.meeting(member.org_id, member.user_id)

    with caplog.at_level(logging.INFO, logger="andai_worker.main"):
        started = _run_jobs(
            _services(world, settings),
            lambda: world.state(member.org_id, meeting_id)[0] == "stored",
        )

    assert any("MomRunner.run" in name for name in started)
    assert world.state(member.org_id, meeting_id) == ("stored", 0, 1)
    assert any("minutes every" in r.getMessage() for r in caplog.records)
