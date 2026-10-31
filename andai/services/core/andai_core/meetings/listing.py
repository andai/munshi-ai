"""The dashboard's meeting list, `GET /api/v1/meetings` (PORT-01, D-42).

Only meetings the caller may read are listed: `access.accessible(actor)` is part of the SQL, before
the page limit, so a page never holds a row the caller may not open and nothing is counted across
rows they may not see (the list carries no totals).

* Sections, by andai status: `live` (dispatching, awaiting admission, recording, processing),
  `upcoming` (scheduled) and `past` (done, failed, cancelled). Without `section` the answer holds
  all three, each capped at `limit`; with it, only that one.
* Order: newest first by `coalesce(started_at, created_at)`, the meeting id breaking ties, read
  through the `(org_id, status, sort time DESC, id DESC)` index.
* `filter`: `all` (default), `mine` (requested by the caller), `shared` (reaching the caller
  through a share, not their own) or `team:<id>` (that team's meetings among those readable).
* `limit`: 1–50, default 25. Pages are keyset pages: `next_cursor` is an opaque token holding the
  section, the filter and the last row's sort time and id; passed back as `cursor` it returns the
  rows after that row, so meetings that arrive meanwhile never shift or repeat a page. A cursor
  decides its own section and filter; a `section` or `filter` that disagrees with it is 422.

Each item: id, title (may be null), platform, status, failure and end reasons, team `{id, name}`
(or null), the requester's id and display name, start, end and creation times, `duration_s`,
`shared` (the meeting reaches the caller through a share) and `has_minutes` (always false until
minutes exist).
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, func, literal, select, tuple_
from sqlalchemy.orm import Session as Tx

from andai_core.db.models import Meeting, Team, User
from andai_core.meetings.access import Actor, accessible, shared_with
from andai_core.meetings.service import MeetingError

SECTIONS: dict[str, tuple[str, ...]] = {
    "live": ("dispatching", "awaiting_admission", "recording", "processing"),
    "upcoming": ("scheduled",),
    "past": ("done", "failed", "cancelled"),
}
DEFAULT_LIMIT = 25
MAX_LIMIT = 50
MAX_CURSOR_CHARS = 512

SORT_TIME = func.coalesce(Meeting.started_at, Meeting.created_at)

BAD_FILTER = MeetingError(422, "bad_filter", "filter is all, mine, shared or team:<team id>")
BAD_CURSOR = MeetingError(422, "bad_cursor", "this cursor is not valid for this list")


# ─── filter and cursor ────────────────────────────────────────────────────────────────────────────


def parse_filter(value: str) -> str:
    """The canonical filter (`all`, `mine`, `shared`, `team:<uuid>`), else the 422 refusal."""
    if value in ("all", "mine", "shared"):
        return value
    if value.startswith("team:"):
        try:
            return f"team:{uuid.UUID(value.removeprefix('team:'))}"
        except ValueError:
            raise BAD_FILTER from None
    raise BAD_FILTER


@dataclass(frozen=True)
class Position:
    """Where a page starts: after the row with this sort time and id."""

    section: str
    filter: str
    sort_time: datetime
    meeting_id: uuid.UUID


def encode_cursor(position: Position) -> str:
    raw = json.dumps(
        {
            "s": position.section,
            "f": position.filter,
            "t": position.sort_time.isoformat(),
            "i": str(position.meeting_id),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> Position:
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise BAD_CURSOR
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        position = Position(
            section=raw["s"],
            filter=parse_filter(raw["f"]),
            sort_time=datetime.fromisoformat(raw["t"]),
            meeting_id=uuid.UUID(raw["i"]),
        )
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError, MeetingError):
        raise BAD_CURSOR from None
    if position.section not in SECTIONS or position.sort_time.tzinfo is None:
        raise BAD_CURSOR
    return position


# ─── the query ────────────────────────────────────────────────────────────────────────────────────


def _filtered(actor: Actor, filter_: str) -> ColumnElement[bool] | None:
    if filter_ == "mine":
        return Meeting.requested_by == actor.user_id
    if filter_ == "shared":
        return and_(Meeting.requested_by != actor.user_id, shared_with(actor))
    if filter_.startswith("team:"):
        return Meeting.team_id == uuid.UUID(filter_.removeprefix("team:"))
    return None  # all


def query(
    actor: Actor, section: str, filter_: str, limit: int, after: Position | None
) -> Select:
    """One section's rows readable by `actor`, newest first, at most `limit`, after `after`."""
    conditions = [accessible(actor), Meeting.status.in_(SECTIONS[section])]
    narrowed = _filtered(actor, filter_)
    if narrowed is not None:
        conditions.append(narrowed)
    if after is not None:
        last = tuple_(literal(after.sort_time), literal(after.meeting_id))
        conditions.append(tuple_(SORT_TIME, Meeting.id) < last)
    return (
        select(
            Meeting,
            Team.name.label("team_name"),
            User.display_name.label("requester_name"),
            and_(Meeting.requested_by != actor.user_id, shared_with(actor)).label("shared"),
            SORT_TIME.label("sort_time"),
        )
        .join(User, User.id == Meeting.requested_by)
        .outerjoin(Team, and_(Team.id == Meeting.team_id, Team.org_id == Meeting.org_id))
        .where(*conditions)
        .order_by(SORT_TIME.desc(), Meeting.id.desc())
        .limit(limit)
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def item(meeting: Meeting, team_name: str | None, requester_name: str, shared: bool) -> dict:
    duration = None
    if meeting.started_at is not None and meeting.ended_at is not None:
        duration = max(0, round((meeting.ended_at - meeting.started_at).total_seconds()))
    return {
        "id": str(meeting.id),
        "title": meeting.title,
        "platform": meeting.platform,
        "status": meeting.status,
        "failure_reason": meeting.failure_reason,
        "end_reason": meeting.end_reason,
        "team": (
            None
            if meeting.team_id is None
            else {"id": str(meeting.team_id), "name": team_name}
        ),
        "requested_by": str(meeting.requested_by),
        "requested_by_name": requester_name,
        "started_at": _iso(meeting.started_at),
        "ended_at": _iso(meeting.ended_at),
        "created_at": _iso(meeting.created_at),
        "duration_s": duration,
        "shared": bool(shared),
        "has_minutes": False,
    }


def _page(tx: Tx, actor: Actor, section: str, filter_: str, limit: int, after: Position | None):
    rows = tx.execute(query(actor, section, filter_, limit + 1, after)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = encode_cursor(Position(section, filter_, last.sort_time, last.Meeting.id))
    return {
        "items": [
            item(row.Meeting, row.team_name, row.requester_name, row.shared) for row in rows
        ],
        "next_cursor": next_cursor,
    }


def list_meetings(
    tx: Tx,
    actor: Actor,
    *,
    section: str | None,
    filter_: str | None,
    limit: int,
    cursor: str | None,
) -> dict:
    after = None
    if cursor is not None:
        after = decode_cursor(cursor)
        if section not in (None, after.section) or (
            filter_ is not None and parse_filter(filter_) != after.filter
        ):
            raise BAD_CURSOR
        section, canonical = after.section, after.filter
    else:
        canonical = parse_filter(filter_ or "all")
    sections = [section] if section is not None else list(SECTIONS)
    return {
        "sections": {
            name: _page(tx, actor, name, canonical, limit, after) for name in sections
        }
    }


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SECTIONS",
    "decode_cursor",
    "encode_cursor",
    "list_meetings",
    "parse_filter",
    "query",
]
