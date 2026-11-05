"""The processor: every stored message becomes scheduled meetings, or a recorded reason why not.

Every `TICK_S` seconds the loop claims up to `BATCH` messages (`andai_claim_inbound`: `received`,
or `processing` with an expired lease, so a message whose processor died is picked up again) and
takes each through, in this order:

1. the per-IP limit (`limits.admit_peer`; before any DNS);
2. the sender check (`auth.verify`, D-56): a failure is quarantined `sender_unverified`, shown to
   the admins of the org the claimed From address belongs to (`sender.attribute_org`);
3. the sender (`sender.resolve`, D-58): anyone but an active member of exactly one org is
   quarantined `unknown_user` (attributed to the org of their domain, if any);
4. the sender and org limits (`limits.admit_verified`; verified mail only): `rate_limited`;
5. parsing (`invite.parse`): `parse_error` for a message with no readable calendar;
6. `bot.autoJoin` for the sender's org and team (`effective_auto_join`, D-57): `off` creates
   nothing; `only_when_invited` creates nothing from an auto-forwarded copy;
7. the state machine (`events.apply`) per calendar UID, under a per-(org, UID) advisory lock.

Step 7's event rows, meetings, and the message's result (`andai_finish_inbound`: status, outcome,
reasons, org, user, sender, check summary, trimmed subject) are written in ONE transaction as the
sender's org. A crash before that commit leaves nothing behind: the lease runs out and the message
is processed again from the start, so meetings are never doubled. Any other error releases the
message (`andai_release_inbound`, the error's type kept) to be tried again; a message that has
been claimed `MAX_ATTEMPTS` times is quarantined `parse_error`.

The processor connects as `andai_app`: it reads and writes `inbound_mail` only through the 0012
functions, identity only through the 0013 functions, and events and meetings under row-level
security as the sender's org. Message content is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.policy import compat32
from typing import Any

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session as Tx

from andai_core.db.models import InviteEvent, Meeting
from andai_core.db.session import transaction
from andai_core.meetings import scheduled
from andai_core.org.bot_settings import effective_auto_join
from andai_intake import events, sender as senders
from andai_intake.auth.verdict import Policy, SenderAuth, from_address, verify
from andai_intake.invite import InviteParseError, ParsedEvent, parse
from andai_intake.invite.ics import clean_text
from andai_intake.invite.recur import HORIZON
from andai_intake.limits import IntakeLimits

TICK_S = 2.0
BATCH = 4
MAX_ATTEMPTS = 5
SUBJECT_MAX = 120
ERROR_MAX = 200

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Claimed:
    id: uuid.UUID
    raw: bytes
    mail_from: str
    peer_ip: str | None
    helo: str | None
    attempts: int


@dataclass(frozen=True)
class Result:
    """What happened to one message (`status` `received` when it was released for a retry)."""

    inbound_id: uuid.UUID
    status: str
    outcome: str | None = None
    quarantine_reason: str | None = None
    ignore_reason: str | None = None
    org_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    created: tuple[uuid.UUID, ...] = ()
    joined_now: tuple[uuid.UUID, ...] = ()
    changed: tuple[uuid.UUID, ...] = ()
    cancelled: tuple[uuid.UUID, ...] = ()


@dataclass
class _Recorded:
    status: str
    outcome: str
    quarantine_reason: str | None = None
    ignore_reason: str | None = None
    org_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    sender: str | None = None
    sender_auth: str | None = None
    auth_summary: dict[str, Any] | None = None
    subject: str | None = None
    plans: list[events.Plan] = field(default_factory=list)


@dataclass(frozen=True)
class _Ready:
    """A verified member's parsed invite, to be applied as their org."""

    recorded: _Recorded
    parsed: list[ParsedEvent]
    who: senders.Resolution


class LeaseLost(RuntimeError):
    """The message was no longer in processing when its result was recorded."""


# ─── inbound_mail through the 0012 functions ──────────────────────────────────────────────────────


def claim(engine: Engine, limit: int) -> list[Claimed]:
    with transaction(engine) as tx:
        rows = tx.execute(
            text(
                "SELECT id, raw, mail_from, host(peer_ip) AS peer, helo, attempts"
                " FROM andai_claim_inbound(:n)"
            ),
            {"n": limit},
        ).all()
    return [
        Claimed(row.id, bytes(row.raw), row.mail_from or "", row.peer, row.helo, row.attempts)
        for row in rows
    ]


def _finish(tx: Tx, inbound_id: uuid.UUID, recorded: _Recorded) -> bool:
    return bool(
        tx.execute(
            text(
                "SELECT andai_finish_inbound(:id, :status, :outcome, :quarantine, :ignore, :org,"
                " :user, :sender, :auth, CAST(:summary AS jsonb), :subject)"
            ),
            {
                "id": inbound_id,
                "status": recorded.status,
                "outcome": recorded.outcome,
                "quarantine": recorded.quarantine_reason,
                "ignore": recorded.ignore_reason,
                "org": recorded.org_id,
                "user": recorded.user_id,
                "sender": recorded.sender,
                "auth": recorded.sender_auth,
                "summary": None if recorded.auth_summary is None else json.dumps(
                    recorded.auth_summary
                ),
                "subject": recorded.subject,
            },
        ).scalar_one()
    )


def release(engine: Engine, inbound_id: uuid.UUID, error: str) -> bool:
    with transaction(engine) as tx:
        return bool(
            tx.execute(
                text("SELECT andai_release_inbound(:id, :error)"),
                {"id": inbound_id, "error": error[:ERROR_MAX]},
            ).scalar_one()
        )


# ─── event groups ─────────────────────────────────────────────────────────────────────────────────


def lock_group(tx: Tx, org_id: uuid.UUID, uid_norm: str) -> None:
    """Serialise every change to one (org, UID) group (the processor and the top-up)."""
    tx.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"andai_invite:{org_id}:{uid_norm}"},
    )


def _stored_event(row: InviteEvent) -> events.StoredEvent:
    return events.StoredEvent(
        id=row.id,
        uid_norm=row.uid_norm,
        recurrence_id=row.recurrence_id,
        sequence=row.sequence,
        dtstamp=row.dtstamp,
        state=row.state,
        summary=row.summary,
        organizer=row.organizer,
        join_url=row.join_url,
        platform=row.platform,
        requested_by=row.requested_by,
        team_id=row.team_id,
        rrule=row.rrule,
        timing=events.Timing.from_json(row.timing or {}),
        forwarded_copy=row.forwarded_copy,
        auto_forwarded=row.auto_forwarded,
        last_inbound_id=row.last_inbound_id,
    )


def load_group(tx: Tx, org_id: uuid.UUID, uid_norm: str) -> events.GroupState:
    """The group's events (locked) and meetings, read as the org."""
    rows = tx.scalars(
        select(InviteEvent)
        .where(InviteEvent.org_id == org_id, InviteEvent.uid_norm == uid_norm)
        .order_by(InviteEvent.created_at, InviteEvent.id)
        .with_for_update()
    ).all()
    if not rows:
        return events.GroupState()
    meetings = tx.scalars(
        select(Meeting).where(
            Meeting.org_id == org_id,
            Meeting.invite_event_id.in_([r.id for r in rows]),
            Meeting.occurrence_key.is_not(None),
        )
    ).all()
    return events.GroupState(
        events=tuple(_stored_event(r) for r in rows),
        meetings=tuple(
            events.StoredMeeting(
                id=m.id,
                invite_event_id=m.invite_event_id,
                occurrence_key=m.occurrence_key,  # type: ignore[arg-type]
                status=m.status,
                scheduled_start=m.scheduled_start,
                skipped=m.skipped_at is not None,
                # A deleted meeting is never brought back.
                end_reason=None if m.deleted_at is not None else m.end_reason,
                dispatched=m.platform_meeting_id is not None,
                join_url=m.join_url,
                platform=m.platform,
                title=m.title,
            )
            for m in meetings
        ),
    )


def write_plan(
    tx: Tx, org_id: uuid.UUID, plan: events.Plan, *, now: datetime, language: str
) -> None:
    """Write `plan`'s event rows and meetings in `tx` (the org's transaction)."""
    for record in plan.events:
        row = tx.get(InviteEvent, record.id)
        values = {
            "uid_norm": record.uid_norm,
            "recurrence_id": record.recurrence_id,
            "sequence": record.sequence,
            "dtstamp": record.dtstamp,
            "state": record.state,
            "summary": record.summary,
            "organizer": record.organizer,
            "join_url": record.join_url,
            "platform": record.platform,
            "requested_by": record.requested_by,
            "team_id": record.team_id,
            "recurring": record.recurring,
            "rrule": record.rrule,
            "timing": record.timing.to_json(),
            "forwarded_copy": record.forwarded_copy,
            "auto_forwarded": record.auto_forwarded,
            "last_inbound_id": record.last_inbound_id,
            "updated_at": now,
        }
        if row is None:
            tx.add(InviteEvent(id=record.id, org_id=org_id, created_at=now, **values))
        else:
            for name, value in values.items():
                setattr(row, name, value)
    tx.flush()
    for meeting_id in plan.cancels:
        scheduled.cancel_scheduled(tx, meeting_id, now=now)
    for change in plan.changes:
        scheduled.update_scheduled(
            tx, change.id, now=now, restore=change.restore, **change.changes
        )
    for new in plan.creates:
        scheduled.insert_scheduled(
            tx,
            meeting_id=new.id,
            org_id=org_id,
            invite_event_id=new.invite_event_id,
            occurrence_key=new.occurrence_key,
            scheduled_start=new.scheduled_start,
            requested_by=new.requested_by,
            team_id=new.team_id,
            join_url=new.join_url,
            platform=new.platform,
            title=new.title,
            language=language,
            now=now,
        )


def may_create(mode: str, auto_forwarded: bool) -> tuple[bool, str | None]:
    """Whether `bot.autoJoin` lets mail create meetings (D-57), and the reason when it does not."""
    if mode == "off":
        return False, "auto_join_off"
    if mode == "only_when_invited" and auto_forwarded:
        return False, "auto_forwarded"
    return True, None


# ─── the processor ────────────────────────────────────────────────────────────────────────────────


def _subject(raw: bytes) -> str | None:
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0] + b"\r\n\r\n"
    value = BytesHeaderParser(policy=compat32).parsebytes(head).get("Subject")
    if value is None:
        return None
    try:
        decoded = str(make_header(decode_header(str(value))))
    except (UnicodeError, LookupError, ValueError):
        decoded = str(value)
    return clean_text(decoded, SUBJECT_MAX) or None


class Processor:
    def __init__(
        self,
        engine: Engine,
        settings: Any,
        limits: IntakeLimits,
        *,
        policy: Policy | None = None,
        clock: Callable[[], datetime] = utcnow,
        horizon: timedelta = HORIZON,
        batch: int = BATCH,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.limits = limits
        self.policy = policy
        self.clock = clock
        self.horizon = horizon
        self.batch = batch

    # ── one message ──

    def process(self, row: Claimed) -> Result:
        if row.attempts > MAX_ATTEMPTS:
            return self._give_up(row)
        try:
            decided = self._decide(row)
            if isinstance(decided, _Ready):
                return self._apply(row, decided)
            recorded = decided
            with transaction(self.engine) as tx:
                if not _finish(tx, row.id, recorded):
                    raise LeaseLost(str(row.id))
            return self._result(row, recorded)
        except LeaseLost:
            log.warning("inbound %s: no longer in processing; result dropped", row.id)
            return Result(row.id, "lost")
        except Exception as exc:  # the message is tried again; the error type only, no content
            log.warning("inbound %s: processing failed: %s", row.id, type(exc).__name__)
            if row.attempts >= MAX_ATTEMPTS:
                return self._give_up(row)
            release(self.engine, row.id, type(exc).__name__)
            return Result(row.id, "received")

    def _quarantine(self, reason: str, **fields: Any) -> _Recorded:
        return _Recorded("quarantined", "quarantined", quarantine_reason=reason, **fields)

    def _give_up(self, row: Claimed) -> Result:
        claimed = from_address(row.raw)[0]
        try:
            org = senders.attribute_org(self.engine, claimed)
        except Exception:  # noqa: BLE001 - attribution is best effort here
            org = None
        recorded = self._quarantine(
            "parse_error", org_id=org, sender=claimed, subject=_subject(row.raw)
        )
        with transaction(self.engine) as tx:
            _finish(tx, row.id, recorded)
        return self._result(row, recorded)

    def _decide(self, row: Claimed) -> _Recorded | _Ready:
        raw = row.raw
        claimed = from_address(raw)[0]
        subject = _subject(raw)
        if not self.limits.admit_peer(row.peer_ip):
            return self._quarantine(
                "rate_limited",
                org_id=senders.attribute_org(self.engine, claimed),
                sender=claimed,
                subject=subject,
            )
        auth: SenderAuth = verify(raw, row.mail_from, row.peer_ip, row.helo, self.policy)
        checked = {
            "sender": auth.sender or claimed,
            "sender_auth": auth.sender_auth,
            "auth_summary": auth.summary,
            "subject": subject,
        }
        if not auth.verified or auth.sender is None:
            return self._quarantine(
                "sender_unverified",
                org_id=senders.attribute_org(self.engine, auth.sender or claimed),
                **checked,
            )
        who = senders.resolve(self.engine, auth.sender)
        if not who.resolved or who.org_id is None or who.user_id is None:
            return self._quarantine("unknown_user", org_id=who.org_id, **checked)
        member = {"org_id": who.org_id, "user_id": who.user_id, **checked}
        if not self.limits.admit_verified(auth.sender, who.org_id):
            return self._quarantine("rate_limited", **member)
        try:
            parsed = parse(
                raw, self.settings.intake_addresses, default_tz=self.settings.default_timezone
            )
        except InviteParseError:
            return self._quarantine("parse_error", **member)
        if not parsed:
            return self._quarantine("parse_error", **member)
        return _Ready(_Recorded("done", "scheduled", **member), parsed, who)

    def _apply(self, row: Claimed, ready: _Ready) -> Result:
        recorded, parsed, who = ready.recorded, ready.parsed, ready.who
        now = self.clock()
        org_id = who.org_id
        assert org_id is not None and who.user_id is not None and recorded.sender is not None
        sender = events.Sender(recorded.sender, who.user_id, who.team_id, row.id)
        groups: dict[str, list[ParsedEvent]] = {}
        for event in parsed:
            groups.setdefault(event.uid_norm, []).append(event)
        language = self.settings.default_language
        with transaction(self.engine, org_id=org_id, user_id=who.user_id) as tx:
            mode = effective_auto_join(tx, org_id, who.team_id)
            allowed, blocked = may_create(mode, any(e.auto_forwarded for e in parsed))
            plans = []
            for uid_norm, items in groups.items():
                lock_group(tx, org_id, uid_norm)
                state = load_group(tx, org_id, uid_norm)
                plan = events.apply(
                    state, items, sender, now, self.horizon,
                    may_create=allowed, blocked_reason=blocked,
                )
                if plan.outcome not in ("ignored", "quarantined"):
                    write_plan(tx, org_id, plan, now=now, language=language)
                if plan.warnings:
                    log.info("inbound %s: %s", row.id, ", ".join(sorted(set(plan.warnings))))
                plans.append(plan)
            outcome = events.strongest([p.outcome for p in plans])
            reason = next((p.reason for p in plans if p.outcome == outcome), None)
            recorded.outcome = outcome
            recorded.status = (
                outcome if outcome in ("quarantined", "ignored") else "done"
            )
            recorded.quarantine_reason = reason if outcome == "quarantined" else None
            recorded.ignore_reason = reason if outcome == "ignored" else None
            recorded.plans = plans
            if not _finish(tx, row.id, recorded):
                raise LeaseLost(str(row.id))
        return self._result(row, recorded)

    def _result(self, row: Claimed, recorded: _Recorded) -> Result:
        applied = [p for p in recorded.plans if p.outcome not in ("ignored", "quarantined")]
        return Result(
            inbound_id=row.id,
            status=recorded.status,
            outcome=recorded.outcome,
            quarantine_reason=recorded.quarantine_reason,
            ignore_reason=recorded.ignore_reason,
            org_id=recorded.org_id,
            user_id=recorded.user_id,
            created=tuple(c.id for p in applied for c in p.creates),
            joined_now=tuple(c.id for p in applied for c in p.creates if c.joined_now),
            changed=tuple(c.id for p in applied for c in p.changes),
            cancelled=tuple(m for p in applied for m in p.cancels),
        )

    # ── the loop ──

    def run_once(self) -> list[Result]:
        return [self.process(row) for row in claim(self.engine, self.batch)]

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                results = await asyncio.to_thread(self.run_once)
                if results:
                    log.info(
                        "inbound mail: %s",
                        ", ".join(f"{r.inbound_id} {r.outcome or r.status}" for r in results),
                    )
                if len(results) >= self.batch:
                    continue  # more may be waiting
            except Exception as exc:  # the next tick retries; the error type only, no content
                log.warning("inbound mail processing failed: %s", type(exc).__name__)
            await asyncio.sleep(every)
