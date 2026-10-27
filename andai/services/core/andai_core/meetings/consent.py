"""Consent records: how the room was told that munshi.ai records, one per meeting the bot joined.

The record is written the first time andai sees evidence that the bot was admitted (see
`status_map.Mapped.admitted`), whether that is a `recording` status or a terminal one that only
follows admission (a removal, a left-alone end) seen for the first time after a gap between polls.

It holds the bot name, notice text and privacy link frozen in the meeting's settings snapshot. The
channel comes from the bot's own report on the platform meeting (`data.disclosure`):

* `chat` with `posted_at` — the notice appeared in meeting chat;
* `bot_name_only` with the bot's reason — chat was not possible; the bot name carried the notice.

Until a report arrives the record reads `bot_name_only` / `pending` (the name is already in the
room). With no report 60 seconds after admission, or once the meeting is over, it becomes
`bot_name_only` / `no_report`; a report that arrives later still replaces that. A removal by a
participant sets `removed_at` once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as Tx

from andai_core.db.models import ConsentRecord, Meeting

PENDING = "pending"
NO_REPORT = "no_report"
REPORT_GRACE = timedelta(seconds=60)
DETAIL_MAX = 200


@dataclass(frozen=True)
class Outcome:
    channel: str
    posted_at: datetime | None = None
    detail: str | None = None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def reported(platform_meeting: dict) -> Outcome | None:
    """The bot's disclosure report on the platform meeting, or None before one arrived."""
    data = platform_meeting.get("data")
    raw = data.get("disclosure") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    channel = raw.get("channel")
    if channel == "chat":
        return Outcome("chat", posted_at=_parse_time(raw.get("posted_at")))
    if channel == "bot_name_only":
        detail = raw.get("detail")
        return Outcome(
            "bot_name_only",
            detail=detail[:DETAIL_MAX] if isinstance(detail, str) and detail else "unspecified",
        )
    return None


def record_for(tx: Tx, meeting_id) -> ConsentRecord | None:
    return tx.scalar(select(ConsentRecord).where(ConsentRecord.meeting_id == meeting_id))


def apply(
    tx: Tx,
    meeting: Meeting,
    platform_meeting: dict,
    *,
    admitted: bool,
    removed: bool,
    terminal: bool,
    admitted_at: datetime | None,
    removed_at: datetime | None,
    now: datetime,
) -> ConsentRecord | None:
    """Create or bring up to date the meeting's consent record. Returns it (None while the bot has
    not been admitted)."""
    record = record_for(tx, meeting.id)
    if record is None and not (admitted or removed):
        return None
    outcome = reported(platform_meeting)
    if record is None:
        snapshot = meeting.settings_snapshot or {}
        record = ConsentRecord(
            meeting_id=meeting.id,
            org_id=meeting.org_id,
            bot_display_name=str(snapshot.get("bot_name") or ""),
            disclosure_text=str(snapshot.get("disclosure_text") or ""),
            privacy_notice_url=str(snapshot.get("privacy_notice_url") or ""),
            disclosure_channel="bot_name_only",
            disclosure_detail=PENDING,
            admitted_at=admitted_at or now,
            created_at=now,
            updated_at=now,
        )
        tx.add(record)
    changed = False
    if outcome is not None and record.disclosure_detail in (PENDING, NO_REPORT):
        record.disclosure_channel = outcome.channel
        record.disclosure_posted_at = outcome.posted_at
        record.disclosure_detail = outcome.detail
        changed = True
    elif record.disclosure_detail == PENDING and (
        terminal or now - record.admitted_at >= REPORT_GRACE
    ):
        record.disclosure_detail = NO_REPORT
        changed = True
    if removed and record.removed_at is None:
        record.removed_at = removed_at or now
        changed = True
    if changed:
        record.updated_at = now
    return record


def summary(record: ConsentRecord | None) -> dict | None:
    if record is None:
        return None
    return {
        "bot_display_name": record.bot_display_name,
        "disclosure_channel": record.disclosure_channel,
        "disclosure_detail": record.disclosure_detail,
        "disclosure_posted_at": (
            None if record.disclosure_posted_at is None
            else record.disclosure_posted_at.isoformat()
        ),
        "privacy_notice_url": record.privacy_notice_url,
        "removed_at": None if record.removed_at is None else record.removed_at.isoformat(),
    }
