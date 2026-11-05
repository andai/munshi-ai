"""The hourly horizon top-up (`andai_intake.horizon`) on a disposable Postgres: recurring invites
scheduled by the processor at T0, topped up with the clock moved on."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

import mailkit
from andai_core.db.session import transaction
from andai_intake.auth.verdict import Policy
from andai_intake.horizon import HorizonRunner
from andai_intake.limits import IntakeLimits
from andai_intake.processor import Processor
from andai_intake.settings import IntakeSettings
from invitekit import NOTETAKER
from schedulekit import Tenant, deliver, drain, meetings_of

# A fixed clock far enough ahead that every start is in the future whenever the tests run.
T0 = datetime(2030, 1, 3, 9, 0, tzinfo=UTC)  # a Thursday
FIRST = datetime(2030, 1, 7, 4, 30, tzinfo=UTC)  # the Monday after
WEEK = timedelta(days=7)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def org(app_engine: Engine) -> Tenant:
    return Tenant(app_engine, mailkit.Kit())


@pytest.fixture
def proc(app_engine: Engine, org: Tenant, clock: Clock) -> Processor:
    settings = IntakeSettings(ANDAI_ENV="dev", ANDAI_INTAKE_ADDRESSES=NOTETAKER)
    return Processor(
        app_engine, settings, IntakeLimits.memory(), policy=Policy(resolver=org.kit.zone),
        clock=clock, batch=50,
    )


@pytest.fixture
def runner(app_engine: Engine, clock: Clock) -> HorizonRunner:
    return HorizonRunner(app_engine, language="en", clock=clock)


def starts(superuser_engine: Engine, org_id: uuid.UUID) -> list[datetime]:
    return [
        m["scheduled_start"]
        for m in meetings_of(superuser_engine, org_id)
        if m["status"] == "scheduled"
    ]


def weekly(org: Tenant, *extra: str, **kwargs) -> bytes:
    return org.invite(start=FIRST, extra=("RRULE:FREQ=WEEKLY", *extra), dtstamp=T0, **kwargs)


def test_a_weekly_series_gets_its_third_occurrence_a_week_later(
    proc: Processor,
    runner: HorizonRunner,
    org: Tenant,
    clock: Clock,
    app_engine: Engine,
    superuser_engine: Engine,
) -> None:
    assert drain(proc, deliver(app_engine, weekly(org))).outcome == "scheduled"
    assert starts(superuser_engine, org.id) == [FIRST, FIRST + WEEK]

    assert runner.top_up_org(org.id) == 0  # nothing new yet
    clock.now = T0 + WEEK
    assert org.id in runner.orgs()
    assert runner.top_up_org(org.id) == 1
    assert runner.top_up_org(org.id) == 0  # idempotent
    assert starts(superuser_engine, org.id) == [FIRST, FIRST + WEEK, FIRST + 2 * WEEK]


def test_the_top_up_respects_exdate(
    proc: Processor,
    runner: HorizonRunner,
    org: Tenant,
    clock: Clock,
    app_engine: Engine,
    superuser_engine: Engine,
) -> None:
    skipped = FIRST + 2 * WEEK
    exdate = f"EXDATE:{skipped.strftime('%Y%m%dT%H%M%SZ')}"
    drain(proc, deliver(app_engine, weekly(org, exdate)))
    clock.now = T0 + 2 * WEEK
    runner.run_once()
    assert starts(superuser_engine, org.id) == [FIRST, FIRST + WEEK, FIRST + 3 * WEEK]


def test_a_cancelled_series_adds_nothing(
    proc: Processor,
    runner: HorizonRunner,
    org: Tenant,
    clock: Clock,
    app_engine: Engine,
    superuser_engine: Engine,
) -> None:
    uid = f"series-{uuid.uuid4().hex}@{org.domain}"
    drain(proc, deliver(app_engine, weekly(org, uid=uid)))
    cancel = org.invite(uid=uid, start=FIRST, method="CANCEL", sequence=1, link=None, dtstamp=T0,
                        extra=("RRULE:FREQ=WEEKLY",))
    assert drain(proc, deliver(app_engine, cancel)).outcome == "cancelled"
    assert org.id not in runner.orgs()
    clock.now = T0 + WEEK
    assert runner.top_up_org(org.id) == 0
    assert starts(superuser_engine, org.id) == []


def test_auto_join_off_stops_the_top_up(
    proc: Processor,
    runner: HorizonRunner,
    org: Tenant,
    clock: Clock,
    app_engine: Engine,
    superuser_engine: Engine,
) -> None:
    drain(proc, deliver(app_engine, weekly(org)))
    org.auto_join("off")
    clock.now = T0 + WEEK
    assert runner.top_up_org(org.id) == 0
    assert starts(superuser_engine, org.id) == [FIRST, FIRST + WEEK]


def test_the_top_up_keeps_skips_and_runs_as_each_org(
    proc: Processor,
    runner: HorizonRunner,
    org: Tenant,
    clock: Clock,
    app_engine: Engine,
    superuser_engine: Engine,
) -> None:
    drain(proc, deliver(app_engine, weekly(org)))
    with transaction(app_engine, org_id=org.id) as tx:
        tx.execute(
            text(
                "UPDATE meetings SET skipped_at = now()"
                " WHERE org_id = :o AND scheduled_start = :s"
            ),
            {"o": org.id, "s": FIRST + WEEK},
        )
    other = Tenant(app_engine, org.kit)
    clock.now = T0 + WEEK
    runner.run_once()
    rows = {m["scheduled_start"]: m for m in meetings_of(superuser_engine, org.id)}
    assert rows[FIRST + WEEK]["skipped_at"] is not None
    assert rows[FIRST + 2 * WEEK]["skipped_at"] is None
    assert meetings_of(superuser_engine, other.id) == []
