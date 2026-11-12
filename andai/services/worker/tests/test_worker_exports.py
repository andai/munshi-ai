"""Data exports in andai-worker: claims through `andai_claim_exports()` (the only cross-org read),
leased with `FOR UPDATE SKIP LOCKED` so two workers never build one request, a build that turns the
request `ready` with its expiry, the hourly expiry that deletes the sealed bundle, the erasure
registry's rule that deletes a person's exports, and the loops starting with the worker.

Every test runs on a brand-new database, so the claim sees only this test's requests.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from andai_core.db.models import TOMBSTONE_USER_ID
from andai_core.db.session import transaction
from andai_core.dpdp import builder
from andai_core.erasure import runner as erasure_runner
from andai_core.erasure.requests import create_request, remove_from_org
from andai_core.mail import MemoryMailer
from andai_core.meetings import deletion
from andai_core.meetings.service import Meetings
from andai_worker.exports import ExpiryRunner, ExportRunner, TickResult
from andai_worker.main import WorkerServices, jobs
from andai_worker.settings import WorkerSettings
from .worker_support import support


@pytest.fixture
def superuser(fresh_app_engine: Engine, postgres) -> Engine:
    from sqlalchemy import create_engine

    name = fresh_app_engine.url.database
    engine = create_engine(postgres.url("postgres", postgres.superuser_password, name))
    yield engine
    engine.dispose()


@pytest.fixture
def world(fresh_app_engine: Engine):
    return support.export_world(fresh_app_engine)


def _settings(**env: str) -> WorkerSettings:
    return WorkerSettings(ANDAI_ENV="dev", ANDAI_ALERT_EMAIL="", **env)


def _runner(world, **kwargs) -> ExportRunner:
    s = world.harness.services
    return ExportRunner(
        world.engine, s.keyring, s.gateway, _settings(), clock=s.clock, **kwargs
    )


def _pending(world, subject=None) -> uuid.UUID:
    return support.export_request(
        world.engine, world.org_id, subject or world.subject.user_id, state="pending"
    )


def _row(superuser: Engine, request_id: uuid.UUID) -> dict | None:
    with superuser.connect() as conn:
        row = conn.execute(
            text("SELECT state, error, attempts, lease_until, next_at, ready_at, expires_at,"
                 " bundle_ciphertext, key_id, recordings FROM export_requests WHERE id = :r"),
            {"r": request_id},
        ).mappings().first()
    return None if row is None else dict(row)


def test_the_claim_takes_due_requests_of_every_org_and_leases_them(
    world, fresh_app_engine: Engine, superuser: Engine
) -> None:
    first = _pending(world)
    other_org = support.new_member(fresh_app_engine)
    second = support.export_request(
        fresh_app_engine, other_org.org_id, other_org.user_id, state="pending"
    )
    runner = _runner(world)

    claimed = runner.claim()

    assert sorted(claimed) == sorted([(world.org_id, first), (other_org.org_id, second)])
    row = _row(superuser, first)
    assert row["state"] == "running"
    assert row["lease_until"] - world.harness.clock() == timedelta(minutes=10)
    assert runner.claim() == []  # leased
    with transaction(fresh_app_engine) as tx:  # the app role with no org context sees no row
        assert tx.execute(text("SELECT count(*) FROM export_requests")).scalar_one() == 0


def test_a_tick_builds_the_request_ready_with_its_expiry(world, superuser: Engine) -> None:
    request_id = _pending(world)

    result = asyncio.run(_runner(world).tick())

    assert (result.claimed, result.ready) == (1, 1)
    row = _row(superuser, request_id)
    assert row["state"] == "ready" and row["lease_until"] is None
    assert row["expires_at"] - row["ready_at"] == timedelta(days=7)
    assert row["bundle_ciphertext"] and row["key_id"]
    assert len(row["recordings"]) == 2
    assert _runner(world).claim() == []  # a ready request is never claimed again


def test_a_failed_attempt_is_claimed_again_only_after_its_backoff(
    world, superuser: Engine
) -> None:
    request_id = _pending(world)
    world.harness.gateway.fail["list recordings"] = 503
    runner = _runner(world)

    result = asyncio.run(runner.tick())

    assert (result.claimed, result.running) == (1, 1)
    row = _row(superuser, request_id)
    assert (row["state"], row["attempts"], row["lease_until"]) == ("running", 1, None)
    assert runner.claim() == []
    world.harness.clock.advance(seconds=builder.BACKOFF_BASE_S + 1)
    assert runner.claim() == [(world.org_id, request_id)]


def test_two_workers_never_build_the_same_request(
    world, fresh_app_engine: Engine, superuser: Engine
) -> None:
    requests = [_pending(world)]
    for _ in range(3):
        member = support.new_member(fresh_app_engine, role="member", org_id=world.org_id)
        requests.append(_pending(world, member.user_id))
    start = threading.Barrier(2)
    results: list[TickResult] = []
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()

    def worker() -> None:
        runner = _runner(world, batch=1)
        original = runner.claim

        def recording_claim():
            got = original()
            with lock:
                claimed.extend(r for _, r in got)
            return got

        runner.claim = recording_claim
        start.wait()
        for _ in range(4):
            results.append(asyncio.run(runner.tick()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert sorted(claimed) == sorted(requests)  # each exactly once
    assert sum(r.ready for r in results) == 4
    with superuser.connect() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM audit_events WHERE action = 'user.export.ready'")
        ).scalar_one()
    assert rows == 4


def test_the_expiry_deletes_the_bundle_once_its_time_has_passed(
    world, superuser: Engine
) -> None:
    request_id = _pending(world)
    asyncio.run(_runner(world).tick())
    expiry = ExpiryRunner(world.engine, clock=world.harness.clock)

    assert expiry.expire() == 0
    assert _row(superuser, request_id)["state"] == "ready"

    world.harness.clock.advance(days=7, seconds=1)
    assert expiry.expire() == 1

    row = _row(superuser, request_id)
    assert (row["state"], row["bundle_ciphertext"], row["key_id"]) == ("expired", None, None)
    assert expiry.expire() == 0


def test_erasing_the_person_deletes_their_exports_and_keeps_the_admins_others(
    world, superuser: Engine
) -> None:
    s = world.harness.services
    own = support.export_request(world.engine, world.org_id, world.subject.user_id)
    asyncio.run(
        builder.run(world.engine, s.keyring, s.gateway, world.org_id, own,
                    settings=_settings(), clock=s.clock)
    )
    assert _row(superuser, own)["state"] == "ready"
    of_subject_by_admin = support.export_request(
        world.engine, world.org_id, world.subject.user_id, world.admin.user_id, state="pending"
    )
    by_subject_as_requester = support.export_request(
        world.engine, world.org_id, world.other.user_id, world.subject.user_id, state="pending"
    )
    with transaction(world.engine, org_id=world.org_id, user_id=world.owner.user_id) as tx:
        remove_from_org(tx, world.org_id, world.subject.user_id)
        erasure = create_request(
            tx, subject=world.subject.user_id, org_id=world.org_id, audit_org_id=world.org_id,
            source="org_admin", requested_by=world.owner.user_id, account=True, now=s.clock(),
        ).id
    meetings = Meetings(world.engine, s.keyring, s.gateway, s.settings, s.clock)

    for _ in range(40):
        state = asyncio.run(
            erasure_runner.run(world.engine, meetings, world.org_id, erasure, clock=s.clock)
        )
        if state in ("done", "failed"):
            break
        with superuser.connect() as conn:
            pending = conn.execute(
                text("SELECT org_id, id FROM meetings WHERE deletion_state = 'pending'")
            ).all()
        for org_id, meeting_id in pending:
            asyncio.run(deletion.purge(world.engine, s.keyring, s.gateway, org_id, meeting_id,
                                       clock=s.clock))
        world.harness.clock.advance(seconds=15)

    assert state == "done"
    assert _row(superuser, own) is None
    assert _row(superuser, of_subject_by_admin) is None
    with superuser.connect() as conn:
        requester = conn.execute(
            text("SELECT requested_by FROM export_requests WHERE id = :r"),
            {"r": by_subject_as_requester},
        ).scalar_one()
    assert requester == TOMBSTONE_USER_ID
    with superuser.connect() as conn:
        scrub = conn.execute(
            text("SELECT counts -> 'scrub' FROM erasure_requests WHERE id = :r"), {"r": erasure}
        ).scalar_one()
    assert scrub["export_requests.subject_user_id"] == 2
    assert scrub["export_requests.requested_by"] == 1


def test_the_worker_always_starts_the_export_loops_and_builds_a_request(
    world, superuser: Engine, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    from andai_worker import exports

    monkeypatch.setattr(exports, "TICK_S", 0.2)
    settings = _settings()
    s = world.harness.services
    services = WorkerServices(
        settings=settings,
        engine=world.engine,
        meetings=Meetings(world.engine, s.keyring, s.gateway, settings, s.clock),
        mailer=MemoryMailer(),
    )
    request_id = _pending(world)

    async def main() -> int:
        tasks = jobs(services)
        try:
            for _ in range(100):
                if _row(superuser, request_id)["state"] == "ready":
                    break
                await asyncio.sleep(0.1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    with caplog.at_level(logging.INFO, logger="andai_worker.main"):
        started = asyncio.run(main())

    assert started == 7
    assert _row(superuser, request_id)["state"] == "ready"
    assert any("data exports every" in r.getMessage() for r in caplog.records)
