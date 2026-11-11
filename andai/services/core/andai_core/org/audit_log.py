"""The org's audit trail, for its owners and admins (OPS-04).

`GET /api/v1/org/audit?from=YYYY-MM-DD&to=YYYY-MM-DD&action=a,b&cursor=` lists the caller's org's
audit events, newest first (time, then id), 50 per page:

    {"items": [{id, at, actor: {id, name}, action, category, target: {type, id, label}, ip}],
     "next": str | null}

* `from` / `to` — whole UTC days, both inclusive; `to` defaults to today and `from` to 29 days
  before `to` (30 days). `from` after `to`, or a range longer than 366 days, is 422 `bad_range`.
* `action` — comma-separated registered actions (`andai_core.audit_actions`); any other is 422
  `unknown_action`.
* `cursor` — the `next` of the previous page, with the same filters; a cursor this route did not
  issue is 422 `bad_cursor`.
* `actor` — who acted (`id` null for the system and for failed sign-ins). `name` is the user's
  display name, or null when the user no longer exists.
* `target` — what the event is about, from the stored `type:id` (`user`, `team`, `meeting`,
  `org`, `invite`). `label` is the user's name for a user who is a member of this org or acted on
  this page, the team's name, or the meeting's title while the meeting exists and is not deleted;
  otherwise null.

`GET /api/v1/org/audit/actions` lists the registry, `[{key, category, label}]`, for the filter.

The app role cannot read `audit_events`: the rows come from `andai_audit_page` (migration 0017),
which reads only the transaction's org. Names and titles are read under the same org's row-level
security. Members and leads get 403.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session as Tx

from andai_core.audit_actions import ACTIONS
from andai_core.auth.deps import Services, get_services, org_transaction
from andai_core.auth.roles import Caller, require_role
from andai_core.db.models import Meeting, OrgMembership, Team, User
from andai_core.meetings.service import MeetingError

router = APIRouter(prefix="/api/v1/org")

PAGE_SIZE = 50
DEFAULT_DAYS = 30
MAX_DAYS = 366
MAX_CURSOR_CHARS = 128
MAX_ACTION_CHARS = 2000
BAD_CURSOR = MeetingError(422, "bad_cursor", "this cursor is not valid for this list")
BAD_RANGE = MeetingError(
    422,
    "bad_range",
    f"the start date must not be after the end date, and the range at most {MAX_DAYS} days",
)
PAGE = text(
    "SELECT id, at, actor_user_id, action, target, host(ip) AS ip FROM andai_audit_page("
    " :start, :end, CAST(:actions AS text[]), CAST(:before_at AS timestamptz),"
    " CAST(:before_id AS uuid), :limit)"
)


# ─── filters and cursor ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """Where a page starts: after the event with this time and id."""

    at: datetime
    event_id: uuid.UUID


def encode_cursor(position: Position) -> str:
    raw = f"{position.at.isoformat()}|{position.event_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> Position:
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise BAD_CURSOR
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, event_id = raw.split("|")
        position = Position(datetime.fromisoformat(at), uuid.UUID(event_id))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BAD_CURSOR from None
    if position.at.tzinfo is None:
        raise BAD_CURSOR
    return position


def day_bounds(start: date | None, end: date | None, today: date) -> tuple[datetime, datetime]:
    """[start 00:00 UTC, the day after end 00:00 UTC) for whole, inclusive days."""
    end = end or today
    start = start or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end or (end - start).days + 1 > MAX_DAYS:
        raise BAD_RANGE
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


def parse_actions(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    actions = sorted({part.strip() for part in value.split(",") if part.strip()})
    unknown = [a for a in actions if a not in ACTIONS]
    if unknown:
        raise MeetingError(422, "unknown_action", "unknown audit action")
    return actions


# ─── names ────────────────────────────────────────────────────────────────────────────────────────


def split_target(target: str) -> tuple[str, str]:
    """`type:id` of the stored target's first word (`meeting:<id> version:3` → meeting, <id>)."""
    head = target.split(" ", 1)[0]
    kind, _, ident = head.partition(":")
    return (kind, ident) if ident else ("", head)


def _uuids(values) -> set[uuid.UUID]:
    found = set()
    for value in values:
        try:
            found.add(uuid.UUID(value))
        except (ValueError, TypeError):
            continue
    return found


def _labels(tx: Tx, org_id: uuid.UUID, rows) -> tuple[dict, dict]:
    """(user names, {(type, id): label}) for the users, teams and meetings a page names."""
    actors = {row.actor_user_id for row in rows if row.actor_user_id is not None}
    targets: dict[str, set[uuid.UUID]] = {"user": set(), "team": set(), "meeting": set()}
    for row in rows:
        kind, ident = split_target(row.target)
        if kind in targets:
            targets[kind] |= _uuids([ident])

    members: set[uuid.UUID] = set()
    if targets["user"]:
        members = set(
            tx.scalars(
                select(OrgMembership.user_id).where(
                    OrgMembership.org_id == org_id, OrgMembership.user_id.in_(targets["user"])
                )
            )
        )
    people = actors | members
    names = (
        dict(tx.execute(select(User.id, User.display_name).where(User.id.in_(people))).all())
        if people
        else {}
    )
    labels: dict[tuple[str, uuid.UUID], str] = {
        ("user", user_id): names[user_id] for user_id in targets["user"] if user_id in names
    }
    if targets["team"]:
        for team_id, name in tx.execute(
            select(Team.id, Team.name).where(
                Team.org_id == org_id, Team.id.in_(targets["team"])
            )
        ):
            labels[("team", team_id)] = name
    if targets["meeting"]:
        for meeting_id, title in tx.execute(
            select(Meeting.id, Meeting.title).where(
                Meeting.org_id == org_id,
                Meeting.id.in_(targets["meeting"]),
                Meeting.deleted_at.is_(None),
            )
        ):
            if title:
                labels[("meeting", meeting_id)] = title
    return names, labels


def _label(labels: dict, kind: str, ident: str) -> str | None:
    found = _uuids([ident])
    return labels.get((kind, found.pop())) if found else None


# ─── routes ───────────────────────────────────────────────────────────────────────────────────────


@router.get("/audit")
def list_audit(
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
    action: str | None = Query(default=None, max_length=MAX_ACTION_CHARS),
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_CHARS),
    caller: Caller = Depends(require_role("owner", "admin")),
    services: Services = Depends(get_services),
) -> dict:
    lower, upper = day_bounds(start, end, services.clock().astimezone(UTC).date())
    actions = parse_actions(action)
    after = None if cursor is None else decode_cursor(cursor)
    org_id = caller.session.org_id
    with org_transaction(services, caller.session) as tx:
        rows = tx.execute(
            PAGE,
            {
                "start": lower,
                "end": upper,
                "actions": actions,
                "before_at": None if after is None else after.at,
                "before_id": None if after is None else after.event_id,
                "limit": PAGE_SIZE + 1,
            },
        ).all()
        page = rows[:PAGE_SIZE]
        names, labels = _labels(tx, org_id, page)
    nxt = None
    if len(rows) > PAGE_SIZE:
        nxt = encode_cursor(Position(page[-1].at, page[-1].id))
    items = []
    for row in page:
        kind, ident = split_target(row.target)
        entry = ACTIONS.get(row.action)
        actor = row.actor_user_id
        items.append(
            {
                "id": str(row.id),
                "at": row.at.isoformat(),
                "actor": {
                    "id": None if actor is None else str(actor),
                    "name": None if actor is None else names.get(actor),
                },
                "action": row.action,
                "category": None if entry is None else entry.category,
                "target": {"type": kind, "id": ident, "label": _label(labels, kind, ident)},
                "ip": row.ip,
            }
        )
    return {"items": items, "next": nxt}


@router.get("/audit/actions")
def list_audit_actions(
    _caller: Caller = Depends(require_role("owner", "admin")),
) -> list[dict]:
    return [
        {"key": key, "category": entry.category, "label": entry.label}
        for key, entry in ACTIONS.items()
    ]
