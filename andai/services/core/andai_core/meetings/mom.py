"""Minutes of meeting, as read by the meeting page and searched (PORT-02, PORT-05; D-43).

A meeting's minutes are a list of versions in `mom_documents`; the newest is the minutes. Each
version's `body` is the SPEC `MomDocument`, its evidence lists naming andai transcript segment ids
(`transcript_segments.id`, the ids the transcript route returns).

* `read(tx, actor, meeting_id)` — the latest version for someone who may read the meeting
  (`GET /api/v1/meetings/{id}/mom`): 404 `no_such_meeting` for anyone else, 404 `no_minutes` when
  the meeting has none.
* `seed_version(tx, org_id, meeting_id, body)` — validate a `MomDocument` against the SPEC schema
  and the meeting's stored segments, and store it as the next version. In this phase only tests and
  seeds call it; the MoM pipeline (Phase 6) will write through it.
* `search_text(body)` — the folded text search reads: summary, decisions, action items, topics and
  risks, the SPEC `MomDocument` text fields and nothing else.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as Tx

from andai_core.db.models import Meeting, MomDocument, TranscriptSegment
from andai_core.meetings.access import Actor
from andai_core.meetings.service import MeetingError, readable
from andai_core.text import fold

NO_MINUTES = MeetingError(404, "no_minutes", "this meeting has no minutes yet")


class MomError(ValueError):
    """A document that is not a valid `MomDocument` for its meeting; nothing is stored."""


# ─── the SPEC MomDocument ─────────────────────────────────────────────────────────────────────────


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class Decision(_Spec):
    text: str = Field(min_length=1)
    evidence: list[str]


class ActionItem(_Spec):
    id: uuid.UUID
    text: str = Field(min_length=1)
    owner: str = Field(min_length=1)  # a name, or "unassigned"
    owner_user_id: uuid.UUID | None = None
    due: date | None = None
    evidence: list[str] = Field(min_length=1)


class Topic(_Spec):
    title: str = Field(min_length=1)
    evidence: list[str]


class Risk(_Spec):
    text: str = Field(min_length=1)
    evidence: list[str]


class Document(_Spec):
    """SPEC `MomDocument`. `meetingId` and `version` are set by the store."""

    meeting_id: uuid.UUID | None = None
    version: int | None = None
    template_id: str = Field(min_length=1)
    output_language: Literal["en", "hi", "hinglish"]
    summary: str
    decisions: list[Decision]
    action_items: list[ActionItem]
    topics: list[Topic]
    risks: list[Risk]
    llm_profile_id: uuid.UUID
    model: str = Field(min_length=1)
    generated_at: datetime
    edited_by: uuid.UUID | None = None

    def evidence(self) -> set[str]:
        return {
            segment
            for entries in (self.decisions, self.action_items, self.topics, self.risks)
            for entry in entries
            for segment in entry.evidence
        }

    def texts(self) -> list[str]:
        """The text fields search reads, in document order."""
        return [
            self.summary,
            *(d.text for d in self.decisions),
            *(a.text for a in self.action_items),
            *(t.title for t in self.topics),
            *(r.text for r in self.risks),
        ]


def search_text(body: dict) -> str:
    """The fold of a document's summary, decisions, action items, topics and risks."""
    return _folded(Document.model_validate(body))


def _folded(doc: Document) -> str:
    return fold(" ".join(doc.texts()))


# ─── read ─────────────────────────────────────────────────────────────────────────────────────────


def latest(tx: Tx, meeting_id: uuid.UUID) -> MomDocument | None:
    """The newest version of the meeting's minutes, or None (no access check: callers check)."""
    return tx.scalar(
        select(MomDocument)
        .where(MomDocument.meeting_id == meeting_id)
        .order_by(MomDocument.version.desc())
        .limit(1)
    )


def read(tx: Tx, actor: Actor, meeting_id: uuid.UUID) -> dict:
    meeting = readable(tx, actor, meeting_id)
    row = latest(tx, meeting.id)
    if row is None:
        raise NO_MINUTES
    return row.body


# ─── write (tests and seeds in this phase) ────────────────────────────────────────────────────────


def seed_version(
    tx: Tx, org_id: uuid.UUID, meeting_id: uuid.UUID, body: dict
) -> MomDocument:
    """Store `body` as the meeting's next minutes version. Raises `MomError` when it is not a
    SPEC `MomDocument`, an action item has no evidence, or an evidence id is not a stored segment
    of this meeting."""
    try:
        doc = Document.model_validate(body)
    except ValidationError as exc:
        raise MomError(f"not a MomDocument: {exc.error_count()} problem(s)") from exc
    if doc.meeting_id not in (None, meeting_id):
        raise MomError("meetingId names another meeting")
    meeting = tx.scalar(
        select(Meeting).where(Meeting.id == meeting_id, Meeting.org_id == org_id)
    )
    if meeting is None:
        raise MomError("no such meeting")
    cited = doc.evidence()
    if cited:
        stored = set(
            tx.scalars(
                select(TranscriptSegment.id).where(
                    TranscriptSegment.org_id == org_id,
                    TranscriptSegment.meeting_id == meeting_id,
                    TranscriptSegment.id.in_(cited),
                )
            )
        )
        if missing := cited - stored:
            raise MomError(
                f"evidence names {len(missing)} id(s) that are not segments of the meeting"
            )
    version = (
        tx.scalar(
            select(func.max(MomDocument.version)).where(MomDocument.meeting_id == meeting_id)
        )
        or 0
    ) + 1
    doc.meeting_id, doc.version = meeting_id, version
    row = MomDocument(
        meeting_id=meeting_id,
        version=version,
        org_id=org_id,
        template_id=doc.template_id,
        output_language=doc.output_language,
        body=doc.model_dump(mode="json", by_alias=True, exclude_none=True),
        search_text=_folded(doc),
    )
    tx.add(row)
    tx.flush()
    return row


__all__ = ["NO_MINUTES", "Document", "MomError", "latest", "read", "search_text", "seed_version"]
