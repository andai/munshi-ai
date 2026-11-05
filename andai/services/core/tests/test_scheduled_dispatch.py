"""Join-time dispatch of meetings scheduled from invites: `Meetings.start_scheduled` takes the
existing `scheduled` row through the paste-link checks and steps (principal, settings snapshot,
recording notice, audit), and maps every refusal onto the row instead of answering a caller.

Against the in-memory platform gateway; the claim that hands the worker due meetings is
andai-worker's (`test_worker_scheduled.py`).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text

from andai_core.db.models import ConsentRecord, Meeting, User
from andai_core.db.session import transaction
from andai_core.meetings import service
from andai_core.meetings.service import (
    MAX_DISPATCH_ATTEMPTS,
    Actor,
    Meetings,
    ScheduledStart,
    join_url_key,
    requester_actor,
)
from andai_core.platform.gateway_client import GatewayError
from conftest import (
    Harness,
    RoleOrg,
    give_principal,
    make_harness,
    role_org,
    sign_in,
    zoom_link,
)

JOIN = "/api/v1/meetings/join"
LEAD = timedelta(seconds=120)


@dataclass
class World:
    harness: Harness
    engine: Engine
    org: RoleOrg
    meetings: Meetings

    @property
    def org_id(self) -> uuid.UUID:
        return self.org.owner.org_id

    def actor(self, role: str = "member") -> Actor:
        member = self.org.by_role(role)
        return Actor(org_id=member.org_id, user_id=member.user_id,
                     role="member" if role in ("member", "lead") else role)

    def now(self) -> datetime:
        return self.harness.clock()

    def scheduled(self, link: str, *, start: datetime | None = None, role: str = "member",
                  language: str = "hi", **fields) -> uuid.UUID:
        """A meeting andai-intake scheduled from an invite of `role`."""
        meeting_id = uuid.uuid4()
        now = self.now()
        with transaction(self.engine, org_id=self.org_id) as tx:
            tx.add(
                Meeting(
                    id=meeting_id, org_id=self.org_id, requested_by=self.actor(role).user_id,
                    team_id=self.org.team, source="invite_mail", platform="zoom",
                    join_url=link, join_url_key=join_url_key(link), status="scheduled",
                    settings_snapshot={"language": language},
                    scheduled_start=start or now + timedelta(seconds=60),
                    created_at=now, updated_at=now, **fields,
                )
            )
        return meeting_id

    def start(self, meeting_id: uuid.UUID, role: str = "member") -> ScheduledStart:
        return asyncio.run(self.meetings.start_scheduled(self.actor(role), meeting_id, lead=LEAD))

    def row(self, meeting_id: uuid.UUID) -> Meeting:
        with transaction(self.engine, org_id=self.org_id) as tx:
            return tx.get(Meeting, meeting_id)


def make_world(engine: Engine, *, principal: bool = True) -> World:
    harness = make_harness(engine)
    org = role_org(engine)
    if principal:
        give_principal(harness, engine, org.owner.org_id)
    s = harness.services
    meetings = Meetings(engine, s.keyring, s.gateway, s.settings, s.clock)
    return World(harness, engine, org, meetings)


@pytest.fixture
def world(app_engine: Engine) -> World:
    return make_world(app_engine)


def audit_of(superuser_engine: Engine, meeting_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT action, actor_user_id FROM audit_events WHERE target = :t ORDER BY at"
                ),
                {"t": f"meeting:{meeting_id}"},
            )
        ]


# ─── the shared path ──────────────────────────────────────────────────────────────────────────────


def test_a_due_meeting_is_dispatched_on_its_own_row_as_its_requester(
    world: World, superuser_engine: Engine
) -> None:
    link = zoom_link()
    meeting_id = world.scheduled(link, language="hi")

    started = world.start(meeting_id)

    assert started == ScheduledStart("dispatched", "dispatching", None)
    [body] = world.harness.gateway.dispatches
    settings = world.harness.services.settings
    assert body == {
        "meeting_url": link,
        "language": "hi",
        "bot_name": "munshi.ai (recording)",
        "automatic_leave": {"max_wait_for_admission": 600_000, "max_time_left_alone": 300_000},
        "disclosure": {
            "text": settings.disclosure_text,
            "privacy_notice_url": "http://portal.test/privacy",
        },
    }
    row = world.row(meeting_id)
    platform_meeting = next(iter(world.harness.gateway.meetings.values()))
    assert (row.status, row.source, row.failure_reason) == ("dispatching", "invite_mail", None)
    assert row.platform_meeting_id == platform_meeting["id"]
    assert row.settings_snapshot == service.settings_snapshot(settings, "hi")
    assert row.dispatch_lease_until is None
    with transaction(world.engine, org_id=world.org_id) as tx:
        count = tx.scalar(text("SELECT count(*) FROM meetings WHERE join_url_key = :k"),
                          {"k": join_url_key(link)})
    assert count == 1  # the scheduled row itself, no second one
    assert audit_of(superuser_engine, meeting_id) == [("meeting.dispatch", world.actor().user_id)]

    # Started already: a second call (a second worker, a late claim) changes nothing.
    assert world.start(meeting_id) == ScheduledStart("left")
    assert len(world.harness.gateway.dispatches) == 1


def test_an_unknown_language_falls_back_to_the_deployment_default(world: World) -> None:
    meeting_id = world.scheduled(zoom_link(), language="xx")
    world.start(meeting_id)
    assert world.row(meeting_id).settings_snapshot["language"] == "en"


def test_not_before_its_start_minus_the_lead(world: World) -> None:
    meeting_id = world.scheduled(zoom_link(), start=world.now() + LEAD + timedelta(seconds=1))
    assert world.start(meeting_id) == ScheduledStart("left")
    assert world.harness.gateway.dispatches == []
    assert world.row(meeting_id).status == "scheduled"


def test_a_skipped_meeting_is_never_dispatched(world: World) -> None:
    meeting_id = world.scheduled(zoom_link(), skipped_at=world.now(),
                                 skipped_by=world.actor().user_id)
    assert world.start(meeting_id) == ScheduledStart("left")
    assert world.harness.gateway.dispatches == []
    assert world.row(meeting_id).status == "scheduled"


def test_only_its_requester_dispatches_it(world: World) -> None:
    meeting_id = world.scheduled(zoom_link(), role="member")
    assert world.start(meeting_id, role="admin") == ScheduledStart("left")
    assert world.harness.gateway.dispatches == []


# ─── refusals mapped onto the row ─────────────────────────────────────────────────────────────────


def assert_cancelled(world: World, meeting_id: uuid.UUID, end_reason: str) -> None:
    row = world.row(meeting_id)
    assert (row.status, row.end_reason, row.failure_reason) == ("cancelled", end_reason, None)
    assert row.ended_at is not None
    # The meeting page reads the reason from the meeting view.
    shown = sign_in(world.harness, world.org.member).get(f"/api/v1/meetings/{meeting_id}").json()
    assert (shown["status"], shown["end_reason"], shown["failure_reason"]) == (
        "cancelled", end_reason, None
    )


def test_a_meeting_claimed_after_its_end_is_cancelled_meeting_ended(world: World) -> None:
    now = world.now()
    over = world.scheduled(zoom_link(), start=now - timedelta(hours=2),
                           scheduled_end=now - timedelta(hours=1))
    ending_now = world.scheduled(zoom_link(), start=now - timedelta(minutes=30), scheduled_end=now)

    for meeting_id in (over, ending_now):
        assert world.start(meeting_id) == ScheduledStart("cancelled", "cancelled", "meeting_ended")
        assert_cancelled(world, meeting_id, "meeting_ended")
        assert world.start(meeting_id) == ScheduledStart("left")  # not retried
    assert world.harness.gateway.dispatches == []


def test_a_late_claim_before_the_end_still_dispatches(world: World) -> None:
    now = world.now()
    meeting_id = world.scheduled(zoom_link(), start=now - timedelta(minutes=10),
                                 scheduled_end=now + timedelta(minutes=20))
    assert world.start(meeting_id) == ScheduledStart("dispatched", "dispatching", None)
    assert len(world.harness.gateway.dispatches) == 1


def test_without_a_known_end_a_meeting_is_taken_to_last_an_hour(world: World) -> None:
    now = world.now()
    within = world.scheduled(zoom_link(), start=now - timedelta(minutes=59))
    past = world.scheduled(zoom_link(), start=now - timedelta(minutes=61))

    assert world.start(past) == ScheduledStart("cancelled", "cancelled", "meeting_ended")
    assert world.start(within) == ScheduledStart("dispatched", "dispatching", None)
    assert len(world.harness.gateway.dispatches) == 1


def test_a_live_meeting_of_the_link_cancels_it_already_in_meeting(world: World) -> None:
    link = zoom_link()
    pasted = sign_in(world.harness, world.org.lead).post(JOIN, json={"join_url": link})
    assert pasted.status_code == 201
    meeting_id = world.scheduled(link)

    assert world.start(meeting_id) == ScheduledStart("cancelled", "cancelled", "already_in_meeting")

    assert len(world.harness.gateway.dispatches) == 1  # the paste's bot only
    assert_cancelled(world, meeting_id, "already_in_meeting")
    assert world.start(meeting_id) == ScheduledStart("left")  # not retried


def test_a_paste_winning_the_race_is_a_unique_violation_mapped_to_already_in_meeting(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = zoom_link()
    meeting_id = world.scheduled(link)
    pasted = sign_in(world.harness, world.org.lead).post(JOIN, json={"join_url": link})
    assert pasted.status_code == 201
    # The paste commits between the live-row check and the row turning `dispatching`.
    monkeypatch.setattr(service, "_live_by_key", lambda tx, org_id, key: None)

    assert world.start(meeting_id) == ScheduledStart("cancelled", "cancelled", "already_in_meeting")

    assert len(world.harness.gateway.dispatches) == 1
    assert_cancelled(world, meeting_id, "already_in_meeting")


def test_the_platform_holding_the_meeting_for_another_live_row_cancels_it(world: World) -> None:
    number = str(uuid.uuid4().int)[-11:].lstrip("0").rjust(11, "1")
    pasted = sign_in(world.harness, world.org.lead).post(
        JOIN, json={"join_url": f"https://acme.zoom.us/j/{number}?pwd=x"}
    )
    assert pasted.status_code == 201
    # The same Zoom meeting under another hostname: another link key, the same platform meeting.
    meeting_id = world.scheduled(f"https://zoom.us/j/{number}?pwd=x")

    assert world.start(meeting_id) == ScheduledStart("cancelled", "cancelled", "already_in_meeting")
    assert_cancelled(world, meeting_id, "already_in_meeting")


def test_a_removal_within_12_hours_cancels_it_removed_by_participant(world: World) -> None:
    link = zoom_link()
    earlier = world.scheduled(link, start=world.now() - timedelta(hours=2),
                              scheduled_end=world.now() + timedelta(minutes=30))
    assert world.start(earlier).outcome == "dispatched"
    at = world.now() - timedelta(hours=1)
    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET status = 'done', end_reason = 'evicted' WHERE id = :m"),
            {"m": earlier},
        )
        tx.add(ConsentRecord(
            meeting_id=earlier, org_id=world.org_id, bot_display_name="munshi.ai (recording)",
            disclosure_text="notice", privacy_notice_url="http://portal.test/privacy",
            disclosure_channel="chat", admitted_at=at - timedelta(minutes=5), removed_at=at,
        ))
    meeting_id = world.scheduled(link)

    assert world.start(meeting_id) == ScheduledStart(
        "cancelled", "cancelled", "removed_by_participant"
    )

    assert len(world.harness.gateway.dispatches) == 1
    assert_cancelled(world, meeting_id, "removed_by_participant")


def test_a_platform_422_fails_it_with_the_platform_reason(world: World) -> None:
    world.harness.gateway.fail["dispatch"] = 422
    meeting_id = world.scheduled(zoom_link())

    started = world.start(meeting_id)

    assert started == ScheduledStart("dispatched", "failed", "meeting_not_found")
    row = world.row(meeting_id)
    assert (row.status, row.failure_reason) == ("failed", "meeting_not_found")
    assert row.ended_at is not None


def test_platform_capacity_fails_it_like_a_paste(world: World) -> None:
    world.harness.gateway.fail["dispatch"] = 429
    meeting_id = world.scheduled(zoom_link())
    assert world.start(meeting_id) == ScheduledStart("dispatched", "failed", "capacity_exhausted")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_gateway_5xx_puts_it_back_for_the_claims_backoff(world: World, status: int) -> None:
    world.harness.gateway.fail["dispatch"] = status
    meeting_id = world.scheduled(zoom_link(), dispatch_attempts=2)

    assert world.start(meeting_id) == ScheduledStart("retry", "scheduled", "gateway_unavailable")
    row = world.row(meeting_id)
    assert (row.status, row.failure_reason, row.ended_at) == ("scheduled", None, None)
    assert row.dispatch_lease_until == world.now() + timedelta(seconds=60)  # 30 s doubled once

    del world.harness.gateway.fail["dispatch"]
    assert world.start(meeting_id) == ScheduledStart("dispatched", "dispatching", None)
    assert len(world.harness.gateway.dispatches) == 1


def test_a_timed_out_dispatch_whose_bot_started_is_found_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = world.meetings.gateway
    sent = gateway.dispatch

    async def times_out(key: str, body: dict) -> dict:
        await sent(key, body)  # the platform starts the bot, but the answer never arrives
        raise GatewayError("dispatch", None, "ReadTimeout")

    monkeypatch.setattr(gateway, "dispatch", times_out)
    meeting_id = world.scheduled(zoom_link(), dispatch_attempts=1)
    assert world.start(meeting_id) == ScheduledStart("retry", "scheduled", "gateway_unavailable")
    assert world.row(meeting_id).dispatch_lease_until == world.now() + timedelta(seconds=30)

    monkeypatch.setattr(gateway, "dispatch", sent)
    assert world.start(meeting_id) == ScheduledStart("dispatched", "dispatching", None)

    [platform_id] = world.harness.gateway.meetings  # one bot, now held by the scheduled row
    assert world.row(meeting_id).platform_meeting_id == platform_id


def test_a_gateway_error_on_the_last_claim_fails_it(world: World) -> None:
    world.harness.gateway.fail["dispatch"] = 503
    meeting_id = world.scheduled(zoom_link(), dispatch_attempts=MAX_DISPATCH_ATTEMPTS)
    assert world.start(meeting_id) == ScheduledStart("dispatched", "failed", "internal_error")
    row = world.row(meeting_id)
    assert (row.status, row.failure_reason) == ("failed", "internal_error")


def test_a_pasted_link_still_fails_at_once_on_a_gateway_error(world: World) -> None:
    world.harness.gateway.fail["dispatch"] = 503
    pasted = sign_in(world.harness, world.org.member).post(JOIN, json={"join_url": zoom_link()})
    assert (pasted.json()["status"], pasted.json()["failure_reason"]) == (
        "failed", "internal_error"
    )


def test_no_principal_stays_due_for_the_backoff_then_fails(app_engine: Engine) -> None:
    world = make_world(app_engine, principal=False)
    meeting_id = world.scheduled(zoom_link())

    for attempts in range(1, MAX_DISPATCH_ATTEMPTS):
        with transaction(world.engine, org_id=world.org_id) as tx:
            tx.execute(text("UPDATE meetings SET dispatch_attempts = :a WHERE id = :m"),
                       {"a": attempts, "m": meeting_id})
        started = world.start(meeting_id)
        assert started == ScheduledStart("retry", "scheduled", "no_platform_principal")
        assert world.row(meeting_id).status == "scheduled"

    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(text("UPDATE meetings SET dispatch_attempts = :a WHERE id = :m"),
                   {"a": MAX_DISPATCH_ATTEMPTS, "m": meeting_id})
    assert world.start(meeting_id) == ScheduledStart("failed", "failed", "internal_error")
    row = world.row(meeting_id)
    assert (row.status, row.failure_reason) == ("failed", "internal_error")
    assert world.harness.gateway.dispatches == []


def test_a_meeting_claimed_past_the_attempt_limit_fails_without_a_dispatch(world: World) -> None:
    meeting_id = world.scheduled(zoom_link(), dispatch_attempts=MAX_DISPATCH_ATTEMPTS + 1)
    assert world.start(meeting_id) == ScheduledStart("failed", "failed", "internal_error")
    assert world.harness.gateway.dispatches == []


# ─── the requester ────────────────────────────────────────────────────────────────────────────────


def test_the_requester_actor_is_an_active_member_only(world: World) -> None:
    member = world.org.member
    actor = requester_actor(world.engine, member.org_id, member.user_id)
    assert actor == Actor(org_id=member.org_id, user_id=member.user_id, role="member")
    admin = world.org.admin
    assert requester_actor(world.engine, admin.org_id, admin.user_id).role == "admin"
    assert requester_actor(world.engine, uuid.uuid4(), member.user_id) is None

    with transaction(world.engine) as tx:
        tx.get(User, member.user_id).status = "disabled"
    assert requester_actor(world.engine, member.org_id, member.user_id) is None


def test_a_meeting_nobody_can_dispatch_is_dropped(world: World) -> None:
    meeting_id = world.scheduled(zoom_link())
    assert world.meetings.drop_scheduled(world.org_id, meeting_id, "requester_inactive") is True
    assert_cancelled(world, meeting_id, "requester_inactive")
    assert world.meetings.drop_scheduled(world.org_id, meeting_id, "requester_inactive") is False


def test_another_orgs_actor_cannot_start_it(world: World, app_engine: Engine) -> None:
    other = role_org(app_engine)
    meeting_id = world.scheduled(zoom_link())
    stranger = Actor(org_id=other.owner.org_id, user_id=world.actor().user_id, role="owner")
    started = asyncio.run(world.meetings.start_scheduled(stranger, meeting_id, lead=LEAD))
    assert started == ScheduledStart("left")
    assert world.row(meeting_id).status == "scheduled"
    with transaction(world.engine, org_id=world.org_id) as tx:
        assert tx.scalar(select(Meeting.status).where(Meeting.id == meeting_id)) == "scheduled"
