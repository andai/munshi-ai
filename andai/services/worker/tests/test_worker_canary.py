"""The canary scheduler: one claim per due run under concurrency (SKIP LOCKED), dispatch through
the product path as the canary org only, outcomes from the canary meeting's states (kept current
by andai-core's reconciler over the in-memory platform), the stop after the hold, and the give-up.

Every test runs on a brand-new database: there is at most one canary org per database.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select, text

from andai_core.db.models import CanaryRun, ConsentRecord, Meeting
from andai_core.db.session import transaction
from andai_core.meetings.reconciler import Reconciler
from andai_core.meetings.service import Actor, MeetingError, Meetings, canary_actor
from andai_worker.canary import HOLD_S, CanaryRunner, tick_seconds
from andai_worker.settings import WorkerSettings
from .worker_support import Harness, canary_setup, give_principal, make_harness, new_member, zoom_link

STARTED = "2026-10-29T10:00:00Z"
ENDED = "2026-10-29T10:05:00Z"


@dataclass
class World:
    engine: Engine
    harness: Harness
    actor: Actor
    meetings: Meetings
    urls: dict[str, str]

    def runner(self, urls: dict[str, str] | None = None, interval_s: int = 3600) -> CanaryRunner:
        return CanaryRunner(
            self.engine, self.meetings, urls or self.urls, interval_s=interval_s,
            clock=self.harness.clock,
        )

    def runs(self) -> list[CanaryRun]:
        with transaction(self.engine) as tx:
            return list(tx.scalars(select(CanaryRun).order_by(CanaryRun.created_at)).all())

    def meeting(self, meeting_id: uuid.UUID) -> Meeting:
        with transaction(self.engine, org_id=self.actor.org_id) as tx:
            return tx.get(Meeting, meeting_id)

    def platform_id(self, meeting_id: uuid.UUID) -> int:
        return self.meeting(meeting_id).platform_meeting_id

    def reconcile(self) -> None:
        asyncio.run(Reconciler(self.harness.services).tick())


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    harness = make_harness(fresh_app_engine)
    canary_setup(harness, fresh_app_engine)
    actor = canary_actor(fresh_app_engine)
    assert actor is not None
    s = harness.services
    meetings = Meetings(fresh_app_engine, s.keyring, s.gateway, s.settings, s.clock)
    urls = {
        "zoom": zoom_link("normal", passcode="canaryZ"),
        "google_meet": "https://meet.google.com/abc-defg-hij",
        "teams": "https://teams.microsoft.com/meet/123456789?p=canaryT",
    }
    return World(fresh_app_engine, harness, actor, meetings, urls)


# ─── settings ─────────────────────────────────────────────────────────────────────────────────────


def test_interval_is_at_most_four_hours_and_links_are_secret() -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(ANDAI_CANARY_INTERVAL_S="14401")
    settings = WorkerSettings(
        ANDAI_CANARY_ZOOM_URL="https://zoom.us/j/12345678901?pwd=s3cret",
        ANDAI_CANARY_MEET_URL="  ",
    )
    assert settings.canary_interval_s == 14400
    assert settings.canary_urls == {"zoom": "https://zoom.us/j/12345678901?pwd=s3cret"}
    assert "s3cret" not in repr(settings)
    assert (tick_seconds(14400), tick_seconds(20)) == (60.0, 5.0)


# ─── schedule and claim ───────────────────────────────────────────────────────────────────────────


def test_two_workers_schedule_one_pending_run_per_platform(world: World) -> None:
    a, b = world.runner(), world.runner()

    assert a.schedule() == 3
    assert b.schedule() == 0
    assert a.schedule() == 0
    runs = world.runs()
    assert sorted(r.platform for r in runs) == ["google_meet", "teams", "zoom"]
    assert {r.state for r in runs} == {"scheduled"}
    assert all(r.due_at == world.harness.clock() for r in runs)


def test_scheduling_keeps_working_once_the_statement_is_prepared(world: World) -> None:
    """psycopg prepares a statement on a connection after 5 runs; Postgres may then plan it
    generically, and ON CONFLICT must still find the partial unique index."""
    runner = world.runner()
    created = [runner.schedule() for _ in range(12)]

    assert created == [3] + [0] * 11


def test_a_locked_due_run_is_skipped_not_waited_for_and_claimed_once(world: World) -> None:
    a, b = world.runner(), world.runner()
    a.schedule()

    # Worker A holds the zoom run's row lock (mid-claim) while worker B claims.
    with transaction(world.engine) as held:
        held.execute(text("SELECT id FROM canary_runs WHERE platform = 'zoom' FOR UPDATE"))
        claimed_b = b.claim()
    claimed_a = a.claim()

    assert sorted(c.platform for c in claimed_b) == ["google_meet", "teams"]
    assert [c.platform for c in claimed_a] == ["zoom"]
    assert a.claim() == [] and b.claim() == []
    assert {r.state for r in world.runs()} == {"running"}


def test_concurrent_claims_start_each_due_run_exactly_once(world: World) -> None:
    runners = [world.runner() for _ in range(6)]
    runners[0].schedule()
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

    platforms = [c.platform for batch in claimed for c in batch]
    assert sorted(platforms) == ["google_meet", "teams", "zoom"]


def test_the_next_run_is_due_one_interval_after_the_last_start(world: World) -> None:
    runner = world.runner({"zoom": world.urls["zoom"]}, interval_s=600)
    runner.schedule()
    [claimed] = runner.claim()
    started = world.harness.clock()
    with transaction(world.engine) as tx:
        tx.execute(
            text(
                "UPDATE canary_runs SET state = 'done', outcome = 'success', finished_at = now()"
                " WHERE id = :i"
            ),
            {"i": claimed.run_id},
        )

    world.harness.clock.advance(seconds=60)
    assert runner.schedule() == 1
    assert runner.claim() == []  # not due yet
    pending = [r for r in world.runs() if r.state == "scheduled"]
    assert [r.due_at for r in pending] == [started + timedelta(seconds=600)]
    world.harness.clock.advance(seconds=540)
    assert [c.platform for c in runner.claim()] == ["zoom"]


# ─── dispatch and outcomes ────────────────────────────────────────────────────────────────────────


def test_success_holds_then_stops_the_bot_and_finishes_when_the_meeting_ends(
    world: World,
) -> None:
    runner = world.runner({"zoom": world.urls["zoom"]})
    gateway = world.harness.gateway

    asyncio.run(runner.tick())
    [run] = world.runs()
    assert (run.state, run.outcome) == ("running", None)
    meeting = world.meeting(run.meeting_id)
    assert (meeting.org_id, meeting.source, meeting.status) == (
        world.actor.org_id, "canary", "dispatching"
    )

    gateway.set(world.platform_id(run.meeting_id), status="active", start_time=STARTED)
    world.reconcile()
    asyncio.run(runner.tick())
    [run] = world.runs()
    assert (run.state, run.outcome, run.reason) == ("running", "success", None)
    assert gateway.stops == []

    world.harness.clock.advance(seconds=HOLD_S)
    asyncio.run(runner.tick())
    assert gateway.stops == [("zoom", world.meeting(run.meeting_id).native_meeting_id)]

    gateway.set(
        world.platform_id(run.meeting_id), status="completed", completion_reason="stopped",
        end_time=ENDED,
    )
    world.reconcile()
    asyncio.run(runner.tick())
    [run] = world.runs()
    assert (run.state, run.outcome) == ("done", "success")
    assert world.meeting(run.meeting_id).status == "done"


@pytest.mark.parametrize(
    ("completion", "evidence", "reason"),
    [
        ("awaiting_admission_rejected", {}, "admission_denied"),
        ("awaiting_admission_timeout", {}, "admission_timeout"),
        ("join_failure", {"reason": "never_reached_lobby"}, "ui_changed"),
    ],
)
def test_a_failed_canary_meeting_fails_the_run_with_its_reason(
    world: World, completion: str, evidence: dict, reason: str
) -> None:
    runner = world.runner({"zoom": world.urls["zoom"]})
    asyncio.run(runner.tick())
    [run] = world.runs()
    platform_id = world.platform_id(run.meeting_id)
    world.harness.gateway.set(
        platform_id, status="failed", completion_reason=completion,
        failure_stage="awaiting_admission",
    )
    world.harness.gateway.set_data(platform_id, join_evidence=evidence)
    world.reconcile()

    asyncio.run(runner.tick())

    [run] = world.runs()
    assert (run.state, run.outcome, run.reason) == ("done", "failure", reason)
    assert run.finished_at is not None


def test_a_canary_left_waiting_is_given_up_and_its_bot_stopped(world: World) -> None:
    runner = world.runner({"zoom": world.urls["zoom"]})
    asyncio.run(runner.tick())
    [run] = world.runs()
    world.harness.gateway.set(world.platform_id(run.meeting_id), status="awaiting_admission")
    world.reconcile()

    world.harness.clock.advance(seconds=world.harness.services.settings.lobby_timeout_s + 301)
    asyncio.run(runner.tick())

    [run] = world.runs()
    assert (run.state, run.outcome, run.reason) == ("done", "failure", "admission_timeout")
    assert len(world.harness.gateway.stops) == 1


def test_a_link_the_platform_refuses_fails_the_run_at_once(world: World) -> None:
    runner = world.runner({"zoom": "https://zoom.us/j/not-a-meeting"})

    asyncio.run(runner.tick())

    [run] = world.runs()
    assert (run.state, run.outcome, run.reason, run.meeting_id) == (
        "done", "failure", "meeting_not_found", None
    )


def test_canaries_are_exempt_from_the_no_rejoin_rule(world: World) -> None:
    link = world.urls["zoom"]
    runner = world.runner({"zoom": link}, interval_s=60)
    asyncio.run(runner.tick())
    [first] = world.runs()
    # Participants removed the canary bot (consent record with removed_at, D-38).
    world.harness.gateway.set(
        world.platform_id(first.meeting_id), status="completed", completion_reason="evicted",
        start_time=STARTED, end_time=ENDED,
    )
    world.reconcile()
    with transaction(world.engine, org_id=world.actor.org_id) as tx:
        record = tx.get(ConsentRecord, first.meeting_id)
        assert record is not None and record.removed_at is not None
    asyncio.run(runner.tick())

    world.harness.clock.advance(seconds=60)
    asyncio.run(runner.tick())

    runs = world.runs()
    assert len(runs) == 2 and runs[1].meeting_id not in (None, first.meeting_id)


def test_only_the_canary_org_sends_canaries_and_it_sends_nothing_else(world: World) -> None:
    customer = new_member(world.engine)
    give_principal(world.harness, world.engine, customer.org_id)
    customer_actor = Actor(customer.org_id, customer.user_id, "owner")

    with pytest.raises(MeetingError) as as_customer:
        asyncio.run(world.meetings.join(
            customer_actor, join_url=zoom_link(), language=None, team_id=None, source="canary"
        ))
    with pytest.raises(MeetingError) as as_portal:
        asyncio.run(world.meetings.join(
            world.actor, join_url=zoom_link(), language=None, team_id=None
        ))

    assert (as_customer.value.status, as_customer.value.code) == (403, "canary_org_only")
    assert (as_portal.value.status, as_portal.value.code) == (403, "canary_org_only")
    assert world.harness.gateway.dispatches == []


def test_without_a_canary_org_the_runner_does_nothing(fresh_app_engine: Engine) -> None:
    harness = make_harness(fresh_app_engine)
    s = harness.services
    runner = CanaryRunner(
        fresh_app_engine,
        Meetings(fresh_app_engine, s.keyring, s.gateway, s.settings, s.clock),
        {"zoom": zoom_link()},
        interval_s=60,
        clock=harness.clock,
    )

    result = asyncio.run(runner.tick())

    assert (result.scheduled, result.started) == (0, 0)
    with transaction(fresh_app_engine) as tx:
        assert tx.execute(text("SELECT count(*) FROM canary_runs")).scalar_one() == 0
