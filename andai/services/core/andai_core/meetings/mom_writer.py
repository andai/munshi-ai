"""The one write path for minutes versions: generated drafts and human edits (MOM-02, MOM-04).

* `build_document(draft, *, segment_ids, attendees, meeting_date, ...)` → `(Document, WriteReport)`
  holds every rule and touches no database (the gold-set evaluator calls it directly):
  - **Evidence.** Each decision, action item, topic and risk must cite at least one segment id of
    the meeting. For a generated draft (`strict=False`) unknown ids are removed and an item left
    with none is dropped; the report counts the unknown ids and dropped items per section. For a
    human edit (`strict=True`) any missing or unknown evidence raises `MomError` naming the
    section and index, and nothing is dropped.
  - **Owners** (generated only): `minutes.owners.resolve` — a named attendee at confidence ≥ 0.6,
    else `unassigned` (D-49). An edit keeps the owner the editor typed (empty → `unassigned`).
  - **Due dates** (generated only): the `dueText` phrase resolved against the meeting's local date
    (`minutes.dates.resolve`) wins; the model's own `due` is kept only when a phrase was said, it
    did not resolve, and the date is within a year after the meeting. No phrase, no date. An edit
    keeps the ISO date the editor typed.
  - andai fills `id` (per action item), `version`, `meetingId`, `llmProfileId`, `model`,
    `generatedAt` and `editedBy`, then validates the SPEC `MomDocument`.
* `write_version(tx, meeting_id, draft, ...)` loads the meeting's segment ids and attendees under
  the caller's org transaction (RLS: another org's meeting is "no such meeting"), builds the
  document and inserts it as the next version through `mom.insert_version`. With `base_version`
  (an edit of a version the editor saw) a newer version raises `VersionConflict`. A concurrent
  insert of the same version number is retried once with the next number; a second collision
  raises `VersionConflict`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as Tx

from andai_core.db.models import Meeting, TranscriptSegment
from andai_core.meetings.mom import Document, MomError, insert_version, next_version
from andai_core.minutes import dates, owners, templates
from andai_core.minutes.generate import Draft

DEFAULT_TIMEZONE = "Asia/Kolkata"
UNIQUE_VIOLATION = "23505"
# Section field of the draft, its name in the SPEC document.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("decisions", "decisions"),
    ("action_items", "actionItems"),
    ("topics", "topics"),
    ("risks", "risks"),
)


class VersionConflict(Exception):
    """The minutes changed since the version the edit started from (the edit route answers 409)."""


@dataclass
class SectionReport:
    invalid_ids: int = 0
    dropped: int = 0


@dataclass
class WriteReport:
    """What the rules changed, as counts only (safe to log)."""

    sections: dict[str, SectionReport] = field(
        default_factory=lambda: {name: SectionReport() for _, name in SECTIONS}
    )
    owners_named: int = 0
    owners_unassigned: int = 0
    dates_resolved: int = 0
    dates_from_model: int = 0

    @property
    def dropped(self) -> int:
        return sum(s.dropped for s in self.sections.values())

    @property
    def invalid_ids(self) -> int:
        return sum(s.invalid_ids for s in self.sections.values())

    def as_dict(self) -> dict:
        return {
            "sections": {
                name: {"invalid_ids": s.invalid_ids, "dropped": s.dropped}
                for name, s in self.sections.items()
            },
            "owners_named": self.owners_named,
            "owners_unassigned": self.owners_unassigned,
            "dates_resolved": self.dates_resolved,
            "dates_from_model": self.dates_from_model,
        }


def local_date(meeting: Meeting, timezone: str = DEFAULT_TIMEZONE) -> date:
    """The meeting's date where it happened: its start (or creation) in `timezone`."""
    moment = meeting.started_at or meeting.created_at
    return moment.astimezone(ZoneInfo(timezone)).date()


# ─── build_document (no database) ─────────────────────────────────────────────────────────────────


def _evidence(
    raw: Iterable[str], known: set[str], where: str, *, strict: bool, report: SectionReport
) -> list[str]:
    cited = list(dict.fromkeys(e.strip() for e in raw if isinstance(e, str) and e.strip()))
    valid = [e for e in cited if e in known]
    unknown = len(cited) - len(valid)
    if strict and (unknown or not valid):
        problem = f"{unknown} unknown segment id(s)" if unknown else "no evidence"
        raise MomError(f"{where}: {problem}; every item must cite segments of the meeting")
    report.invalid_ids += unknown
    return valid


def _model_due(item, meeting_date: date) -> date | None:
    if not (item.due_text and item.due_text.strip() and item.due):
        return None
    try:
        due = date.fromisoformat(item.due.strip())
    except ValueError:
        return None
    return due if meeting_date <= due <= meeting_date + timedelta(days=366) else None


def _typed_due(value: str | None, where: str) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise MomError(f"{where}: due must be a date (YYYY-MM-DD)") from None


def build_document(
    draft: Draft,
    *,
    segment_ids: Iterable[str],
    attendees: Iterable[Mapping | str],
    meeting_date: date,
    template_id: str,
    output_language: str,
    model: str,
    llm_profile_id: uuid.UUID,
    version: int | None = None,
    meeting_id: uuid.UUID | None = None,
    generated_at: datetime | None = None,
    edited_by: uuid.UUID | None = None,
    strict: bool = False,
) -> tuple[Document, WriteReport]:
    """The SPEC `MomDocument` for `draft` with every grounding, owner and date rule applied."""
    try:
        templates.get(template_id)
    except ValueError as exc:
        raise MomError(str(exc)) from None
    if output_language not in templates.LANGUAGES:
        raise MomError(f"unknown output language {output_language!r}")
    known = set(segment_ids)
    people = list(attendees)
    report = WriteReport()
    body: dict = {"summary": draft.summary.strip()}
    for field_name, name in SECTIONS:
        section = report.sections[name]
        out: list[dict] = []
        for index, item in enumerate(getattr(draft, field_name)):
            where = f"{name}[{index}]"
            text = (getattr(item, "title", None) if name == "topics" else item.text) or ""
            if not text.strip():
                if strict:
                    raise MomError(f"{where}: the text is empty")
                section.dropped += 1
                continue
            evidence = _evidence(item.evidence, known, where, strict=strict, report=section)
            if not evidence:
                section.dropped += 1
                continue
            key = "title" if name == "topics" else "text"
            entry: dict = {key: text.strip(), "evidence": evidence}
            if name == "actionItems":
                entry["id"] = str(uuid.uuid4())
                if strict:
                    entry["owner"] = (item.owner or "").strip() or owners.UNASSIGNED
                    due = _typed_due(item.due, where)
                else:
                    entry["owner"] = owners.resolve(item.owner, item.owner_confidence, people)
                    due = dates.resolve(item.due_text, meeting_date)
                    if due is not None:
                        report.dates_resolved += 1
                    elif (due := _model_due(item, meeting_date)) is not None:
                        report.dates_from_model += 1
                if entry["owner"] == owners.UNASSIGNED:
                    report.owners_unassigned += 1
                else:
                    report.owners_named += 1
                if due is not None:
                    entry["due"] = due.isoformat()
            out.append(entry)
        body[name] = out
    body.update(
        {
            "meetingId": None if meeting_id is None else str(meeting_id),
            "version": version,
            "templateId": template_id,
            "outputLanguage": output_language,
            "llmProfileId": str(llm_profile_id),
            "model": model,
            "generatedAt": (generated_at or datetime.now(UTC)).isoformat(),
            "editedBy": None if edited_by is None else str(edited_by),
        }
    )
    try:
        return Document.model_validate(body), report
    except ValidationError as exc:
        raise MomError(f"not a MomDocument: {exc.error_count()} problem(s)") from exc


# ─── write_version ────────────────────────────────────────────────────────────────────────────────


def write_version(
    tx: Tx,
    meeting_id: uuid.UUID,
    draft: Draft,
    *,
    template_id: str,
    output_language: str,
    model: str,
    llm_profile_id: uuid.UUID,
    edited_by: uuid.UUID | None = None,
    strict: bool = False,
    base_version: int | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> tuple[Document, WriteReport]:
    """Store `draft` as the meeting's next minutes version, in the caller's org transaction."""
    meeting = tx.scalar(
        select(Meeting).where(Meeting.id == meeting_id, Meeting.deleted_at.is_(None))
    )
    if meeting is None:
        raise MomError("no such meeting")
    segment_ids = tx.scalars(
        select(TranscriptSegment.id).where(
            TranscriptSegment.org_id == meeting.org_id,
            TranscriptSegment.meeting_id == meeting.id,
        )
    ).all()
    doc, report = build_document(
        draft,
        segment_ids=segment_ids,
        attendees=meeting.attendees or [],
        meeting_date=local_date(meeting, timezone),
        template_id=template_id,
        output_language=output_language,
        model=model,
        llm_profile_id=llm_profile_id,
        meeting_id=meeting.id,
        generated_at=now,
        edited_by=edited_by,
        strict=strict,
    )
    for attempt in (1, 2):
        version = next_version(tx, meeting.id)
        if base_version is not None and base_version != version - 1:
            raise VersionConflict(f"the minutes are at version {version - 1}, not {base_version}")
        try:
            with tx.begin_nested():
                insert_version(tx, meeting.org_id, meeting.id, doc, version)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) != UNIQUE_VIOLATION:
                raise
            if attempt == 2:
                raise VersionConflict("another version was stored at the same time") from None
            continue
        return doc, report
    raise AssertionError("unreachable")


__all__ = [
    "SectionReport",
    "VersionConflict",
    "WriteReport",
    "build_document",
    "local_date",
    "write_version",
]
