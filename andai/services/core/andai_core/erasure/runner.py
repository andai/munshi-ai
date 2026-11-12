"""The erasure step machine: what andai-worker runs for each request `andai_claim_erasures` hands
it.

`run(engine, meetings, audit_org_id, request_id)` works one request forward from its stored `step`
until it has to wait, fails or is done. Every step is idempotent and its progress is committed
before the next one starts, so a crash or a second worker repeats at most the step it was in. All
reads and writes of the request run under its audit org (`audit_org_id`); the org steps run under
the org being erased (`org_id`); the account step touches only the global tables.

1. `stop_live` — the person's scheduled meetings in the org are cancelled (`requester_inactive`) and
   their meetings with a bot on its way or in the room are stopped through `Meetings.stop` (as the
   requesting admin, or the tombstone user for the operator), again every 2 minutes while any is
   still live. The step waits until every meeting of theirs is over.
2. `delete_owned` — each of their meetings not yet deleted is hidden for the purge exactly as
   `DELETE /meetings/{id}` does (`deleted_at`, `deleted_by`, `deletion_state = pending`) and
   audited `meeting.delete`. The purge itself is the meeting deletion path (`meetings.deletion`,
   andai-worker's `DeletionRunner`): platform first, then andai's rows.
3. `wait_purge` — waits until none of their meetings is left in the org. A purge that spent its
   attempts (`deletion_state = failed`) fails the request with `purge_failed`.
4. `scrub` — one transaction applying every `org`-scope rule of the registry (`SCRUB_HANDLERS`):
   memberships and sessions, received shares, invites to their address, the invite events they
   sent, the mailbox rows that are theirs and the copies of their data (exports) go; who granted a
   share, deleted or skipped a meeting, sent an invite, changed a setting or asked for another
   member's export is re-pointed at the tombstone user. Rows touched per table
   go into `counts`.
5. `account` — only for a request with `account` set, and only once no membership remains and no
   other request for the person is unfinished (`andai_erasure_siblings`; it waits otherwise): all
   sessions, the TOTP secret, recovery codes and password resets go, the org-less mailbox rows from
   their address go, and the users row is anonymised (`erased-<request id>@munshi.invalid`,
   `Erased user`, no password, status `erased`). A membership that appeared again in the meantime
   leaves the account as it is (`counts.account = "kept"`).
6. `done` — `user.erase.complete` (target `user:<id>`) in the audit org, `state = done`.

Waiting is not an attempt: the request is put back with `next_at` 10 s on. An error is: it is
retried after 10 s doubling to 10 minutes, and after `MAX_ATTEMPTS` the request is `failed` with
`internal_error` and `user.erase.failed` is audited (as it is at once for `purge_failed`). Nothing
written here names the person: ids, states and counts only.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import anyio.to_thread
from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.db.models import (
    ERASED_NAME,
    TERMINAL_STATUSES,
    TOMBSTONE_USER_ID,
    BotSetting,
    ErasureRequest,
    ExportRequest,
    Invite,
    InviteEvent,
    Meeting,
    MeetingShare,
    OrgMembership,
    PasswordReset,
    RecoveryCode,
    Session,
    TeamMembership,
    User,
    UserTotp,
)
from andai_core.db.session import transaction
from andai_core.erasure.requests import memberships_of
from andai_core.meetings.access import Actor
from andai_core.meetings.service import MeetingError, Meetings

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BACKOFF_BASE_S = 10
BACKOFF_CAP_S = 600
WAIT = timedelta(seconds=10)
RESTOP_AFTER = timedelta(minutes=2)
# A bot on its way in or in the room: what `Meetings.stop` can end.
STOPPABLE = ("dispatching", "awaiting_admission", "recording")
REQUESTER_INACTIVE = "requester_inactive"
ORG_STEPS = ("stop_live", "delete_owned", "wait_purge", "scrub")
NEXT_STEP = {
    "stop_live": "delete_owned",
    "delete_owned": "wait_purge",
    "wait_purge": "scrub",
    "scrub": "account",
    "account": "done",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_CAP_S))


def erased_email(request_id: uuid.UUID) -> str:
    return f"erased-{request_id}@munshi.invalid"


@dataclass
class Request:
    """What the steps need of one request (read under its audit org)."""

    id: uuid.UUID
    org_id: uuid.UUID | None
    audit_org_id: uuid.UUID
    subject: uuid.UUID
    requested_by: uuid.UUID | None
    account: bool
    state: str
    step: str
    attempts: int
    counts: dict = field(default_factory=dict)

    @property
    def actor_id(self) -> uuid.UUID:
        """Who the steps act as in the org: the requesting admin, or the tombstone user."""
        return self.requested_by or TOMBSTONE_USER_ID


class _Wait(Exception):
    """The step cannot finish yet; the request is due again after `WAIT`."""


class _Fail(Exception):
    """The request fails at once with this error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load(engine: Engine, audit_org_id: uuid.UUID, request_id: uuid.UUID) -> Request | None:
    with transaction(engine, org_id=audit_org_id) as tx:
        row = tx.get(ErasureRequest, request_id)
        if row is None:
            return None
        return Request(
            id=row.id,
            org_id=row.org_id,
            audit_org_id=row.audit_org_id,
            subject=row.subject_user_id,
            requested_by=row.requested_by,
            account=row.account,
            state=row.state,
            step=row.step,
            attempts=row.attempts,
            counts=dict(row.counts or {}),
        )


def _save(engine: Engine, request: Request, **values) -> None:
    with transaction(engine, org_id=request.audit_org_id) as tx:
        tx.execute(
            update(ErasureRequest).where(ErasureRequest.id == request.id).values(**values)
        )


def _owned(org_id: uuid.UUID, subject: uuid.UUID):
    return select(Meeting).where(Meeting.org_id == org_id, Meeting.requested_by == subject)


# ─── the org steps ────────────────────────────────────────────────────────────────────────────────


async def _stop_live(
    engine: Engine, meetings: Meetings, request: Request, now: datetime
) -> None:
    org_id = request.org_id
    assert org_id is not None
    with transaction(engine, org_id=org_id) as tx:
        open_meetings = [
            (m.id, m.status)
            for m in tx.scalars(
                _owned(org_id, request.subject).where(Meeting.status.not_in(TERMINAL_STATUSES))
            )
        ]
    scheduled = [mid for mid, status in open_meetings if status == "scheduled"]
    for meeting_id in scheduled:
        await anyio.to_thread.run_sync(
            lambda m=meeting_id: meetings.drop_scheduled(org_id, m, REQUESTER_INACTIVE)
        )
    live = [(mid, status) for mid, status in open_meetings if status != "scheduled"]
    if scheduled:
        request.counts["scheduled_cancelled"] = (
            request.counts.get("scheduled_cancelled", 0) + len(scheduled)
        )
    if not live:
        if scheduled:
            _save(engine, request, counts=request.counts)
        return
    stoppable = [mid for mid, status in live if status in STOPPABLE]
    sent = request.counts.get("stop_sent_at")
    if stoppable and (
        sent is None or now - datetime.fromisoformat(sent) >= RESTOP_AFTER
    ):
        actor = Actor(org_id=org_id, user_id=request.actor_id, role="owner")
        for meeting_id in stoppable:
            try:
                await meetings.stop(actor, meeting_id)
            except MeetingError as exc:
                if exc.code not in ("not_running", "no_such_meeting"):
                    raise
        request.counts["stop_sent_at"] = now.isoformat()
        request.counts["stopped"] = request.counts.get("stopped", 0) + len(stoppable)
    _save(engine, request, counts=request.counts)
    raise _Wait


def _delete_owned(engine: Engine, request: Request, now: datetime) -> None:
    org_id = request.org_id
    assert org_id is not None
    with transaction(engine, org_id=org_id, user_id=request.actor_id) as tx:
        found = tx.scalars(
            _owned(org_id, request.subject)
            .where(Meeting.deleted_at.is_(None))
            .with_for_update(of=Meeting)
        ).all()
        if any(m.status not in TERMINAL_STATUSES for m in found):
            # A meeting started after the stop step (a scheduled dispatch in flight): stop it too.
            request.step = "stop_live"
            raise _Wait
        for meeting in found:
            meeting.deleted_at = now
            meeting.deleted_by = request.actor_id
            meeting.deletion_state = "pending"
            meeting.deletion_attempts = 0
            meeting.deletion_next_at = None
            meeting.updated_at = now
            audit.record(
                tx, org_id, request.requested_by, "meeting.delete", f"meeting:{meeting.id}"
            )
        request.counts["meetings_deleted"] = request.counts.get("meetings_deleted", 0) + len(found)


def _wait_purge(engine: Engine, request: Request) -> None:
    org_id = request.org_id
    assert org_id is not None
    with transaction(engine, org_id=org_id) as tx:
        states = [
            state
            for state in tx.scalars(
                select(Meeting.deletion_state).where(
                    Meeting.org_id == org_id, Meeting.requested_by == request.subject
                )
            )
        ]
    if "failed" in states:
        raise _Fail("purge_failed")
    if states:
        raise _Wait


@dataclass(frozen=True)
class Scrub:
    """What a scrub handler needs: the org being erased, the person, their address."""

    org_id: uuid.UUID
    subject: uuid.UUID
    email: str


def _repoint(model, column):
    def handler(tx: Tx, s: Scrub) -> int:
        return tx.execute(
            update(model)
            .where(model.org_id == s.org_id, column == s.subject)
            .values({column.key: TOMBSTONE_USER_ID})
        ).rowcount

    return handler


def _team_memberships(tx: Tx, s: Scrub) -> int:
    return tx.execute(
        delete(TeamMembership).where(
            TeamMembership.org_id == s.org_id, TeamMembership.user_id == s.subject
        )
    ).rowcount


def _org_memberships(tx: Tx, s: Scrub) -> int:
    return tx.execute(
        delete(OrgMembership).where(
            OrgMembership.org_id == s.org_id, OrgMembership.user_id == s.subject
        )
    ).rowcount


def _sessions(tx: Tx, s: Scrub) -> int:
    return tx.execute(
        delete(Session).where(Session.org_id == s.org_id, Session.user_id == s.subject)
    ).rowcount


def _received_shares(tx: Tx, s: Scrub) -> int:
    return tx.execute(
        delete(MeetingShare).where(
            MeetingShare.org_id == s.org_id,
            MeetingShare.grantee_type == "user",
            MeetingShare.grantee_id == s.subject,
        )
    ).rowcount


def _invites_to(tx: Tx, s: Scrub) -> int:
    return tx.execute(
        delete(Invite).where(Invite.org_id == s.org_id, Invite.email == s.email.lower())
    ).rowcount


def _invite_events(tx: Tx, s: Scrub) -> int:
    return tx.execute(
        delete(InviteEvent).where(
            InviteEvent.org_id == s.org_id, InviteEvent.requested_by == s.subject
        )
    ).rowcount


def _exports_of(tx: Tx, s: Scrub) -> int:
    return tx.execute(
        delete(ExportRequest).where(
            ExportRequest.org_id == s.org_id, ExportRequest.subject_user_id == s.subject
        )
    ).rowcount


def _inbound(tx: Tx, s: Scrub) -> int:
    return int(
        tx.execute(
            text("SELECT andai_erase_inbound(:o, :u, :e)"),
            {"o": s.org_id, "u": s.subject, "e": s.email},
        ).scalar_one()
    )


# (table, column) → handler, in the order they run; one handler may serve several columns (the
# mailbox rows of a person go in one call) and runs once.
SCRUB_HANDLERS: dict[tuple[str, str], Callable[[Tx, Scrub], int]] = {
    ("team_memberships", "user_id"): _team_memberships,
    ("org_memberships", "user_id"): _org_memberships,
    ("sessions", "user_id"): _sessions,
    ("meeting_shares", "grantee_id"): _received_shares,
    ("meeting_shares", "granted_by"): _repoint(MeetingShare, MeetingShare.granted_by),
    ("meetings", "deleted_by"): _repoint(Meeting, Meeting.deleted_by),
    ("meetings", "skipped_by"): _repoint(Meeting, Meeting.skipped_by),
    ("invites", "invited_by"): _repoint(Invite, Invite.invited_by),
    ("invites", "email"): _invites_to,
    ("invite_events", "requested_by"): _invite_events,
    ("inbound_mail", "resolved_user_id"): _inbound,
    ("inbound_mail", "sender"): _inbound,
    ("inbound_mail", "mail_from"): _inbound,
    ("bot_settings", "updated_by"): _repoint(BotSetting, BotSetting.updated_by),
    ("export_requests", "subject_user_id"): _exports_of,
    ("export_requests", "requested_by"): _repoint(ExportRequest, ExportRequest.requested_by),
}


def _scrub(engine: Engine, request: Request) -> None:
    org_id = request.org_id
    assert org_id is not None
    with transaction(engine, org_id=org_id) as tx:
        email = tx.scalar(select(User.email).where(User.id == request.subject))
        if email is None:
            raise RuntimeError("the subject's users row is missing")
        left = tx.scalar(
            select(func.count()).select_from(Meeting).where(
                Meeting.org_id == org_id, Meeting.requested_by == request.subject
            )
        )
        if left:
            raise RuntimeError("meetings of the subject remain after the purge")
        context = Scrub(org_id=org_id, subject=request.subject, email=email)
        counts: dict[str, int] = {}
        done: dict[Callable, int] = {}
        for (table, column), handler in SCRUB_HANDLERS.items():
            if handler not in done:
                done[handler] = handler(tx, context)
                counts[f"{table}.{column}"] = done[handler]
        request.counts["scrub"] = counts


# ─── the account step ─────────────────────────────────────────────────────────────────────────────


def _delete_all(model) -> Callable[[Tx, uuid.UUID, uuid.UUID], int]:
    def handler(tx: Tx, subject: uuid.UUID, request_id: uuid.UUID) -> int:  # noqa: ARG001
        return tx.execute(delete(model).where(model.user_id == subject)).rowcount

    return handler


def _anonymise(tx: Tx, subject: uuid.UUID, request_id: uuid.UUID) -> int:
    return tx.execute(
        update(User)
        .where(User.id == subject)
        .values(
            email=erased_email(request_id),
            display_name=ERASED_NAME,
            password_hash=None,
            status="erased",
            last_login_at=None,
        )
    ).rowcount


# (table, column) → handler for the `account`-scope rules; the users row is anonymised last.
ACCOUNT_HANDLERS: dict[tuple[str, str], Callable[[Tx, uuid.UUID, uuid.UUID], int]] = {
    ("user_totp", "user_id"): _delete_all(UserTotp),
    ("recovery_codes", "user_id"): _delete_all(RecoveryCode),
    ("password_resets", "user_id"): _delete_all(PasswordReset),
    ("users", "email"): _anonymise,
    ("users", "display_name"): _anonymise,
}


def _account(engine: Engine, request: Request) -> None:
    if not request.account:
        return
    with transaction(engine) as tx:
        if memberships_of(tx, request.subject, caller=None):
            request.counts["account"] = "kept"
            return
        siblings = tx.scalar(
            text("SELECT andai_erasure_siblings(:s, :r)"),
            {"s": request.subject, "r": request.id},
        )
        if siblings:
            raise _Wait
        user = tx.get(User, request.subject, with_for_update=True)
        if user is None or user.status == "erased":
            request.counts["account"] = "erased"
            return
        counts = {
            "sessions": tx.execute(
                delete(Session).where(Session.user_id == request.subject)
            ).rowcount,
            "inbound_mail": int(
                tx.execute(
                    text("SELECT andai_erase_inbound(NULL, :u, :e)"),
                    {"u": request.subject, "e": user.email},
                ).scalar_one()
            ),
        }
        done: set[Callable] = set()
        for (table, column), handler in ACCOUNT_HANDLERS.items():
            if handler not in done:
                done.add(handler)
                counts[f"{table}.{column}"] = handler(tx, request.subject, request.id)
        request.counts["account"] = counts


def _finish(engine: Engine, request: Request, now: datetime) -> None:
    with transaction(engine, org_id=request.audit_org_id) as tx:
        tx.execute(
            update(ErasureRequest)
            .where(ErasureRequest.id == request.id)
            .values(
                state="done",
                step="done",
                counts=request.counts,
                next_at=None,
                lease_until=None,
                error=None,
                finished_at=now,
            )
        )
        audit.record(
            tx,
            request.audit_org_id,
            request.requested_by,
            "user.erase.complete",
            f"user:{request.subject}",
        )


def _failed(engine: Engine, request: Request, code: str, now: datetime) -> None:
    with transaction(engine, org_id=request.audit_org_id) as tx:
        tx.execute(
            update(ErasureRequest)
            .where(ErasureRequest.id == request.id)
            .values(
                state="failed",
                error=code,
                counts=request.counts,
                next_at=None,
                lease_until=None,
                finished_at=now,
            )
        )
        audit.record(
            tx,
            request.audit_org_id,
            request.requested_by,
            "user.erase.failed",
            f"user:{request.subject}",
        )


def _retry(engine: Engine, request: Request, now: datetime) -> str:
    attempts = request.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        request.attempts = attempts
        _save(engine, request, attempts=attempts)
        _failed(engine, request, "internal_error", now)
        return "failed"
    _save(
        engine,
        request,
        attempts=attempts,
        next_at=now + backoff(attempts),
        lease_until=None,
        counts=request.counts,
    )
    return "running"


async def run(
    engine: Engine,
    meetings: Meetings,
    audit_org_id: uuid.UUID,
    request_id: uuid.UUID,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> str | None:
    """Work one request forward. Returns its state afterwards (`running` while it waits or retries,
    `done`, `failed`), or None when it is not an open request of `audit_org_id`."""
    request = await anyio.to_thread.run_sync(lambda: load(engine, audit_org_id, request_id))
    if request is None or request.state not in ("pending", "running"):
        return None
    try:
        while request.step != "done":
            step = request.step
            now = clock()
            if step in ORG_STEPS and request.org_id is None:
                pass  # an account-only request has no org to work on
            elif step == "stop_live":
                await _stop_live(engine, meetings, request, now)
            elif step == "delete_owned":
                await anyio.to_thread.run_sync(lambda: _delete_owned(engine, request, now))
            elif step == "wait_purge":
                await anyio.to_thread.run_sync(lambda: _wait_purge(engine, request))
            elif step == "scrub":
                await anyio.to_thread.run_sync(lambda: _scrub(engine, request))
            elif step == "account":
                await anyio.to_thread.run_sync(lambda: _account(engine, request))
            else:
                raise RuntimeError(f"unknown erasure step {step!r}")
            request.step = NEXT_STEP[step]
            await anyio.to_thread.run_sync(
                lambda: _save(engine, request, step=request.step, counts=request.counts)
            )
        now = clock()
        await anyio.to_thread.run_sync(lambda: _finish(engine, request, now))
        return "done"
    except _Wait:
        now = clock()
        await anyio.to_thread.run_sync(
            lambda: _save(
                engine,
                request,
                step=request.step,
                next_at=now + WAIT,
                lease_until=None,
                counts=request.counts,
            )
        )
        return "running"
    except _Fail as exc:
        now = clock()
        log.warning("erasure %s: failed (%s)", request.id, exc.code)
        await anyio.to_thread.run_sync(lambda: _failed(engine, request, exc.code, now))
        return "failed"
    except Exception:  # noqa: BLE001 - every failed attempt counts toward the cap
        now = clock()
        log.exception("erasure %s: step %s failed", request.id, request.step)
        return await anyio.to_thread.run_sync(lambda: _retry(engine, request, now))


__all__ = [
    "ACCOUNT_HANDLERS",
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "MAX_ATTEMPTS",
    "SCRUB_HANDLERS",
    "backoff",
    "erased_email",
    "load",
    "run",
]
