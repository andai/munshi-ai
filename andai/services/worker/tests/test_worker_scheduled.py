"""Join-time dispatch: the worker claims scheduled meetings at their start minus the lead
(`andai_claim_scheduled`, SKIP LOCKED and a lease) and dispatches each once, as its requester,
through `Meetings.start_scheduled` against the in-memory platform. The clock is injected.

Every test runs on a brand-new database: the claim is cross-org, so another test's scheduled
meetings must not be due here.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text

from andai_core.db.models import Meeting, User
from andai_core.db.session import transaction
from andai_core.meetings.service import MAX_DISPATCH_ATTEMPTS, Meetings, join_url_key
from andai_worker.scheduled import ScheduledRunner, TickResult
from andai_worker.settings import WorkerSettings

from .worker_support import Harness, give_principal, make_harness, new_member, zoom_link

LEAD_S = 120


@dataclass
class World:
    engine: Engine
    harness: Harness
    meetings: Meetings
    org_id: uuid.UUID
    requester: uuid.UUID
    key: str | None

    @property
    def clock(self):
        return self.harness.clock

    def runner(self) -> ScheduledRunner:
        return ScheduledRunner(self.engine, self.meetings, lead_s=LEAD_S, clock=self.clock)

    def tick(self, runner: ScheduledRunner | None = None) -> TickResult:
        return asyncio.run((runner or self.runner()).tick())

    def scheduled(self, start: datetime, *, requester: uuid.UUID | None = None) -> uuid.UUID:
        meeting_id, link = uuid.uuid4(), zoom_link()
        now = self.clock()
        with transaction(self.engine, org_id=self.org_id) as tx:
            tx.add(
                Meeting(
                    id=meeting_id, org_id=self.org_id, requested_by=requester or self.requester,
                    source="invite_mail", platform="zoom", join_url=link,
                    join_url_key=join_url_key(link), status="scheduled",
                    settings_snapshot={"language": "en"}, scheduled_start=start,
                    created_at=now, updated_at=now,
                )
            )
        return meeting_id

    def set(self, meeting_id: uuid.UUID, **values) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in values)
        with transaction(self.engine, org_id=self.org_id) as tx:
            tx.execute(
                text(f"UPDATE meetings SET {sets} WHERE id = :m"), {"m": meeting_id, **values}
            )

    def row(self, meeting_id: uuid.UUID) -> Meeting:
        with transaction(self.engine, org_id=self.org_id) as tx:
            return tx.get(Meeting, meeting_id)


def make_world(engine: Engine, *, principal: bool = True) -> World:
    harness = make_harness(engine)
    member = new_member(engine, role="member")
    key = give_principal(harness, engine, member.org_id) if principal else None
    s = harness.services
    meetings = Meetings(engine, s.keyring, s.gateway, s.settings, s.clock)
    return World(engine, harness, meetings, member.org_id, member.user_id, key)


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    return make_world(fresh_app_engine)


# ─── settings ─────────────────────────────────────────────────────────────────────────────────────


def test_tick_and_lead_defaults_and_bounds() -> None:
    settings = WorkerSettings(ANDAI_ENV="dev")
    assert (settings.scheduled_tick_s, settings.join_lead_s) == (15.0, 120)
    with pytest.raises(ValidationError):
        WorkerSettings(ANDAI_ENV="dev", ANDAI_SCHEDULED_TICK_S="31")
    with pytest.raises(ValidationError):
        WorkerSettings(ANDAI_ENV="dev", ANDAI_INTAKE_JOIN_LEAD_S="-1")


# ─── on time, once ────────────────────────────────────────────────────────────────────────────────


def test_due_at_start_minus_the_lead_and_not_before(world: World) -> None:
    start = world.clock() + timedelta(minutes=10)
    meeting_id = world.scheduled(start)

    world.clock.now = start - timedelta(seconds=LEAD_S + 1)
    assert world.tick() == TickResult()
    assert world.row(meeting_id).status == "scheduled"

    world.clock.now = start - timedelta(seconds=LEAD_S)
    assert world.tick() == TickResult(claimed=1, outcomes={"dispatched": 1})

    row = world.row(meeting_id)
    assert (row.status, row.dispatch_attempts) == ("dispatching", 1)
    assert row.platform_meeting_id is not None
    assert len(world.harness.gateway.dispatches) == 1
    world.clock.advance(minutes=30)
    assert world.tick() == TickResult()  # dispatched once
    assert len(world.harness.gateway.dispatches) == 1


def test_a_skipped_meeting_is_never_dispatched_and_an_unskipped_one_is(world: World) -> None:
    start = world.clock() + timedelta(minutes=5)
    skipped = world.scheduled(start)
    unskipped = world.scheduled(start)
    for meeting_id in (skipped, unskipped):
        world.set(meeting_id, skipped_at=world.clock(), skipped_by=world.requester)
    world.set(unskipped, skipped_at=None, skipped_by=None)  # un-skipped before its time

    world.clock.now = start - timedelta(seconds=LEAD_S)
    assert world.tick() == TickResult(claimed=1, outcomes={"dispatched": 1})
    world.clock.now = start + timedelta(minutes=30)
    assert world.tick() == TickResult()

    assert world.row(skipped).status == "scheduled"
    assert world.row(unskipped).status == "dispatching"
    assert len(world.harness.gateway.dispatches) == 1


def test_a_meeting_that_already_started_goes_on_the_next_tick(world: World) -> None:
    # andai-intake's `joined_now`: an invite that arrived after the start is scheduled for now.
    meeting_id = world.scheduled(world.clock())
    assert world.tick() == TickResult(claimed=1, outcomes={"dispatched": 1})
    assert world.row(meeting_id).status == "dispatching"


def test_two_workers_never_dispatch_the_same_meeting(world: World) -> None:
    start = world.clock()
    ids = {world.scheduled(start) for _ in range(5)}
    runners = [world.runner() for _ in range(6)]
    barrier = threading.Barrier(len(runners))
    claimed: list[list] = [[] for _ in runners]

    def claim(i: int) -> None:
        barrier.wait()
        claimed[i] = runners[i].claim()

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(len(runners))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    taken = [c.meeting_id for batch in claimed for c in batch]
    assert sorted(taken) == sorted(ids)  # each exactly once

    async def both() -> list[TickResult]:
        return list(await asyncio.gather(world.runner().tick(), world.runner().tick()))

    world.clock.advance(seconds=31)  # every lease has run out; still `scheduled`
    results = asyncio.run(both())
    assert sum(r.claimed for r in results) == 5
    assert len(world.harness.gateway.dispatches) == 5
    assert {world.row(m).status for m in ids} == {"dispatching"}


def test_a_worker_that_died_after_its_claim_is_replaced_once_the_lease_runs_out(
    world: World,
) -> None:
    meeting_id = world.scheduled(world.clock())
    [claimed] = world.runner().claim()  # and the worker dies
    assert claimed.attempts == 1

    world.clock.advance(seconds=29)
    assert world.tick() == TickResult()
    world.clock.advance(seconds=1)
    assert world.tick() == TickResult(claimed=1, outcomes={"dispatched": 1})
    assert world.row(meeting_id).dispatch_attempts == 2


# ─── who it is sent as ────────────────────────────────────────────────────────────────────────────


def test_it_is_sent_with_its_orgs_platform_key_and_keeps_source_invite_mail(
    world: World,
) -> None:
    # The audit row (`meeting.dispatch` by the requester) is asserted in andai-core's
    # test_scheduled_dispatch.py; the app role cannot read audit events.
    meeting_id = world.scheduled(world.clock())
    world.tick()
    row = world.row(meeting_id)
    gateway, admin_api = world.harness.gateway, world.harness.admin_api
    [org_user] = [t["user_id"] for t in admin_api.tokens.values() if t["token"] == world.key]
    assert gateway.owners[row.platform_meeting_id] == org_user
    assert (row.source, row.requested_by) == ("invite_mail", world.requester)


def test_a_requester_no_longer_active_sends_nothing_and_the_meeting_is_cancelled(
    world: World,
) -> None:
    meeting_id = world.scheduled(world.clock())
    with transaction(world.engine) as tx:
        tx.get(User, world.requester).status = "disabled"

    assert world.tick() == TickResult(claimed=1, outcomes={"cancelled": 1})

    row = world.row(meeting_id)
    assert (row.status, row.end_reason, row.failure_reason) == (
        "cancelled", "requester_inactive", None
    )
    assert world.harness.gateway.dispatches == []


def test_no_principal_is_retried_with_backoff_then_failed(fresh_app_engine: Engine) -> None:
    world = make_world(fresh_app_engine, principal=False)
    meeting_id = world.scheduled(world.clock())
    leases = []
    for attempt in range(1, MAX_DISPATCH_ATTEMPTS):
        assert world.tick() == TickResult(claimed=1, outcomes={"retry": 1})
        row = world.row(meeting_id)
        assert (row.status, row.dispatch_attempts) == ("scheduled", attempt)
        leases.append(row.dispatch_lease_until - world.clock())
        assert world.tick() == TickResult()  # not before the backoff ran out
        world.clock.now = row.dispatch_lease_until

    assert leases == [timedelta(seconds=30 * 2**n) for n in range(MAX_DISPATCH_ATTEMPTS - 1)]
    assert world.tick() == TickResult(claimed=1, outcomes={"failed": 1})
    row = world.row(meeting_id)
    assert (row.status, row.failure_reason) == ("failed", "internal_error")
    assert world.harness.gateway.dispatches == []


def test_one_meeting_failing_does_not_stop_the_others(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken, fine = world.scheduled(world.clock()), world.scheduled(world.clock())
    real = world.meetings.start_scheduled

    async def start(actor, meeting_id, *, lead):
        if meeting_id == broken:
            raise RuntimeError("boom")
        return await real(actor, meeting_id, lead=lead)

    monkeypatch.setattr(world.meetings, "start_scheduled", start)
    assert world.tick() == TickResult(claimed=2, outcomes={"error": 1, "dispatched": 1})
    assert (world.row(broken).status, world.row(fine).status) == ("scheduled", "dispatching")
