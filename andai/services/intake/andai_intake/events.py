"""The invite state machine: what one message (or the hourly top-up) does to one event group.

Pure: no database, no network. `apply(state, incoming, sender, now)` takes the stored rows of one
event group — every `invite_events` row of one org with one normalised UID, and the meetings
created from them — and the parsed VEVENTs of a message for that UID, and returns a `Plan`: the
event rows to write, the meetings to create, change or cancel, and the message's outcome. The
processor (`processor.py`) and the horizon top-up (`horizon.py`) write the plan in the org's
transaction.

**Identity and order (RFC 5546).** An event is `(uid_norm, RECURRENCE-ID | None)`. A VEVENT changes
its row only when it is newer: a higher SEQUENCE, or the same SEQUENCE and a later DTSTAMP. An older
one changes nothing (`stale`); the same one again changes nothing (`duplicate`). So the same state
results whatever order the messages arrive in.

**Methods.** REQUEST creates or updates; CANCEL (or STATUS:CANCELLED) without RECURRENCE-ID
cancels the whole group, with one cancels that instance. Anything else — REPLY, COUNTER, REFRESH,
ADD, and UNKNOWN (PUBLISH or no METHOD, `ics.Method`) — is `ignored` (`method_not_handled`).

**Who may change it.** Once a group exists, only its requester (the member whose verified mail
created it) or a sender whose address is its ORGANIZER may change it; anyone else is `ignored`
(`not_event_owner`). The requester stays the original one.

**Occurrences.** A series (a row without RECURRENCE-ID) is expanded by `recur.occurrences` with
its instance rows as overrides; without one, each instance row is its own occurrence (an invite for
one instance schedules only that instance). Each occurrence is keyed by its original start
(`occurrence_key`). Meetings are kept for occurrences that start within `horizon` (D-58: 14 days);
the top-up adds the rest as they come within it.

**Meetings.** Only `scheduled` meetings change: a meeting a bot was sent for is never touched.
* A wanted occurrence without a meeting gets one (when `may_create`): `scheduled_start` is its
  start, or `now` when it has started but not ended (`joined_now`).
* A scheduled meeting whose occurrence moved gets the new start, link and title; its skip stays.
* A scheduled meeting whose occurrence is gone is cancelled — every one when the group is
  cancelled, the one instance when an instance is, and otherwise only those not started yet. When
  an update moves a one-off event, or a whole series (new original starts), the meeting is re-keyed
  to the new occurrence (for a series, the one on the same local day) instead, so a skip survives
  that too.
* A meeting an earlier CANCEL cancelled comes back when a newer REQUEST restores the occurrence.

**Outcomes** (SPEC `InboundInvite.outcome`): `scheduled`, `updated`, `cancelled`, `joined_now`,
`quarantined` (`no_meeting_link`: a new invite with no join link) and `ignored` with a reason:
`method_not_handled`, `not_event_owner`, `stale`, `duplicate`, `ended` (nothing left to join),
`all_day`, `unsupported_recurrence`, or the caller's `blocked_reason` (`auto_join_off`,
`auto_forwarded`) when a new group may not be created. An ignored or quarantined plan writes
nothing.
"""

from __future__ import annotations

import uuid
import zoneinfo
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any

from andai_intake.invite.ics import Method, ParsedEvent, stored_vtimezone
from andai_intake.invite.recur import HORIZON, occurrences
from andai_intake.invite.tz import Zone

ACTIVE, CANCELLED = "active", "cancelled"
APPLIED = (Method.REQUEST, Method.CANCEL)
OUTCOME_ORDER = ("joined_now", "scheduled", "updated", "cancelled", "quarantined", "ignored")
FAR = timedelta(days=366)  # how far to look for a later occurrence of a new invite
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
INVITE_CANCELLED = "invite_cancelled"  # andai_core.meetings.scheduled.INVITE_CANCELLED


# ─── what is stored ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Timing:
    """What expanding an event again needs, kept as `invite_events.timing`."""

    start: datetime | None  # UTC
    end: datetime | None  # UTC
    start_local: datetime | None  # aware, in `zone`
    zone: Zone | None
    all_day: bool
    status: str | None
    rdates: tuple[datetime, ...] = ()
    exdates: tuple[datetime, ...] = ()
    exdate_days: tuple[date, ...] = ()

    @classmethod
    def of(cls, event: ParsedEvent) -> Timing:
        return cls(
            start=event.start,
            end=event.end,
            start_local=event.start_local,
            zone=event.zone,
            all_day=event.all_day,
            status=event.status,
            rdates=event.rdates,
            exdates=event.exdates,
            exdate_days=event.exdate_days,
        )

    def to_json(self) -> dict[str, Any]:
        zone = self.zone
        offset = None
        if self.start_local is not None and self.start_local.utcoffset() is not None:
            offset = int(self.start_local.utcoffset().total_seconds())  # type: ignore[union-attr]
        return {
            "start": _iso(self.start),
            "end": _iso(self.end),
            "start_wall": (
                None if self.start_local is None
                else self.start_local.replace(tzinfo=None).isoformat()
            ),
            "zone": None if zone is None else {
                "name": zone.name,
                "source": zone.source,
                "assumed": zone.assumed,
                "vtimezone": zone.vtimezone,
                "offset_s": offset,
            },
            "all_day": self.all_day,
            "status": self.status,
            "rdates": [d.isoformat() for d in self.rdates],
            "exdates": [d.isoformat() for d in self.exdates],
            "exdate_days": [d.isoformat() for d in self.exdate_days],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Timing:
        zone = _zone(data.get("zone"))
        wall = data.get("start_wall")
        start_local = None
        if wall and zone is not None:
            start_local = datetime.fromisoformat(wall).replace(tzinfo=zone.tzinfo)
        return cls(
            start=_utc(data.get("start")),
            end=_utc(data.get("end")),
            start_local=start_local,
            zone=zone,
            all_day=bool(data.get("all_day")),
            status=data.get("status"),
            rdates=tuple(_utc(v) for v in data.get("rdates") or ()),  # type: ignore[misc]
            exdates=tuple(_utc(v) for v in data.get("exdates") or ()),  # type: ignore[misc]
            exdate_days=tuple(date.fromisoformat(v) for v in data.get("exdate_days") or ()),
        )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _utc(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def _zone(data: dict[str, Any] | None) -> Zone | None:
    """The stored zone again: an IANA name, UTC, the VTIMEZONE's own offsets, or — when none of
    those can be rebuilt — the fixed offset the start had."""
    if not data:
        return None
    name, source = str(data.get("name") or ""), str(data.get("source") or "")
    assumed = bool(data.get("assumed"))
    info: tzinfo | None = None
    if source == "utc" or name == "UTC":
        info = UTC
    elif source == "vtimezone" and data.get("vtimezone"):
        try:
            info = stored_vtimezone(str(data["vtimezone"]))
        except ValueError:
            info = None
    elif name and not name.startswith("vtimezone:"):
        try:
            info = zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            info = None
    if info is None:
        offset = data.get("offset_s")
        info = UTC if offset is None else timezone(timedelta(seconds=int(offset)))
        source = "offset"
    return Zone(info, name or "UTC", source, assumed=assumed, vtimezone=data.get("vtimezone"))


@dataclass(frozen=True)
class StoredEvent:
    """One `invite_events` row."""

    id: uuid.UUID
    uid_norm: str
    recurrence_id: datetime | None
    sequence: int
    dtstamp: datetime | None
    state: str
    summary: str | None
    organizer: str | None
    join_url: str | None
    platform: str | None
    requested_by: uuid.UUID
    team_id: uuid.UUID | None
    rrule: str | None
    timing: Timing
    forwarded_copy: bool = False
    auto_forwarded: bool = False
    last_inbound_id: uuid.UUID | None = None

    @property
    def recurring(self) -> bool:
        return self.recurrence_id is None and bool(self.rrule or self.timing.rdates)

    @property
    def order(self) -> tuple[int, datetime]:
        return (self.sequence, self.dtstamp or EPOCH)

    def as_parsed(self) -> ParsedEvent:
        """The row as the `ParsedEvent` `recur.occurrences` reads."""
        t = self.timing
        status = "CANCELLED" if self.state == CANCELLED else t.status
        return ParsedEvent(
            uid=self.uid_norm,
            uid_norm=self.uid_norm,
            method=Method.REQUEST,
            method_raw=None,
            recurrence_id=self.recurrence_id,
            recurrence_range=None,
            sequence=self.sequence,
            dtstamp=self.dtstamp,
            summary=self.summary or "",
            organizer=self.organizer,
            organizer_name=None,
            attendees=(),
            status=status,
            all_day=t.all_day,
            start=t.start,
            end=t.end,
            start_date=None,
            start_local=t.start_local,
            zone=t.zone,
            floating=False,
            rrule=self.rrule,
            rdates=t.rdates,
            exdates=t.exdates,
            exdate_days=t.exdate_days,
            join_url=self.join_url,
            platform=self.platform,
            join_source=None,
            is_forwarded_copy=self.forwarded_copy,
            auto_forwarded=self.auto_forwarded,
            via_forward_attachment=False,
            warnings=(),
        )


@dataclass(frozen=True)
class StoredMeeting:
    """A meeting created from one of the group's events."""

    id: uuid.UUID
    invite_event_id: uuid.UUID | None
    occurrence_key: datetime
    status: str
    scheduled_start: datetime | None
    skipped: bool = False
    end_reason: str | None = None
    dispatched: bool = False  # a bot was sent for it (platform_meeting_id set)
    join_url: str | None = None
    platform: str | None = None
    title: str | None = None

    @property
    def restorable(self) -> bool:
        return self.status == "cancelled" and self.end_reason == INVITE_CANCELLED and (
            not self.dispatched
        )


@dataclass(frozen=True)
class GroupState:
    events: tuple[StoredEvent, ...] = ()
    meetings: tuple[StoredMeeting, ...] = ()


@dataclass(frozen=True)
class Sender:
    """The verified sender of the message being applied."""

    address: str
    user_id: uuid.UUID
    team_id: uuid.UUID | None
    inbound_id: uuid.UUID | None = None


# ─── the plan ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewMeeting:
    id: uuid.UUID
    invite_event_id: uuid.UUID
    occurrence_key: datetime
    scheduled_start: datetime
    join_url: str
    platform: str
    title: str | None
    requested_by: uuid.UUID
    team_id: uuid.UUID | None
    joined_now: bool


@dataclass(frozen=True)
class MeetingChange:
    id: uuid.UUID
    changes: dict[str, Any]
    restore: bool = False


@dataclass
class Plan:
    outcome: str
    reason: str | None = None
    events: list[StoredEvent] = field(default_factory=list)  # rows to insert or update
    creates: list[NewMeeting] = field(default_factory=list)
    changes: list[MeetingChange] = field(default_factory=list)
    cancels: list[uuid.UUID] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def writes(self) -> bool:
        return bool(self.events or self.creates or self.changes or self.cancels)


def ignored(reason: str) -> Plan:
    return Plan("ignored", reason)


def strongest(outcomes: Sequence[str]) -> str:
    """The outcome a message with several groups reports."""
    return min(outcomes, key=OUTCOME_ORDER.index) if outcomes else "ignored"


# ─── applying a message ───────────────────────────────────────────────────────────────────────────


def _owns(state: GroupState, sender: Sender) -> bool:
    address = sender.address.strip().lower()
    return any(
        e.requested_by == sender.user_id or (e.organizer or "").lower() == address
        for e in state.events
    )


def _newer(event: ParsedEvent, current: StoredEvent) -> bool:
    return (event.sequence, event.dtstamp or EPOCH) > current.order


def _cancels(event: ParsedEvent) -> bool:
    return event.method == Method.CANCEL or (event.status or "") == "CANCELLED"


def _record(
    event: ParsedEvent,
    current: StoredEvent | None,
    anchor: StoredEvent | None,
    sender: Sender,
    new_id: Callable[[], uuid.UUID],
) -> StoredEvent:
    rid = event.recurrence_id if isinstance(event.recurrence_id, datetime) else None
    cancel = _cancels(event)
    keep = current
    timing = Timing.of(event)
    if event.method == Method.CANCEL and keep is not None:
        timing = keep.timing  # a CANCEL may carry nothing but its key
    base = keep or anchor
    return StoredEvent(
        id=current.id if current is not None else new_id(),
        uid_norm=event.uid_norm,
        recurrence_id=rid,
        sequence=event.sequence,
        dtstamp=event.dtstamp,
        state=CANCELLED if cancel else ACTIVE,
        summary=event.summary or (keep.summary if keep else None) or (
            anchor.summary if anchor else None
        ),
        organizer=(base.organizer if base else None) or event.organizer,
        join_url=event.join_url or (keep.join_url if keep else None),
        platform=(event.platform if event.join_url else None) or (keep.platform if keep else None),
        requested_by=base.requested_by if base else sender.user_id,
        team_id=base.team_id if base else sender.team_id,
        rrule=(keep.rrule if keep else None) if event.method == Method.CANCEL else event.rrule,
        timing=timing,
        forwarded_copy=(
            keep.forwarded_copy if keep and event.method == Method.CANCEL
            else event.is_forwarded_copy
        ),
        auto_forwarded=(
            keep.auto_forwarded if keep and event.method == Method.CANCEL else event.auto_forwarded
        ),
        last_inbound_id=sender.inbound_id,
    )


def apply(
    state: GroupState,
    incoming: Sequence[ParsedEvent],
    sender: Sender | None,
    now: datetime,
    horizon: timedelta = HORIZON,
    *,
    may_create: bool = True,
    blocked_reason: str | None = None,
    new_id: Callable[[], uuid.UUID] = uuid.uuid4,
) -> Plan:
    """The plan for `incoming` (one UID's VEVENTs of one message; empty for the top-up) against
    `state`. See the module docstring."""
    events = {e.recurrence_id: e for e in state.events}
    changed: list[StoredEvent] = []
    new_group = not state.events
    message = bool(incoming)

    if message:
        if sender is None:
            raise ValueError("a message needs its sender")
        relevant = [e for e in incoming if e.method in APPLIED]
        if not relevant:
            return ignored("method_not_handled")
        if not new_group and not _owns(state, sender):
            return ignored("not_event_owner")
        if new_group and not may_create:
            return ignored(blocked_reason or "auto_join_off")
        stale = duplicate = 0
        anchor = events.get(None) or (state.events[0] if state.events else None)
        for event in relevant:
            if isinstance(event.recurrence_id, date) and not isinstance(
                event.recurrence_id, datetime
            ):
                continue  # an all-day instance: never joined
            rid = event.recurrence_id
            current = events.get(rid)
            if current is not None and not _newer(event, current):
                if current.order == (event.sequence, event.dtstamp or EPOCH):
                    duplicate += 1
                else:
                    stale += 1
                continue
            record = _record(event, current, anchor or (changed[0] if changed else None),
                             sender, new_id)
            events[rid] = record
            changed.append(record)
        if not changed:
            if stale or duplicate:
                return ignored("stale" if stale else "duplicate")
            return ignored("all_day")
        if new_group:
            requests = [e for e in changed if e.state == ACTIVE]
            master = events.get(None)
            if master is not None and master.state == ACTIVE and master.timing.all_day:
                return ignored("all_day")
            if requests and not any(e.join_url for e in requests):
                return Plan("quarantined", "no_meeting_link")

    plan = _reconcile(events, state.meetings, now, horizon, may_create, new_id)
    plan.events = changed

    if not message:
        plan.outcome = "scheduled" if plan.creates else "updated"
        return plan
    cancelling = all(e.state == CANCELLED for e in changed)
    if cancelling:
        plan.outcome = "cancelled"
        return plan
    if any(c.joined_now for c in plan.creates):
        plan.outcome = "joined_now"
        return plan
    if not new_group:
        plan.outcome = "updated"
        return plan
    if plan.creates:
        plan.outcome = "scheduled"
        return plan
    # A new invite with nothing to join inside the window: later occurrences, or nothing at all.
    if _refused(events, now):
        return ignored("unsupported_recurrence")
    if _later(events, now, horizon):
        plan.outcome = "scheduled"
        return plan
    return ignored("ended")


# ─── occurrences and meetings ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Want:
    start: datetime
    end: datetime
    event_id: uuid.UUID
    join_url: str | None
    platform: str | None
    title: str | None


def _wanted(
    events: dict[datetime | None, StoredEvent], now: datetime, span: timedelta
) -> tuple[dict[datetime, _Want], list[str]]:
    """The group's occurrences that have not ended and start by `now + span`, by original start."""
    out: dict[datetime, _Want] = {}
    warnings: list[str] = []
    master = events.get(None)
    if master is not None:
        if master.state == CANCELLED:
            return out, warnings
        overrides = [e.as_parsed() for rid, e in events.items() if rid is not None]
        expansion = occurrences(master.as_parsed(), now, span, overrides)
        if expansion.capped:
            warnings.append("recurrence_capped")
        if expansion.refused:
            warnings.append(f"recurrence_refused:{expansion.refused}")
        for occ in expansion.occurrences:
            override = events.get(occ.recurrence_id) if occ.overridden else None
            source = override if override is not None and override.join_url else master
            out[occ.recurrence_id] = _Want(
                occ.start,
                occ.end,
                master.id,
                source.join_url,
                source.platform,
                (override.summary if override is not None and override.summary else None)
                or master.summary,
            )
        return out, warnings
    for rid, event in events.items():
        if rid is None or event.state == CANCELLED:
            continue
        for occ in occurrences(event.as_parsed(), now, span).occurrences:
            out[occ.recurrence_id] = _Want(
                occ.start, occ.end, event.id, event.join_url, event.platform, event.summary
            )
    return out, warnings


def _local_day(key: datetime, events: dict[datetime | None, StoredEvent]) -> date:
    """Which occurrence a moved one replaces: the same local day of a series; for a one-off event,
    its only occurrence, whatever day it moved to."""
    master = events.get(None)
    if master is not None and not master.recurring:
        return date.min
    zone = master.timing.zone if master is not None else None
    return key.astimezone(zone.tzinfo).date() if zone is not None else key.date()


def _reconcile(
    events: dict[datetime | None, StoredEvent],
    meetings: Sequence[StoredMeeting],
    now: datetime,
    horizon: timedelta,
    may_create: bool,
    new_id: Callable[[], uuid.UUID],
) -> Plan:
    plan = Plan("updated")
    master = events.get(None)
    group_cancelled = master is not None and master.state == CANCELLED
    cancelled_keys = {
        rid for rid, e in events.items() if rid is not None and e.state == CANCELLED
    }
    by_key: dict[datetime, StoredMeeting] = {}
    for meeting in meetings:
        by_key.setdefault(meeting.occurrence_key, meeting)

    scheduled = [m for m in by_key.values() if m.status == "scheduled"]
    span = horizon
    for m in scheduled:
        furthest = max(m.occurrence_key, m.scheduled_start or m.occurrence_key)
        span = max(span, furthest - now + timedelta(minutes=1))
    wanted, plan.warnings = _wanted(events, now, span)
    window_end = now + horizon
    anchor = master or next(iter(events.values()), None)

    orphans: dict[date, list[StoredMeeting]] = {}
    for m in sorted(scheduled, key=lambda m: m.occurrence_key):
        key = m.occurrence_key
        if group_cancelled or key in cancelled_keys:
            plan.cancels.append(m.id)
            continue
        want = wanted.get(key)
        if want is not None:
            change = _moved(m, want, now)
            if change:
                plan.changes.append(MeetingChange(m.id, change))
            continue
        if m.scheduled_start is not None and m.scheduled_start > now:
            orphans.setdefault(_local_day(key, events), []).append(m)

    for key, want in sorted(wanted.items(), key=lambda kv: (kv[1].start, kv[0])):
        if not want.join_url or not want.platform:
            if "no_meeting_link" not in plan.warnings:
                plan.warnings.append("no_meeting_link")
            continue
        existing = by_key.get(key)
        if existing is not None:
            if existing.restorable and may_create and want.start <= window_end:
                change = _fields(want, now, existing.scheduled_start)
                change["invite_event_id"] = want.event_id
                plan.changes.append(MeetingChange(existing.id, change, restore=True))
            continue
        if want.start > window_end:
            continue
        same_day = orphans.get(_local_day(key, events))
        if same_day:
            moved = same_day.pop(0)
            change = _fields(want, now, moved.scheduled_start)
            change.update(occurrence_key=key, invite_event_id=want.event_id)
            plan.changes.append(MeetingChange(moved.id, change))
            continue
        if not may_create or anchor is None:
            continue
        plan.creates.append(
            NewMeeting(
                id=new_id(),
                invite_event_id=want.event_id,
                occurrence_key=key,
                scheduled_start=max(want.start, now),
                join_url=want.join_url,
                platform=want.platform,
                title=want.title,
                requested_by=anchor.requested_by,
                team_id=anchor.team_id,
                joined_now=want.start <= now,
            )
        )
    for left in orphans.values():
        plan.cancels.extend(m.id for m in left)
    return plan


def _fields(want: _Want, now: datetime, current_start: datetime | None) -> dict[str, Any]:
    started = want.start <= now
    if started and current_start is not None and current_start <= now:
        start = current_start  # already due: keep it, do not chase the clock
    else:
        start = max(want.start, now)
    return {
        "scheduled_start": start,
        "join_url": want.join_url,
        "platform": want.platform,
        "title": want.title,
    }


def _moved(meeting: StoredMeeting, want: _Want, now: datetime) -> dict[str, Any]:
    """The fields of `meeting` that differ from its occurrence now."""
    target = _fields(want, now, meeting.scheduled_start)
    if not want.join_url or not want.platform:
        target.pop("join_url")
        target.pop("platform")
    current = {
        "scheduled_start": meeting.scheduled_start,
        "join_url": meeting.join_url,
        "platform": meeting.platform,
        "title": meeting.title,
    }
    if want.event_id != meeting.invite_event_id:
        target["invite_event_id"] = want.event_id
        current["invite_event_id"] = meeting.invite_event_id
    return {k: v for k, v in target.items() if current.get(k) != v}


def _refused(events: dict[datetime | None, StoredEvent], now: datetime) -> bool:
    master = events.get(None)
    if master is None or master.state == CANCELLED or not master.recurring:
        return False
    return occurrences(master.as_parsed(), now, timedelta(days=1)).refused is not None


def _later(events: dict[datetime | None, StoredEvent], now: datetime, horizon: timedelta) -> bool:
    """Whether the group has an occurrence to join after the window (the top-up will add it)."""
    wanted, _ = _wanted(events, now, max(FAR, horizon))
    return any(w.join_url for w in wanted.values())


__all__ = [
    "ACTIVE",
    "CANCELLED",
    "GroupState",
    "MeetingChange",
    "NewMeeting",
    "Plan",
    "Sender",
    "StoredEvent",
    "StoredMeeting",
    "Timing",
    "apply",
    "ignored",
    "strongest",
]
