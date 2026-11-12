"""Building a data export: what andai-worker does with each request `andai_claim_exports` hands it.

`run(engine, keyring, gateway, org_id, request_id, settings=, clock=)` builds one claimed request
under its org and records the outcome; `build` is the work itself:

1. The request is read under its org (row-level security). Only a `running` request is built.
2. The person's data, in one transaction: their account (the global `users` row, two-factor on or
   off), their membership of the org and its teams, the org's name, and every meeting they own —
   the set the erasure registry removes (`erasure.registry`, the `purge_meeting` rule on
   `meetings.requested_by`), deleted meetings left out. For each meeting: the record, attendees
   and share grantees by display name (`meetings.access.share_grants`), the consent record, every
   stored transcript segment in order and every minutes version.
3. Recordings: for each owned meeting the platform recorded, the org's key lists its recordings
   (`list_recordings`) and each media file of a type the bundle names is streamed once
   (`stream_recording`) to measure its size and SHA-256. Nothing is kept: the download streams it
   again and compares. A meeting still going on is listed as `meeting_in_progress` instead (its
   recording is still growing). A recording with no finished master (404), larger than
   `MAX_RECORDING_BYTES`, or of a media type the bundle does not name is listed in the manifest's
   `recordingsUnavailable`; any other platform error fails the attempt.
4. The JSON files (`bundle.dump`: `account.json`, `meetings/<id>.json`, then `manifest.json` with
   every other file's size and SHA-256) form the JSON part. Over `ANDAI_EXPORT_MAX_BYTES` (summed
   before compression) the request fails `too_large`. Otherwise the part is gzip-compressed and
   sealed with the keyring (AAD `export:<org>:<request>`) into the row, which turns `ready` with
   `expires_at = now + ANDAI_EXPORT_RETENTION_DAYS` and `user.export.ready` audited, in one
   transaction.

A person who is no longer a member of the org fails the request at once (`not_a_member`). Any other
error is retried after 30 s doubling to 10 minutes; the fifth failed attempt fails the request
`internal_error`. A failure is audited `user.export.failed`. Logs carry ids and error classes only,
never a name, address, transcript text or key.

`open_part(keyring, org_id, request_id, ciphertext, key_id)` opens a stored JSON part again
(`routes.py`, the download).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import anyio.to_thread
from sqlalchemy import Engine, select, update

from andai_core import audit
from andai_core.crypto import Keyring
from andai_core.db.models import (
    TERMINAL_STATUSES,
    ConsentRecord,
    ExportRequest,
    Meeting,
    MomDocument,
    Org,
    OrgMembership,
    Team,
    TeamMembership,
    TranscriptSegment,
    User,
    UserTotp,
)
from andai_core.db.session import transaction
from andai_core.dpdp import bundle
from andai_core.erasure.registry import ERASURE_RULES
from andai_core.meetings.access import share_grants
from andai_core.platform import principals
from andai_core.platform.gateway_client import GatewayError, PlatformGateway, RecordingTooLarge
from andai_core.settings import Settings

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 600
# One recording of a bundle; a longer one is listed as unavailable.
MAX_RECORDING_BYTES = 8 * 1024**3
JSON_MEDIA_TYPE = "application/json"
# (media file type, format) → (media type, file extension): what the platform's recordings are.
MEDIA = {
    ("audio", "wav"): ("audio/wav", "wav"),
    ("audio", "webm"): ("audio/webm", "webm"),
    ("video", "webm"): ("video/webm", "webm"),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_CAP_S))


def aad(org_id: uuid.UUID, request_id: uuid.UUID) -> str:
    return f"export:{org_id}:{request_id}"


def owner_column():
    """The column that says whose a meeting is, from the erasure registry: an export holds exactly
    the meetings an erasure purges."""
    [rule] = [r for r in ERASURE_RULES if r.action == "purge_meeting"]
    if rule.table != Meeting.__tablename__:
        raise RuntimeError(f"the purge rule names {rule.table}, not meetings")
    return getattr(Meeting, rule.column)


class Failed(Exception):
    """The request fails at once with this error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class _Meeting:
    """What a meeting's file needs, read in the build's transaction."""

    export: dict
    platform_meeting_id: int | None


@dataclass
class _Data:
    subject: uuid.UUID
    requested_by: uuid.UUID
    org_name: str
    account: bundle.Account
    meetings: list[_Meeting]
    key: str | None


@dataclass
class Recording:
    """One recording of the bundle, as the row's `recordings` keeps it."""

    meeting_id: uuid.UUID
    recording_id: int
    kind: str
    file: str
    bytes: int
    sha256: str
    media_type: str

    def stored(self) -> dict:
        return {
            "meetingId": str(self.meeting_id),
            "platformRecordingId": self.recording_id,
            "kind": self.kind,
            "file": self.file,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "mediaType": self.media_type,
        }

    @classmethod
    def from_stored(cls, row: dict) -> Recording:
        return cls(
            meeting_id=uuid.UUID(row["meetingId"]),
            recording_id=int(row["platformRecordingId"]),
            kind=row["kind"],
            file=row["file"],
            bytes=int(row["bytes"]),
            sha256=row["sha256"],
            media_type=row["mediaType"],
        )


@dataclass
class Built:
    """A built bundle: its JSON files (manifest last) and its recordings."""

    files: dict[str, bytes]
    recordings: list[Recording] = field(default_factory=list)

    @property
    def json_bytes(self) -> int:
        return sum(len(data) for data in self.files.values())

    @property
    def total_bytes(self) -> int:
        return self.json_bytes + sum(r.bytes for r in self.recordings)


# ─── reading the person's data ────────────────────────────────────────────────────────────────────


def _names(attendees: list) -> list[str]:
    seen: list[str] = []
    for entry in attendees or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.strip() and name not in seen:
            seen.append(name)
    return seen


def _consent(record: ConsentRecord | None) -> dict | None:
    if record is None:
        return None
    return bundle.Consent(
        bot_display_name=record.bot_display_name,
        disclosure_text=record.disclosure_text,
        privacy_notice_url=record.privacy_notice_url,
        disclosure_channel=record.disclosure_channel,
        disclosure_detail=record.disclosure_detail,
        disclosure_posted_at=record.disclosure_posted_at,
        admitted_at=record.admitted_at,
        removed_at=record.removed_at,
        removed_by=record.removed_by,
    ).model_dump()


def _meeting(tx, meeting: Meeting) -> _Meeting:
    segments = tx.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting.id)
        .order_by(TranscriptSegment.seq)
    ).all()
    versions = tx.scalars(
        select(MomDocument)
        .where(MomDocument.meeting_id == meeting.id)
        .order_by(MomDocument.version)
    ).all()
    consent = tx.scalar(select(ConsentRecord).where(ConsentRecord.meeting_id == meeting.id))
    export = {
        "id": meeting.id,
        "title": meeting.title,
        "platform": meeting.platform,
        "source": meeting.source,
        "language": (meeting.settings_snapshot or {}).get("language"),
        "status": meeting.status,
        "failure_reason": meeting.failure_reason,
        "created_at": meeting.created_at,
        "scheduled_start": meeting.scheduled_start,
        "started_at": meeting.started_at,
        "ended_at": meeting.ended_at,
        "attendees": _names(meeting.attendees),
        "shared_with": [
            {"type": grant["grantee_type"], "name": grant["name"]}
            for grant in share_grants(tx, meeting)
        ],
        "consent": _consent(consent),
        "transcript": [
            {
                "id": s.id,
                "speaker": s.speaker,
                "start": s.start_s,
                "end": s.end_s,
                "absolute_start": s.absolute_start,
                "text_raw": s.text_raw,
                "text_canonical": s.text_canonical,
                "language": s.language,
            }
            for s in segments
        ],
        "minutes": [
            {
                "version": v.version,
                "template_id": v.template_id,
                "output_language": v.output_language,
                "created_at": v.created_at,
                "body": v.body,
            }
            for v in versions
        ],
    }
    return _Meeting(export=export, platform_meeting_id=meeting.platform_meeting_id)


def _read(
    engine: Engine, keyring: Keyring, org_id: uuid.UUID, request_id: uuid.UUID, note: str
) -> _Data | None:
    with transaction(engine, org_id=org_id) as tx:
        request = tx.get(ExportRequest, request_id)
        if request is None or request.state != "running":
            return None
        subject = request.subject_user_id
        user = tx.get(User, subject)
        membership = tx.scalar(
            select(OrgMembership).where(
                OrgMembership.org_id == org_id, OrgMembership.user_id == subject
            )
        )
        if user is None or membership is None or user.status == "erased":
            raise Failed("not_a_member")
        org = tx.get(Org, org_id)
        teams = tx.execute(
            select(Team.id, Team.name, TeamMembership.role)
            .join(TeamMembership, TeamMembership.team_id == Team.id)
            .where(TeamMembership.org_id == org_id, TeamMembership.user_id == subject)
            .order_by(Team.name, Team.id)
        ).all()
        two_factor = tx.scalar(
            select(UserTotp.confirmed_at).where(UserTotp.user_id == subject)
        )
        account = bundle.Account(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            two_factor_enabled=two_factor is not None,
            membership=bundle.Membership(
                role=membership.role,
                joined_at=membership.created_at,
                teams=[
                    bundle.TeamRef(team_id=t.id, name=t.name, lead=t.role == "lead")
                    for t in teams
                ],
            ),
            processing=bundle.Processing(note=note),
        )
        owned = tx.scalars(
            select(Meeting)
            .where(
                Meeting.org_id == org_id,
                owner_column() == subject,
                Meeting.deleted_at.is_(None),
            )
            .order_by(Meeting.created_at, Meeting.id)
        ).all()
        meetings = [_meeting(tx, m) for m in owned]
        key = None
        if any(m.platform_meeting_id is not None for m in meetings):
            key = principals.open_key(tx, keyring, org_id)
            if key is None:
                raise RuntimeError("the org has no platform key")
        return _Data(
            subject=subject,
            requested_by=request.requested_by,
            org_name=org.name if org is not None else "",
            account=account,
            meetings=meetings,
            key=key,
        )


# ─── recordings ───────────────────────────────────────────────────────────────────────────────────


async def measure(
    gateway: PlatformGateway, key: str, recording_id: int, kind: str, max_bytes: int
) -> tuple[int, str]:
    """Stream one recording once: its size and SHA-256."""
    digest = hashlib.sha256()
    size = 0
    async for chunk in gateway.stream_recording(
        key, recording_id, media_type=kind, max_bytes=max_bytes
    ):
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


async def _recordings(
    gateway: PlatformGateway, key: str, meeting_id: uuid.UUID, platform_meeting_id: int
) -> tuple[list[Recording], list[bundle.RecordingUnavailable]]:
    found: list[Recording] = []
    missing: list[bundle.RecordingUnavailable] = []
    for recording in await gateway.list_recordings(key, platform_meeting_id):
        recording_id = recording.get("id")
        if isinstance(recording_id, bool) or not isinstance(recording_id, int):
            continue
        for media in recording.get("media_files") or []:
            if not isinstance(media, dict):
                continue
            kind = media.get("type")
            named = MEDIA.get((kind, media.get("format")))
            if named is None:
                missing.append(
                    bundle.RecordingUnavailable(meeting_id=meeting_id, reason="unsupported_media")
                )
                continue
            try:
                size, sha256 = await measure(
                    gateway, key, recording_id, kind, MAX_RECORDING_BYTES
                )
            except RecordingTooLarge:
                reason = "too_large"
            except GatewayError as exc:
                if exc.status != 404:
                    raise
                reason = "no_master"
            else:
                reason = None
            if reason is not None:
                missing.append(bundle.RecordingUnavailable(meeting_id=meeting_id, reason=reason))
                continue
            media_type, extension = named
            found.append(
                Recording(
                    meeting_id=meeting_id,
                    recording_id=recording_id,
                    kind=kind,
                    file=f"recordings/{meeting_id}/{recording_id}-{kind}.{extension}",
                    bytes=size,
                    sha256=sha256,
                    media_type=media_type,
                )
            )
    return found, missing


# ─── the bundle ───────────────────────────────────────────────────────────────────────────────────


def _entry(path: str, data: bytes, media_type: str = JSON_MEDIA_TYPE) -> bundle.ManifestFile:
    return bundle.ManifestFile(
        path=path, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), media_type=media_type
    )


async def collect(
    engine: Engine,
    keyring: Keyring,
    gateway: PlatformGateway,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    *,
    note: str,
    now: datetime,
) -> tuple[_Data, Built] | None:
    """Read and measure everything of one running request; None when it is not running."""
    data = await anyio.to_thread.run_sync(
        lambda: _read(engine, keyring, org_id, request_id, note)
    )
    if data is None:
        return None
    files: dict[str, bytes] = {"account.json": bundle.dump(data.account)}
    recordings: list[Recording] = []
    unavailable: list[bundle.RecordingUnavailable] = []
    for meeting in data.meetings:
        found: list[Recording] = []
        if meeting.platform_meeting_id is not None and data.key is not None:
            if meeting.export["status"] not in TERMINAL_STATUSES:
                unavailable.append(
                    bundle.RecordingUnavailable(
                        meeting_id=meeting.export["id"], reason="meeting_in_progress"
                    )
                )
            else:
                found, missing = await _recordings(
                    gateway, data.key, meeting.export["id"], meeting.platform_meeting_id
                )
                unavailable += missing
        recordings += found
        export = bundle.MeetingExport(
            **meeting.export,
            recordings=[
                bundle.RecordingFile(
                    file=r.file, bytes=r.bytes, sha256=r.sha256, media_type=r.media_type
                )
                for r in found
            ],
        )
        files[f"meetings/{export.id}.json"] = bundle.dump(export)
    manifest = bundle.Manifest(
        schema=bundle.SCHEMA,
        generated_at=now,
        product=bundle.PRODUCT,
        version=os.environ.get("ANDAI_VERSION", "dev"),
        subject=bundle.Subject(user_id=data.subject),
        org=bundle.OrgRef(org_id=org_id, name=data.org_name),
        files=[_entry(path, content) for path, content in files.items()]
        + [
            bundle.ManifestFile(
                path=r.file, bytes=r.bytes, sha256=r.sha256, media_type=r.media_type
            )
            for r in recordings
        ],
        recordings_unavailable=unavailable,
    )
    files["manifest.json"] = bundle.dump(manifest)
    return data, Built(files=files, recordings=recordings)


def seal_part(keyring: Keyring, org_id: uuid.UUID, request_id: uuid.UUID, built: Built):
    """The JSON part, gzip-compressed and sealed."""
    part = json.dumps(
        {"files": {path: data.decode() for path, data in built.files.items()}},
        ensure_ascii=False,
    ).encode()
    return keyring.seal(gzip.compress(part, mtime=0), aad(org_id, request_id))


def open_part(
    keyring: Keyring, org_id: uuid.UUID, request_id: uuid.UUID, ciphertext: str, key_id: str
) -> dict[str, bytes]:
    """A stored JSON part's files, path → bytes (manifest last)."""
    part = json.loads(gzip.decompress(keyring.open(ciphertext, key_id, aad(org_id, request_id))))
    return {path: text.encode() for path, text in part["files"].items()}


def _ready(
    engine: Engine,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    data: _Data,
    built: Built,
    sealed,
    now: datetime,
    retention: timedelta,
) -> bool:
    with transaction(engine, org_id=org_id) as tx:
        written = tx.execute(
            update(ExportRequest)
            .where(ExportRequest.id == request_id, ExportRequest.state == "running")
            .values(
                state="ready",
                error=None,
                bundle_ciphertext=sealed.ciphertext,
                key_id=sealed.key_id,
                bundle_bytes=built.total_bytes,
                recordings=[r.stored() for r in built.recordings],
                ready_at=now,
                expires_at=now + retention,
                next_at=None,
                lease_until=None,
            )
        ).rowcount
        if written:
            audit.record(
                tx, org_id, data.requested_by, "user.export.ready", f"user:{data.subject}"
            )
        return bool(written)


def _failed(engine: Engine, org_id: uuid.UUID, request_id: uuid.UUID, code: str) -> bool:
    with transaction(engine, org_id=org_id) as tx:
        request = tx.get(ExportRequest, request_id, with_for_update=True)
        if request is None or request.state != "running":
            return False
        request.state = "failed"
        request.error = code
        request.next_at = None
        request.lease_until = None
        request.bundle_ciphertext = None
        request.key_id = None
        audit.record(
            tx,
            org_id,
            request.requested_by,
            "user.export.failed",
            f"user:{request.subject_user_id}",
        )
        return True


def _retry(engine: Engine, org_id: uuid.UUID, request_id: uuid.UUID, now: datetime) -> str:
    with transaction(engine, org_id=org_id) as tx:
        request = tx.get(ExportRequest, request_id, with_for_update=True)
        if request is None or request.state != "running":
            return "gone"
        attempts = request.attempts + 1
        request.attempts = attempts
        if attempts < MAX_ATTEMPTS:
            request.next_at = now + backoff(attempts)
            request.lease_until = None
            return "running"
    _failed(engine, org_id, request_id, "internal_error")
    return "failed"


async def build(
    engine: Engine,
    keyring: Keyring,
    gateway: PlatformGateway,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Callable[[], datetime] = utcnow,
) -> str | None:
    """Build one running request: `ready`, `failed` (too large, not a member), or None when it is
    not a running request of `org_id`. Other errors propagate."""
    now = clock()
    collected = await collect(
        engine,
        keyring,
        gateway,
        org_id,
        request_id,
        note=settings.export_processing_note,
        now=now,
    )
    if collected is None:
        return None
    data, built = collected
    if built.json_bytes > settings.export_max_bytes:
        log.warning(
            "export %s: the JSON part is %d bytes, over the %d-byte limit",
            request_id,
            built.json_bytes,
            settings.export_max_bytes,
        )
        await anyio.to_thread.run_sync(lambda: _failed(engine, org_id, request_id, "too_large"))
        return "failed"
    sealed = await anyio.to_thread.run_sync(lambda: seal_part(keyring, org_id, request_id, built))
    retention = timedelta(days=settings.export_retention_days)
    done = clock()
    written = await anyio.to_thread.run_sync(
        lambda: _ready(engine, org_id, request_id, data, built, sealed, done, retention)
    )
    return "ready" if written else None


async def run(
    engine: Engine,
    keyring: Keyring,
    gateway: PlatformGateway,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Callable[[], datetime] = utcnow,
) -> str | None:
    """Build one claimed request and record the outcome. Returns `ready`, `failed`, `running`
    (retried later) or None (not a running request of this org)."""
    try:
        return await build(
            engine, keyring, gateway, org_id, request_id, settings=settings, clock=clock
        )
    except Failed as exc:
        log.warning("export %s: failed (%s)", request_id, exc.code)
        failed = await anyio.to_thread.run_sync(
            lambda: _failed(engine, org_id, request_id, exc.code)
        )
        return "failed" if failed else None
    except Exception as exc:  # noqa: BLE001 - every failed attempt counts toward the cap
        log.warning("export %s: attempt failed (%s)", request_id, type(exc).__name__)
        now = clock()
        state = await anyio.to_thread.run_sync(lambda: _retry(engine, org_id, request_id, now))
        return None if state == "gone" else state


__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "MAX_ATTEMPTS",
    "MAX_RECORDING_BYTES",
    "MEDIA",
    "Built",
    "Failed",
    "Recording",
    "aad",
    "backoff",
    "build",
    "collect",
    "measure",
    "open_part",
    "owner_column",
    "run",
    "seal_part",
]
