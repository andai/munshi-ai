"""User erasure in andai-worker: claims through `andai_claim_erasures()` (the only cross-org read),
leased with `FOR UPDATE SKIP LOCKED` so two workers never work one request at once, a request
whose wait is not over is not claimed, and the loop starts with the worker and finishes a request
together with the deletion loop.

Every test runs on a brand-new database, so the claim sees only this test's requests.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from andai_core.erasure.requests import create_request, remove_from_org
from andai_core.mail import MemoryMailer
from andai_core.meetings.service import Meetings
from andai_worker.erasure import ErasureRunner, TickResult
from andai_worker.main import WorkerServices, jobs
from andai_worker.settings import WorkerSettings
from .worker_support import Harness, make_harness, new_member, seed_meeting


@pytest.fixture
def harness(fresh_app_engine: Engine) -> Harness:
    return make_harness(fresh_app_engine)


@pytest.fixture
def superuser(fresh_app_engine: Engine, postgres) -> Engine:
    from sqlalchemy import create_engine

    name = fresh_app_engine.url.database
    engine = create_engine(postgres.url("postgres", postgres.superuser_password, name))
    yield engine
    engine.dispose()


def _meetings(harness: Harness, engine: Engine) -> Meetings:
    s = harness.services
    return Meetings(engine, s.keyring, s.gateway, s.settings, s.clock)


def _request(harness: Harness, engine: Engine, *, meetings: int = 0) -> tuple:
    """A member of a fresh org, with `meetings` finished meetings of their own (not on the
    platform), and an open erasure request for them; returns (org id, subject id, request id)."""
    admin = new_member(engine)
    subject = new_member(engine, role="member", org_id=admin.org_id)
    for _ in range(meetings):
        seed_meeting(engine, admin.org_id, subject.user_id)
    with transaction(engine, org_id=admin.org_id, user_id=admin.user_id) as tx:
        remove_from_org(tx, admin.org_id, subject.user_id)
        request = create_request(
            tx, subject=subject.user_id, org_id=admin.org_id, audit_org_id=admin.org_id,
            source="org_admin", requested_by=admin.user_id, account=True,
            now=harness.clock(),
        )
    return admin.org_id, subject.user_id, request.id


def _state(superuser: Engine, request_id: uuid.UUID) -> tuple:
    with superuser.connect() as conn:
        return tuple(
            conn.execute(
                text("SELECT state, step, lease_until, next_at FROM erasure_requests"
                     " WHERE id = :r"),
                {"r": request_id},
            ).one()
        )


def test_the_claim_takes_due_requests_of_every_org_and_leases_them(
    harness: Harness, fresh_app_engine: Engine, superuser: Engine
) -> None:
    engine = fresh_app_engine
    first = _request(harness, engine)
    second = _request(harness, engine)
    runner = ErasureRunner(engine, _meetings(harness, engine), clock=harness.clock)

    claimed = runner.claim()

    assert sorted(claimed) == sorted([(first[0], first[2]), (second[0], second[2])])
    state, _, lease, _ = _state(superuser, first[2])
    assert state == "running"
    assert lease - harness.clock() == timedelta(seconds=60)
    assert runner.claim() == []  # leased
    with transaction(engine) as tx:  # the app role with no org context sees no request
        assert tx.execute(text("SELECT count(*) FROM erasure_requests")).scalar_one() == 0


def test_a_request_waiting_for_its_purge_is_not_claimed_before_it_is_due(
    harness: Harness, fresh_app_engine: Engine, superuser: Engine
) -> None:
    engine = fresh_app_engine
    org_id, subject, request_id = _request(harness, engine, meetings=1)
    runner = ErasureRunner(engine, _meetings(harness, engine), clock=harness.clock)

    result = asyncio.run(runner.tick())

    assert (result.claimed, result.running) == (1, 1)
    state, step, lease, next_at = _state(superuser, request_id)
    assert (state, step, lease) == ("running", "wait_purge", None)
    assert next_at - harness.clock() == timedelta(seconds=10)
    assert runner.claim() == []
    harness.clock.advance(seconds=11)
    assert runner.claim() == [(org_id, request_id)]


def test_two_workers_never_work_the_same_request(
    harness: Harness, fresh_app_engine: Engine, superuser: Engine
) -> None:
    engine = fresh_app_engine
    requests = [_request(harness, engine) for _ in range(6)]
    start = threading.Barrier(2)
    results: list[TickResult] = []
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()

    def worker() -> None:
        runner = ErasureRunner(engine, _meetings(harness, engine), batch=2, clock=harness.clock)
        original = runner.claim

        def recording_claim():
            got = original()
            with lock:
                claimed.extend(r for _, r in got)
            return got

        runner.claim = recording_claim
        start.wait()
        for _ in range(5):
            results.append(asyncio.run(runner.tick()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(claimed) == sorted(r for _, _, r in requests)  # each exactly once
    assert sum(r.done for r in results) == 6
    with superuser.connect() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM audit_events WHERE action = 'user.erase.complete'")
        ).scalar_one()
    assert rows == 6


def test_the_worker_always_starts_the_erasure_loop_and_finishes_with_the_purge(
    harness: Harness, fresh_app_engine: Engine, superuser: Engine,
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    from andai_worker import deletion, erasure

    # Both loops tick every 10 s in production; here every 0.2 s, so the test takes seconds.
    monkeypatch.setattr(erasure, "TICK_S", 0.2)
    monkeypatch.setattr(deletion, "TICK_S", 0.2)

    engine = fresh_app_engine
    settings = WorkerSettings(ANDAI_ENV="dev", ANDAI_ALERT_EMAIL="")
    s = harness.services
    services = WorkerServices(
        settings=settings,
        engine=engine,
        meetings=Meetings(engine, s.keyring, s.gateway, settings, s.clock),
        mailer=MemoryMailer(),
    )
    _, subject, request_id = _request(harness, engine, meetings=2)

    def done() -> bool:
        return _state(superuser, request_id)[0] == "done"

    async def main() -> int:
        tasks = jobs(services)
        try:
            for _ in range(300):
                if done():
                    break
                harness.clock.advance(seconds=11)  # the erasure waits 10 s between looks
                await asyncio.sleep(0.1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    with caplog.at_level(logging.INFO, logger="andai_worker.main"):
        started = asyncio.run(main())

    assert started == 7  # with the export build and expiry loops
    assert done()
    assert any("user erasure every" in r.getMessage() for r in caplog.records)
    with superuser.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM meetings WHERE requested_by = :u"),
                            {"u": subject}).scalar_one() == 0
        assert conn.execute(text("SELECT status FROM users WHERE id = :u"),
                            {"u": subject}).scalar_one() == "erased"
