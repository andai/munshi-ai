"""The status reconciler, run as `andai_app` with no ambient org context, over two orgs and the
in-memory platform gateway, with the harness's settable clock."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from andai_core.db.models import ConsentRecord, Meeting
from andai_core.db.session import transaction
from andai_core.meetings.reconciler import Reconciler
from conftest import (
    FakeGateway,
    Harness,
    RoleOrg,
    give_principal,
    make_harness,
    role_org,
    sign_in,
    zoom_link,
)

JOIN = "/api/v1/meetings/join"
STARTED = "2026-10-28T10:00:00Z"
ENDED = "2026-10-28T10:30:00Z"
#: The harness clock at the start of each test: just after STARTED, inside the 60 s grace for the
#: bot's disclosure report, so the tests read the same whatever day they run.
NOW = datetime(2026, 10, 28, 10, 0, 30, tzinfo=UTC)


@dataclass
class World:
    harness: Harness
    engine: Engine
    a: RoleOrg
    b: RoleOrg
    reconciler: Reconciler

    @property
    def gateway(self) -> FakeGateway:
        return self.harness.gateway

    def join(self, org: RoleOrg, link: str | None = None) -> dict:
        response = sign_in(self.harness, org.member).post(
            JOIN, json={"join_url": link or zoom_link()}
        )
        assert response.status_code == 201, response.text
        return response.json()

    def platform_id(self, meeting: dict) -> int:
        return self.gateway.by_native(meeting["native_meeting_id"])["id"]

    def tick(self):
        return asyncio.run(self.reconciler.tick())

    def row(self, org: RoleOrg, meeting: dict) -> Meeting:
        with transaction(self.engine, org_id=org.owner.org_id) as tx:
            return tx.scalar(select(Meeting).where(Meeting.id == uuid.UUID(meeting["id"])))

    def consent(self, org: RoleOrg, meeting: dict) -> ConsentRecord | None:
        with transaction(self.engine, org_id=org.owner.org_id) as tx:
            return tx.scalar(
                select(ConsentRecord).where(ConsentRecord.meeting_id == uuid.UUID(meeting["id"]))
            )


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    """A fresh database, so the open-meeting org list holds these two orgs only."""
    harness = make_harness(fresh_app_engine)
    harness.clock.now = NOW
    a, b = role_org(fresh_app_engine), role_org(fresh_app_engine)
    for org in (a, b):
        give_principal(harness, fresh_app_engine, org.owner.org_id)
    reconciler = Reconciler(harness.services, interval=0.05)
    yield World(harness, fresh_app_engine, a, b, reconciler)
    reconciler.release()


def test_transitions_land_for_both_orgs_through_to_the_end(world: World) -> None:
    ma, mb = world.join(world.a), world.join(world.b)
    world.gateway.set(world.platform_id(ma), status="active", start_time=STARTED)
    world.gateway.set(world.platform_id(mb), status="awaiting_admission")

    first = world.tick()

    assert (first.orgs, first.updated) == (2, 2)
    assert world.row(world.a, ma).status == "recording"
    assert world.row(world.a, ma).started_at.isoformat() == "2026-10-28T10:00:00+00:00"
    assert world.row(world.b, mb).status == "awaiting_admission"
    record = world.consent(world.a, ma)
    assert (record.disclosure_channel, record.disclosure_detail) == ("bot_name_only", "pending")
    assert record.bot_display_name == "munshi.ai (recording)"
    assert record.privacy_notice_url == "http://portal.test/privacy"
    assert record.disclosure_text == world.harness.services.settings.disclosure_text
    assert world.consent(world.b, mb) is None  # not admitted yet

    world.gateway.set_data(
        world.platform_id(ma), disclosure={"channel": "chat", "posted_at": "2026-10-28T10:00:01Z"}
    )
    world.gateway.set(
        world.platform_id(ma), status="completed", completion_reason="stopped", end_time=ENDED
    )
    world.gateway.set(
        world.platform_id(mb), status="failed", completion_reason="awaiting_admission_rejected",
        failure_stage="awaiting_admission",
    )
    world.tick()

    done, failed = world.row(world.a, ma), world.row(world.b, mb)
    assert (done.status, done.end_reason, done.ended_at.isoformat()) == (
        "done", "stopped", "2026-10-28T10:30:00+00:00"
    )
    assert (failed.status, failed.failure_reason) == ("failed", "admission_denied")
    record = world.consent(world.a, ma)
    assert (record.disclosure_channel, record.disclosure_detail) == ("chat", None)
    assert record.disclosure_posted_at.isoformat() == "2026-10-28T10:00:01+00:00"
    # Nothing is open any more: the next pass has no org to visit.
    assert world.tick().orgs == 0


def test_a_jump_from_the_lobby_to_evicted_still_writes_consent_and_blocks_the_link(
    world: World,
) -> None:
    link = zoom_link()
    meeting = world.join(world.a, link)
    world.gateway.set(world.platform_id(meeting), status="awaiting_admission")
    world.tick()
    assert world.row(world.a, meeting).status == "awaiting_admission"

    # Between two polls: admitted, then removed by a participant.
    world.gateway.set(
        world.platform_id(meeting), status="completed", completion_reason="evicted",
        start_time=STARTED, end_time=ENDED,
    )
    world.tick()

    row = world.row(world.a, meeting)
    assert (row.status, row.end_reason) == ("done", "evicted")
    record = world.consent(world.a, meeting)
    assert record is not None
    assert record.removed_at.isoformat() == "2026-10-28T10:30:00+00:00"
    assert (record.disclosure_channel, record.disclosure_detail) == ("bot_name_only", "no_report")
    assert record.admitted_at.isoformat() == "2026-10-28T10:00:00+00:00"

    # D-38: the same link is refused while the removal is recent (the clock is "now", the removal
    # time is the platform's end time; move the clock next to it).
    world.harness.clock.now = datetime(2026, 10, 28, 11, 0, tzinfo=UTC)
    again = sign_in(world.harness, world.a.member).post(JOIN, json={"join_url": link})
    assert (again.status_code, again.json()["code"]) == (409, "removed_by_participant")


def test_no_report_after_60_seconds_and_a_late_report_still_wins(world: World) -> None:
    meeting = world.join(world.a)
    world.gateway.set(world.platform_id(meeting), status="active", start_time=None)
    world.tick()
    assert world.consent(world.a, meeting).disclosure_detail == "pending"

    world.harness.clock.advance(seconds=59)
    world.tick()
    assert world.consent(world.a, meeting).disclosure_detail == "pending"
    world.harness.clock.advance(seconds=2)
    world.tick()
    assert world.consent(world.a, meeting).disclosure_detail == "no_report"

    world.gateway.set_data(
        world.platform_id(meeting),
        disclosure={"channel": "bot_name_only", "detail": "composer_unavailable"},
    )
    world.tick()
    record = world.consent(world.a, meeting)
    assert (record.disclosure_channel, record.disclosure_detail) == (
        "bot_name_only", "composer_unavailable"
    )


def test_left_alone_ends_done_with_the_reason(world: World) -> None:
    meeting = world.join(world.a)
    world.gateway.set(
        world.platform_id(meeting), status="completed", completion_reason="left_alone",
        start_time=STARTED, end_time=ENDED,
    )
    world.tick()
    row = world.row(world.a, meeting)
    assert (row.status, row.end_reason, row.failure_reason) == ("done", "left_alone", None)
    assert world.consent(world.a, meeting).removed_at is None


def test_a_failure_is_classified_on_the_full_record(world: World) -> None:
    """The list view leaves out `last_error`; the detail read carries Teams' refusal."""
    meeting = world.join(world.a)
    pid = world.platform_id(meeting)
    world.gateway.set(pid, status="failed", completion_reason="join_failure", failure_stage="joining")
    world.gateway.set_data(
        pid,
        last_error={"exit_code": 1, "reason": "teams_join_disabled: Teams kept Join now disabled",
                    "error_details": None},
    )
    world.tick()
    assert ("GET", f"/meetings/{pid}") in world.gateway.calls
    assert world.row(world.a, meeting).failure_reason == "blocked_by_policy"


def test_a_dispatch_that_never_answered_fails_after_60_seconds(world: World) -> None:
    owner = world.a.owner
    young, old = uuid.uuid4(), uuid.uuid4()
    now = world.harness.clock()
    from datetime import timedelta

    with transaction(world.engine, org_id=owner.org_id) as tx:
        for meeting_id, age in ((young, 30), (old, 61)):
            tx.add(
                Meeting(
                    id=meeting_id, org_id=owner.org_id, requested_by=owner.user_id,
                    platform="zoom", join_url=f"https://zoom.us/j/{meeting_id.hex}",
                    join_url_key=f"zoom.us/j/{meeting_id.hex}", status="dispatching",
                    settings_snapshot={}, created_at=now - timedelta(seconds=age),
                    updated_at=now - timedelta(seconds=age),
                )
            )

    world.tick()

    with transaction(world.engine, org_id=owner.org_id) as tx:
        states = dict(tx.execute(select(Meeting.id, Meeting.failure_reason)).all())
    assert states[old] == "internal_error"
    assert states[young] is None
    assert world.gateway.calls == []  # no platform id, nothing to ask


def test_a_users_stop_is_not_overwritten(world: World) -> None:
    meeting = world.join(world.a)
    world.gateway.set(world.platform_id(meeting), status="awaiting_admission")
    reconciler = world.reconciler
    open_rows, key = reconciler._read(world.a.owner.org_id)
    observations = asyncio.run(reconciler._observe(key, open_rows))
    # The user stops it between the reconciler's read and its write.
    stop = sign_in(world.harness, world.a.member).post(f"/api/v1/meetings/{meeting['id']}/stop")
    assert stop.json()["status"] == "cancelled"

    reconciler._write(world.a.owner.org_id, open_rows, observations)

    assert world.row(world.a, meeting).status == "cancelled"


def test_a_meeting_the_platform_does_not_know_fails(world: World) -> None:
    meeting = world.join(world.a)
    pid = world.platform_id(meeting)
    del world.gateway.meetings[pid]
    del world.gateway.owners[pid]
    world.tick()
    assert world.row(world.a, meeting).failure_reason == "internal_error"


def test_an_unreachable_gateway_skips_the_org_and_keeps_the_rest(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    ma, mb = world.join(world.a), world.join(world.b)
    world.gateway.set(world.platform_id(ma), status="active")
    world.gateway.set(world.platform_id(mb), status="active")
    world.gateway.fail["list bots"] = 503

    result = world.tick()

    assert (result.orgs, result.failed_orgs, result.updated) == (2, 2, 0)
    assert world.row(world.a, ma).status == "dispatching"
    del world.gateway.fail["list bots"]
    assert world.tick().updated == 2


def test_an_unknown_platform_status_fails_and_is_logged_raw(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="andai_core.meetings.reconciler")
    meeting = world.join(world.a)
    world.gateway.set(world.platform_id(meeting), status="teleported")
    world.tick()
    assert world.row(world.a, meeting).failure_reason == "internal_error"
    assert any("status=teleported" in r.getMessage() for r in caplog.records)


def test_one_runner_at_a_time(world: World) -> None:
    other = Reconciler(world.harness.services)
    try:
        assert world.reconciler.try_lock() is True
        assert world.reconciler.try_lock() is True  # re-entrant for the holder
        assert other.try_lock() is False
        world.reconciler.release()
        assert other.try_lock() is True
        assert world.reconciler.try_lock() is False
    finally:
        other.release()


def test_the_loop_reconciles_within_seconds_and_releases_its_lock(world: World) -> None:
    meeting = world.join(world.a)
    world.gateway.set(world.platform_id(meeting), status="active", start_time=STARTED)

    async def run_briefly() -> None:
        task = asyncio.create_task(world.reconciler.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if world.row(world.a, meeting).status == "recording":
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_briefly())

    assert world.row(world.a, meeting).status == "recording"
    assert world.reconciler.holds_lock is False
    other = Reconciler(world.harness.services)
    try:
        assert other.try_lock() is True
    finally:
        other.release()


def test_the_cross_org_listing_is_called_from_no_http_route() -> None:
    package = Path(__file__).resolve().parents[1] / "andai_core"
    callers = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "andai_open_meeting_orgs" in path.read_text()
    )
    assert callers == ["meetings/reconciler.py"]


def test_the_reconciler_sees_no_rows_without_an_org_context(world: World) -> None:
    world.join(world.a)
    with transaction(world.engine) as tx:
        assert tx.scalar(text("SELECT count(*) FROM meetings")) == 0
    assert set(world.reconciler.open_orgs()) == {world.a.owner.org_id}
