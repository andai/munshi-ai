"""The invite state machine (`andai_intake.events`), pure: no database. A small in-memory world
plays the part of the processor's writes, so a sequence of messages can be followed to its end
state."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from andai_intake import events
from andai_intake.events import GroupState, Sender, StoredMeeting
from andai_intake.invite import ParsedEvent, parse
from invitekit import NOTETAKER, ORGANIZER, calendar, message, vevent

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)  # a Thursday
MEET = "https://meet.google.com/abc-defg-hij"
OTHER = "https://meet.google.com/xyz-wxyz-xyz"
ANA = Sender(ORGANIZER, uuid.uuid4(), uuid.uuid4())  # the organizer, a member
BO = Sender("bo@acme.test", uuid.uuid4(), None)  # another member, not the organizer
WEEKLY = "RRULE:FREQ=WEEKLY"
TUE = datetime(2026, 11, 10, 4, 30, tzinfo=UTC)


def ev(
    uid: str = "evt-1@acme.test",
    *,
    start: str = "20261110T043000Z",
    end: str = "20261110T053000Z",
    sequence: int = 0,
    dtstamp: str = "20261105T090000Z",
    method: str = "REQUEST",
    link: str | None = MEET,
    summary: str = "Weekly sync",
    organizer: str = ORGANIZER,
    extra: tuple[str, ...] = (),
) -> list[ParsedEvent]:
    lines = [*extra]
    if link:
        lines.append(f"X-GOOGLE-CONFERENCE:{link}")
    raw = message(
        inline=calendar(
            vevent(
                uid,
                dtstart=f"DTSTART:{start}",
                dtend=f"DTEND:{end}" if end else None,
                sequence=sequence,
                dtstamp=f"DTSTAMP:{dtstamp}",
                summary=summary,
                organizer=f"ORGANIZER:mailto:{organizer}",
                extra=lines,
            ),
            method=method,
        ),
        method=method,
    )
    return parse(raw, [NOTETAKER])


def series(**kwargs) -> list[ParsedEvent]:
    return ev(extra=(WEEKLY, *kwargs.pop("extra", ())), **kwargs)


class World:
    """The rows of one group, changed as the processor would change them."""

    def __init__(self) -> None:
        self.events: dict[uuid.UUID, events.StoredEvent] = {}
        self.meetings: dict[uuid.UUID, StoredMeeting] = {}
        self.skips: set[uuid.UUID] = set()

    @property
    def state(self) -> GroupState:
        return GroupState(tuple(self.events.values()), tuple(self.meetings.values()))

    def send(
        self, incoming: list[ParsedEvent], sender: Sender = ANA, now: datetime = NOW, **kw
    ) -> events.Plan:
        plan = events.apply(self.state, incoming, sender, now, **kw)
        if plan.outcome in ("ignored", "quarantined"):
            assert not plan.writes
            return plan
        self.write(plan)
        return plan

    def top_up(self, now: datetime, **kw) -> events.Plan:
        plan = events.apply(self.state, (), None, now, **kw)
        self.write(plan)
        return plan

    def write(self, plan: events.Plan) -> None:
        for record in plan.events:
            self.events[record.id] = record
        for meeting_id in plan.cancels:
            m = self.meetings[meeting_id]
            assert m.status == "scheduled"
            self.meetings[meeting_id] = replace(
                m, status="cancelled", end_reason=events.INVITE_CANCELLED
            )
        for change in plan.changes:
            m = self.meetings[change.id]
            if change.restore:
                assert m.restorable
                m = replace(m, status="scheduled", end_reason=None)
            assert m.status == "scheduled"
            self.meetings[change.id] = replace(m, **change.changes)
        for new in plan.creates:
            assert all(
                (m.occurrence_key != new.occurrence_key) for m in self.meetings.values()
            ), "one meeting per occurrence"
            self.meetings[new.id] = StoredMeeting(
                id=new.id,
                invite_event_id=new.invite_event_id,
                occurrence_key=new.occurrence_key,
                status="scheduled",
                scheduled_start=new.scheduled_start,
                scheduled_end=new.scheduled_end,
                join_url=new.join_url,
                platform=new.platform,
                title=new.title,
            )

    def live(self) -> dict[datetime, datetime | None]:
        """Scheduled meetings: occurrence key → scheduled start."""
        return {
            m.occurrence_key: m.scheduled_start
            for m in self.meetings.values()
            if m.status == "scheduled"
        }

    def by_key(self, key: datetime) -> StoredMeeting:
        [m] = [m for m in self.meetings.values() if m.occurrence_key == key]
        return m

    def skip(self, key: datetime) -> None:
        m = self.by_key(key)
        self.meetings[m.id] = replace(m, skipped=True)

    def dispatch(self, key: datetime) -> None:
        m = self.by_key(key)
        self.meetings[m.id] = replace(m, status="dispatching", dispatched=True)


# ─── create, update, order ────────────────────────────────────────────────────────────────────────


def test_a_new_invite_schedules_its_meeting() -> None:
    world = World()
    plan = world.send(ev())
    assert (plan.outcome, plan.reason) == ("scheduled", None)
    [new] = plan.creates
    assert (new.occurrence_key, new.scheduled_start, new.joined_now) == (TUE, TUE, False)
    assert new.scheduled_end == TUE + timedelta(hours=1)  # no bot is sent after it
    assert (new.join_url, new.platform, new.title) == (MEET, "google_meet", "Weekly sync")
    assert (new.requested_by, new.team_id) == (ANA.user_id, ANA.team_id)
    [record] = plan.events
    assert (record.uid_norm, record.recurrence_id, record.state) == (
        "evt-1@acme.test", None, "active"
    )
    assert record.organizer == ORGANIZER and record.requested_by == ANA.user_id


def test_a_higher_sequence_moves_the_meeting() -> None:
    world = World()
    world.send(ev())
    plan = world.send(ev(start="20261110T063000Z", end="20261110T073000Z", sequence=1,
                         link=OTHER, summary="Moved sync"))
    assert plan.outcome == "updated"
    [meeting] = world.meetings.values()  # the same meeting, keyed to the one-off's new start
    assert meeting.status == "scheduled" and not plan.creates and not plan.cancels
    assert meeting.occurrence_key == meeting.scheduled_start == TUE + timedelta(hours=2)
    assert meeting.scheduled_end == TUE + timedelta(hours=3)
    assert (meeting.join_url, meeting.title) == (OTHER, "Moved sync")


def test_the_same_sequence_with_a_later_dtstamp_moves_it() -> None:
    world = World()
    world.send(ev())
    plan = world.send(ev(start="20261110T053000Z", end="20261110T063000Z",
                         dtstamp="20261105T100000Z"))
    assert plan.outcome == "updated"
    assert world.live() == {TUE + timedelta(hours=1): TUE + timedelta(hours=1)}


def test_an_older_sequence_is_stale_and_the_same_message_twice_a_duplicate() -> None:
    world = World()
    world.send(ev(sequence=2))
    older = world.send(ev(start="20261110T063000Z", end="20261110T073000Z", sequence=1))
    assert (older.outcome, older.reason) == ("ignored", "stale")
    again = world.send(ev(sequence=2))
    assert (again.outcome, again.reason) == ("ignored", "duplicate")
    assert world.live() == {TUE: TUE}


def test_out_of_order_arrival_converges_to_the_same_state() -> None:
    create, update = ev(), ev(start="20261110T063000Z", end="20261110T073000Z", sequence=1)
    in_order, reversed_ = World(), World()
    in_order.send(create)
    in_order.send(update)
    first = reversed_.send(update)
    late = reversed_.send(create)
    assert first.outcome == "scheduled"
    assert (late.outcome, late.reason) == ("ignored", "stale")
    moved = TUE + timedelta(hours=2)
    assert in_order.live() == reversed_.live() == {moved: moved}
    def rows(world: World) -> set:
        return {(e.recurrence_id, e.sequence, e.state) for e in world.events.values()}

    assert rows(in_order) == rows(reversed_)


# ─── cancellations ────────────────────────────────────────────────────────────────────────────────


def test_a_series_cancel_cancels_every_scheduled_occurrence() -> None:
    world = World()
    world.send(series())
    assert set(world.live()) == {TUE, TUE + timedelta(days=7)}
    plan = world.send(series(method="CANCEL", sequence=1, link=None))
    assert plan.outcome == "cancelled"
    assert world.live() == {}
    assert {m.end_reason for m in world.meetings.values()} == {events.INVITE_CANCELLED}
    # Nothing comes back with the top-up.
    world.top_up(NOW + timedelta(days=7))
    assert world.live() == {}


def test_a_cancel_with_recurrence_id_cancels_one_occurrence() -> None:
    world = World()
    world.send(series())
    second = TUE + timedelta(days=7)
    plan = world.send(
        ev(start="20261117T043000Z", end="20261117T053000Z", method="CANCEL", sequence=1,
           link=None, extra=("RECURRENCE-ID:20261117T043000Z",))
    )
    assert plan.outcome == "cancelled"
    assert world.live() == {TUE: TUE}
    assert world.by_key(second).status == "cancelled"


def test_a_cancel_before_the_invite_keeps_it_cancelled() -> None:
    world = World()
    first = world.send(ev(method="CANCEL", sequence=1, link=None))
    assert first.outcome == "cancelled" and not first.creates
    late = world.send(ev())
    assert (late.outcome, late.reason) == ("ignored", "stale")
    assert world.live() == {}


def test_a_newer_request_restores_a_cancelled_occurrence() -> None:
    world = World()
    world.send(series())
    second = TUE + timedelta(days=7)
    rid = ("RECURRENCE-ID:20261117T043000Z",)
    world.send(ev(start="20261117T043000Z", end="20261117T053000Z", method="CANCEL",
                  sequence=1, link=None, extra=rid))
    cancelled = world.by_key(second)
    plan = world.send(ev(start="20261117T050000Z", end="20261117T060000Z", sequence=2, extra=rid))
    assert plan.outcome == "updated"
    restored = world.by_key(second)
    assert restored.id == cancelled.id and restored.status == "scheduled"
    assert restored.scheduled_start == second + timedelta(minutes=30)


# ─── instances and overrides ──────────────────────────────────────────────────────────────────────


def test_an_instance_only_invite_schedules_only_that_instance() -> None:
    world = World()
    plan = world.send(
        ev(start="20261117T060000Z", end="20261117T070000Z",
           extra=("RECURRENCE-ID:20261117T043000Z",))
    )
    assert plan.outcome == "scheduled"
    key = TUE + timedelta(days=7)
    assert world.live() == {key: key + timedelta(hours=1, minutes=30)}
    # The series arriving later adopts that occurrence instead of adding a second one.
    world.send(series())
    assert world.live() == {TUE: TUE, key: key + timedelta(hours=1, minutes=30)}


def test_an_override_moves_one_occurrence() -> None:
    world = World()
    world.send(series())
    plan = world.send(
        ev(start="20261117T090000Z", end="20261117T100000Z", sequence=1,
           extra=("RECURRENCE-ID:20261117T043000Z",))
    )
    assert plan.outcome == "updated"
    second = TUE + timedelta(days=7)
    assert world.live() == {TUE: TUE, second: second + timedelta(hours=4, minutes=30)}


def test_a_series_update_keeps_a_skipped_occurrence_skipped() -> None:
    world = World()
    world.send(series())
    world.skip(TUE)
    world.send(series(sequence=1, summary="Renamed sync"))
    assert world.by_key(TUE).skipped and world.by_key(TUE).title == "Renamed sync"
    # The whole series moves an hour: the meeting is re-keyed to the new occurrence, skip kept.
    skipped_id = world.by_key(TUE).id
    world.send(series(start="20261110T053000Z", end="20261110T063000Z", sequence=2))
    moved = world.meetings[skipped_id]
    assert moved.status == "scheduled" and moved.skipped
    assert moved.occurrence_key == moved.scheduled_start == TUE + timedelta(hours=1)
    assert len(world.live()) == 2


def test_a_one_off_moved_to_another_day_keeps_its_skip() -> None:
    world = World()
    world.send(ev())
    world.skip(TUE)
    world.send(ev(start="20261112T043000Z", end="20261112T053000Z", sequence=1))
    [meeting] = world.meetings.values()
    assert meeting.skipped and meeting.status == "scheduled"
    assert meeting.scheduled_start == TUE + timedelta(days=2)


def test_a_dispatched_meeting_is_never_touched() -> None:
    world = World()
    world.send(series())
    world.dispatch(TUE)
    world.send(series(start="20261110T063000Z", end="20261110T073000Z", sequence=1))
    assert world.by_key(TUE).status == "dispatching"
    assert world.by_key(TUE).scheduled_start == TUE
    world.send(series(method="CANCEL", sequence=2, link=None))
    assert world.by_key(TUE).status == "dispatching"


# ─── who may change it ────────────────────────────────────────────────────────────────────────────


def test_another_member_cannot_move_or_cancel_the_meeting() -> None:
    world = World()
    world.send(ev())
    moved = world.send(ev(start="20261110T063000Z", end="20261110T073000Z", sequence=1), BO)
    cancelled = world.send(ev(method="CANCEL", sequence=2, link=None), BO)
    assert (moved.outcome, moved.reason) == ("ignored", "not_event_owner")
    assert (cancelled.outcome, cancelled.reason) == ("ignored", "not_event_owner")
    assert world.live() == {TUE: TUE}


def test_the_organizer_may_change_a_meeting_another_member_scheduled() -> None:
    world = World()
    world.send(ev(), BO)  # Bo forwarded Ana's invite: Bo is the requester
    plan = world.send(ev(start="20261110T063000Z", end="20261110T073000Z", sequence=1), ANA)
    assert plan.outcome == "updated"
    assert world.live() == {TUE + timedelta(hours=2): TUE + timedelta(hours=2)}
    assert {e.requested_by for e in world.events.values()} == {BO.user_id}
    assert world.send(ev(method="CANCEL", sequence=2, link=None), ANA).outcome == "cancelled"


# ─── what is not scheduled ────────────────────────────────────────────────────────────────────────


def test_an_invite_already_running_is_joined_now_and_an_ended_one_ignored() -> None:
    running = World().send(
        ev(start="20261105T083000Z", end="20261105T093000Z"), now=NOW
    )
    assert running.outcome == "joined_now"
    [new] = running.creates
    assert new.joined_now and new.scheduled_start == NOW
    assert new.scheduled_end == datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    assert new.occurrence_key == datetime(2026, 11, 5, 8, 30, tzinfo=UTC)

    ended = World().send(ev(start="20261105T070000Z", end="20261105T080000Z"), now=NOW)
    assert (ended.outcome, ended.reason) == ("ignored", "ended")
    assert not ended.writes


@pytest.mark.parametrize("method", ["REPLY", "COUNTER", "REFRESH", "ADD", "PUBLISH"])
def test_other_methods_are_ignored(method: str) -> None:
    plan = World().send(ev(method=method))
    assert (plan.outcome, plan.reason) == ("ignored", "method_not_handled")


def test_a_new_invite_without_a_link_is_quarantined() -> None:
    plan = World().send(ev(link=None))
    assert (plan.outcome, plan.reason) == ("quarantined", "no_meeting_link")


def test_a_new_invite_is_not_scheduled_when_mail_may_not_create() -> None:
    plan = World().send(ev(), may_create=False, blocked_reason="auto_forwarded")
    assert (plan.outcome, plan.reason) == ("ignored", "auto_forwarded")
    assert (World().send(ev(), may_create=False).reason) == "auto_join_off"


def test_a_series_starting_after_the_horizon_is_scheduled_by_the_top_up() -> None:
    world = World()
    plan = world.send(series(start="20261201T043000Z", end="20261201T053000Z"))
    assert plan.outcome == "scheduled" and not plan.creates
    world.top_up(datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
    assert set(world.live()) == {datetime(2026, 12, 1, 4, 30, tzinfo=UTC)}


# ─── the horizon ──────────────────────────────────────────────────────────────────────────────────


def test_the_top_up_adds_occurrences_as_they_enter_the_horizon() -> None:
    world = World()
    world.send(series())
    assert set(world.live()) == {TUE, TUE + timedelta(days=7)}
    plan = world.top_up(NOW + timedelta(days=7))
    assert [c.occurrence_key for c in plan.creates] == [TUE + timedelta(days=14)]
    assert world.top_up(NOW + timedelta(days=7)).creates == []  # idempotent
    assert len(world.live()) == 3


def test_a_stored_event_expands_like_the_parsed_one() -> None:
    """The timing kept in `invite_events.timing` gives the same occurrences after a round trip,
    with a custom VTIMEZONE (its own offsets) included."""
    vtimezone = (
        "BEGIN:VTIMEZONE\r\nTZID:Customized Time Zone\r\nBEGIN:STANDARD\r\n"
        "DTSTART:16010101T000000\r\nTZOFFSETFROM:+0530\r\nTZOFFSETTO:+0530\r\n"
        "END:STANDARD\r\nEND:VTIMEZONE"
    )
    raw = message(
        inline=calendar(
            vevent(
                dtstart="DTSTART;TZID=Customized Time Zone:20261110T100000",
                dtend="DTEND;TZID=Customized Time Zone:20261110T110000",
                extra=[WEEKLY, f"X-GOOGLE-CONFERENCE:{MEET}"],
            ),
            vtimezones=[vtimezone],
        )
    )
    [parsed] = parse(raw, [NOTETAKER])
    assert parsed.zone is not None and parsed.zone.source == "vtimezone"
    timing = events.Timing.of(parsed)
    again = events.Timing.from_json(timing.to_json())
    record = events.StoredEvent(
        id=uuid.uuid4(), uid_norm=parsed.uid_norm, recurrence_id=None, sequence=0,
        dtstamp=None, state="active", summary="x", organizer=None, join_url=MEET,
        platform="google_meet", requested_by=ANA.user_id, team_id=None, rrule=parsed.rrule,
        timing=again,
    )
    from andai_intake.invite.recur import occurrences

    expected = [o.start for o in occurrences(parsed, NOW).occurrences]
    assert [o.start for o in occurrences(record.as_parsed(), NOW).occurrences] == expected
    assert expected[0] == datetime(2026, 11, 10, 4, 30, tzinfo=UTC)
