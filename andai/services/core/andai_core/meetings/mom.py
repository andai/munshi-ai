"""Minutes of meeting, as read by the meeting page and searched (PORT-02, PORT-05; D-43).

A meeting's minutes are a list of versions in `mom_documents`; the newest is the minutes. Each
version's `body` is the SPEC `MomDocument`, its evidence lists naming andai transcript segment ids
(`transcript_segments.id`, the ids the transcript route returns).

* `read(tx, actor, meeting_id, version=None)` — the latest version, or the named one, for someone
  who may read the meeting (`GET /api/v1/meetings/{id}/mom`): 404 `no_such_meeting` for anyone
  else, 404 `no_minutes` when the meeting has none, 404 `no_such_version` for a version it does not
  have. The document comes with its template's layout (`layout`), so the page and the exports
  (`minutes/render.py`) lay it out from one source.
* `layout(template_id, language)` — `sections` (the template's order), `headings` (per section, in
  the output language) and `labels` (`noneRecorded`, `unassigned`) from `minutes.templates`.
* `versions(tx, actor, meeting_id)` — every version, newest first: number, template, language,
  model, when it was generated or edited and by whom (`GET /api/v1/meetings/{id}/mom/versions`).
* `export_source(tx, actor, meeting_id, number, timezone)` — a version's document and the
  `minutes.render.Context` its downloads need (title, local date, attendees, the start time of
  every segment), for the meeting's readers (`GET /api/v1/meetings/{id}/mom/export.md|pdf`).
* `seed_version(tx, org_id, meeting_id, body)` — validate a `MomDocument` against the SPEC schema
  and the meeting's stored segments, and store it as the next version (tests and seeds).
* `next_version(tx, meeting_id)` and `insert_version(tx, org_id, meeting_id, doc, version)` — the
  one insert of a version, shared by `seed_version` and `meetings/mom_writer.py` (the writer of
  generated and edited minutes).
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

from andai_core.db.models import Meeting, MomDocument, TranscriptSegment, User
from andai_core.meetings.access import Actor
from andai_core.meetings.service import MeetingError, readable
from andai_core.minutes import templates
from andai_core.minutes.render import Context
from andai_core.text import fold

NO_MINUTES = MeetingError(404, "no_minutes", "this meeting has no minutes yet")
NO_SUCH_VERSION = MeetingError(404, "no_such_version", "the minutes have no such version")


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


def version(tx: Tx, meeting_id: uuid.UUID, number: int) -> MomDocument | None:
    """Version `number` of the meeting's minutes, or None (no access check: callers check)."""
    return tx.scalar(
        select(MomDocument).where(
            MomDocument.meeting_id == meeting_id, MomDocument.version == number
        )
    )


def layout(template_id: str, language: str) -> dict:
    """How a document of `template_id` in `language` is laid out: the template's section order,
    its heading per section in that language, and the labels for an empty section and an action
    item without an owner."""
    template = templates.get(template_id)
    lang = language if language in templates.LANGUAGES else "en"
    return {
        "sections": list(template.sections),
        "headings": dict(template.headings[lang]),  # type: ignore[index]
        "labels": {
            "noneRecorded": templates.NONE_RECORDED[lang],  # type: ignore[index]
            "unassigned": templates.UNASSIGNED_LABEL[lang],  # type: ignore[index]
        },
    }


def with_layout(row: MomDocument) -> dict:
    """The stored document of `row` and its template's layout."""
    return {**row.body, **layout(row.template_id, row.output_language)}


def stored(tx: Tx, meeting_id: uuid.UUID, number: int | None = None) -> MomDocument:
    """The latest version, or version `number`, else the 404 refusal (callers check access)."""
    row = latest(tx, meeting_id) if number is None else version(tx, meeting_id, number)
    if row is None:
        raise NO_MINUTES if number is None or latest(tx, meeting_id) is None else NO_SUCH_VERSION
    return row


def read(tx: Tx, actor: Actor, meeting_id: uuid.UUID, number: int | None = None) -> dict:
    meeting = readable(tx, actor, meeting_id)
    return with_layout(stored(tx, meeting.id, number))


def versions(tx: Tx, actor: Actor, meeting_id: uuid.UUID) -> dict:
    meeting = readable(tx, actor, meeting_id)
    rows = tx.scalars(
        select(MomDocument)
        .where(MomDocument.meeting_id == meeting.id)
        .order_by(MomDocument.version.desc())
    ).all()
    editors = {row.body.get("editedBy") for row in rows} - {None}
    names = (
        dict(
            tx.execute(
                select(User.id, User.display_name).where(
                    User.id.in_([uuid.UUID(e) for e in editors])
                )
            ).all()
        )
        if editors
        else {}
    )
    out = []
    for row in rows:
        editor = row.body.get("editedBy")
        out.append(
            {
                "version": row.version,
                "templateId": row.template_id,
                "outputLanguage": row.output_language,
                "model": row.body.get("model"),
                "generatedAt": row.body.get("generatedAt"),
                "editedBy": editor,
                "editedByName": None if editor is None else names.get(uuid.UUID(editor)),
                "createdAt": row.created_at.isoformat(),
            }
        )
    return {"versions": out}


def export_source(
    tx: Tx, actor: Actor, meeting_id: uuid.UUID, number: int | None, timezone: str
) -> tuple[dict, Context]:
    """The document of the latest version (or `number`) and what its downloads show besides it."""
    from andai_core.meetings.mom_writer import local_date  # mom_writer imports this module

    meeting = readable(tx, actor, meeting_id)
    row = stored(tx, meeting.id, number)
    times = dict(
        tx.execute(
            select(TranscriptSegment.id, TranscriptSegment.start_s).where(
                TranscriptSegment.org_id == meeting.org_id,
                TranscriptSegment.meeting_id == meeting.id,
            )
        ).all()
    )
    attendees = [
        str(a.get("name") or "").strip()
        for a in meeting.attendees or []
        if isinstance(a, dict) and str(a.get("name") or "").strip()
    ]
    context = Context(
        title=meeting.title,
        meeting_date=local_date(meeting, timezone),
        attendees=attendees,
        times={k: float(v or 0.0) for k, v in times.items()},
    )
    return row.body, context


# ─── write ────────────────────────────────────────────────────────────────────────────────────────


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
    return insert_version(tx, org_id, meeting_id, doc, next_version(tx, meeting_id))


def next_version(tx: Tx, meeting_id: uuid.UUID) -> int:
    """One more than the meeting's newest version (1 for the first)."""
    return (
        tx.scalar(
            select(func.max(MomDocument.version)).where(MomDocument.meeting_id == meeting_id)
        )
        or 0
    ) + 1


def insert_version(
    tx: Tx, org_id: uuid.UUID, meeting_id: uuid.UUID, doc: Document, version: int
) -> MomDocument:
    """Insert `doc` as `version` of the meeting's minutes (the caller has checked it). A version
    that already exists raises the database's unique violation at the flush."""
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


__all__ = [
    "NO_MINUTES",
    "NO_SUCH_VERSION",
    "Document",
    "MomError",
    "export_source",
    "insert_version",
    "latest",
    "layout",
    "next_version",
    "read",
    "search_text",
    "seed_version",
    "stored",
    "version",
    "versions",
    "with_layout",
]
