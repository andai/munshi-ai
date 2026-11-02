"""One run of a meeting's minutes job (MOM-01): stored transcript → draft → stored version.

andai-worker claims due meetings through `andai_claim_mom()` (migration 0010), which leases each
one with a fresh token, and calls `run_one(engine, llm, org_id, meeting_id, lease)` for it:

1. **Load**, under the meeting's org: its segments (`text_raw`, in order, D-50), attendee names,
   local date, the dispatch language (`settings_snapshot.language`) and any `mom_request` (the
   template and language of a pending regenerate). Nothing is done when the meeting is gone,
   deleted, or no longer holds this run's lease.
2. **Empty transcript** → `mom_state = skipped` (and no recap). Nothing is generated.
3. **Generate** (`minutes.generate`) with the template and language of `mom_request`, else the
   `standard` template in the meeting's dispatch language (D-48).
4. **Write**, in one transaction under the meeting's row lock: the meeting must still be undeleted
   and still hold this run's lease token (a slower run whose lease ran out and was taken by another
   worker writes nothing). The version goes through `mom_writer.write_version`, the one write path.
   Then `mom_state = stored`, `mom_at` (first minutes only), and `recap_state = pending` when this
   was the meeting's first version. If `mom_request` changed while the run worked (a regenerate
   arrived), the meeting stays `pending` with the newer request, so the next claim runs it.
5. **Failures** are typed (`LlmError.code`, `mom_invalid`, `internal_error`) and stored in
   `mom_error`. Each counts an attempt and moves `mom_next_at` by the backoff (30 s, doubling, at
   most 30 min); the eighth ends `failed`. A request naming an unknown template or language fails
   at once (`mom_invalid_request`). The lease is released either way.

The function never raises for a meeting's own failure; it returns what happened (`MomResult`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import anyio.to_thread
from sqlalchemy import Engine, select

from andai_core.db.models import Meeting, TranscriptSegment
from andai_core.db.session import transaction
from andai_core.llm.client import LlmClient, LlmError
from andai_core.meetings.ingest import backoff
from andai_core.meetings.mom import MomError
from andai_core.meetings.mom_writer import DEFAULT_TIMEZONE, WriteReport, local_date, write_version
from andai_core.minutes import templates
from andai_core.minutes.chunking import Segment
from andai_core.minutes.generate import Generation, generate

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
DEFAULT_LANGUAGE = "en"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Job:
    segments: list[Segment]
    attendees: list[str]
    meeting_date: date
    template_id: str
    language: str
    request: dict | None


@dataclass
class MomResult:
    """What one run did. `state` is the meeting's `mom_state` afterwards, or `lost` when the run
    no longer held the meeting (deleted, or its lease was taken over) and wrote nothing."""

    state: str  # stored | pending | skipped | failed | lost
    version: int | None = None
    error: str | None = None
    report: WriteReport | None = None
    parts: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    first: bool = False


class _InvalidRequest(ValueError):
    pass


def _held(tx, meeting_id: uuid.UUID, lease: uuid.UUID) -> Meeting | None:
    """The meeting under its row lock while it is undeleted and still holds `lease`."""
    meeting = tx.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
    if meeting is None or meeting.deleted_at is not None or meeting.deletion_state != "none":
        return None
    return meeting if meeting.mom_lease == lease else None


def _release(meeting: Meeting) -> None:
    meeting.mom_lease = None
    meeting.mom_lease_until = None


def _choose(request: dict | None, snapshot: dict, default_language: str) -> tuple[str, str]:
    template_id = templates.DEFAULT_TEMPLATE
    language = str(snapshot.get("language") or default_language)
    if request:
        template_id = str(request.get("template") or template_id)
        language = str(request.get("language") or language)
    try:
        templates.get(template_id)
    except ValueError:
        raise _InvalidRequest(f"unknown template {template_id!r}") from None
    if language not in templates.LANGUAGES:
        raise _InvalidRequest(f"unknown output language {language!r}")
    return template_id, language


def _load(
    engine: Engine,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    lease: uuid.UUID,
    timezone: str,
    default_language: str,
) -> _Job | None:
    with transaction(engine, org_id=org_id) as tx:
        meeting = tx.scalar(select(Meeting).where(Meeting.id == meeting_id))
        if (
            meeting is None
            or meeting.deleted_at is not None
            or meeting.deletion_state != "none"
            or meeting.mom_lease != lease
        ):
            return None
        rows = tx.execute(
            select(
                TranscriptSegment.id,
                TranscriptSegment.start_s,
                TranscriptSegment.speaker,
                TranscriptSegment.text_raw,
            )
            .where(
                TranscriptSegment.org_id == org_id, TranscriptSegment.meeting_id == meeting_id
            )
            .order_by(TranscriptSegment.seq)
        ).all()
        request = dict(meeting.mom_request) if isinstance(meeting.mom_request, dict) else None
        template_id, language = _choose(
            request, meeting.settings_snapshot or {}, default_language
        )
        names = [
            str(a["name"]).strip()
            for a in meeting.attendees or []
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        ]
        return _Job(
            segments=[Segment.from_row(row) for row in rows if (row.text_raw or "").strip()],
            attendees=names,
            meeting_date=local_date(meeting, timezone),
            template_id=template_id,
            language=language,
            request=request,
        )


def _finish_skipped(
    engine: Engine, org_id: uuid.UUID, meeting_id: uuid.UUID, lease: uuid.UUID
) -> str:
    with transaction(engine, org_id=org_id) as tx:
        meeting = _held(tx, meeting_id, lease)
        if meeting is None:
            return "lost"
        meeting.mom_state = "skipped"
        meeting.mom_next_at = None
        meeting.mom_error = None
        meeting.mom_request = None
        if meeting.recap_state == "none":
            meeting.recap_state = "skipped"
        _release(meeting)
        return "skipped"


def _retry(
    engine: Engine,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    lease: uuid.UUID,
    now: datetime,
    code: str,
    *,
    final: bool = False,
) -> str:
    """Count a failed attempt; returns the new state (`pending`, `failed`, or `lost`)."""
    with transaction(engine, org_id=org_id) as tx:
        meeting = _held(tx, meeting_id, lease)
        if meeting is None:
            return "lost"
        attempts = meeting.mom_attempts + 1
        meeting.mom_attempts = attempts
        meeting.mom_error = code
        if final or attempts >= MAX_ATTEMPTS:
            meeting.mom_state = "failed"
            meeting.mom_next_at = None
        else:
            meeting.mom_state = "pending"
            meeting.mom_next_at = now + backoff(attempts)
        _release(meeting)
        return meeting.mom_state


def _write(
    engine: Engine,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    lease: uuid.UUID,
    job: _Job,
    generation: Generation,
    llm: LlmClient,
    timezone: str,
    now: datetime,
) -> MomResult:
    with transaction(engine, org_id=org_id) as tx:
        meeting = _held(tx, meeting_id, lease)
        if meeting is None:
            return MomResult(state="lost")
        doc, report = write_version(
            tx,
            meeting_id,
            generation.draft,
            template_id=job.template_id,
            output_language=job.language,
            model=llm.model,
            llm_profile_id=llm.profile_id,
            timezone=timezone,
            now=now,
        )
        first = doc.version == 1
        if first and meeting.recap_state == "none":
            meeting.recap_state = "pending"
            meeting.recap_next_at = None
        if meeting.mom_at is None:
            meeting.mom_at = now
        meeting.mom_attempts = 0
        meeting.mom_next_at = None
        meeting.mom_error = None
        current = meeting.mom_request if isinstance(meeting.mom_request, dict) else None
        if current == job.request:
            meeting.mom_state = "stored"
            meeting.mom_request = None
        else:  # a regenerate arrived while this run worked: the next claim runs it
            meeting.mom_state = "pending"
        _release(meeting)
        return MomResult(state=meeting.mom_state, version=doc.version, report=report, first=first)


async def run_one(
    engine: Engine,
    llm: LlmClient,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    lease: uuid.UUID,
    *,
    timezone: str = DEFAULT_TIMEZONE,
    default_language: str = DEFAULT_LANGUAGE,
    clock: Callable[[], datetime] = utcnow,
) -> MomResult:
    """Generate and store the minutes of one claimed meeting (see the module docstring)."""
    try:
        job = await anyio.to_thread.run_sync(
            lambda: _load(engine, org_id, meeting_id, lease, timezone, default_language)
        )
    except _InvalidRequest as exc:
        log.warning("meeting %s: minutes request refused: %s", meeting_id, exc)
        now = clock()
        state = await anyio.to_thread.run_sync(
            lambda: _retry(
                engine, org_id, meeting_id, lease, now, "mom_invalid_request", final=True
            )
        )
        return MomResult(state=state, error="mom_invalid_request")
    if job is None:
        return MomResult(state="lost")
    if not job.segments:
        state = await anyio.to_thread.run_sync(
            lambda: _finish_skipped(engine, org_id, meeting_id, lease)
        )
        log.info("meeting %s: empty transcript, minutes %s", meeting_id, state)
        return MomResult(state=state)

    code: str | None = None
    generation: Generation | None = None
    try:
        generation = await generate(
            llm.chat_json,
            job.segments,
            job.attendees,
            templates.get(job.template_id),
            job.language,  # type: ignore[arg-type]
            job.meeting_date,
            context_tokens=llm.config.context_tokens,
            max_output_tokens=llm.config.max_output_tokens,
        )
    except LlmError as exc:
        code = exc.code
    except ValueError:  # the configured context cannot hold a prompt
        code = "mom_context_too_small"
    except Exception:  # noqa: BLE001 - a meeting's failure is recorded, never raised
        log.exception("meeting %s: minutes generation failed", meeting_id)
        code = "internal_error"

    result: MomResult | None = None
    if generation is not None:
        now = clock()
        try:
            result = await anyio.to_thread.run_sync(
                lambda: _write(
                    engine, org_id, meeting_id, lease, job, generation, llm, timezone, now
                )
            )
        except MomError as exc:
            log.warning("meeting %s: minutes not stored: %s", meeting_id, exc)
            code = "mom_invalid"
        except Exception:  # noqa: BLE001
            log.exception("meeting %s: minutes not stored", meeting_id)
            code = "internal_error"

    if result is None:
        now = clock()
        state = await anyio.to_thread.run_sync(
            lambda: _retry(engine, org_id, meeting_id, lease, now, code or "internal_error")
        )
        log.warning("meeting %s: minutes attempt failed (%s): %s", meeting_id, code, state)
        return MomResult(state=state, error=code)

    usage = generation.usage  # type: ignore[union-attr]
    result.parts = generation.parts  # type: ignore[union-attr]
    result.calls = usage.calls
    result.prompt_tokens = usage.prompt_tokens
    result.completion_tokens = usage.completion_tokens
    if result.report is not None:
        log.info(
            "meeting %s: minutes %s (version %s, %d part(s), %d call(s), %d dropped, "
            "%d invalid ids)",
            meeting_id, result.state, result.version, result.parts, result.calls,
            result.report.dropped, result.report.invalid_ids,
        )  # fmt: skip
    return result


__all__ = ["MAX_ATTEMPTS", "MomResult", "run_one"]
