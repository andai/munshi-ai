"""The dashboard's Unassigned action items, `GET /api/v1/action-items/unassigned` (PORT-01, D-42).

The action items nobody owns yet, across the meetings the caller may read: every item whose owner
is `unassigned` (compared case-insensitively, as the meeting page groups them) in the latest
minutes version of each meeting `access.accessible(actor)` admits. Older versions never count, so
an edit that names an owner takes the item off the list on the next call, and a deleted meeting is
never readable, so its items leave with it.

Access, the latest-version join and the owner test are all SQL, before the page limit: a page
never holds an item of a meeting the caller may not open, and nothing is counted (no totals).

* Order: meetings newest first by `coalesce(started_at, created_at)`, the meeting id breaking ties
  (as the meeting list), then the items in their order in the minutes.
* `limit`: 1–50, default 25. Pages are keyset pages: `next_cursor` is an opaque token holding the
  last item's meeting sort time, meeting id and position; passed back as `cursor` it returns the
  items after it.

Each item: the meeting's id, title (may be null), platform, start and creation times and team
`{id, name}` (or null); the item's id, text and due date (or null); and `evidence_first_id`, the
first transcript segment it cites, which the page links to as `/meetings/<id>#seg-<segment id>`.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, column, func, literal, or_, select, true, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session as Tx

from andai_core.db.models import Meeting, MomDocument, Team
from andai_core.meetings.access import Actor, accessible
from andai_core.meetings.listing import DEFAULT_LIMIT, MAX_CURSOR_CHARS, MAX_LIMIT, SORT_TIME
from andai_core.meetings.service import MeetingError

UNASSIGNED = "unassigned"

BAD_CURSOR = MeetingError(422, "bad_cursor", "this cursor is not valid for this list")


# ─── cursor ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """Where a page starts: after this item (its meeting's sort time and id, its position)."""

    sort_time: datetime
    meeting_id: uuid.UUID
    ordinal: int


def encode_cursor(position: Position) -> str:
    raw = json.dumps(
        {
            "t": position.sort_time.isoformat(),
            "i": str(position.meeting_id),
            "n": position.ordinal,
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
            sort_time=datetime.fromisoformat(raw["t"]),
            meeting_id=uuid.UUID(raw["i"]),
            ordinal=raw["n"],
        )
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise BAD_CURSOR from None
    if (
        position.sort_time.tzinfo is None
        or not isinstance(position.ordinal, int)
        or isinstance(position.ordinal, bool)
        or position.ordinal < 1
    ):
        raise BAD_CURSOR
    return position


# ─── the query ────────────────────────────────────────────────────────────────────────────────────


def query(actor: Actor, limit: int, after: Position | None) -> Select:
    """The unassigned items of the latest minutes of the meetings `actor` may read, in list order,
    at most `limit`, after `after`."""
    latest = (
        select(MomDocument.body)
        .where(MomDocument.meeting_id == Meeting.id, MomDocument.org_id == Meeting.org_id)
        .order_by(MomDocument.version.desc())
        .limit(1)
        .correlate(Meeting)
        .lateral("latest")
    )
    item = (
        func.jsonb_array_elements(latest.c.body["actionItems"])
        .table_valued(column("value", JSONB), with_ordinality="ordinal")
        .render_derived()
        .lateral("item")
    )
    conditions = [accessible(actor), func.lower(item.c.value["owner"].astext) == UNASSIGNED]
    if after is not None:
        last = tuple_(literal(after.sort_time), literal(after.meeting_id))
        conditions.append(
            or_(
                tuple_(SORT_TIME, Meeting.id) < last,
                and_(
                    SORT_TIME == after.sort_time,
                    Meeting.id == after.meeting_id,
                    item.c.ordinal > after.ordinal,
                ),
            )
        )
    return (
        select(
            Meeting.id,
            Meeting.title,
            Meeting.platform,
            Meeting.started_at,
            Meeting.created_at,
            Meeting.team_id,
            Team.name.label("team_name"),
            SORT_TIME.label("sort_time"),
            item.c.value,
            item.c.ordinal,
        )
        .select_from(Meeting)
        .join(latest, true())
        .join(item, true())
        .outerjoin(Team, and_(Team.id == Meeting.team_id, Team.org_id == Meeting.org_id))
        .where(*conditions)
        .order_by(SORT_TIME.desc(), Meeting.id.desc(), item.c.ordinal)
        .limit(limit)
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _item(row) -> dict:
    value = row.value
    evidence = value.get("evidence") or []
    return {
        "meeting_id": str(row.id),
        "title": row.title,
        "platform": row.platform,
        "started_at": _iso(row.started_at),
        "created_at": _iso(row.created_at),
        "team": None if row.team_id is None else {"id": str(row.team_id), "name": row.team_name},
        "item_id": value.get("id"),
        "text": value.get("text"),
        "due": value.get("due"),
        "evidence_first_id": evidence[0] if evidence else None,
    }


def list_unassigned(tx: Tx, actor: Actor, *, limit: int, cursor: str | None) -> dict:
    after = None if cursor is None else decode_cursor(cursor)
    rows = tx.execute(query(actor, limit + 1, after)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = encode_cursor(Position(last.sort_time, last.id, last.ordinal))
    return {"items": [_item(row) for row in rows], "next_cursor": next_cursor}


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "UNASSIGNED",
    "decode_cursor",
    "encode_cursor",
    "list_unassigned",
    "query",
]
