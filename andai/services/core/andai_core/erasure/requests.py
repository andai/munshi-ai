"""Asking for an erasure: the admin route, the listing, and `create_request` for the operator CLI.

* `POST /api/v1/org/users/{id}/erasure {confirm}` (owners and admins) erases a member's data in the
  caller's org. `confirm` must be the member's email address (any case). In one transaction:
  the guards (an open request for the person in this org → 409 `erasure_pending`; not a member of
  this org → 404 `no_such_member`; the caller → 409 `cannot_erase_self`; the org's owner → 409
  `owner_cannot_be_erased`; a wrong `confirm` → 422 `confirm_mismatch`), then the person's team
  memberships and membership in this org and their sessions in this org go, and the request is
  inserted with `org_id = audit_org_id =` this org, `account = true` when no other membership
  remains, and `user.erase.request` (target `user:<id>`) is audited. The person loses access to the
  org at that commit; andai-worker does the rest (`runner.py`). The answer is 202
  `{id, state, eraseBy}` (`eraseBy` = the request's time + 24 h, the SLA).
* `GET /api/v1/org/erasures?cursor=` (owners and admins) lists the org's requests, newest first,
  50 per page: `{items: [{id, subjectId, state, step, createdAt, finishedAt, error}], next}`. Ids
  and states only: no name or address.

Two requests for one person in two orgs at the same moment would each still see the other
membership and neither would erase the account, so the membership count and the insert run under a
transaction-scoped advisory lock on the person's id (`pg_advisory_xact_lock`).

The person's memberships in other orgs are read through the membership policy's own-row branch
(`app.user_id` set to the person for that one statement, then set back), the way login finds a
user's orgs.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth.deps import Services, client_ip, get_services, org_transaction
from andai_core.auth.roles import Caller, require_role
from andai_core.db.models import (
    ERASURE_OPEN_STATES,
    ErasureRequest,
    OrgMembership,
    Session,
    TeamMembership,
    User,
)
from andai_core.db.session import USER_ID
from andai_core.meetings.service import MeetingError

ERASE_WITHIN = timedelta(hours=24)
PAGE_SIZE = 50
MAX_CURSOR_CHARS = 128
OWNER = "owner"

ERASURE_PENDING = MeetingError(
    409, "erasure_pending", "an erasure of this person's data in this organisation is under way"
)
NO_SUCH_MEMBER = MeetingError(404, "no_such_member", "no such member")
CANNOT_ERASE_SELF = MeetingError(
    409, "cannot_erase_self", "you cannot erase your own data here"
)
OWNER_CANNOT_BE_ERASED = MeetingError(
    409,
    "owner_cannot_be_erased",
    "the organisation's owner cannot be erased; transfer ownership first",
)
CONFIRM_MISMATCH = MeetingError(
    422, "confirm_mismatch", "type the member's email address exactly to confirm"
)
BAD_CURSOR = MeetingError(422, "bad_cursor", "this cursor is not valid for this list")


# ─── shared with the operator CLI ─────────────────────────────────────────────────────────────────


def lock_subject(tx: Tx, subject: uuid.UUID) -> None:
    """Serialise erasure requests for one person until the transaction ends."""
    tx.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"erasure:{subject}"},
    )


def memberships_of(tx: Tx, subject: uuid.UUID, *, caller: uuid.UUID | None) -> list[tuple]:
    """Every (org id, role) membership of `subject`, in any org. `app.user_id` is the subject's for
    this one read and is set back to `caller` (or cleared) afterwards."""
    tx.execute(text("SELECT set_config(:n, :v, true)"), {"n": USER_ID, "v": str(subject)})
    try:
        rows = tx.execute(
            select(OrgMembership.org_id, OrgMembership.role)
            .where(OrgMembership.user_id == subject)
            .order_by(OrgMembership.created_at, OrgMembership.org_id)
        ).all()
    finally:
        tx.execute(
            text("SELECT set_config(:n, :v, true)"),
            {"n": USER_ID, "v": "" if caller is None else str(caller)},
        )
    return [(row.org_id, row.role) for row in rows]


def remove_from_org(tx: Tx, org_id: uuid.UUID, subject: uuid.UUID) -> dict[str, int]:
    """End the person's team memberships, membership and sessions in `org_id` (the transaction's
    org)."""
    teams = tx.execute(
        delete(TeamMembership).where(
            TeamMembership.org_id == org_id, TeamMembership.user_id == subject
        )
    ).rowcount
    member = tx.execute(
        delete(OrgMembership).where(
            OrgMembership.org_id == org_id, OrgMembership.user_id == subject
        )
    ).rowcount
    sessions = tx.execute(
        delete(Session).where(Session.org_id == org_id, Session.user_id == subject)
    ).rowcount
    return {"team_memberships": teams, "org_memberships": member, "sessions": sessions}


def open_request(tx: Tx, audit_org_id: uuid.UUID, subject: uuid.UUID) -> ErasureRequest | None:
    return tx.scalar(
        select(ErasureRequest).where(
            ErasureRequest.audit_org_id == audit_org_id,
            ErasureRequest.subject_user_id == subject,
            ErasureRequest.state.in_(ERASURE_OPEN_STATES),
        )
    )


def create_request(
    tx: Tx,
    *,
    subject: uuid.UUID,
    org_id: uuid.UUID | None,
    audit_org_id: uuid.UUID,
    source: str,
    requested_by: uuid.UUID | None,
    account: bool,
    now: datetime,
    counts: dict | None = None,
    ip: str | None = None,
) -> ErasureRequest:
    """Insert one request and audit `user.erase.request` in `audit_org_id` (the transaction's
    org). Raises `ERASURE_PENDING` when the person already has an open request there."""
    request = ErasureRequest(
        id=uuid.uuid4(),
        org_id=org_id,
        audit_org_id=audit_org_id,
        subject_user_id=subject,
        requested_by=requested_by,
        source=source,
        account=account,
        state="pending",
        step="stop_live" if org_id is not None else "account",
        attempts=0,
        counts={"request": counts or {}},
        created_at=now,
    )
    try:
        with tx.begin_nested():
            tx.add(request)
            tx.flush()
    except IntegrityError:
        raise ERASURE_PENDING from None
    audit.record(tx, audit_org_id, requested_by, "user.erase.request", f"user:{subject}", ip)
    return request


def view(request: ErasureRequest) -> dict:
    return {
        "id": str(request.id),
        "subjectId": str(request.subject_user_id),
        "state": request.state,
        "step": request.step,
        "createdAt": request.created_at.isoformat(),
        "finishedAt": None if request.finished_at is None else request.finished_at.isoformat(),
        "error": request.error,
    }


# ─── the admin's request ──────────────────────────────────────────────────────────────────────────


def request_erasure(
    tx: Tx,
    *,
    org_id: uuid.UUID,
    caller_id: uuid.UUID,
    subject_id: uuid.UUID,
    confirm: str,
    now: datetime,
    ip: str | None = None,
) -> dict:
    """The admin route's transaction (under the caller's org and user context)."""
    lock_subject(tx, subject_id)
    if open_request(tx, org_id, subject_id) is not None:
        raise ERASURE_PENDING
    row = tx.execute(
        select(OrgMembership.role, User.email)
        .join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_id == org_id, OrgMembership.user_id == subject_id)
    ).first()
    if row is None:
        raise NO_SUCH_MEMBER
    if subject_id == caller_id:
        raise CANNOT_ERASE_SELF
    if row.role == OWNER:
        raise OWNER_CANNOT_BE_ERASED
    if confirm.strip().lower() != row.email.lower():
        raise CONFIRM_MISMATCH
    removed = remove_from_org(tx, org_id, subject_id)
    others = memberships_of(tx, subject_id, caller=caller_id)
    request = create_request(
        tx,
        subject=subject_id,
        org_id=org_id,
        audit_org_id=org_id,
        source="org_admin",
        requested_by=caller_id,
        account=not others,
        now=now,
        counts=removed,
        ip=ip,
    )
    return {
        "id": str(request.id),
        "state": request.state,
        "eraseBy": (now + ERASE_WITHIN).isoformat(),
    }


def encode_cursor(at: datetime, request_id: uuid.UUID) -> str:
    raw = f"{at.isoformat()}|{request_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise BAD_CURSOR
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, request_id = raw.split("|")
        position = (datetime.fromisoformat(at), uuid.UUID(request_id))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BAD_CURSOR from None
    if position[0].tzinfo is None:
        raise BAD_CURSOR
    return position


def list_requests(tx: Tx, org_id: uuid.UUID, cursor: str | None) -> dict:
    query = select(ErasureRequest).where(ErasureRequest.audit_org_id == org_id)
    if cursor is not None:
        at, request_id = decode_cursor(cursor)
        query = query.where(
            func.row(ErasureRequest.created_at, ErasureRequest.id) < func.row(at, request_id)
        )
    rows = tx.scalars(
        query.order_by(ErasureRequest.created_at.desc(), ErasureRequest.id.desc()).limit(
            PAGE_SIZE + 1
        )
    ).all()
    page = rows[:PAGE_SIZE]
    nxt = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > PAGE_SIZE else None
    return {"items": [view(r) for r in page], "next": nxt}


# ─── routes ───────────────────────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/org")
ADMINS = ("owner", "admin")


class ErasureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(min_length=1, max_length=320)


@router.post("/users/{user_id}/erasure", status_code=202)
def post_erasure(
    user_id: uuid.UUID,
    body: ErasureBody,
    caller: Caller = Depends(require_role(*ADMINS)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        return request_erasure(
            tx,
            org_id=caller.session.org_id,
            caller_id=caller.session.user_id,
            subject_id=user_id,
            confirm=body.confirm,
            now=services.clock(),
            ip=ip,
        )


@router.get("/erasures")
def get_erasures(
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_CHARS),
    caller: Caller = Depends(require_role(*ADMINS)),
    services: Services = Depends(get_services),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        return list_requests(tx, caller.session.org_id, cursor)
