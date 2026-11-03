"""Changing a meeting's minutes (MOM-03, MOM-04): a human edit, or a request to generate them again
with another template or output language. Either way the minutes gain a version; none is changed
or removed.

Who may: the requester, a lead of the meeting's team, admins and owners (`access.can_share`). For
anyone else, a grantee included, the meeting does not exist (404 `no_such_meeting`), as it does for
every caller once the meeting is deleted.

* `save_edit(tx, actor, meeting_id, draft, base_version=, ip=)` — `PUT /api/v1/meetings/{id}/mom`.
  The draft is written through `mom_writer.write_version` with `strict=True` and `edited_by` the
  editor: every decision, action item, topic and risk must cite segments of this meeting (an item
  with none, or with an id that is not one, is 422 `invalid_minutes` naming the item, such as
  `actionItems[1]`), owners and due dates are kept as typed, and the template, output language,
  model and profile stay those of the latest version. With `base_version` (the version the editor
  started from) a newer version is 409 `version_conflict`. 404 `no_minutes` when there is nothing
  to edit. Audited `mom.edit` in the same transaction.
* `request_regenerate(tx, actor, meeting_id, template_id, language, ip=)` —
  `POST /api/v1/meetings/{id}/mom/regenerate`. Sets `mom_state = pending`, `mom_attempts = 0`,
  `mom_next_at = now()`, clears `mom_error` and stores `mom_request = {template, language,
  requestedBy, requestedAt}`, which the next `andai_claim_mom` claim runs (`minutes_job.py`) into a
  new version. The lease (`mom_lease`, `mom_lease_until`) is never touched: a generation already
  running finishes, sees the newer request and leaves the meeting `pending` for it. Refused with
  422 `unknown_template` / `unknown_language`, 409 `transcript_not_ready` before the transcript is
  stored, and 409 `minutes_pending` while an earlier request is still waiting. Audited
  `mom.regenerate` in the same transaction. A regenerated version never sends a recap.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.db.models import Meeting
from andai_core.meetings import mom
from andai_core.meetings.access import Actor, accessible, can_share
from andai_core.meetings.mom_writer import VersionConflict, write_version
from andai_core.meetings.service import NO_SUCH_MEETING, MeetingError
from andai_core.minutes import templates
from andai_core.minutes.generate import Draft

VERSION_CONFLICT = "version_conflict"
MINUTES_PENDING = MeetingError(
    409, "minutes_pending", "the minutes are already being generated again; wait for that version"
)
TRANSCRIPT_NOT_READY = MeetingError(
    409, "transcript_not_ready", "the meeting's transcript is not stored yet"
)


def _managed(tx: Tx, actor: Actor, meeting_id: uuid.UUID, *, lock: bool = False) -> Meeting:
    """The meeting `actor` may edit and regenerate, else the 404 every non-manager gets."""
    query = select(Meeting).where(Meeting.id == meeting_id, accessible(actor))
    if lock:
        query = query.with_for_update(of=Meeting)
    meeting = tx.scalar(query)
    if meeting is None or not can_share(tx, actor, meeting):
        raise NO_SUCH_MEETING
    return meeting


def save_edit(
    tx: Tx,
    actor: Actor,
    meeting_id: uuid.UUID,
    draft: Draft,
    *,
    base_version: int | None = None,
    now: datetime | None = None,
    ip: str | None = None,
) -> dict:
    """Store `draft` as the next version, edited by `actor`; returns it with its layout."""
    meeting = _managed(tx, actor, meeting_id)
    current = mom.stored(tx, meeting.id)
    try:
        doc, _ = write_version(
            tx,
            meeting.id,
            draft,
            template_id=current.template_id,
            output_language=current.output_language,
            model=current.body["model"],
            llm_profile_id=uuid.UUID(current.body["llmProfileId"]),
            edited_by=actor.user_id,
            strict=True,
            base_version=base_version,
            now=now,
        )
    except mom.MomError as exc:
        raise MeetingError(422, "invalid_minutes", str(exc)) from None
    except VersionConflict as exc:
        raise MeetingError(409, VERSION_CONFLICT, str(exc)) from None
    audit.record(
        tx, actor.org_id, actor.user_id, "mom.edit", f"meeting:{meeting.id} version:{doc.version}",
        ip,
    )  # fmt: skip
    body = doc.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {**body, **mom.layout(doc.template_id, doc.output_language)}


def request_regenerate(
    tx: Tx,
    actor: Actor,
    meeting_id: uuid.UUID,
    template_id: str,
    language: str,
    *,
    now: datetime,
    ip: str | None = None,
) -> dict:
    """Queue the minutes to be generated again in `template_id` and `language`."""
    meeting = _managed(tx, actor, meeting_id, lock=True)
    if template_id not in templates.TEMPLATES:
        raise MeetingError(422, "unknown_template", f"no minutes template {template_id!r}")
    if language not in templates.LANGUAGES:
        raise MeetingError(422, "unknown_language", f"no output language {language!r}")
    if meeting.transcript_state != "stored":
        raise TRANSCRIPT_NOT_READY
    if meeting.mom_request is not None:
        raise MINUTES_PENDING
    meeting.mom_request = {
        "template": template_id,
        "language": language,
        "requestedBy": str(actor.user_id),
        "requestedAt": now.isoformat(),
    }
    meeting.mom_state = "pending"
    meeting.mom_attempts = 0
    meeting.mom_next_at = func.now()
    meeting.mom_error = None
    audit.record(tx, actor.org_id, actor.user_id, "mom.regenerate", f"meeting:{meeting.id}", ip)
    tx.flush()
    return {
        "id": str(meeting.id),
        "mom_state": "pending",
        "templateId": template_id,
        "outputLanguage": language,
    }


__all__ = [
    "MINUTES_PENDING",
    "TRANSCRIPT_NOT_READY",
    "request_regenerate",
    "save_edit",
]
